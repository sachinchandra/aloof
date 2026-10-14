"""Self-healing deploys: the edge records per-release 5xx rate and latency; for a window after each deploy the new release is
compared with the previous release's baseline and rolled back automatically (engine.rollback) when it is clearly worse.

Guards: min requests on both sides, a min absolute count of bad requests from several distinct client IPs, 5xx on routes that
already failed under the baseline don't count, strict comparisons, no baseline -> no watch, never watches a rollback, one
auto-rollback per deploy, pinned to the watched release (any newer deploy or a deleted app cancels it), per-app opt-out.
State is in memory in the edge process (a restart drops an in-progress watch); decisions are persisted on the release
(kv 'autorb'), as an app event and in the audit log."""
import os, time, json, threading, collections
from .db import q, event, kv_get, kv_put, kv_del, audit

def _f(k, d): return float(os.environ.get(k, d))
def cfg():
    return {"window_s": _f("PAAS_SELFHEAL_WINDOW", 300), "min_requests": int(_f("PAAS_SELFHEAL_MIN_REQS", 100)),
            "min_bad": int(_f("PAAS_SELFHEAL_MIN_BAD", 10)), "min_ips": int(_f("PAAS_SELFHEAL_MIN_IPS", 3)),
            "err_pp": _f("PAAS_SELFHEAL_ERR_PP", 5), "err_x": _f("PAAS_SELFHEAL_ERR_X", 2),
            "p95_x": _f("PAAS_SELFHEAL_P95_X", 2), "p95_ms": _f("PAAS_SELFHEAL_P95_MS", 500)}
SAMPLES = 2000; MAX_ROUTES = 200; MAX_IPS = 200

def route_class(path):
    seg = (path or "").lstrip("/").split("?", 1)[0].split("/", 1)[0]
    return "/" + seg[:40]

class Stats:
    def __init__(self): self.n = 0; self.e5 = 0; self.lat = collections.deque(maxlen=SAMPLES); self.err_routes = {}; self.err_ips = {}
    def add(self, status, ms, ip=None, route="/"):
        self.n += 1; self.lat.append((ms, ip))
        if status >= 500:
            self.e5 += 1
            if route in self.err_routes or len(self.err_routes) < MAX_ROUTES: self.err_routes[route] = self.err_routes.get(route, 0) + 1
            d = self.err_ips.setdefault(route, set())
            if ip and len(d) < MAX_IPS: d.add(ip)
    def rate(self): return self.e5 / self.n if self.n else 0.0
    def p95(self):
        if not self.lat: return 0.0
        s = sorted(m for m, _ in self.lat); return s[min(len(s) - 1, int(0.95 * len(s)))]
    def summary(self, exclude_routes=()):
        e = sum(v for r, v in self.err_routes.items() if r not in exclude_routes)
        ips = set().union(*[v for r, v in self.err_ips.items() if r not in exclude_routes] or [set()])
        return {"requests": self.n, "errors_5xx": self.e5, "counted_5xx": e, "error_rate": round(e / self.n, 4) if self.n else 0.0,
                "error_ips": len(ips), "p95_ms": round(self.p95(), 1)}
    def slow(self, threshold):
        xs = [ip for m, ip in self.lat if m > threshold]; return len(xs), len({i for i in xs if i})

_lock = threading.Lock()
_active = {}; _stats = {}; _watch = {}

def enabled(app): return kv_get("selfheal_off", app) is None
def set_enabled(app, on):
    if on: kv_del("selfheal_off", app)
    else: kv_put("selfheal_off", app, "1")

def forget(app):
    """App deleted: drop all state (no watch, no stats)."""
    with _lock:
        _active.pop(app, None); _watch.pop(app, None)
        for k in [k for k in _stats if k[0] == app]: _stats.pop(k)

def _active_version(app):
    v = _active.get(app)
    if v is None:
        r = q("select version from releases where app=? and status='active' order by version desc", (app,), one=True)
        v = _active[app] = r["version"] if r else 0
    return v

def record(app, status, ms, ip=None, path="/"):
    """Called by the edge for every proxied request (status = upstream status; 502 only for connection failures)."""
    v = _active_version(app)
    with _lock:
        s = _stats.get((app, v))
        if s is None: s = _stats[(app, v)] = Stats()
        s.add(int(status), float(ms), ip, route_class(path))

def is_rollback(reason): return (reason or "").startswith(("rollback", "auto-rollback"))

def on_active(app, version, prev_version, reason, release_id=None):
    with _lock:
        _active[app] = version; _watch.pop(app, None)
        base = _stats.get((app, prev_version)) if prev_version else None
        for k in [k for k in _stats if k[0] == app and k[1] not in (version, prev_version)]: _stats.pop(k)
        _stats[(app, version)] = Stats()
    if is_rollback(reason) or reason in ("scale", "cold start") or not prev_version: return None
    if not enabled(app): event(app, f"self-heal: v{version} not watched (disabled for this app)"); return None
    c = cfg()
    if not base or base.n < c["min_requests"]:
        event(app, f"self-heal: v{version} not watched (no baseline: v{prev_version} served {base.n if base else 0} requests, need {c['min_requests']})"); return None
    w = {"version": version, "release_id": release_id, "prev": prev_version, "start": time.time(), "baseline": base.summary(),
         "baseline_err_routes": set(base.err_routes)}
    with _lock: _watch[app] = w
    event(app, f"self-heal: watching v{version} for {int(c['window_s'])}s against v{prev_version} baseline "
               f"(5xx {base.rate():.1%}, p95 {base.p95():.0f}ms, {base.n} requests)")
    return w

def verdict(new, base, c=None, slow=None):
    """Reason string if `new` is clearly worse than `base` (summary dicts), else None. `slow(threshold)` -> (count, distinct ips)."""
    c = c or cfg()
    if new["requests"] < c["min_requests"] or base["requests"] < c["min_requests"]: return None
    er, br = new["error_rate"], base["error_rate"]
    if (new.get("counted_5xx", new["errors_5xx"]) >= c["min_bad"] and new.get("error_ips", c["min_ips"]) >= c["min_ips"]
            and er - br > c["err_pp"] / 100 and er > c["err_x"] * br):
        return f"5xx rate {er:.1%} vs {br:.1%} baseline ({new['requests']} requests)"
    np_, bp = new["p95_ms"], base["p95_ms"]
    if np_ > c["p95_x"] * bp and np_ - bp > c["p95_ms"]:
        thr = max(c["p95_x"] * bp, bp + c["p95_ms"])
        n_slow, ips = slow(thr) if slow else (c["min_bad"], c["min_ips"])
        if n_slow >= c["min_bad"] and ips >= c["min_ips"]:
            return f"p95 latency {np_:.0f}ms vs {bp:.0f}ms baseline ({new['requests']} requests)"
    return None

def _still_current(app, w):
    a = q("select status from apps where name=?", (app,), one=True)
    if not a or a["status"] not in ("running", "idle"): return f"app is {a['status'] if a else 'gone'}"
    top = q("select max(version) v from releases where app=?", (app,), one=True)["v"]
    act = q("select id, version from releases where app=? and status='active' order by version desc", (app,), one=True)
    if top != w["version"] or not act or act["version"] != w["version"] or (w.get("release_id") and act["id"] != w["release_id"]):
        return "a newer release exists"
    return None

def tick():
    from . import engine
    c = cfg(); now = time.time()
    for app, w in list(_watch.items()):
        stale = _still_current(app, w)
        if stale:
            with _lock:
                if _watch.get(app) is w: _watch.pop(app, None)
            if not stale.startswith("app is destroyed") and stale != "app is gone": event(app, f"self-heal: stopped watching v{w['version']} ({stale})")
            continue
        with _lock:
            if _watch.get(app) is not w: continue
            s = _stats.get((app, w["version"])) or Stats()
            new = s.summary(w["baseline_err_routes"]); slow = s.slow
        why = verdict(new, w["baseline"], c, slow)
        if why:
            with _lock: _watch.pop(app, None)   # one shot per deploy, before acting
            if not enabled(app): continue
            info = {"reason": why, "rolled_back_to": w["prev"], "at": now, "new": new, "baseline": w["baseline"]}
            try: engine.rollback(app, w["prev"], reason=f"auto-rollback to v{w['prev']}: v{w['version']} {why}", only_if_latest=w["version"])
            except Exception as e: event(app, f"self-heal: rollback not started: {e}"); continue
            kv_put("autorb", f"{app}:{w['version']}", json.dumps(info))
            event(app, f"self-heal: rolling back v{w['version']} to v{w['prev']}: {why}")
            acct = q("select account_id from apps where name=?", (app,), one=True)
            try: audit(acct["account_id"] if acct else None, app, "auto-rollback", f"v{w['version']} -> v{w['prev']}: {why}", "self-heal")
            except Exception: pass
        elif now - w["start"] >= c["window_s"]:
            with _lock: _watch.pop(app, None)
            event(app, f"self-heal: v{w['version']} passed its {int(c['window_s'])}s watch (5xx {new['error_rate']:.1%}, p95 {new['p95_ms']:.0f}ms, {new['requests']} requests)")

def auto_rollback_info(app, version):
    v = kv_get("autorb", f"{app}:{version}"); return json.loads(v) if v else None
def status(app):
    w = _watch.get(app)
    return {"enabled": enabled(app), "watching": ({"version": w["version"], "against": w["prev"], "seconds_left": max(0, int(cfg()["window_s"] - (time.time() - w["start"])))} if w else None),
            "thresholds": cfg()}
