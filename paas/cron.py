"""Scheduled machines: spec.cron = [{name, schedule ("*/5 * * * *" or @hourly/@daily/@weekly/@monthly), command}] (aloof.toml [[cron]]).
Each due job runs as a one-off machine from the app's current image with its env/networks/volumes (UTC schedule); output is kept in cron_runs."""
import time, threading, logging, datetime
from .db import q, event, kv_get, kv_put
from .crypto import load_spec
log = logging.getLogger("paas.cron")
ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *"}
RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
def _field(f, lo, hi):
    out = set()
    for part in f.split(","):
        step = 1
        if "/" in part: part, st = part.split("/", 1); step = int(st)
        if step < 1: raise ValueError("bad cron step")
        if part == "*": a, b = lo, hi
        elif "-" in part: a, b = map(int, part.split("-", 1))
        else: a = b = int(part); b = hi if step > 1 else a
        if a < lo or b > hi or a > b: raise ValueError(f"cron field {f} out of range {lo}-{hi}")
        out |= set(range(a, b + 1, step))
    return out
def parse(expr):
    expr = ALIASES.get(expr.strip(), expr.strip()); parts = expr.split()
    if len(parts) != 5: raise ValueError(f"cron schedule needs 5 fields (min hour dom mon dow) or @hourly/@daily/...: {expr!r}")
    try: f = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, RANGES)]
    except ValueError as e: raise ValueError(f"bad cron schedule {expr!r}: {e}")
    f[4] = {d % 7 for d in f[4]}; return f, parts
def due(expr, t):
    (mi, hr, dom, mon, dow), parts = parse(expr); d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    wd = (d.weekday() + 1) % 7
    day_ok = (d.day in dom or wd in dow) if (parts[2] != "*" and parts[4] != "*") else (d.day in dom and wd in dow)
    return d.minute in mi and d.hour in hr and d.month in mon and day_ok
def run_job(app, job, spec, drv):
    t0 = time.time()
    try: code, out = drv.run_once(app, spec["_image"], spec, job["command"], timeout=int(job.get("timeout", 3600)))
    except Exception as e: code, out = -1, f"{type(e).__name__}: {e}"
    q("insert into cron_runs(app,job,ts,exit_code,output) values(?,?,?,?,?)", (app, job["name"], t0, code, out[-20000:]))
    event(app, f"cron {job['name']}: exit {code} in {time.time()-t0:.0f}s")
    for old in q("select id from cron_runs where app=? and job=? order by id desc", (app, job["name"]))[50:]: q("delete from cron_runs where id=?", (old["id"],))
    return code
def tick(now=None):
    from .drivers import get_drivers
    now = now or time.time(); minute = int(now // 60)
    for a in q("select name, spec, provider, status from apps where status in ('running','idle','stopped')"):
        spec = load_spec(a["spec"])
        for job in spec.get("cron") or []:
            k = f"{a['name']}:{job['name']}"
            try:
                if not due(job["schedule"], now) or kv_get("cron", k) == str(minute) or not spec.get("_image"): continue
            except ValueError: continue
            kv_put("cron", k, str(minute), ttl=7200); drv = get_drivers().get(a["provider"])
            if drv and hasattr(drv, "run_once"): threading.Thread(target=run_job, args=(a["name"], job, spec, drv), daemon=True).start()
def _loop():
    while True:
        try: tick()
        except Exception: log.exception("cron tick")
        time.sleep(20)
def start(): threading.Thread(target=_loop, daemon=True).start()
