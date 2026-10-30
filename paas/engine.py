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
    if src.get("image"):
        from .creds import check_image
        if not isinstance(src["image"], str) or re.search(r"\s", src["image"]): raise ValueError("source.image is not a valid image reference")
        check_image(src["image"])
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
    if isinstance(spec.get("health"), dict): check_health_path(spec["health"].get("path"))
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
import socket, urllib.request, urllib.error, urllib.parse
_HP_BAD = re.compile(r"[@\\\s\x00-\x1f\x7f]")
def check_health_path(path):
    """Health check paths are a plain absolute path (+ optional query): no userinfo (@), scheme/host, //, backslash, whitespace or control chars."""
    if path in (None, ""): return None
    if not isinstance(path, str) or len(path) > 512 or not path.startswith("/") or path.startswith("//") or "://" in path or _HP_BAD.search(path):
        raise ValueError("health path must be an absolute path like /healthz (no @, scheme, host, // prefix, backslashes or spaces)")
    return path
def health_url(host, port, path):
    """http://host:port + a checked path, assembled with urlunsplit so the path can never change the host."""
    p = check_health_path(path) or "/"; pth, _, qs = p.partition("?")
    return urllib.parse.urlunsplit(("http", f"{host}:{port}", pth, qs, ""))
def wait_ready(host, port, path=None, timeout=None, tcp=False):
    end = time.time() + (timeout or float(__import__("os").environ.get("PAAS_READY_TIMEOUT", "30")))
    while time.time() < end:
        if tcp:
            try: socket.create_connection((host, port), timeout=2).close(); return True
            except OSError: time.sleep(0.3); continue
        try: urllib.request.urlopen(health_url(host, port, path), timeout=2); return True
        except urllib.error.HTTPError as e:
            if path is None or e.code < 500: return True
        except Exception: pass
        time.sleep(0.2)
    return False
def _start(name, drv, image, spec):
    ow = q("select account_id from apps where name=?", (name,), one=True)
    if ow and ow.get("account_id") is not None:
        st = q("select deleting from accounts where id=?", (ow["account_id"],), one=True)
        if not st or st.get("deleting"): raise PermissionError("account deleted or being deleted: not starting instances")
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
PAID_DEFAULT_PLAN = "shared-1x-1gb"
def default_plan(account_id):
    """Plan used when a new app doesn't name one: the largest plan the account's tier allows (Free: shared-1x-512mb), or
    shared-1x-1gb on paid tiers (which allow every plan). Falls back to shared-1x-256mb."""
    try:
        _, t = account_tier(account_id)
        if t.get("plans"): return max(t["plans"], key=lambda p: (PLANS[p]["memory_mb"], PLANS[p]["vcpu"])) if all(p in PLANS for p in t["plans"]) else t["plans"][0]
        return PAID_DEFAULT_PLAN
    except Exception: return "shared-1x-256mb"
def apply_tier_limits(account_id, spec, redeploy=False):
    """Free tier cost is bounded: small shared plans only, 1 instance, always scale-to-zero after idle, 1 GB volumes,
    and usage hard-capped at the included amount (see billing.meter_tick)."""
    k, t = account_tier(account_id)
    if t.get("max_apps") and not redeploy:
        n = q("select count(*) n from apps where account_id=? and status!='destroyed' and spec not like ?", (account_id, '%"_parent": %'), one=True)["n"]
        if n >= t["max_apps"]: raise ValueError(f"{k} tier allows {t['max_apps']} apps; upgrade with PUT /v1/billing/tier")
    if t.get("plans"):
        if spec["plan"] not in t["plans"]: raise ValueError(f"plan {spec['plan']} isn't available on the {k} tier. Allowed plans: {', '.join(t['plans'])} (or leave the plan out to get {default_plan(account_id)}). Upgrade to hobby for {spec['plan']}.")
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
def account_active(account_id):
    """False for a missing, suspended or being-deleted account (D1). None = platform-internal caller (no account)."""
    if account_id is None: return True
    a = q("select suspended, deleting from accounts where id=?", (account_id,), one=True)
    return bool(a) and not a.get("suspended") and not a.get("deleting")
def deploy(account_id, spec):
    if not account_active(account_id): raise PermissionError("this account is suspended or being deleted; deploys are disabled")
    if not spec.get("plan"): spec.pop("plan", None)   # empty/None = "not given": redeploys keep their plan, new apps get default_plan()
    ex = q("select * from apps where name=?", (spec.get("name"),), one=True)
    if ex and ex["account_id"] == account_id and ex["status"] != "destroyed":   # redeploy: keep attached state
        old = load_spec(ex["spec"])
        for k in ("volumes", "autoscale", "health", "plan", "port", "instances", "command", "networks", "net_alias", "kind", "pg_plan", "provider",
                  "processes", "process_counts", "cron", "release_command", "_parent", "_preview_of"):
            if k not in spec and k in old: spec[k] = old[k]
        if not spec.pop("_replace_env", False): spec["env"] = {**old.get("env", {}), **spec.get("env", {})}
    if "plan" not in spec and account_id is not None: spec["plan"] = default_plan(account_id)
    spec = validate(spec); name = spec["name"]
    if ex and ex["account_id"] != account_id: raise PermissionError(f"app name taken: '{name}' belongs to another account (names are global). Pick another name, e.g. {name}-{secrets.token_hex(2)}")
    _set_region(spec, ex, account_id)
    _check_platform_image(account_id, spec)
    acct_net = f"aloof-acct-{account_id}"   # private network: <app>.internal resolves between this account's apps only
    spec["networks"] = sorted(set(spec.get("networks") or []) | {acct_net}, key=lambda n: (n != acct_net, n)) if spec.get("kind") != "postgres" else sorted(set(spec.get("networks") or []) | {acct_net})
    spec["net_aliases"] = {**(spec.get("net_aliases") or {}), acct_net: [name, f"{name}.internal"]}
    apply_tier_limits(account_id, spec, redeploy=bool(ex and ex["status"] != "destroyed"))
    prov = pick_provider(spec); now = time.time()
    if prov == "local-docker":   # fail fast with a clear 503 before queueing; start_instance re-checks per instance
        from . import capacity; capacity.admit(spec["plan"], spec["instances"])
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
    from . import selfheal
    return [{k: r[k] for k in ("version", "image", "status", "reason", "created")} | {"plan": load_spec(r["spec"]).get("plan"), "env_keys": sorted(load_spec(r["spec"]).get("env", {}))}
            | {"auto_rollback": selfheal.auto_rollback_info(name, r["version"])}
            for r in q("select * from releases where app=? order by version desc limit 50", (name,))]
def rollback(name, version=None, reason=None, only_if_latest=None):
    """Redeploy an earlier release's image + config (env, plan, port, command, scale, health). Volumes, Postgres attachments and
    domains stay as they are now. No rebuild, so it's fast; zero-downtime like any deploy."""
    cur = q("select version from releases where app=? and status='active' order by version desc", (name,), one=True)
    if version is None:
        r = q("select * from releases where app=? and status='superseded' and image is not null" + (" and version<?" if cur else "") + " order by version desc",
              (name, *([cur["version"]] if cur else [])), one=True)
    else: r = q("select * from releases where app=? and version=?", (name, int(version)), one=True)
    if not r or not r["image"]: raise ValueError("no earlier release to roll back to" if version is None else f"release v{version} not found or never built")
    app = q("select * from apps where name=?", (name,), one=True)
    if not app or app["status"] == "destroyed": raise ValueError("app was deleted")
    if only_if_latest is not None:   # auto-rollback: only if the watched release is still the newest and the app is idle/running
        top = q("select max(version) v from releases where app=?", (name,), one=True)["v"]
        if top != only_if_latest or app["status"] not in ("running", "idle"): raise ValueError(f"v{only_if_latest} is no longer the latest release")
    now_spec = load_spec(app["spec"]); spec = load_spec(r["spec"])
    for k in ("volumes", "networks"): spec[k] = now_spec.get(k, [])
    for db in q("select attached from pg_dbs"):   # keep live database attachments (their URLs) as they are now
        for x in json.loads(db["attached"] or "[]"):
            if x["app"] == name and x["var"] in now_spec.get("env", {}): spec["env"][x["var"]] = now_spec["env"][x["var"]]
    spec["_image"] = r["image"]
    q("update apps set spec=?, status='queued', updated=?, error=null where name=?", (dump_spec(spec), time.time(), name))
    event(name, f"rollback to v{r['version']} queued")
    threading.Thread(target=_run_deploy, args=(name, r["image"], reason or f"rollback to v{r['version']}", only_if_latest), daemon=True).start()
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
def _run_deploy(name, image=None, reason=None, only_if_latest=None):
    """Zero-downtime: new instances start and must pass readiness (HTTP health path, or TCP for databases) before the old ones
    stop. If any new instance fails, the new ones are removed and the previous release keeps serving. Apps with a volume are
    single-writer, so the old instance stops first (a brief gap) and a failed start is reported."""
    with _lock(name):
        app = q("select * from apps where name=?", (name,), one=True)
        if not app or app["status"] == "destroyed": return   # deleted while queued: never resurrect
        if only_if_latest is not None and (q("select max(version) v from releases where app=?", (name,), one=True)["v"] or 0) != only_if_latest:
            event(name, f"self-heal: auto-rollback dropped (a newer deploy superseded v{only_if_latest})"); return
        spec = load_spec(app["spec"])
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
            try:
                from . import selfheal
                rid = (q("select id from releases where app=? and version=?", (name, ver), one=True) or {}).get("id")
                selfheal.on_active(name, ver, prev["version"] if prev else None, reason or "deploy", rid)
            except Exception as e: log(f"self-heal: {e}")
            _track_deploy(app, spec, ver, reason)
            try: _sync_groups(name, app["account_id"], spec, image)
            except Exception as e: log(f"process groups: {e}")
            _prune_images(name, drv)
        except Exception as e:
            for n in new:
                try: _stop({"id": n.id, "provider": drv.name})
                except Exception: pass
            _set_release(name, ver, "failed")
            try: q("update releases set error=? where app=? and version=?", (str(e)[:2000], name, ver))
            except Exception: pass
            alive = q("select 1 from instances where app=? and status='running'", (name,), one=True)
            if prev and alive:   # previous release still serving: keep it, report the failure
                q("update apps set spec=?, status='running', error=? where name=?", (prev["spec"], f"v{ver} failed: {e}; still serving v{prev['version']}", name))
                log(f"ERROR: v{ver} failed ({e}); v{prev['version']} still serving, no downtime")
            else:
                q("update apps set status='failed', error=? where name=?", (str(e), name)); log(f"ERROR: {e}")
def release_state(name):
    """What was asked for vs what is actually live, side by side, so a failed deploy never looks like success:
    requested_release (newest release: version, status, error), serving_release (the active one), health of the
    serving release, and mismatch + a plain warning when they differ."""
    app = q("select status, error from apps where name=?", (name,), one=True)
    if not app: return None
    cols = "version, status, reason, created, error, image"
    req = q(f"select {cols} from releases where app=? order by version desc limit 1", (name,), one=True)
    srv = q(f"select {cols} from releases where app=? and status='active' order by version desc limit 1", (name,), one=True)
    n = (q("select count(*) n from instances where app=? and status='running'", (name,), one=True) or {}).get("n") or 0
    st = app["status"]
    state = "sleeping" if st == "idle" and srv else "stopped" if st == "stopped" else "healthy" if n and st == "running" else \
            "starting" if st in ("queued", "building", "starting") and n else "down"
    health = {"release": srv["version"] if srv else None, "state": state if srv else "none", "instances_running": n,
              "note": (f"health checks reflect v{srv['version']}" if srv else "nothing is serving")}
    mismatch = bool(req and (not srv or req["version"] != srv["version"]))
    warn = None
    if mismatch:
        rv = f"v{req['version']}"
        if req["status"] == "failed":
            what = "failed to build" if not req.get("image") else "failed to start or pass health checks"
            warn = (f"{rv} {what}; v{srv['version']} is still serving. Health checks reflect v{srv['version']}, not your deploy." if srv
                    else f"{rv} {what} and nothing is serving.")
        elif req["status"] == "deploying":
            warn = (f"{rv} is still deploying; v{srv['version']} is serving until it passes health checks." if srv else f"{rv} is still deploying; nothing is serving yet.")
        else:
            warn = f"{rv} is {req['status']}; " + (f"v{srv['version']} is serving." if srv else "nothing is serving.")
    trim = lambda r: r and {"version": r["version"], "status": r["status"], "error": r.get("error"), "reason": r.get("reason"), "created": r["created"]}
    return {"requested_release": trim(req), "serving_release": trim(srv), "health": health, "mismatch": mismatch, "warning": warn,
            "deploy_ok": bool(req and not mismatch and req["status"] == "active"), "deploy_failed": bool(req and req["status"] == "failed")}
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
    from_image = bool(src.get("image")) or bool(rel and (rel["reason"] or "").startswith(("rollback", "auto-rollback")))
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
    finally:
        q("update instances set status='stopped', stopped=? where id=?", (time.time(), inst["id"]))
        try: from . import lb; lb.forget(inst["id"])
        except Exception: pass
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
    if not account_active(account_id): raise PermissionError("this account is suspended or being deleted")
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
        # every instance not already stopped: 'starting'/'unready' rows (deploy interrupted mid-readiness, e.g. by a
        # restart) used to be skipped here, leaving their containers running after the app was destroyed
        for o in q("select * from instances where app=? and status!='stopped'", (name,)): _stop(o)
        _sweep_containers(name)
    q("update apps set status='destroyed' where name=?", (name,)); event(name, "destroyed")
    try: remove_app_networks(name)
    except Exception as e: event(name, f"network cleanup: {e}")
    try: from . import selfheal; selfheal.forget(name)
    except Exception: pass
def _sweep_containers(name):
    """Remove any container still labelled for this app (one the DB never recorded: created by the driver but the
    process died before the instances row was written). Exact label match, so other apps are never touched."""
    for d in get_drivers().values():
        f = getattr(d, "remove_app_containers", None)
        if f:
            try:
                n = f(name)
                if n: event(name, f"removed {n} leftover container(s)")
            except Exception as e: event(name, f"container sweep: {e}")
def sweep_account_containers(account_id):
    """Remove every container labelled paas.account=<id> or attached to the account's private network (older containers)."""
    n = 0
    for d in get_drivers().values():
        f = getattr(d, "remove_account_containers", None)
        if f: n += f(account_id)
    return n
def reap_destroyed():
    """Periodic safety net: remove containers whose app is destroyed. Only status='destroyed' apps; never unknown ones."""
    for d in get_drivers().values():
        f = getattr(d, "app_labels", None)
        if not f: continue
        for app in set(f()):
            r = q("select status from apps where name=?", (app,), one=True)
            if r and r["status"] == "destroyed":
                for o in q("select * from instances where app=? and status!='stopped'", (app,)): _stop(o)
                r2 = q("select status from apps where name=?", (app,), one=True)   # re-check: a redeploy may have revived the name meanwhile
                if r2 and r2["status"] == "destroyed": _sweep_containers(app)
    try: prune_networks()
    except Exception as e: event("system", f"network prune: {e}")
NET_PRUNE_AGE = 600
def _net_owner_live(n):
    """True if an aloof-* network still belongs to something live: aloof-acct-<id> -> any non-destroyed app of that account;
    aloof-pg-<db> -> that database exists and its app isn't destroyed; anything else -> a non-destroyed app listing it in spec.networks."""
    if n.startswith("aloof-acct-"):
        aid = n[len("aloof-acct-"):]
        return bool(aid.isdigit() and q("select 1 from apps where account_id=? and status!='destroyed' limit 1", (int(aid),), one=True))
    if n.startswith("aloof-pg-"):
        db = q("select app from pg_dbs where name=?", (n[len("aloof-pg-"):],), one=True)
        if db and q("select 1 from apps where name=? and status!='destroyed'", (db["app"],), one=True): return True
    for r in q("select spec from apps where status!='destroyed' and spec like ?", (f"%{n}%",)):
        try:
            if n in (load_spec(r["spec"]).get("networks") or []): return True
        except Exception: return True   # unreadable spec: be safe
    return False
def remove_app_networks(name, spec=None):
    """After destroy: drop this app's networks that are now empty and no longer used by any live app/db (the shared
    aloof-acct-<id> network stays while the account has other live apps)."""
    if spec is None:
        r = q("select spec from apps where name=?", (name,), one=True)
        spec = load_spec(r["spec"]) if r else {}
    gone = []
    for d in get_drivers().values():
        f = getattr(d, "remove_network_if_empty", None)
        if not f: continue
        for n in spec.get("networks") or []:
            if n.startswith("aloof-") and not _net_owner_live(n) and f(n): gone.append(n)
    if gone: event(name, f"removed network(s) {gone}")
    return gone
def prune_networks(now=None):
    """Reaper: remove aloof-* networks we created that have no containers, aren't owned by a live app/db, and are older than
    NET_PRUNE_AGE. Never touches a network with containers or a live owner."""
    now = now or time.time(); gone = []
    for d in get_drivers().values():
        ls, rm = getattr(d, "aloof_networks", None), getattr(d, "remove_network_if_empty", None)
        if not (ls and rm): continue
        for n, ts in ls():
            if ts is None or now - ts < NET_PRUNE_AGE or _net_owner_live(n): continue
            if rm(n): gone.append(n)
    if gone: event("system", f"pruned {len(gone)} empty network(s): {gone[:20]}")
    return gone
def destroy_app(name, with_volumes=False):
    """User-facing delete: destroy the app, then remove what its template created (volumes, Postgres). Volumes the user
    attached separately are detached and kept unless with_volumes. Returns what was deleted and kept."""
    tpl_vols = [r["name"] for r in q("select name from volumes where template_app=?", (name,))]
    other_vols = [r["name"] for r in q("select name from volumes where app=? and (template_app is null or template_app!=?)", (name, name))]
    pgs = [r["name"] for r in q("select name from pg_dbs where template_app=?", (name,))]
    destroy(name)
    out = {"ok": True, "app": name, "deleted": {"volumes": [], "postgres": []}, "kept": {"volumes": []}, "errors": []}
    from . import parity
    for p in pgs:
        try: parity.pg_destroy(p); out["deleted"]["postgres"].append(p); out["deleted"]["volumes"].append(f"pgdata-{p}")
        except Exception as e: out["errors"].append(f"postgres {p}: {e}")
    for v in tpl_vols + (other_vols if with_volumes else []):
        try: q("update volumes set app=null, mount_path=null where name=?", (v,)); delete_volume(v); out["deleted"]["volumes"].append(v)
        except Exception as e: out["errors"].append(f"volume {v}: {e}")
    if not with_volumes: out["kept"]["volumes"] = other_vols
    if not out["errors"]: out.pop("errors")
    event(name, f"deleted with app: volumes={out['deleted']['volumes']} postgres={out['deleted']['postgres']} kept={out['kept']['volumes']}")
    return out
def template_orphans():
    """Template-created volumes/Postgres whose app is gone (left by destroys before v14), plus legacy rows inferred from
    template.deploy audit records. Read-only."""
    gone = lambda app: not q("select 1 from apps where name=? and status!='destroyed'", (app,), one=True)
    vols = [dict(r) for r in q("select name, account_id, size_gb, app, template_app from volumes where template_app is not null") if gone(r["template_app"])]
    pgs = [dict(r) for r in q("select name, account_id, plan, template_app from pg_dbs where template_app is not null") if gone(r["template_app"])]
    return {"volumes": vols, "postgres": pgs}
def backfill_template_owned():
    """One-off (idempotent): mark resources created by template deploys before v14, using template.deploy audit rows and the
    naming the template deployer uses (<app>-<vol>, <app>-db)."""
    import json as _j
    from . import templating
    n = 0
    for r in q("select app, account_id, detail from audit where action='template.deploy'"):
        app = r["app"]
        try: t = templating.get(_j.loads(r["detail"] or "{}").get("template"))
        except Exception: t = None
        if not t: continue
        for v in t.get("volumes", []):   # exact names the deployer used
            vn = f"{app}-{v['name']}"[:32].rstrip("-")
            if q("update volumes set template_app=? where name=? and account_id=? and template_app is null returning name", (app, vn, r["account_id"])): n += 1
        if "postgres" in t and q("update pg_dbs set template_app=? where name=? and account_id=? and template_app is null returning name", (app, f"{app}-db", r["account_id"])): n += 1
    return n
def get_app(name):
    a = q("select * from apps where name=?", (name,), one=True)
    if not a: return None
    spec = load_spec(a.pop("spec")); a["spec"] = {k:v for k,v in spec.items() if k!="env"}
    a["env_keys"] = sorted(spec.get("env",{})); a["domains"] = [d["domain"] for d in q("select domain from domains where app=?", (name,))]; a["plan"] = PLANS[spec["plan"]] | {"id": spec["plan"]}
    a["instances"] = q("select id,provider,node_id,host,port,status,started from instances where app=? and status='running'", (name,))
    return a
def logs(name, tail=200):
    out = ["== platform events (times UTC) =="] + [time.strftime("%H:%M:%SZ ", time.gmtime(e["ts"]))+e["msg"] for e in q("select * from events where app=? order by id desc limit 50", (name,))][::-1]
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
# Reserved, non-deliverable domains (RFC 2606/6761): always test accounts, and no mail is ever sent there (it can only bounce).
UNDELIVERABLE_PATTERNS = ("%@example.com", "%@example.org", "%@example.net", "%.test", "%.invalid", "%@localhost")
# Our own domains: test (analytics/metrics only) once the address is verified (magic link, or OAuth with a verified email).
INTERNAL_DOMAINS = ("aloof.si", "enigmaticitsolutions.com")
TEST_EMAIL_PATTERNS = UNDELIVERABLE_PATTERNS + tuple(f"%@{d}" for d in INTERNAL_DOMAINS)   # SQL LIKE; also used by www.mark_flags
# Staff mailboxes on internal domains: can't be claimed by password/API signup (no proof of ownership).
RESERVED_LOCAL_PARTS = frozenset("support admin billing legal privacy security sales abuse postmaster noreply hello sachin team info ops".split())
def _e(email): return (email or "").strip().lower()
def undeliverable_email(email): return any(_e(email).endswith(p[1:]) for p in UNDELIVERABLE_PATTERNS)
def internal_email(email): return _e(email).rpartition("@")[2] in INTERNAL_DOMAINS
def reserved_staff_email(email):
    """Ignores dots and +tags in the local-part (sup.port+x@ = support@) and a trailing dot on the domain."""
    local, _, dom = _e(email).rpartition("@")
    return dom.rstrip(".") in INTERNAL_DOMAINS and local.split("+")[0].replace(".", "") in RESERVED_LOCAL_PARTS
def is_test_email(email, verified=False):
    """is_test drives analytics/metrics only (never transactional mail). Internal domains count only when verified."""
    return undeliverable_email(email) or (bool(verified) and internal_email(email))
def recompute_test(account_id):
    """Re-derive is_test for internal-domain/undeliverable accounts after verification changes; syncs PostHog when it flips."""
    a = q("select id, email, email_verified, is_test from accounts where id=?", (account_id,), one=True)
    if not a or not (internal_email(a["email"]) or undeliverable_email(a["email"])): return False
    from .db import kv_get
    if kv_get("is_test_pin", str(account_id)) is not None: return False   # set by the owner in /admin
    v = int(is_test_email(a["email"], a.get("email_verified")))
    if v == int(a.get("is_test") or 0): return False
    q("update accounts set is_test=? where id=?", (v, account_id))
    try:
        from . import analytics; analytics.identify(account_id)
    except Exception: pass
    return True
def create_account(email, password_hash=None, verified=False):
    """API keys are stored only as SHA-256 hashes; the plaintext key is returned once. Reserved test domains get is_test=1 at creation."""
    key = new_api_key()
    q("insert into accounts(email,api_key_hash,api_key_prefix,password_hash,email_verified,created,is_test) values(?,?,?,?,?,?,?)",
      (email.lower(), sha256(key), key[:10], password_hash, int(verified), time.time(), int(is_test_email(email, verified))))
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
        if a: a["_token"] = t["id"]; a["_scope_app"] = t["app"]; a["_can_destroy"] = bool(t.get("can_destroy"))
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
