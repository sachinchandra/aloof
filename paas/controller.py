"""Background loops running inside the edge process: autoscaler (concurrency/CPU, scale-to-zero) and health checks."""
import os, json, math, time, threading, urllib.request, urllib.error, logging
from .db import q, event
from . import engine, lb
from .drivers import get_drivers
log = logging.getLogger("paas.controller")
class Metrics:
    def __init__(self): self.inflight = {}; self.peak = {}; self.last = {}; self.reqs = {}; self.lock = threading.Lock()
    def begin(self, app):
        with self.lock:
            n = self.inflight[app] = self.inflight.get(app, 0) + 1
            self.peak[app] = max(self.peak.get(app, 0), n); self.last[app] = time.time(); self.reqs[app] = self.reqs.get(app, 0) + 1
    def end(self, app):
        with self.lock: self.inflight[app] = max(0, self.inflight.get(app, 1) - 1); self.last[app] = time.time()
    def take_peak(self, app):
        with self.lock: p = self.peak.get(app, 0); self.peak[app] = self.inflight.get(app, 0); return p
M = Metrics()
_low_since = {}; _cold = {}
def cold_start(app):
    """Called by the proxy when an autoscaled app has 0 instances. Blocks until one instance is ready."""
    with _cold.setdefault(app, threading.Lock()):
        if q("select 1 from instances where app=? and status='running'", (app,), one=True): return True
        t = time.time(); engine.scale(app, 1, reason="cold start"); event(app, f"cold start took {time.time()-t:.2f}s")
        return bool(q("select 1 from instances where app=? and status='running'", (app,), one=True))
def autoscale_tick():
    for a in q("select name, spec, status, updated from apps where status in ('running','idle')"):
        spec = json.loads(a["spec"]); cfg = spec.get("autoscale")
        if not cfg: continue
        name = a["name"]; insts = q("select * from instances where app=? and status='running'", (name,)); cur = len(insts)
        peak = M.take_peak(name); cpu = None; want = math.ceil(peak / cfg["target_concurrency"]) if peak else 0
        if cfg.get("cpu_percent") and insts:
            drv = get_drivers()[insts[0]["provider"]]
            cpu = sum(drv.cpu_percent(i["id"]) for i in insts) / cur
            if cpu > cfg["cpu_percent"]: want = max(want, cur + 1)
            elif cpu > cfg["cpu_percent"] * 0.6: want = max(want, cur)
        M.last.setdefault(name, time.time())
        # idle is measured from the later of the last request and the last deploy/scale change, so a redeployed app
        # doesn't scale to zero immediately because its pre-deploy traffic is old
        busy = M.inflight.get(name, 0) > 0; idle = 0 if busy else time.time() - max(M.last[name], a.get("updated") or 0)
        if cfg["min"] > 0: floor = cfg["min"]
        elif busy or idle < cfg["idle_seconds"]: floor = 1 if cur else 0
        else: floor = 0
        want = max(floor, min(want, cfg["max"]))
        if want > cur: _low_since.pop(name, None); engine.scale(name, want, reason=f"autoscale up: peak={peak}" + (f" cpu={cpu:.0f}%" if cpu is not None else ""))
        elif want < cur:
            s = _low_since.setdefault(name, time.time())
            if want == 0 or time.time() - s >= 15:      # 15s scale-down cooldown
                _low_since.pop(name, None)
                engine.scale(name, want, reason=("scale-to-zero: idle %ds" % idle) if want == 0 else f"autoscale down: peak={peak}" + (f" cpu={cpu:.0f}%" if cpu is not None else ""))
        else: _low_since.pop(name, None)
_fails = {}; _hlock = threading.Lock()
def health_tick():
    if not _hlock.acquire(blocking=False): return   # one health pass at a time (loop + edge retry)
    try: _health_pass()
    finally: _hlock.release()
    for a in q("select name, spec from apps where status='running'"):
        if not json.loads(a["spec"]).get("autoscale"): engine.reconcile(a["name"])
def _health_pass():
    for i in q("select i.*, a.spec from instances i join apps a on a.name=i.app where i.status='running' and a.status='running'"):
        spec = json.loads(i["spec"]); h = spec.get("health") or {}; drv = get_drivers()[i["provider"]]
        st = drv.inspect(i["id"]); ok = st == "running"; why = f"container {st}"
        ep = ok and hasattr(drv, "endpoint") and drv.endpoint(i["id"])
        if ep and ep[1] != i["port"]:   # host port remapped after a daemon restart: reconcile routing
            q("update instances set host=?, port=? where id=?", (ep[0], ep[1], i["id"])); i["port"] = ep[1]; event(i["app"], f"instance {i['id']} endpoint reconciled -> {ep[1]}")
        if ok and h.get("path"):
            try: urllib.request.urlopen(engine.health_url(i['host'], i['port'], h['path']), timeout=3)
            except urllib.error.HTTPError as e: ok = e.code < 500; why = f"HTTP {e.code}"
            except Exception as e: ok = False; why = f"{type(e).__name__}"
        if ok: _fails.pop(i["id"], None); lb.health_ok(i["id"]); continue
        try: lb.health_failed(i["app"], i["id"], q("select id from instances where app=? and status='running'", (i["app"],)), why)   # out of rotation now
        except Exception: log.exception("lb eject")
        n = _fails[i["id"]] = _fails.get(i["id"], 0) + 1
        if n >= int(h.get("failures", 3)) or st in ("exited", "dead", "gone"):
            _fails.pop(i["id"], None); engine.replace_instance(i["app"], i, why)
def _loop(fn, every):
    while True:
        try: fn()
        except Exception: log.exception(fn.__name__)
        time.sleep(every)
def start():
    from .drivers import remote, wg
    from . import billing
    try: log.info("wg peers re-applied: %s", wg.sync_peers(q("select * from nodes where status in ('ready','lost')")))
    except Exception: log.exception("wg sync")
    threading.Thread(target=_loop, args=(autoscale_tick, 3), daemon=True).start()
    threading.Thread(target=_loop, args=(health_tick, 5), daemon=True).start()
    from . import selfheal
    threading.Thread(target=_loop, args=(selfheal.tick, 5), daemon=True).start()
    threading.Thread(target=_loop, args=(remote.node_tick, 30), daemon=True).start()
    threading.Thread(target=_loop, args=(engine.reap_destroyed, 120), daemon=True).start()
    from . import waitlist as _wl
    threading.Thread(target=_loop, args=(_wl.retry_unsent, 300), daemon=True).start()
    from . import domains as _doms
    threading.Thread(target=_loop, args=(_doms.tick, int(os.environ.get("PAAS_DOMAIN_TICK_S", "60"))), daemon=True).start()
    threading.Thread(target=_loop, args=(billing.meter_tick, int(os.environ.get("PAAS_METER_INTERVAL", "60"))), daemon=True).start()
