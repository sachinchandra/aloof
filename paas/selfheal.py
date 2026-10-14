"""Self-healing deploys: the edge records per-release 5xx rate and latency; for a window after each deploy the new release is
compared with the previous release's baseline and rolled back automatically (engine.rollback) when it is clearly worse.

Guards: needs PAAS_SELFHEAL_MIN_REQS requests on both sides, skips when there is no baseline, never watches a rollback,
at most one auto-rollback per deploy, per-app opt-out (kv 'selfheal_off'). State is in memory in the edge process; the
decision (reason + numbers) is persisted on the release (kv 'autorb') and as an app event."""
import os, time, json, threading, collections
from .db import q, event, kv_get, kv_put, kv_del

def _f(k, d): return float(os.environ.get(k, d))
def cfg():
    return {"window_s": _f("PAAS_SELFHEAL_WINDOW", 300), "min_requests": int(_f("PAAS_SELFHEAL_MIN_REQS", 20)),
            "err_pp": _f("PAAS_SELFHEAL_ERR_PP", 5), "err_x": _f("PAAS_SELFHEAL_ERR_X", 2),
            "p95_x": _f("PAAS_SELFHEAL_P95_X", 2), "p95_ms": _f("PAAS_SELFHEAL_P95_MS", 500)}
SAMPLES = 2000   # latency samples kept per release (ring buffer)

class Stats:
    def __init__(self): self.n = 0; self.e5 = 0; self.lat = collections.deque(maxlen=SAMPLES)
    def add(self, status, ms): self.n += 1; self.e5 += status >= 500; self.lat.append(ms)
    def rate(self): return self.e5 / self.n if self.n else 0.0
    def p95(self):
        if not self.lat: return 0.0
        s = sorted(self.lat); return s[min(len(s) - 1, int(0.95 * len(s)))]
    def summary(self): return {"requests": self.n, "errors_5xx": self.e5, "error_rate": round(self.rate(), 4), "p95_ms": round(self.p95(), 1)}

_lock = threading.Lock()
_active = {}    # app -> active release version (cached; set by engine on activation)
_stats = {}     # (app, version) -> Stats
_watch = {}     # app -> {version, prev, start, baseline}

def enabled(app): return kv_get("selfheal_off", app) is None
def set_enabled(app, on):
    if on: kv_del("selfheal_off", app)
    else: kv_put("selfheal_off", app, "1")

def _active_version(app):
    v = _active.get(app)
    if v is None:
        r = q("select version from releases where app=? and status='active' order by version desc", (app,), one=True)
        v = _active[app] = r["version"] if r else 0
    return v

def record(app, status, ms):
    """Called by the edge for every proxied request (status = upstream status, 502 for upstream errors)."""
    v = _active_version(app)
    with _lock:
        s = _stats.get((app, v))
        if s is None: s = _stats[(app, v)] = Stats()
        s.add(int(status), float(ms))

def is_rollback(reason): return (reason or "").startswith(("rollback", "auto-rollback"))

def on_active(app, version, prev_version, reason):
    """engine._run_deploy calls this when a release goes live. Starts the watch unless it's a rollback/scale or opted out."""
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
    with _lock: _watch[app] = {"version": version, "prev": prev_version, "start": time.time(), "baseline": base.summary()}
    event(app, f"self-heal: watching v{version} for {int(c['window_s'])}s against v{prev_version} baseline "
               f"(5xx {base.rate():.1%}, p95 {base.p95():.0f}ms, {base.n} requests)")
    return _watch[app]

def verdict(new, base, c=None):
    """Return a reason string if `new` (summary dict) is clearly worse than `base`, else None."""
    c = c or cfg()
    if new["requests"] < c["min_requests"] or base["requests"] < c["min_requests"]: return None
    er, br = new["error_rate"], base["error_rate"]
    if er - br >= c["err_pp"] / 100 and er >= c["err_x"] * br:
        return f"5xx rate {er:.1%} vs {br:.1%} baseline ({new['requests']} requests)"
    np_, bp = new["p95_ms"], base["p95_ms"]
    if np_ >= c["p95_x"] * bp and np_ - bp >= c["p95_ms"]:
        return f"p95 latency {np_:.0f}ms vs {bp:.0f}ms baseline ({new['requests']} requests)"
    return None

def tick():
    """Controller loop: evaluate every watched app; roll back at most once per deploy."""
    from . import engine
    c = cfg(); now = time.time()
    for app, w in list(_watch.items()):
        with _lock:
            if _watch.get(app) is not w: continue
            s = _stats.get((app, w["version"])); new = s.summary() if s else Stats().summary()
        why = verdict(new, w["baseline"], c)
        if why:
            with _lock: _watch.pop(app, None)   # one shot per deploy, before acting
            if not enabled(app): continue
            if _active_version(app) != w["version"]: continue   # superseded meanwhile
            reason = f"auto-rollback: v{w['version']} {why}"
            info = {"reason": why, "rolled_back_to": w["prev"], "at": now, "new": new, "baseline": w["baseline"]}
            kv_put("autorb", f"{app}:{w['version']}", json.dumps(info))
            event(app, f"self-heal: rolling back v{w['version']} to v{w['prev']}: {why}")
            try: engine.rollback(app, w["prev"], reason=f"auto-rollback to v{w['prev']}: v{w['version']} {why}")
            except Exception as e: event(app, f"self-heal: rollback failed: {e}")
        elif now - w["start"] >= c["window_s"]:
            with _lock: _watch.pop(app, None)
            event(app, f"self-heal: v{w['version']} passed its {int(c['window_s'])}s watch (5xx {new['error_rate']:.1%}, p95 {new['p95_ms']:.0f}ms, {new['requests']} requests)")

def auto_rollback_info(app, version):
    v = kv_get("autorb", f"{app}:{version}"); return json.loads(v) if v else None
def status(app):
    w = _watch.get(app)
    return {"enabled": enabled(app), "watching": ({"version": w["version"], "against": w["prev"], "seconds_left": max(0, int(cfg()["window_s"] - (time.time() - w["start"])))} if w else None),
            "thresholds": cfg()}
