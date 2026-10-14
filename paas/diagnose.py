"""diagnose_app: a read-only report of what's wrong with an app and the likely fix. Never mutates anything (no restarts,
no health probes, no scaling). Log lines and events are redacted: env values of the app, and anything key/token-shaped."""
import re, time, json, base64, urllib.parse
from .db import q
from .crypto import load_spec
from .drivers import get_drivers

ERR_RE = re.compile(r"(error|exception|traceback|fatal|panic|critical|killed|out of memory|oom|segfault|refused|cannot|failed|EADDRINUSE|unhandled)", re.I)
_R = [   # (pattern, replacement), applied in order
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*"), "[redacted private key]"),
    (re.compile(r"(?i)\b((?:proxy-)?authorization|cookie|set-cookie|x-api-key|x-auth-token|api-key)(\s*[:=]\s*)[^\r\n]*"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[^\s\"',;]+"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9+/=]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^@\s/]*:[^@\s/]+@"), r"\1[redacted]@"),
    (re.compile(r"(?i)(--?(?:password|passwd|pwd|token|secret|api-key)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"), r"\1[redacted]"),
    (re.compile(r"(?i)([\"']?\b(?:password|passwd|pwd|pass|secret|client[_-]?secret|token|access[_-]?token|refresh[_-]?token|api[_-]?key|access[_-]?key|private[_-]?key|session)[\"']?)"
                r"(\s*[:=]\s*|\s+is\s+)(\"[^\"]*\"|'[^']*'|[^\s&,;]+)"), r"\1\2[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b|\bASIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\b(?:sk|rk|pk|whsec|ghp|gho|ghu|ghs|ghr|github_pat|dt|re|rzp_live|rzp_test|phc|phx)_[A-Za-z0-9_]{8,}"), "[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[redacted]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), "[redacted]"),
    # generic high-entropy token: >=24 chars with upper, lower and digit (AWS secret keys, random API keys)
    (re.compile(r"(?<![A-Za-z0-9/+_\-])(?=[A-Za-z0-9/+_\-=]*[A-Z])(?=[A-Za-z0-9/+_\-=]*[a-z])(?=[A-Za-z0-9/+_\-=]*[0-9])[A-Za-z0-9/+_\-]{24,}={0,2}"), "[redacted]"),
]
LISTEN_RE = re.compile(r"(?i)(listening|running|started|serving|bound)\D{0,40}(?:port\s*|:)(\d{2,5})\b")

def _decodings(tok):
    out = []
    try: out.append(urllib.parse.unquote_plus(tok))
    except Exception: pass
    t = tok.strip("\"'.,;:()[]{}")
    if len(t) >= 8 and re.fullmatch(r"[A-Za-z0-9+/_\-]+=*", t):
        for f in (base64.b64decode, base64.urlsafe_b64decode):
            try: out.append(f(t + "=" * (-len(t) % 4)).decode("utf-8", "ignore"))
            except Exception: pass
    return out

def redactor(*envs):
    """Redact env values (verbatim, URL-encoded and base64 forms), then key/token/password-shaped strings."""
    vals = sorted({str(v) for env in envs for v in (env or {}).values() if v is not None and len(str(v)) >= 4}, key=len, reverse=True)
    def r(s):
        for v in vals: s = s.replace(v, "[redacted]")
        if vals:
            toks = s.split()
            for t in set(toks):
                if any(v in d for d in _decodings(t) for v in vals): s = s.replace(t, "[redacted]")
        for pat, rep in _R: s = pat.sub(rep, s)
        return s
    return r

_cache = {}   # app -> (ts, report): at most one full diagnosis (log fetches) per app every few seconds
def _exit_info(drv, iid):
    try: return drv.exit_info(iid) if hasattr(drv, "exit_info") else None
    except Exception: return None

def diagnose(name, log_lines=300, now=None, cache_s=5):
    c = _cache.get(name)
    if cache_s and c and time.time() - c[0] < cache_s: return c[1]
    rep = _diagnose(name, log_lines, now); _cache[name] = (time.time(), rep); return rep
def _diagnose(name, log_lines=300, now=None):
    from . import controller, billing
    now = now or time.time()
    app = q("select * from apps where name=?", (name,), one=True)
    spec = load_spec(app["spec"]); drivers = get_drivers()
    old_envs = [load_spec(r["spec"]).get("env") for r in q("select spec from releases where app=? order by version desc limit 10", (name,))]
    red = redactor(spec.get("env"), *old_envs)
    events = q("select ts, msg from events where app=? and ts>? order by id desc limit 200", (name, now - 86400))
    ev = [{"ts": e["ts"], "msg": red(e["msg"])} for e in events][::-1]
    pick = lambda *pats: [e for e in ev if any(p in e["msg"] for p in pats)]
    deploys = pick("release v", "deploy complete", "deploy failed", "failed readiness", "still serving", "deploy queued")
    rollbacks = pick("rollback", "self-heal")
    restarts = pick("health: replacing", "reconcile", "cold start", "scale-to-zero", "autoscale", "not ready after timeout", "container exited")
    health = pick("health: replacing", "not ready after timeout", "failed readiness")
    insts = []
    for i in q("select * from instances where app=? order by started desc limit 10", (name,)):
        d = drivers.get(i["provider"]); live = i["status"] == "running"
        st = None
        if live and d:
            try: st = d.inspect(i["id"])
            except Exception: st = "unknown"
        x = {"id": i["id"], "status": i["status"], "container": st, "started": i["started"], "stopped": i["stopped"],
             "consecutive_health_failures": controller._fails.get(i["id"], 0) if live else None}
        ei = _exit_info(d, i["id"]) if d else None
        if ei: x.update(ei)
        insts.append(x)
    errors = []
    for i in [x for x in insts if x["status"] == "running"][:5]:
        try: out = drivers[q("select provider from instances where id=?", (i["id"],), one=True)["provider"]].logs(i["id"], log_lines)
        except Exception as e: out = f"(logs unavailable: {type(e).__name__})"
        lines = [red(l)[:500] for l in out.splitlines()]
        errors += [{"instance": i["id"], "line": l} for l in lines if ERR_RE.search(l)][-30:]
        i["_lines"] = lines
    rel = q("select version, status, reason, created from releases where app=? order by version desc limit 5", (name,))
    causes = _rules(app, spec, ev, insts, errors, rel, now)
    for i in insts: i.pop("_lines", None)
    try: bal = billing.runway(app["account_id"]) if hasattr(billing, "runway") else None
    except Exception: bal = None
    return {"app": name, "status": app["status"], "error": red(app.get("error") or "") or None, "region": app.get("region"),
            "plan": spec.get("plan"), "port": spec.get("port"), "health_check": {k: v for k, v in (spec.get("health") or {}).items()},
            "autoscale": spec.get("autoscale"), "instances": insts,
            "recent_releases": [{k: (red(v) if isinstance(v, str) else v) for k, v in r.items()} for r in rel],
            "deploys": deploys[-15:], "rollbacks": rollbacks[-10:], "restarts": restarts[-15:], "failed_health_checks": health[-15:],
            "error_log_lines": errors[-40:], "likely_causes": causes,
            "balance": ({"level": bal.get("level"), "hours_left": bal.get("hours_left")} if isinstance(bal, dict) else None),
            "summary": (causes[0]["cause"] if causes else ("No problems detected." if app["status"] in ("running", "idle") else f"App is {app['status']}.")),
            "read_only": True, "generated": now}

def _c(cause, conf, evidence, fix): return {"cause": cause, "confidence": conf, "evidence": evidence, "fix": fix}
def _rules(app, spec, ev, insts, errors, rel, now):
    out = []; st = app["status"]; text = "\n".join(e["msg"] for e in ev[-80:]); errtext = "\n".join(e["line"] for e in errors)
    if st == "suspended" or "suspended: prepaid balance exhausted" in text:
        out.append(_c("Suspended: prepaid balance reached $0", "high", "app status suspended" if st == "suspended" else "suspension event",
                      "Top up credit (Dashboard → Billing, or POST /v1/billing/topup); suspended apps resume automatically."))
    if st == "capped":
        out.append(_c("Paused: Free tier monthly usage cap reached", "high", "app status capped", "Upgrade to Hobby (PUT /v1/billing/tier) or wait for the next month."))
    oom = [i for i in insts if i.get("oom_killed") or i.get("exit_code") == 137] or ("137" in text and "exit" in text) or re.search(r"(?i)out of memory|oomkilled|killed process", errtext)
    if oom:
        out.append(_c("Out of memory (exit 137 / OOM-killed)", "high", "instance exit code 137 or OOM in logs",
                      f"Use a bigger plan (aloof scale {app['name']} --plan <larger>; current {spec.get('plan')}) or reduce memory use (worker count, caches)."))
    replaced = [e for e in ev if "health: replacing" in e["msg"] and e["ts"] > now - 1800]
    if len(replaced) >= 3:
        out.append(_c("Crash loop: instances keep failing and being replaced", "high", f"{len(replaced)} replacements in the last 30 min",
                      "Check error_log_lines for the crash; fix and redeploy, or roll back to the last good release (aloof rollback)."))
    port = spec.get("port"); heard = {int(m.group(2)) for i in insts for l in i.get("_lines", []) for m in [LISTEN_RE.search(l)] if m}
    readiness = [e for e in ev if any(p in e["msg"] for p in ("not ready after timeout", "failed readiness", "container exited at start")) and e["ts"] > now - 3600]
    if heard and port not in heard:
        out.append(_c(f"Port mismatch: app listens on {sorted(heard)[0]} but Aloof routes to {port}", "high", "listen line in logs",
                      f"Set port = {sorted(heard)[0]} in aloof.toml (or listen on $PORT={port}) and redeploy."))
    elif readiness:
        hp = (spec.get("health") or {}).get("path")
        out.append(_c("Health/readiness check failing", "medium", readiness[-1]["msg"],
                      f"Make sure the app listens on 0.0.0.0:{port} (the PORT env var)" + (f" and that {hp} returns a non-5xx status" if hp else "") +
                      "; or change the health path with aloof health."))
    last_deploy = next((e for e in reversed(ev) if e["msg"].startswith("deploy complete")), None)
    if last_deploy and now - last_deploy["ts"] < 1800:
        prev = next((r for r in rel if r["status"] == "superseded"), None)
        after = [e for e in ev if e["ts"] >= last_deploy["ts"]]
        bad = [e for e in after if "health: replacing" in e["msg"] or "self-heal: rolling back" in e["msg"] or "lb: ejected" in e["msg"]]
        rb = f"aloof rollback {app['name']}" + (f" (to v{prev['version']})" if prev else "")
        if bad:
            out.append(_c("Bad release: problems started right after the latest deploy", "medium", f"{last_deploy['msg']} {int((now - last_deploy['ts']) / 60)} min ago, then: {bad[-1]['msg']}",
                          f"Roll back with {rb}, then fix and redeploy."))
        elif len(errors) >= 5 and prev:
            out.append(_c("Errors logged since the latest deploy", "low", f"{len(errors)} error-like log lines; the app is still serving",
                          f"Check error_log_lines; if they are new in this release, {rb}."))
    if st == "idle" or (spec.get("autoscale") and not [i for i in insts if i["status"] == "running"] and st in ("running", "idle")):
        out.append(_c("Scaled to zero (idle): the next request wakes it in about a second", "info", "autoscale min 0 with no running instances",
                      f"Expected behaviour. To keep it warm: aloof autoscale {app['name']} --min 1."))
    if st == "failed":
        out.append(_c("Last deploy failed and nothing is serving", "high", app.get("error") or "status failed", "See error_log_lines and deploys; fix the build/start and redeploy."))
    return out
