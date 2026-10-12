import json, time, threading, re, secrets
from .db import q, event
from .crypto import load_spec, dump_spec, sha256
from .drivers import get_drivers
from .plans import PLANS
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
_locks = {}
def _lock(app): return _locks.setdefault(app, threading.Lock())
def validate(spec):
    if not NAME_RE.match(spec.get("name","")): raise ValueError("name must be 3-32 chars, lowercase letters/digits/hyphens")
    if spec.get("plan","cpu-small") not in PLANS: raise ValueError(f"unknown plan; choose from {list(PLANS)}")
    src = spec.get("source") or {}
    if not (src.get("image") or src.get("git") or src.get("path")): raise ValueError("source.image or source.git required")
    spec.setdefault("plan","cpu-small"); spec.setdefault("port",8080); spec.setdefault("env",{}); spec.setdefault("instances",1)
    spec["instances"] = max(0, min(int(spec["instances"]), 10))
    spec.setdefault("volumes", []); spec.setdefault("health", {"path": None, "interval": 10, "failures": 3})
    a = spec.get("autoscale")
    if a:
        a = {"min": 1, "max": 3, "target_concurrency": 10, "cpu_percent": None, "idle_seconds": 60} | {k:v for k,v in a.items() if v is not None}
        a["min"] = max(0, int(a["min"])); a["max"] = max(a["min"], 1, min(int(a["max"]), 10)); spec["autoscale"] = a
        spec["instances"] = max(a["min"], min(spec["instances"], a["max"]))
    return spec
class NoCapacity(ValueError): pass
def pick_provider(spec):
    from . import plans as _plans
    d = get_drivers(); plan = PLANS[spec["plan"]]
    if spec.get("provider"):
        if spec["provider"] not in d: raise ValueError(f"unknown provider {spec['provider']}")
        if not _plans.pool_allowed(spec["plan"], spec["provider"]):
            m = _plans.pool_margin(spec["plan"], spec["provider"])
            raise ValueError(f"plan {spec['plan']} can't run on {spec['provider']}: margin "
                             + ("unknown (no cost on file)" if m is None else f"{m:.0%}") + f" is under the {_plans.MIN_MARGIN:.0%} floor")
        return spec["provider"]
    tried = []
    for pool in plan["providers"]:
        if not _plans.pool_allowed(spec["plan"], pool): tried.append(f"{pool}: refused, under {_plans.MIN_MARGIN:.0%} margin floor"); continue
        ok, why = d[pool].available(); tried.append(f"{pool}: {why}")
        if ok: return pool
    if plan["kind"] == "cpu" and _plans.pool_allowed(spec["plan"], "local-docker"): return "local-docker"
    if plan["kind"] == "gpu" and d["local-docker"].supports_gpu and _plans.pool_allowed(spec["plan"], "local-docker"): return "local-docker"
    raise NoCapacity(f"no {'GPU ' if plan['kind'] == 'gpu' else ''}capacity for {spec['plan']} (" + "; ".join(tried or [f"local-docker: refused, under {_plans.MIN_MARGIN:.0%} margin floor"]) + ")")
import socket, urllib.request, urllib.error
def wait_ready(host, port, path=None, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try: urllib.request.urlopen(f"http://{host}:{port}{path or '/'}", timeout=2); return True
        except urllib.error.HTTPError as e:
            if path is None or e.code < 500: return True
        except Exception: pass
        time.sleep(0.2)
    return False
def _start(name, drv, image, spec):
    idx = f"{int(time.time()*1000)%10**8}"
    try: inst = drv.start_instance(name, image, spec, idx)
    except Exception as e:
        # image lost (docker data reset, registry GC): rebuild from the recorded source once, then retry
        if "ImageNotFound" not in type(e).__name__ and "pull access denied" not in str(e) and "No such image" not in str(e): raise
        with _lock(f"rebuild:{name}"):
            event(name, f"image {image} missing; rebuilding from source")
            image = drv.build(name, spec["source"], lambda m: event(name, m))
        inst = drv.start_instance(name, image, spec, idx)
    node_id = inst.id.split(".", 1)[0] if "." in inst.id else None
    q("insert into instances(id,app,provider,host,port,status,started,node_id) values(?,?,?,?,?,?,?,?)", (inst.id, name, drv.name, inst.host, inst.port, "starting", time.time(), node_id))
    ok = wait_ready(inst.host, inst.port, (spec.get("health") or {}).get("path"))
    q("update instances set status='running' where id=?", (inst.id,))   # only now does the edge route to it
    event(name, f"instance {inst.id} up at {inst.host}:{inst.port}" + ("" if ok else " (not ready after 30s)"))
    return inst
def account_tier(account_id):
    from .plans import TIERS
    a = q("select tier from accounts where id=?", (account_id,), one=True); k = (a or {}).get("tier") or "free"
    return k, TIERS.get(k, TIERS["free"])
def apply_tier_limits(account_id, spec, redeploy=False):
    """Free tier cost is bounded: small shared plans only, 1 instance, always scale-to-zero after idle, 1 GB volumes,
    and usage hard-capped at the included amount (see billing.meter_tick)."""
    k, t = account_tier(account_id)
    if t.get("max_apps") and not redeploy:
        n = q("select count(*) n from apps where account_id=? and status!='destroyed'", (account_id,), one=True)["n"]
        if n >= t["max_apps"]: raise ValueError(f"{k} tier allows {t['max_apps']} apps; upgrade with PUT /v1/billing/tier")
    if t.get("plans"):
        if spec["plan"] not in t["plans"]: raise ValueError(f"{k} tier can use {', '.join(t['plans'])}; upgrade to hobby for {spec['plan']}")
        a = spec.get("autoscale") or {}
        spec["autoscale"] = {"min": 0, "max": min(int(a.get("max") or 1), t["max_instances"]), "target_concurrency": a.get("target_concurrency") or 10,
                             "cpu_percent": a.get("cpu_percent"), "idle_seconds": min(int(a.get("idle_seconds") or t["idle_seconds"]), t["idle_seconds"])}
        spec["instances"] = min(spec["instances"], t["max_instances"])
    return spec
def deploy(account_id, spec):
    ex = q("select * from apps where name=?", (spec.get("name"),), one=True)
    if ex and ex["account_id"] == account_id and ex["status"] != "destroyed":   # redeploy: keep attached state
        old = load_spec(ex["spec"])
        for k in ("volumes", "autoscale", "health", "plan", "port", "instances", "command"):
            if k not in spec and k in old: spec[k] = old[k]
        spec["env"] = {**old.get("env", {}), **spec.get("env", {})}
    spec = validate(spec); name = spec["name"]
    if ex and ex["account_id"] != account_id: raise PermissionError("app name taken")
    apply_tier_limits(account_id, spec, redeploy=bool(ex and ex["status"] != "destroyed"))
    prov = pick_provider(spec); now = time.time()
    q("insert into apps(name,account_id,spec,status,provider,created,updated,error) values(?,?,?,?,?,?,?,null) "
      "on conflict(name) do update set account_id=excluded.account_id, spec=excluded.spec, status=excluded.status, provider=excluded.provider, updated=excluded.updated, error=null",
      (name, account_id, dump_spec(spec), "queued", prov, ex["created"] if ex else now, now))
    event(name, f"deploy queued on {prov} plan={spec['plan']} instances={spec['instances']}")
    threading.Thread(target=_run_deploy, args=(name,), daemon=True).start()
    return get_app(name)
def _run_deploy(name):
    with _lock(name):
        app = q("select * from apps where name=?", (name,), one=True); spec = load_spec(app["spec"])
        drv = get_drivers()[app["provider"]]; log = lambda m: event(name, m)
        try:
            q("update apps set status='building' where name=?", (name,))
            image = drv.build(name, spec["source"], log); spec["_image"] = image
            q("update apps set spec=?, status='starting' where name=?", (dump_spec(spec), name))
            old = q("select * from instances where app=? and status='running'", (name,))
            new = []
            for o in old if spec.get("volumes") else []: _stop(o)   # single-writer volumes: stop old first
            for i in range(spec["instances"]): new.append(_start(name, drv, image, spec))
            for o in old if not spec.get("volumes") else []: _stop(o)   # roll: new up before old down
            q("update apps set status=?, updated=? where name=?", ("running" if new else ("idle" if spec.get("autoscale") else "stopped"), time.time(), name))
            log("deploy complete")
        except Exception as e:
            q("update apps set status='failed', error=? where name=?", (str(e), name)); log(f"ERROR: {e}")
def _stop(inst):
    try: get_drivers()[inst["provider"]].stop_instance(inst["id"])
    finally: q("update instances set status='stopped', stopped=? where id=?", (time.time(), inst["id"]))
def scale(name, n, reason="manual"):
    with _lock(name):
        app = q("select * from apps where name=?", (name,), one=True); spec = load_spec(app["spec"])
        if app["status"] in ("destroyed","failed","building","queued") or not spec.get("_image"): return get_app(name)
        a = spec.get("autoscale"); n = max(0, min(int(n), 10))
        if a and reason == "manual": a["min"] = min(a["min"], n); a["max"] = max(a["max"], n)
        cur = q("select * from instances where app=? and status='running' order by started", (name,))
        if n == len(cur): return get_app(name)
        spec["instances"] = n; drv = get_drivers()[app["provider"]]
        for o in cur[n:][::-1]: _stop(o)
        for i in range(len(cur), n): _start(name, drv, spec["_image"], spec)
        q("update apps set spec=?, status=?, updated=? where name=?", (dump_spec(spec), "running" if n else ("idle" if a else "stopped"), time.time(), name))
    event(name, f"scaled {len(cur)} -> {n} ({reason})"); return get_app(name)
def replace_instance(name, inst, why):
    with _lock(name):
        app = q("select * from apps where name=?", (name,), one=True); spec = load_spec(app["spec"])
        if not q("select 1 from instances where id=? and status='running'", (inst["id"],), one=True): return   # already handled
        event(name, f"health: replacing {inst['id']} ({why})"); _stop(inst)
        if app["status"] == "running": _start(name, get_drivers()[app["provider"]], spec["_image"], spec)
def update_spec(name, fn):
    """Mutate spec and roll a redeploy from the already-built image."""
    app = q("select * from apps where name=?", (name,), one=True); spec = load_spec(app["spec"]); fn(spec)
    spec["source"] = {"image": spec["_image"]} if spec.get("_image") else spec["source"]
    spec["instances"] = max(spec["instances"], 1) if not spec.get("autoscale") else max(spec["autoscale"]["min"], min(max(spec["instances"],1), spec["autoscale"]["max"]))
    return deploy(app["account_id"], spec)
def set_env(name, env):
    def f(spec):
        for k,v in env.items():
            if v is None: spec["env"].pop(k, None)
            else: spec["env"][k] = v
    return update_spec(name, f)
def set_autoscale(name, cfg):
    def f(spec): spec["autoscale"] = cfg
    return update_spec(name, f)
# ---- volumes ----
def create_volume(account_id, name, size_gb):
    if not NAME_RE.match(name): raise ValueError("bad volume name")
    if q("select 1 from volumes where name=?", (name,), one=True): raise PermissionError("volume exists")
    k, t = account_tier(account_id)
    if t.get("max_volume_gb"):
        used = q("select coalesce(sum(size_gb),0) s from volumes where account_id=?", (account_id,), one=True)["s"]
        if used + size_gb > t["max_volume_gb"]: raise ValueError(f"{k} tier allows {t['max_volume_gb']} GB of volumes in total")
    # created lazily by the driver of the node that first mounts it (docker named volume)
    q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", (name, account_id, size_gb, time.time())); return q("select * from volumes where name=?", (name,), one=True)
def attach_volume(app, vol, path):
    v = q("select * from volumes where name=?", (vol,), one=True)
    if v["app"] and v["app"] != app: raise ValueError(f"volume attached to {v['app']}; detach first")
    q("update volumes set app=?, mount_path=? where name=?", (app, path, vol)); event(app, f"volume {vol} attached at {path}")
    def f(spec): spec["volumes"] = [x for x in spec.get("volumes",[]) if x["name"] != vol] + [{"name": vol, "path": path}]
    return update_spec(app, f)
def detach_volume(app, vol):
    q("update volumes set app=null, mount_path=null where name=?", (vol,)); event(app, f"volume {vol} detached")
    def f(spec): spec["volumes"] = [x for x in spec.get("volumes",[]) if x["name"] != vol]
    return update_spec(app, f)
def delete_volume(vol):
    v = q("select * from volumes where name=?", (vol,), one=True)
    if v["app"]: raise ValueError("detach first")
    for d in get_drivers().values():
        try: d.delete_volume(vol) if hasattr(d, "delete_volume") else None
        except Exception: pass
    q("delete from volumes where name=?", (vol,))
def destroy(name):
    q("update volumes set app=null, mount_path=null where app=?", (name,)); q("delete from domains where app=?", (name,))
    for o in q("select * from instances where app=? and status='running'", (name,)): _stop(o)
    q("update apps set status='destroyed' where name=?", (name,)); event(name, "destroyed")
def get_app(name):
    a = q("select * from apps where name=?", (name,), one=True)
    if not a: return None
    spec = load_spec(a.pop("spec")); a["spec"] = {k:v for k,v in spec.items() if k!="env"}
    a["env_keys"] = sorted(spec.get("env",{})); a["domains"] = [d["domain"] for d in q("select domain from domains where app=?", (name,))]; a["plan"] = PLANS[spec["plan"]] | {"id": spec["plan"]}
    a["instances"] = q("select id,provider,node_id,host,port,status,started from instances where app=? and status='running'", (name,))
    return a
def logs(name, tail=200):
    out = ["== platform events =="] + [time.strftime("%H:%M:%S ", time.localtime(e["ts"]))+e["msg"] for e in q("select * from events where app=? order by id desc limit 50", (name,))][::-1]
    for i in q("select * from instances where app=? and status='running'", (name,)):
        out += [f"== instance {i['id']} =="] + get_drivers()[i["provider"]].logs(i["id"], tail).splitlines()
    return "\n".join(out)
def usage(account_id):
    rows = q("select a.name, a.spec, i.started, i.stopped from instances i join apps a on a.name=i.app where a.account_id=?", (account_id,))
    tot = {}; now = time.time()
    for r in rows:
        plan = load_spec(r["spec"])["plan"]; hrs = ((r["stopped"] or now) - r["started"])/3600
        t = tot.setdefault(r["name"], {"plan":plan,"hours":0,"cost_usd":0}); t["hours"] += hrs; t["cost_usd"] += hrs*PLANS[plan]["price_usd_hr"]
    for t in tot.values(): t["hours"]=round(t["hours"],4); t["cost_usd"]=round(t["cost_usd"],2)
    return {"apps": tot, "total_usd": round(sum(t["cost_usd"] for t in tot.values()),2)}
def new_api_key(): return "pk_" + secrets.token_urlsafe(24)
def create_account(email, password_hash=None, verified=False):
    """API keys are stored only as SHA-256 hashes; the plaintext key is returned once."""
    key = new_api_key()
    q("insert into accounts(email,api_key_hash,api_key_prefix,password_hash,email_verified,created) values(?,?,?,?,?,?)",
      (email.lower(), sha256(key), key[:10], password_hash, int(verified), time.time()))
    a = q("select * from accounts where email=?", (email.lower(),), one=True); a["api_key"] = key; return a
def rotate_api_key(account_id):
    key = new_api_key(); q("update accounts set api_key_hash=?, api_key_prefix=? where id=?", (sha256(key), key[:10], account_id)); return key
def account_by_key(key):
    if not key: return None
    h = sha256(key); a = q("select * from accounts where api_key_hash=?", (h,), one=True)
    if a: return a
    k = q("select account_id from api_keys where key_hash=?", (h,), one=True)
    return q("select * from accounts where id=?", (k["account_id"],), one=True) if k else None
def create_named_key(account_id, label, replace=True):
    """Extra keys (MCP connector, CI) that don't revoke the primary CLI key. replace=True rotates the key with the same label."""
    if replace: q("delete from api_keys where account_id=? and label=?", (account_id, label))
    key = "pk_" + secrets.token_urlsafe(24)
    q("insert into api_keys(id,account_id,key_hash,prefix,label,created) values(?,?,?,?,?,?)", ("k_"+secrets.token_hex(6), account_id, sha256(key), key[:10], label, time.time()))
    return key

def reconcile(name):
    """Converge running instance count to spec (heals duplicates / losses)."""
    with _lock(name):
        app = q("select * from apps where name=?", (name,), one=True)
        if not app or app["status"] != "running": return
        spec = load_spec(app["spec"]); cur = q("select * from instances where app=? and status='running' order by started", (name,))
        want = spec["instances"]
        for o in cur[want:]: _stop(o)
        if cur[want:]: event(name, f"reconcile: stopped {len(cur)-want} extra instance(s)")
        for _ in range(len(cur), want): _start(name, get_drivers()[app["provider"]], spec["_image"], spec)
