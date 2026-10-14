"""Smart load balancing at the edge: least-connections across ready instances, passive outlier ejection, and immediate
ejection on a failed health check. Never ejects an app's last routable instance. In-memory, per edge process."""
import os, time, threading, itertools
from .db import event

def cfg():
    e = os.environ.get
    return {"health_fails": int(e("PAAS_LB_HEALTH_FAILS", 2)), "fails": int(e("PAAS_LB_FAILS", 5)), "cooldown_s": float(e("PAAS_LB_COOLDOWN", 10)), "cooldown_max_s": float(e("PAAS_LB_COOLDOWN_MAX", 300))}
_lock = threading.Lock()
inflight = {}   # instance id -> open requests
_fails = {}     # instance id -> consecutive connection errors / 5xx
_ejected = {}   # instance id -> (until, reason)
_strikes = {}   # instance id -> times ejected without a success since (backoff exponent)
_rr = {}        # app -> counter for tie-breaking
_hfails = {}    # instance id -> consecutive failed health checks
EJECT_STATUSES = (502, 503, 504)   # plus connection errors and timeouts; a plain 500 is app behaviour and never ejects

def _routable(insts, now):
    return [i for i in insts if _ejected.get(i["id"], (0,))[0] <= now]

def pick(app, insts, exclude=()):
    """Least connections among non-ejected instances; ties go round-robin. If every instance is ejected, use them all."""
    now = time.time()
    with _lock:
        pool = [i for i in _routable(insts, now) if i["id"] not in exclude] or [i for i in insts if i["id"] not in exclude] or list(insts)
        low = min(inflight.get(i["id"], 0) for i in pool)
        ties = [i for i in pool if inflight.get(i["id"], 0) == low]
        c = _rr.setdefault(app, itertools.count())
        return ties[next(c) % len(ties)]

def begin(iid):
    with _lock: inflight[iid] = inflight.get(iid, 0) + 1
def end(iid):
    with _lock: inflight[iid] = max(0, inflight.get(iid, 1) - 1)

def success(iid):
    with _lock: _fails.pop(iid, None); _strikes.pop(iid, None)

def _eject(app, iid, insts, reason):
    """Caller holds _lock. Returns True if ejected; refuses when it would leave no routable instance."""
    now = time.time(); c = cfg()
    others = [i for i in _routable(insts, now) if i["id"] != iid]
    if not others or len(others) < (len(insts) + 1) // 2: return False   # never the last; at most half the instances out
    n = _strikes[iid] = _strikes.get(iid, 0) + 1
    cd = min(c["cooldown_s"] * 2 ** (n - 1), c["cooldown_max_s"])
    _ejected[iid] = (now + cd, reason); _fails.pop(iid, None)
    return cd

def failure(app, iid, insts, reason):
    """Connection error or 5xx from `iid`. Ejects after N consecutive failures."""
    with _lock:
        n = _fails[iid] = _fails.get(iid, 0) + 1
        cd = _eject(app, iid, insts, reason) if n >= cfg()["fails"] else False
    if cd: event(app, f"lb: ejected {iid} for {cd:.0f}s after {n} consecutive failures ({reason})")
    return cd

def health_failed(app, iid, insts, reason):
    """PAAS_LB_HEALTH_FAILS (2) consecutive failed health checks eject (still never the last instance)."""
    with _lock:
        h = _hfails[iid] = _hfails.get(iid, 0) + 1
        if h < cfg()["health_fails"] or _ejected.get(iid, (0,))[0] > time.time(): return False
        cd = _eject(app, iid, insts, f"health check: {reason}")
    if cd: event(app, f"lb: ejected {iid} for {cd:.0f}s (health check failed: {reason})")
    return cd

def health_ok(iid):
    """Passing health check: lift a health ejection early, keep the backoff count until real traffic succeeds."""
    with _lock:
        _hfails.pop(iid, None); e = _ejected.get(iid)
        if e and e[1].startswith("health check"): _ejected.pop(iid, None)

def forget(iid):
    with _lock:
        for d in (inflight, _fails, _ejected, _strikes, _hfails): d.pop(iid, None)

def state(insts):
    now = time.time()
    with _lock: return [{"id": i["id"], "inflight": inflight.get(i["id"], 0), "consecutive_failures": _fails.get(i["id"], 0),
             "ejected_for_s": max(0, round(_ejected[i["id"]][0] - now, 1)) if _ejected.get(i["id"], (0,))[0] > now else 0,
             "ejected_reason": _ejected[i["id"]][1] if _ejected.get(i["id"], (0,))[0] > now else None} for i in insts]
