import json, time, threading, re, secrets
from .db import q, event, PG as PGDB
from .crypto import load_spec, dump_spec, sha256
from .drivers import get_drivers
from .plans import PLANS
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
_locks = {}
def _lock(app): return _locks.setdefault(app, threading.Lock())
def validate(spec):
    if not NAME_RE.match(spec.get("name","")): raise ValueError("name must be 3-32 chars, lowercase letters/digits/hyphens")
    if spec.get("plan","cpu-small") not in PLANS: raise ValueError(f"unknown plan; choose from {list(PLANS)}")
    from . import plans as _plans
    if PLANS[spec.get("plan","cpu-small")]["kind"] == "gpu" and not _plans.GPU_ENABLED:
        raise ValueError("GPU plans are coming soon (phase 2). Join the waitlist: POST /v1/waitlist {\"email\": \"...\"}")
    src = spec.get("source") or {}
    if not (src.get("image") or src.get("git") or src.get("path") or src.get("upload")): raise ValueError("source.image, source.git or an uploaded build context required")
    pr = spec.get("processes")
    if pr:
        if not isinstance(pr, dict) or not all(re.match(r"^[a-z][a-z0-9]{0,15}$", k) for k in pr): raise ValueError("processes: {group: command}, group names lowercase a-z0-9")
        spec["process_counts"] = {k: max(0, min(int((spec.get("process_counts") or {}).get(k, 1)), 10)) for k in pr}
        if "web" in pr and pr["web"]: spec["command"] = ["sh", "-c", pr["web"]] if isinstance(pr["web"], str) else list(pr["web"])
    for c in spec.get("cron") or []:
        from .cron import parse
        parse(c.get("schedule", "")); c.setdefault("name", f"job{(spec['cron'].index(c))}")
        if not c.get("command"): raise ValueError("cron job needs a command")
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
def _avail(drv, region):
    try: return drv.available(region=region)
    except TypeError: return drv.available()
def pick_provider(spec):
    """Pool for the spec's region. local-docker is the control plane host, so it only serves the control plane's region;
    remote pools only count if they have (or may provision) a node in that region. Never places outside the region."""
    from . import plans as _plans, regions as R
    d = get_drivers(); plan = PLANS[spec["plan"]]; region = R.canonical(spec.get("region"))
    local_ok = region == R.home() and R.local_capacity()
    if spec.get("provider"):
        if spec["provider"] not in d: raise ValueError(f"unknown provider {spec['provider']}")
        if spec["provider"] == "local-docker" and not local_ok: raise NoCapacity(f"local-docker only serves region {R.home()}; app is in {region}")
        if not _plans.pool_allowed(spec["plan"], spec["provider"]):
            m = _plans.pool_margin(spec["plan"], spec["provider"])
            raise ValueError(f"plan {spec['plan']} can't run on {spec['provider']}: margin "
                             + ("unknown (no cost on file)" if m is None else f"{m:.0%}") + f" is under the {_plans.MIN_MARGIN:.0%} floor")
        return spec["provider"]
    tried = []
    for pool in plan["providers"]:
        if not _plans.pool_allowed(spec["plan"], pool): tried.append(f"{pool}: refused, under {_plans.MIN_MARGIN:.0%} margin floor"); continue
        if pool == "local-docker" and not local_ok: tried.append(f"local-docker: only serves {R.home()}"); continue
        ok, why = _avail(d[pool], region); tried.append(f"{pool}: {why}")
        if ok: return pool
    if local_ok and plan["kind"] == "cpu" and _plans.pool_allowed(spec["plan"], "local-docker"): return "local-docker"
    if local_ok and plan["kind"] == "gpu" and d["local-docker"].supports_gpu and _plans.pool_allowed(spec["plan"], "local-docker"): return "local-docker"
    raise NoCapacity(f"no {'GPU ' if plan['kind'] == 'gpu' else ''}capacity for {spec['plan']} in region {region} (" + "; ".join(tried or [f"local-docker: refused, under {_plans.MIN_MARGIN:.0%} margin floor"]) + ")")
import socket, urllib.request, urllib.error
def wait_ready(host, port, path=None, timeout=None, tcp=False):
    end = time.time() + (timeout or float(__import__("os").environ.get("PAAS_READY_TIMEOUT", "30")))
    while time.time() < end:
        if tcp:
            try: socket.create_connection((host, port), timeout=2).close(); return True
            except OSError: time.sleep(0.3); continue
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
    h = spec.get("health") or {}
    ok = True if h.get("none") else wait_ready(inst.host, inst.port, h.get("path"), tcp=bool(h.get("tcp")))
    inst.status = "running" if ok else "unready"
    q("update instances set status='running' where id=?", (inst.id,))   # only now does the edge route to it
    event(name, f"instance {inst.id} up at {inst.host}:{inst.port}" + ("" if ok else " (not ready after timeout)"))
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
        n = q("select count(*) n from apps where account_id=? and status!='destroyed' and spec not like ?", (account_id, '%"_parent": %'), one=True)["n"]
        if n >= t["max_apps"]: raise ValueError(f"{k} tier allows {t['max_apps']} apps; upgrade with PUT /v1/billing/tier")
    if t.get("plans"):
        if spec["plan"] not in t["plans"]: raise ValueError(f"{k} tier can use {', '.join(t['plans'])}; upgrade to hobby for {spec['plan']}")
        if spec.get("kind") == "postgres" or spec.get("_parent"): return spec   # databases/workers never sleep (compute stays inside the hard cap)
        a = spec.get("autoscale") or {}
        spec["autoscale"] = {"min": 0, "max": min(int(a.get("max") or 1), t["max_instances"]), "target_concurrency": a.get("target_concurrency") or 10,
                             "cpu_percent": a.get("cpu_percent"), "idle_seconds": min(int(a.get("idle_seconds") or t["idle_seconds"]), t["idle_seconds"])}
        spec["instances"] = min(spec["instances"], t["max_instances"])
    return spec
def _check_platform_image(account_id, spec):
    """Images we built (paas/<app>:r..., <registry>/<app>:ts) may only be deployed by the account owning <app>."""
    import os
    img = (spec.get("source") or {}).get("image") or ""
    regs = [r for r in (os.environ.get("PAAS_REGISTRY", "127.0.0.1:5000"), os.environ.get("PAAS_REGISTRY_NODE")) if r]
    owner = None
    if img.startswith("paas/"): owner = img[5:].split(":")[0]
    for r in regs:
        if img.startswith(r + "/"): owner = img[len(r) + 1:].split(":")[0]
    src = spec.get("source") or {}
    if src.get("github_installation"):
        from . import github_app
        if not github_app.owns_installation(account_id, src["github_installation"]): raise PermissionError("GitHub installation not linked to this account")
    if src.get("upload"):
        u = q("select account_id, app from uploads where id=?", (src["upload"],), one=True)
        if not u or u["account_id"] != account_id or u["app"] != spec.get("name"): raise PermissionError("unknown upload for this app")
    if owner is None: return
    o = q("select account_id from apps where name=?", (owner,), one=True)
    if not o or o["account_id"] != account_id: raise PermissionError("that image belongs to another app")
def _set_region(spec, ex, account_id=None):
    """Canonical region on the spec. New apps must target a live region; an app never moves region (its volumes and Postgres
    live there); volumes in the spec must be in the app's region."""
    from . import regions as R
    live_app = ex and ex["status"] != "destroyed"
    cur = R.canonical(ex.get("region")) if live_app else None
    want = R.canonical(spec.get("region")) if spec.get("region") else (cur or R.home())
    if cur and want != cur:
        raise R.RegionError(f"app {spec['name']} runs in {cur}; moving an app to another region isn't supported (create a new app in {want})")
    if not cur: R.require_live(want)
    if account_id is not None:
        a = q("select tier from accounts where id=?", (account_id,), one=True)
        R.check_allowed(want, (a or {}).get("tier") or "free", PLANS.get(spec.get("plan", "cpu-small")))
    spec["region"] = want
    for v in spec.get("volumes") or []:
        vr = q("select region from volumes where name=?", (v["name"],), one=True)
        if vr: R.same_region("volume", v["name"], vr.get("region"), spec["name"], want)
def deploy(account_id, spec):
    ex = q("select * from apps where name=?", (spec.get("name"),), one=True)
    if ex and ex["account_id"] == account_id and ex["status"] != "destroyed":   # redeploy: keep attached state
        old = load_spec(ex["spec"])
        for k in ("volumes", "autoscale", "health", "plan", "port", "instances", "command", "networks", "net_alias", "kind", "pg_plan", "provider",
                  "processes", "process_counts", "cron", "release_command", "_parent", "_preview_of"):
            if k not in spec and k in old: spec[k] = old[k]
        if not spec.pop("_replace_env", False): spec["env"] = {**old.get("env", {}), **spec.get("env", {})}
    spec = validate(spec); name = spec["name"]
    if ex and ex["account_id"] != account_id: raise PermissionError("app name taken")
    _set_region(spec, ex, account_id)
    _check_platform_image(account_id, spec)
    acct_net = f"aloof-acct-{account_id}"   # private network: <app>.internal resolves between this account's apps only
    spec["networks"] = sorted(set(spec.get("networks") or []) | {acct_net}, key=lambda n: (n != acct_net, n)) if spec.get("kind") != "postgres" else sorted(set(spec.get("networks") or []) | {acct_net})
    spec["net_aliases"] = {**(spec.get("net_aliases") or {}), acct_net: [name, f"{name}.internal"]}
    apply_tier_limits(account_id, spec, redeploy=bool(ex and ex["status"] != "destroyed"))
    prov = pick_provider(spec); now = time.time()
    q("insert into apps(name,account_id,spec,status,provider,created,updated,error) values(?,?,?,?,?,?,?,null) "
      "on conflict(name) do update set account_id=excluded.account_id, spec=excluded.spec, status=excluded.status, provider=excluded.provider, updated=excluded.updated, error=null",
      (name, account_id, dump_spec(spec), "queued", prov, ex["created"] if ex else now, now))
    q("update apps set region=? where name=?", (spec["region"], name))
    event(name, f"deploy queued on {prov} plan={spec['plan']} instances={spec['instances']}")
    threading.Thread(target=_run_deploy, args=(name,), daemon=True).start()
    return get_app(name)
# ---- releases: every deploy/config change is a numbered release; rollback redeploys an earlier one ----
RELEASE_KEEP = 20
def _new_release(name, spec, reason):
    v = (q("select max(version) v from releases where app=?", (name,), one=True) or {}).get("v") or 0
    q("insert into releases(app,version,image,spec,status,reason,created) values(?,?,?,?,?,?,?)",
      (name, v + 1, spec.get("_image"), dump_spec(spec), "deploying", reason, time.time()))
    return v + 1
def _set_release(name, version, status, image=None, spec=None):
    if status == "active": q("update releases set status='superseded' where app=? and status='active'", (name,))
    q("update releases set status=?" + (", image=?" if image else "") + (", spec=?" if spec else "") + " where app=? and version=?",
      (status, *([image] if image else []), *([dump_spec(spec)] if spec else []), name, version))
def releases(name):
    return [{k: r[k] for k in ("version", "image", "status", "reason", "created")} | {"plan": load_spec(r["spec"]).get("plan"), "env_keys": sorted(load_spec(r["spec"]).get("env", {}))}
            for r in q("select * from releases where app=? order by version desc limit 50", (name,))]
def rollback(name, version=None):
    """Redeploy an earlier release's image + config (env, plan, port, command, scale, health). Volumes, Postgres attachments and
    domains stay as they are now. No rebuild, so it's fast; zero-downtime like any deploy."""
    cur = q("select version from releases where app=? and status='active' order by version desc", (name,), one=True)
    if version is None:
        r = q("select * from releases where app=? and status='superseded' and image is not null" + (" and version<?" if cur else "") + " order by version desc",
              (name, *([cur["version"]] if cur else [])), one=True)
    else: r = q("select * from releases where app=? and version=?", (name, int(version)), one=True)
    if not r or not r["image"]: raise ValueError("no earlier release to roll back to" if version is None else f"release v{version} not found or never built")
    app = q("select * from apps where name=?", (name,), one=True); now_spec = load_spec(app["spec"]); spec = load_spec(r["spec"])
    for k in ("volumes", "networks"): spec[k] = now_spec.get(k, [])
    for db in q("select attached from pg_dbs"):   # keep live database attachments (their URLs) as they are now
        for x in json.loads(db["attached"] or "[]"):
            if x["app"] == name and x["var"] in now_spec.get("env", {}): spec["env"][x["var"]] = now_spec["env"][x["var"]]
    spec["_image"] = r["image"]
    q("update apps set spec=?, status='queued', updated=?, error=null where name=?", (dump_spec(spec), time.time(), name))
    event(name, f"rollback to v{r['version']} queued")
    threading.Thread(target=_run_deploy, args=(name, r["image"], f"rollback to v{r['version']}"), daemon=True).start()
    return get_app(name)
def _prune_images(name, drv):
    keep = {r["image"] for r in q("select image from releases where app=? order by version desc limit ?", (name, RELEASE_KEEP))}
    for r in q("select image from releases where app=? order by version desc limit -1 offset ?" if not PGDB else
               "select image from releases where app=? order by version desc offset ?", (name, RELEASE_KEEP)):
        if r["image"] and r["image"] not in keep and hasattr(drv, "remove_image"):
            try: drv.remove_image(r["image"])
            except Exception: pass
def _source_type(spec):
    s = spec.get("source") or {}
    if s.get("github_installation"): return "github"
    for k in ("git", "upload", "path", "image"):
        if s.get(k): return k
    return "postgres" if spec.get("kind") == "postgres" else "other"
def _track_deploy(app, spec, ver, reason):
    """Analytics only for real deploys (not scale/restart), never raises."""
    try:
        if reason in ("scale",): return
        from . import analytics
        p = {"app_id": app["name"], "region": spec.get("region"), "source_type": _source_type(spec), "version": ver}
        analytics.capture(app["account_id"], "deploy_succeeded", p)
        if analytics.enabled() and analytics.once(app["account_id"], "first_deploy_succeeded"):
            analytics.capture(app["account_id"], "first_deploy_succeeded", {k: p[k] for k in ("app_id", "region", "source_type")})
    except Exception: pass
def _run_deploy(name, image=None, reason=None):
    """Zero-downtime: new instances start and must pass readiness (HTTP health path, or TCP for databases) before the old ones
    stop. If any new instance fails, the new ones are removed and the previous release keeps serving. Apps with a volume are
    single-writer, so the old instance stops first (a brief gap) and a failed start is reported."""
    with _lock(name):
        app = q("select * from apps where name=?", (name,), one=True); spec = load_spec(app["spec"])
        drv = get_drivers()[app["provider"]]; log = lambda m: event(name, m)
        prev = q("select * from releases where app=? and status='active' order by version desc", (name,), one=True)
        src = spec.get("source") or {}
        ver = _new_release(name, spec, reason or ("config change" if src.get("image") and src.get("image") == spec.get("_image") else "deploy"))
        log(f"release v{ver}: {reason or 'deploy'}")
        new = []
        try:
            if not image:
                q("update apps set status='building' where name=?", (name,))
                image = drv.build(name, spec["source"], log)
            spec["_image"] = image; _set_release(name, ver, "deploying", image=image, spec=spec)
            q("update apps set spec=?, status='starting' where name=?", (dump_spec(spec), name))
            if spec.get("release_command") and reason not in ("scale",):
                log(f"release_command: {spec['release_command']}")
                if not hasattr(drv, "run_once"): raise RuntimeError(f"release_command not supported on {drv.name}")
                code, out = drv.run_once(name, image, spec, spec["release_command"])
                for line in out.splitlines()[-30:]: log(f"[release] {line}")
                if code != 0: raise RuntimeError(f"release_command exited {code}; new release not started")
            old = q("select * from instances where app=? and status='running'", (name,))
            single = bool(spec.get("volumes"))
            for o in old if single else []: _stop(o)   # single-writer volumes: stop old first
            for i in range(spec["instances"]): new.append(_start(name, drv, image, spec))
            bad = [n for n in new if n.status != "running"]
            if bad and old and not single:
                raise RuntimeError(f"{len(bad)} new instance(s) failed readiness")
            for o in old if not single else []: _stop(o)   # roll: new up before old down
            if (q("select status from apps where name=?", (name,), one=True) or {}).get("status") == "destroyed":
                for n in new: _stop({"id": n.id, "provider": drv.name})
                _set_release(name, ver, "failed"); log("deploy aborted: app destroyed"); return
            q("update apps set status=?, updated=?, error=? where name=?", ("running" if new else ("idle" if spec.get("autoscale") else "stopped"), time.time(),
              (f"{len(bad)} instance(s) not ready" if bad else None), name))
            _set_release(name, ver, "active"); log(f"deploy complete (v{ver} live)")
            _track_deploy(app, spec, ver, reason)
            try: _sync_groups(name, app["account_id"], spec, image)
            except Exception as e: log(f"process groups: {e}")
            _prune_images(name, drv)
        except Exception as e:
            for n in new:
                try: _stop({"id": n.id, "provider": drv.name})
                except Exception: pass
            _set_release(name, ver, "failed")
            alive = q("select 1 from instances where app=? and status='running'", (name,), one=True)
            if prev and alive:   # previous release still serving: keep it, report the failure
                q("update apps set spec=?, status='running', error=? where name=?", (prev["spec"], f"v{ver} failed: {e}; still serving v{prev['version']}", name))
                log(f"ERROR: v{ver} failed ({e}); v{prev['version']} still serving, no downtime")
            else:
                q("update apps set status='failed', error=? where name=?", (str(e), name)); log(f"ERROR: {e}")
def deploy_progress(name):
    """The latest deploy as real backend states only: apps.status (queued -> building -> starting -> running|idle|stopped|failed),
    the release row (deploying -> active|failed) and the platform events logged since the deploy was queued."""
    app = q("select * from apps where name=?", (name,), one=True)
    if not app: return None
    anchor = q("select id, ts, msg from events where app=? and (msg like ? or msg like ?) order by id desc limit 1", (name, "deploy queued%", "rollback to % queued"), one=True)
    t0 = anchor["ts"] if anchor else 0
    evs = q("select id, ts, msg from events where app=? and id>=? order by id", (name, anchor["id"] if anchor else 0))[-400:]
    rel = q("select version, status, reason, created, image from releases where app=? order by version desc limit 1", (name,), one=True)
    if rel and rel["created"] < t0 - 1: rel = None   # this deploy's release row isn't written yet
    st = app["status"]; src = load_spec(app["spec"]).get("source") or {}
    from_image = bool(src.get("image")) or bool(rel and (rel["reason"] or "").startswith("rollback"))
    pushed = any(e["msg"].startswith("pushed ") for e in evs)
    if rel and rel["status"] == "active": phase = "live"
    elif rel and rel["status"] == "failed": phase = "failed"
    elif st == "failed": phase = "failed"
    elif st in ("queued", "building", "starting"): phase = st
    elif rel and rel["status"] == "deploying": phase = "starting"
    else: phase = "live" if st in ("running", "idle", "stopped") else st
    order = ["queued", "building", "starting", "live"]
    reached = {"queued": 0, "building": 1, "starting": 2, "live": 3}.get(phase)
    if phase == "failed":   # where it stopped: last state the events show
        reached = 2 if any(e["msg"].startswith(("release_command", "[release]")) for e in evs) or (rel and rel["image"]) else 1 if any(not e["msg"].startswith(("deploy queued", "release v")) for e in evs) else 0
    steps = []
    for i, k in enumerate(order):
        lab = {"queued": "Queued", "building": "Pull image" if from_image else "Build", "starting": "Start & health check", "live": "Live"}[k]
        if phase == "failed": state = "done" if i < reached else "failed" if i == reached else "pending"
        else: state = "done" if i < reached or phase == "live" else "active" if i == reached else "pending"
        steps.append({"id": k, "label": lab, "state": state})
    if pushed or (phase in ("building",) and app["provider"] != "local-docker"):
        steps.insert(2, {"id": "pushing", "label": "Push to registry", "state": "done" if pushed else "pending" if phase == "building" else steps[1]["state"]})
    err = app["error"]
    still = rel and rel["status"] == "failed" and st == "running"
    return {"app": name, "phase": phase, "version": rel["version"] if rel else None, "reason": rel["reason"] if rel else None, "queued_at": t0 or None,
            "steps": steps, "error": err, "still_serving_previous": bool(still), "events": evs[-200:], "last_event_id": evs[-1]["id"] if evs else 0,
            "excerpt": [e["msg"] for e in evs if e["msg"].strip()][-20:] if phase == "failed" else []}
def group_app(name, g): return f"{name}-{g}"
def _sync_groups(name, account_id, spec, image):
    """Process groups (fly.toml [processes]): 'web' is this app; every other group runs as a child app <app>-<group> from the same image,
    env and networks, with its own command and count (aloof scale count worker=2). No HTTP routing or health probe for workers."""
    groups = {g: c for g, c in (spec.get("processes") or {}).items() if g != "web"}
    for ch in q("select name, spec from apps where status!='destroyed' and name like ?", (name + "-%",)):
        cs = load_spec(ch["spec"])
        if cs.get("_parent") == name and ch["name"] not in {group_app(name, g) for g in groups}: destroy(ch["name"]); event(name, f"process group {ch['name']} removed")
    for g, cmd in groups.items():
        cs = {"name": group_app(name, g), "source": {"image": image}, "plan": spec["plan"], "port": spec.get("port", 8080), "env": dict(spec.get("env") or {}),
              "_replace_env": True, "instances": (spec.get("process_counts") or {}).get(g, 1), "command": ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd),
              "health": {"none": True}, "volumes": [], "networks": [n for n in spec.get("networks") or []], "_parent": name, "provider": spec.get("provider")}
        if spec.get("net_alias"): cs["net_alias"] = None
        if len(cs["name"]) > 32: raise ValueError(f"process group name too long for {name}")
        deploy(account_id, cs); event(name, f"process group {g}: {cs['instances']} x `{cmd}`")
def children(name):
    return [r["name"] for r in q("select name, spec from apps where status!='destroyed' and name like ?", (name + "-%",))
            if load_spec(r["spec"]).get("_parent") == name or load_spec(r["spec"]).get("_preview_of") == name]
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
    spec["source"] = {"image": spec["_image"]} if spec.get("_image") else spec["source"]; spec["_replace_env"] = True   # full env (unsets stick)
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
def create_volume(account_id, name, size_gb, region=None):
    from . import regions as R
    if not NAME_RE.match(name): raise ValueError("bad volume name")
    region = R.require_live(region or R.home())
    if q("select 1 from volumes where name=?", (name,), one=True): raise PermissionError("volume exists")
    k, t = account_tier(account_id)
    R.check_allowed(region, k)   # region plan rules (Free is EU-only) apply to storage too
    if t.get("max_volume_gb"):
        used = q("select coalesce(sum(size_gb),0) s from volumes where account_id=?", (account_id,), one=True)["s"]
        if used + size_gb > t["max_volume_gb"]: raise ValueError(f"{k} tier allows {t['max_volume_gb']} GB of volumes in total")
    # created lazily by the driver of the node that first mounts it (docker named volume)
    q("insert into volumes(name,account_id,size_gb,created,region) values(?,?,?,?,?)", (name, account_id, size_gb, time.time(), region)); return q("select * from volumes where name=?", (name,), one=True)
def attach_volume(app, vol, path):
    v = q("select * from volumes where name=?", (vol,), one=True)
    if v["app"] and v["app"] != app: raise ValueError(f"volume attached to {v['app']}; detach first")
    from . import regions as R
    ap = q("select region from apps where name=?", (app,), one=True)
    if ap: R.same_region("volume", vol, v.get("region"), app, ap.get("region"))
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
    for ch in children(name): destroy(ch)
    q("update apps set status='destroyed' where name=?", (name,))   # first, so an in-flight deploy aborts instead of resurrecting it
    q("update volumes set app=null, mount_path=null where app=?", (name,)); q("delete from domains where app=?", (name,))
    with _lock(name):
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
TEST_EMAIL_PATTERNS = ("%@example.com", "%@example.org", "%@example.net", "%.test", "%.invalid", "%@localhost")   # SQL LIKE; also used by www.mark_flags
def is_test_email(email):
    e = (email or "").strip().lower()
    return any(e.endswith(p[1:]) for p in TEST_EMAIL_PATTERNS)
def create_account(email, password_hash=None, verified=False):
    """API keys are stored only as SHA-256 hashes; the plaintext key is returned once. Reserved test domains get is_test=1 at creation."""
    key = new_api_key()
    q("insert into accounts(email,api_key_hash,api_key_prefix,password_hash,email_verified,created,is_test) values(?,?,?,?,?,?,?)",
      (email.lower(), sha256(key), key[:10], password_hash, int(verified), time.time(), int(is_test_email(email))))
    a = q("select * from accounts where email=?", (email.lower(),), one=True); a["api_key"] = key; return a
def rotate_api_key(account_id):
    key = new_api_key(); q("update accounts set api_key_hash=?, api_key_prefix=? where id=?", (sha256(key), key[:10], account_id)); return key
def account_by_key(key):
    if not key: return None
    h = sha256(key); a = q("select * from accounts where api_key_hash=?", (h,), one=True)
    if a: return a
    k = q("select id,account_id from api_keys where key_hash=?", (h,), one=True)
    if k: q("update api_keys set last_used=? where id=?", (time.time(), k["id"])); return q("select * from accounts where id=?", (k["account_id"],), one=True)
    if key.startswith("dt_"):   # deploy token: scoped to one app (or the whole account = org), optional expiry
        t = q("select * from deploy_tokens where token_hash=?", (h,), one=True)
        if not t or (t["expires"] and t["expires"] < time.time()): return None
        q("update deploy_tokens set last_used=? where id=?", (time.time(), t["id"]))
        a = q("select * from accounts where id=?", (t["account_id"],), one=True)
        if a: a["_token"] = t["id"]; a["_scope_app"] = t["app"]
        return a
    return None
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
