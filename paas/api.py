import os, json, itertools, httpx, time, asyncio, socket, logging, weakref
from fastapi import FastAPI, Header, HTTPException, Request, Depends, Cookie, WebSocket, BackgroundTasks
log = logging.getLogger("paas.api")
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse, StreamingResponse, Response, RedirectResponse, JSONResponse
from pydantic import BaseModel
from . import engine
from .db import q
from .plans import PLANS, REGIONS, REGION_NOTE, TIERS, RATES
from . import plans as _plans
from .drivers import get_drivers
from . import analytics
from . import controller, auth, billing, stripe_billing, razorpay_legacy as rzp, domains as doms
from .drivers import remote
from .drivers.providers import PROVISIONERS
BRAND = os.environ.get("PAAS_BRAND", "Aloof")
BASE_DOMAIN = os.environ.get("PAAS_BASE_DOMAIN", "localhost")
PUBLIC_PROXY = os.environ.get("PAAS_PROXY_URL", "http://localhost:8080")
# Extra app domains served alongside BASE_DOMAIN (e.g. <ip>.sslip.io before DNS exists); URL_DOMAIN is the one shown in app URLs.
ALT_DOMAINS = [d.strip().lower() for d in os.environ.get("PAAS_ALT_DOMAINS", "").split(",") if d.strip()]
APP_DOMAINS = [BASE_DOMAIN] + [d for d in ALT_DOMAINS if d != BASE_DOMAIN]
URL_DOMAIN = os.environ.get("PAAS_URL_DOMAIN") or BASE_DOMAIN
# Public hostnames. Go-live: PAAS_BASE_DOMAIN=aloof.si -> <app>.aloof.si, api.aloof.si, mcp.aloof.si
API_HOST = os.environ.get("PAAS_API_HOST", f"api.{BASE_DOMAIN}"); MCP_HOST = os.environ.get("PAAS_MCP_HOST", f"mcp.{BASE_DOMAIN}")
PUBLIC_API_URL = os.environ.get("PAAS_PUBLIC_API_URL", f"https://{API_HOST}"); PUBLIC_MCP_URL = os.environ.get("PAAS_PUBLIC_MCP_URL", f"https://{MCP_HOST}/mcp")
ROOT = os.path.dirname(os.path.dirname(__file__))
app = FastAPI(title=f"{BRAND} control plane", version="0.3", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
from . import capacity
@app.exception_handler(capacity.PlatformAtCapacity)
async def _at_capacity(request, exc):   # local node memory budget exhausted: 503 + Retry-After, clients retry with backoff
    return JSONResponse({"detail": str(exc), "retry_after": exc.retry_after, "retry": "safe to retry the same request after Retry-After seconds"},
                        status_code=503, headers={"Retry-After": str(exc.retry_after)})
# Security headers on control-plane responses (www/api/dashboard). Caddy sets the same non-CSP headers at the edge; setdefault keeps
# any per-route override. CSP only on HTML: our pages use inline <script>/<style> and same-origin fetch/WebSocket, nothing third-party.
SEC_HEADERS = {"Strict-Transport-Security": "max-age=31536000; includeSubDomains", "X-Content-Type-Options": "nosniff",
               "X-Frame-Options": "DENY", "Referrer-Policy": "strict-origin-when-cross-origin"}
HTML_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
            "font-src 'self' data:; connect-src 'self' wss: https:; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; "
            "form-action 'self' https://checkout.stripe.com https://billing.stripe.com")
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    r = await call_next(request)
    for k, v in SEC_HEADERS.items(): r.headers.setdefault(k, v)
    if r.headers.get("content-type", "").startswith("text/html"): r.headers.setdefault("Content-Security-Policy", HTML_CSP)
    else: r.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    return r
SECURE_COOKIES = PUBLIC_API_URL.startswith("https://") and os.environ.get("PAAS_INSECURE_COOKIES") != "1"
SUSPENDED_MSG = "this account is suspended; email support@aloof.si"
def acct(request: Request, authorization: str = Header(None)):
    """Bearer API key (CLI/MCP/automation) or session cookie (dashboard). Cookie-authenticated writes need the
    X-Aloof-CSRF header, which cross-site forms cannot set (SameSite=Lax is the first line of defence)."""
    key = (authorization or "").removeprefix("Bearer ").strip()
    if key:
        a = engine.account_by_key(key)
        if not a: raise HTTPException(401, "invalid API key")
        if a.get("suspended"): raise HTTPException(403, SUSPENDED_MSG)
        if a.get("_token"): scope_check(request.url.path, a)
        return level_check(request, a)
    a = auth.session_account(request.cookies.get(auth.COOKIE))
    if not a: raise HTTPException(401, "not authenticated: send Authorization: Bearer pk_... or log in at /login")
    if a.get("impersonated_by"):   # admin's read-only view of this account: reads only, never writes
        if request.method not in ("GET", "HEAD", "OPTIONS"): raise HTTPException(403, "read-only admin view: changes are disabled")
        a["_level"] = 1; return a
    if a.get("suspended"): raise HTTPException(403, SUSPENDED_MSG)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-aloof-csrf") != "1": raise HTTPException(403, "missing X-Aloof-CSRF header")
    return level_check(request, a)
# ---- access levels: read (GET) < write (everything except destroy/restore/account control) < admin (all) ----
LEVELS = {"paas:read": 1, "paas:write": 2, "paas:admin": 3, "paas": 3}
LEVEL_NAMES = {1: "paas:read", 2: "paas:write", 3: "paas:admin"}
_ADMIN_RULES = [("DELETE", r"^/v1/apps/[^/]+/?$"), ("DELETE", r"^/v1/volumes/[^/]+/?$"), ("DELETE", r"^/v1/pg/[^/]+/?$"),
                ("DELETE", r"^/v1/volumes/[^/]+/snapshots/[^/]+/?$"), ("POST", r"^/v1/(pg|volumes)/[^/]+/(restore|offsite-restore)/?$"),
                ("*", r"^/v1/(auth|billing)(/|$)")]
def required_level(method, path):
    """3 = destroy an app/volume/database/snapshot, restore data, or change auth/billing; 1 = read; 2 = any other change."""
    m = method.upper()
    if m in ("GET", "HEAD", "OPTIONS"): return 1
    for meth, rx in _ADMIN_RULES:
        if (meth == "*" or meth == m) and _re2.match(rx, path): return 3
    return 2
def caller_level(request, a):
    """API keys and sessions are admin; deploy tokens are write unless created with can_destroy. X-Aloof-Scope (sent by the MCP server
    for OAuth connections) can only lower it; an unknown value counts as read."""
    lv = (3 if a.get("_can_destroy") else 2) if a.get("_token") else 3
    h = (request.headers.get("x-aloof-scope") or "").strip().lower()
    if h: lv = min(lv, LEVELS.get(h, 1))
    return lv
def level_check(request, a):
    lv = caller_level(request, a); need = required_level(request.method, request.url.path); a["_level"] = lv
    if lv < need:
        what = {1: "read-only", 2: "write (no destroy/restore)"}[lv]
        raise HTTPException(403, f"insufficient scope: this {'deploy token' if a.get('_token') else 'connection'} is {what} ({LEVEL_NAMES[lv]}); "
                                 f"{request.method} {request.url.path} needs {LEVEL_NAMES[need]}")
    return a
import re as _re2
def scope_ok(name, a):
    sc = a.get("_scope_app")
    if not sc or name == sc: return True
    if name.startswith(sc + "-pr-"): return True
    return name in engine.children(sc)
def scope_check(path, a):
    """Deploy tokens (dt_...) never manage auth, billing, tokens or admin; app-scoped ones only touch their app (+ its previews/groups)."""
    if _re2.match(r"^/v1/(auth|billing|tokens|admin|signup)(/|$)", path): raise HTTPException(403, "deploy tokens can't use this endpoint")
    if not a.get("_scope_app"): return
    m = _re2.match(r"^/v1/apps/([^/]+)", path)
    if m and scope_ok(m.group(1), a): return
    if path in ("/v1/apps", "/v1/config/validate", "/v1/launch/detect", "/v1/plans"): return
    raise HTTPException(403, f"this deploy token is scoped to app {a['_scope_app']}")
def own(name, a):
    if not scope_ok(name, a): raise HTTPException(404, "app not found")
    ap = engine.get_app(name)
    if not ap or ap["account_id"] != a["id"] or ap["status"]=="destroyed": raise HTTPException(404, "app not found")
    return ap
def with_url(ap): ap["url"] = f"https://{ap['name']}.{URL_DOMAIN}/"; ap["http_url"] = f"http://{ap['name']}.{URL_DOMAIN}:8080/"; ap["path_url"] = f"{PUBLIC_PROXY}/_app/{ap['name']}/"; return ap
class Attribution(BaseModel):
    """Optional first-touch attribution from the frontend (sent to analytics only; sanitized and length-limited)."""
    utm_source: str|None=None; utm_medium: str|None=None; utm_campaign: str|None=None; utm_term: str|None=None; utm_content: str|None=None; referrer: str|None=None
class Signup(Attribution): email: str
class Source(BaseModel):
    image: str|None=None; git: str|None=None; ref: str|None=None; subdir: str|None=None; path: str|None=None
    upload: str|None=None; dockerfile: str|None=None; build_args: dict[str,str]|None=None; github_installation: int|None=None
class DeployReq(BaseModel):
    name: str; source: Source; plan: str|None=None; port: int=8080; env: dict[str,str]={}; instances: int=1
    command: list[str]|None=None; region: str|None=None; provider: str|None=None
    volumes: list[dict]=[]; autoscale: dict|None=None; health: dict|None=None
    processes: dict[str, str|list[str]]|None=None; process_counts: dict[str,int]|None=None; cron: list[dict]|None=None; release_command: str|list[str]|None=None
@app.post("/v1/signup")
def signup(s: Signup):
    """API-only signup (CLI/MCP): returns an API key once. Web users register with a password or magic link."""
    try: e = auth._check_email(s.email)
    except ValueError as x: raise HTTPException(400, str(x))
    if q("select 1 from accounts where email=?", (e,), one=True): raise HTTPException(409, "exists")
    a = engine.create_account(e); analytics.capture(a["id"], "signup_completed", {"method": "api"} | analytics.attribution(s))
    return {"email":a["email"],"api_key":a["api_key"],"credit_usd":billing.usd(a.get("credit_cents"))}
def _live_regions():
    from . import regions as R
    return {r["id"]: f"{r['name']} ({r['provider']})" for r in R.status() if r["live"]}   # only deployable regions (in-del overflow isn't a selectable region)
@app.get("/v1/regions")
def list_regions():
    """Regions with live status derived from healthy nodes. Only status=live regions accept new apps, volumes and databases."""
    from . import regions as R
    st = R.status(); return {"default": R.home(), "regions": st, "live": [r["id"] for r in st if r["live"]],
                             "aliases": R.ALIASES, "pricing": R.live_prices(), "note": "Status is computed from registered healthy nodes; coming_soon regions reject new workloads."}
@app.get("/v1/plans")
def plans(legacy: bool = False):
    """Pricing v3 (USD excl. tax/VAT): per-second plans, usage rates, workspace tiers and regions."""
    """GPU plans are only listed once GPUs are enabled (phase 2); until then `gpu` says they're coming soon, with the waitlist."""
    out = [{"id": k, **{x: y for x, y in v.items() if x not in ("providers",)}} for k, v in PLANS.items()
           if (legacy or not v["legacy"]) and (v["kind"] != "gpu" or _plans.GPU_ENABLED)]
    gpu = _plans.gpu_status() | ({} if _plans.GPU_ENABLED else {"coming_soon": [{"id": k, "gpu": v["gpu"]} for k, v in PLANS.items() if v["kind"] == "gpu"]})
    return {"currency": "USD", "tax": "Prices in USD; taxes may apply where required", "billing": "per second while running; volumes per provisioned GB-month; egress per GB (counts toward included usage)",
            "plans": out, "gpu": gpu, "rates": RATES, "tiers": TIERS, "regions": _live_regions(), "region_pricing": __import__("paas.regions", fromlist=["x"]).live_prices(), "region_note": REGION_NOTE}
class WaitlistReq(BaseModel):
    email: str; gpu: str | None = None; note: str | None = None; kind: str = "gpu"; region: str | None = None; company: str | None = None
    source: str | None = None; website: str | None = None   # website = honeypot (hidden field; humans leave it empty)
@app.post("/v1/waitlist")
def waitlist(w: WaitlistReq, request: Request, bg: BackgroundTasks):
    """Waitlists: kind=gpu (phase 2 GPUs), kind=region (a coming-soon region), kind=design_partner. One entry per email (and region);
    signing up again keeps the first signup date and updates the choice. Sends a confirmation (with a removal link) and notifies the admin."""
    from . import waitlist as W
    if w.kind not in ("gpu", "region", "design_partner"): raise HTTPException(400, "kind must be gpu, region or design_partner")
    ip = client_ip(request)
    try: e = auth._check_email(w.email)
    except ValueError as x: raise HTTPException(400, str(x))
    from .www_content import SOON_REGIONS
    from .regions import ALIASES as _RA
    codes = {c: l for c, l, _, _ in SOON_REGIONS}
    if w.region: w.region = _RA.get(w.region.lower(), w.region.lower())   # legacy waitlist codes (eu-fsn, us-ash, ...) map to region ids
    if w.kind == "region" and w.region not in codes: raise HTTPException(400, f"region must be one of {list(codes)}")
    if w.kind == "gpu" and w.gpu and (w.gpu not in PLANS or PLANS[w.gpu]["kind"] != "gpu"): raise HTTPException(400, "gpu must be a GPU plan id")
    msgs = {"gpu": "You're on the GPU waitlist. We'll email you when GPUs launch.", "region": f"You're on the waitlist for {codes.get(w.region, '')}. We'll email you once, when it's live.",
            "design_partner": "Thanks. We read every application and will reply by email."}
    W.rate_check(ip)   # counts well-formed submissions only, so a typo doesn't burn the allowance
    if w.website:   # honeypot tripped: look successful, store and send nothing
        log.info("waitlist honeypot ip=%s", ip); return {"ok": True, "message": msgs[w.kind], "duplicate": False}
    note = (w.note or "")[:1000 if w.kind == "design_partner" else 500] or None
    r, created = W.upsert(w.kind, e, gpu=w.gpu if w.kind == "gpu" else None, region=w.region if w.kind == "region" else None,
                          company=(w.company or "")[:200] or None if w.kind == "design_partner" else None, note=note,
                          source=W._source(w.source, request.headers.get("referer")), ip=ip)
    if created and w.kind == "gpu":
        acc = q("select id from accounts where email=?", (e,), one=True)
        if acc: analytics.capture(acc["id"], "gpu_waitlist_joined", {"gpu": w.gpu})
    if created: bg.add_task(W.send_emails, r["id"]); return {"ok": True, "message": msgs[w.kind], "duplicate": False}
    return {"ok": True, "message": "You're already on the list, we updated your choice." if w.kind != "design_partner" else "You're already on the list, we updated your application.", "duplicate": True}
class ByoReq(BaseModel):
    provider: str | None = None
@app.get("/v1/waitlist/byo-server")
def byo_get(a=Depends(acct)):
    from . import waitlist as W
    r = W.byo_status(a["email"]); return {"joined": bool(r), "provider": r["note"] if r else None, "joined_at": r["created"] if r else None}
@app.post("/v1/waitlist/byo-server")
def byo_join(b: ByoReq, request: Request, bg: BackgroundTasks, a=Depends(acct)):
    """Signed-in early-access waitlist for deploying to your own server. One entry per account email; a second join changes nothing."""
    from . import waitlist as W
    p = (b.provider or "").strip().lower() or None
    if p and p not in W.BYO_PROVIDERS: raise HTTPException(400, "provider must be one of: " + ", ".join(W.BYO_PROVIDERS))
    if W.byo_status(a["email"]): return {"ok": True, "joined": True, "duplicate": True, "message": "You're already on the waitlist."}
    r, created = W.upsert("byo_server", a["email"], note=p, source="dashboard", ip=client_ip(request))
    if created:
        analytics.capture(a["id"], "byo_waitlist_join", {"provider": p or "none"}); bg.add_task(W.send_emails, r["id"])
    return {"ok": True, "joined": True, "duplicate": not created, "message": "You're on the waitlist. We'll email you when we can let you in."}
@app.get("/v1/providers")
def providers():
    out = []
    for d in get_drivers().values():
        ok, why = d.available(); nodes = q("select count(*) as c from nodes where provider=? and status='ready'", (d.name,), one=True)["c"] if d.name in PROVISIONERS else None
        out.append({"name": d.name, "gpu": d.supports_gpu, "regions": d.regions, "available": ok, "detail": why, "ready_nodes": nodes,
                    "docs": getattr(PROVISIONERS.get(d.name), "docs", "")})
    return out
@app.post("/v1/apps")
def deploy(r: DeployReq, a=Depends(acct)):
    if r.name.startswith("pg-"): raise HTTPException(400, "app names starting with pg- are reserved for managed Postgres")
    if not scope_ok(r.name, a): raise HTTPException(403, f"this deploy token is scoped to app {a['_scope_app']}")
    spec = r.model_dump(exclude_unset=True) | {"name": r.name, "source": r.source.model_dump()}; spec["source"] = {k:v for k,v in spec["source"].items() if v}
    try: return with_url(engine.deploy(a["id"], spec))
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
@app.get("/v1/apps")
def list_apps(a=Depends(acct)): return [with_url(engine.get_app(r["name"])) for r in q("select name from apps where account_id=? and status!='destroyed' and name not like ?", (a["id"], "pg-%")) if scope_ok(r["name"], a)]
@app.get("/v1/apps/{name}")
def get_app(name: str, a=Depends(acct)): return with_url(own(name, a)) | (engine.release_state(name) or {})
@app.get("/v1/apps/{name}/deploy")
def app_deploy_progress(name: str, a=Depends(acct)):
    """Latest deploy: phase (queued|building|starting|live|failed), steps, error, a log excerpt on failure, and the platform events so far."""
    own(name, a); return with_url(engine.deploy_progress(name) | {"name": name}) | (engine.release_state(name) or {})
@app.get("/v1/apps/{name}/events")
def app_events(name: str, after: int = 0, limit: int = 200, a=Depends(acct)):
    """Platform events (deploy steps, build output, release_command output) after an event id, for polling."""
    own(name, a); return q("select id, ts, msg from events where app=? and id>? order by id limit ?", (name, after, max(1, min(limit, 500))))
@app.post("/v1/apps/{name}/retry")
def app_retry(name: str, request: Request, a=Depends(acct)):
    """Run the last deploy again from the same source (rebuilds; env, plan, volumes and scale are kept)."""
    ap = own(name, a)
    if ap["status"] in ("queued", "building", "starting"): raise HTTPException(409, "a deploy is already in progress")
    spec = {"name": name, "source": (ap.get("spec") or {}).get("source") or {}}
    if not spec["source"]: raise HTTPException(400, "this app has no source to redeploy")
    try: r = engine.deploy(a["id"], spec)
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], name, "deploy.retry", "", _ip(request)); return with_url(r)
@app.get("/v1/apps/{name}/logs", response_class=PlainTextResponse)
def logs(name: str, tail: int=200, a=Depends(acct)): own(name, a); return engine.logs(name, tail)
class Scale(BaseModel): instances: int
@app.get("/v1/apps/{name}/metrics")
def app_metrics(name: str, a=Depends(acct)):
    """Live counters for the dashboard: total requests since process start, in-flight, instance CPU %, egress bytes pending billing."""
    ap = own(name, a); M = controller.M; d = get_drivers(); cpu = []
    for i in ap.get("instances") or []:
        drv = d.get(i["provider"])
        try: cpu.append(round(float(drv.cpu_percent(i["id"])), 1) if drv and hasattr(drv, "cpu_percent") else None)
        except Exception: cpu.append(None)
    return {"ts": time.time(), "requests_total": M.reqs.get(name, 0), "inflight": M.inflight.get(name, 0), "instances": len(ap.get("instances") or []),
            "cpu_percent": cpu, "egress_bytes_unbilled": EGRESS.get(name, 0), "last_request": M.last.get(name)}
@app.post("/v1/apps/{name}/scale")
def scale(name: str, s: Scale, a=Depends(acct)): own(name, a); return with_url(engine.scale(name, s.instances))
class Env(BaseModel): env: dict[str, str|None]
@app.post("/v1/apps/{name}/env")
def env(name: str, e: Env, a=Depends(acct)): own(name, a); return with_url(engine.set_env(name, e.env))
@app.delete("/v1/apps/{name}")
def delete(name: str, with_volumes: bool = False, a=Depends(acct)):
    """Delete the app plus the volumes and Postgres its template created. Volumes you attached yourself are kept unless with_volumes=true."""
    own(name, a); return engine.destroy_app(name, with_volumes=with_volumes)
class Autoscale(BaseModel):
    min: int=1; max: int=3; target_concurrency: int=10; cpu_percent: float|None=None; idle_seconds: int=60
@app.put("/v1/apps/{name}/autoscale")
def autoscale(name: str, c: Autoscale|None=None, a=Depends(acct)):
    own(name, a); return with_url(engine.set_autoscale(name, c.model_dump() if c else None))
@app.delete("/v1/apps/{name}/autoscale")
def no_autoscale(name: str, a=Depends(acct)): own(name, a); return with_url(engine.set_autoscale(name, None))
class Health(BaseModel): path: str|None="/"; failures: int=3
@app.put("/v1/apps/{name}/health")
def health_cfg(name: str, h: Health, a=Depends(acct)):
    own(name, a)
    try: engine.check_health_path(h.path)
    except ValueError as e: raise HTTPException(422, str(e))
    return with_url(engine.update_spec(name, lambda s: s.__setitem__("health", h.model_dump())))
# ---- volumes ----
class VolReq(BaseModel): name: str; size_gb: int=1; region: str|None=None
@app.post("/v1/volumes")
def vol_create(v: VolReq, a=Depends(acct)):
    try: return engine.create_volume(a["id"], v.name, v.size_gb, v.region)
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
@app.get("/v1/volumes")
def vol_list(a=Depends(acct)): return q("select name,size_gb,app,mount_path,node_id,created,region from volumes where account_id=?", (a["id"],))
def own_vol(n, a):
    v = q("select * from volumes where name=?", (n,), one=True)
    if not v or v["account_id"] != a["id"]: raise HTTPException(404, "volume not found")
    return v
@app.delete("/v1/volumes/{vol}")
def vol_delete(vol: str, a=Depends(acct)):
    own_vol(vol, a)
    try: engine.delete_volume(vol); return {"ok": True}
    except ValueError as e: raise HTTPException(409, str(e))
class Attach(BaseModel): volume: str; path: str="/data"
@app.post("/v1/apps/{name}/volumes")
def vol_attach(name: str, b: Attach, a=Depends(acct)):
    own(name, a); own_vol(b.volume, a)
    try: return with_url(engine.attach_volume(name, b.volume, b.path))
    except ValueError as e: raise HTTPException(409, str(e))
@app.delete("/v1/apps/{name}/volumes/{vol}")
def vol_detach(name: str, vol: str, a=Depends(acct)): own(name, a); own_vol(vol, a); return with_url(engine.detach_volume(name, vol))
# ---- custom domains (TXT ownership + routing, background re-check, www/apex pairing) + on-demand TLS gate ----
EDGE_IPS = [x.strip() for x in os.environ.get("PAAS_EDGE_IPS", "127.0.0.1").split(",") if x.strip()]
class DomainReq(BaseModel): domain: str
def _lim(e: "doms.LimitError"):
    return HTTPException(e.status, e.msg, headers={"Retry-After": str(e.retry_after)} if e.retry_after else None)
def _domain_view(d):
    now = time.time(); st = d.get("status") or ("verified" if d["verified"] else "pending")
    v = {"domain": d["domain"], "app": d["app"], "status": st, "verified": bool(d["verified"]), "owned": bool(d["owned"]), "routed": bool(d["routed"]),
         "dns_error": st == "dns_error", "last_checked_at": d.get("last_checked_at"), "next_check_at": d.get("next_check_at") or None, "verified_at": d.get("verified_at"),
         "detail": [x for x in (d.get("last_detail") or "").split("; ") if x] if st != "verified" else [],
         "dns_records": doms.instructions(d["domain"], d["app"], BASE_DOMAIN, d["txt_token"], EDGE_IPS), "https": f"https://{d['domain']}/"}
    if not d["verified"]: v["claim_expires_at"] = (d.get("created") or now) + doms.CLAIM_TTL_DAYS * 86400
    p = doms.pair_hint(d["domain"], d["app"], BASE_DOMAIN, EDGE_IPS)
    if p: v["www_pair"] = p
    return v
def _verify(d, manual=True):
    r = doms.check(d["domain"], d["app"], BASE_DOMAIN, d["txt_token"], EDGE_IPS)
    doms.apply(q, d, r, manual=manual)
    return _domain_view(q("select * from domains where domain=?", (d["domain"],), one=True)) | {"detail": r["detail"] or []}
def _tier(a): return (q("select tier from accounts where id=?", (a["id"],), one=True) or {}).get("tier") or "free"
def _domain_quota(a):
    t = _tier(a); lim = doms.limit_for(t); used = q("select count(*) n from domains where account_id=?", (a["id"],), one=True)["n"]
    return {"tier": t, "limit": lim, "used": used, "remaining": max(0, lim - used)}
def _domain_hosts_blocked(): return [BASE_DOMAIN, URL_DOMAIN, API_HOST, MCP_HOST] + list(APP_DOMAINS)
@app.post("/v1/apps/{name}/domains")
def add_domain(name: str, d: DomainReq, a=Depends(acct)):
    own(name, a); dom = doms.normalize(d.domain)
    try: doms.rate_check("add", a["id"])
    except doms.LimitError as e: raise _lim(e)
    err = doms.valid_error(dom, _domain_hosts_blocked())
    if err: raise HTTPException(400, f"subdomains of {BASE_DOMAIN} are assigned automatically" if "managed by the platform" in err else err)
    ex = q("select * from domains where domain=?", (dom,), one=True)
    if ex and ex["account_id"] != a["id"]:
        # a verified (or recently added) claim is protected; a stale unverified one can't squat the name from other accounts
        if ex["verified"] or time.time() - (ex.get("created") or 0) < doms.CLAIM_GRACE_S:
            raise HTTPException(409, "domain claimed by another account. If it's yours, the current claim must be verified or expire (unverified claims are released after "
                                     f"{doms.CLAIM_TTL_DAYS} days); contact support to dispute a verified claim")
        q("delete from domains where domain=? and verified=0", (dom,)); ex = None
    if not ex:
        qt = _domain_quota(a)
        if qt["used"] >= qt["limit"]:
            nxt = {"free": "Hobby allows 3 and Pro 10", "hobby": "Pro allows 10"}.get(qt["tier"], "contact support for more")
            raise HTTPException(403, f"custom domain limit reached: the {qt['tier'].title()} plan allows {qt['limit']} domain{'s' if qt['limit'] != 1 else ''} per account (you have {qt['used']}). Remove one, or upgrade ({nxt}).")
        if q("select count(*) n from domains where verified=0", one=True)["n"] >= doms.MAX_PENDING:
            raise HTTPException(503, "too many pending domain claims platform-wide right now; try again later", headers={"Retry-After": "600"})
        q("insert into domains(domain,app,account_id,verified,created,txt_token,owned,routed,status,next_check_at,check_count,fail_count) values(?,?,?,0,?,?,0,0,'pending',0,0,0)", (dom, name, a["id"], time.time(), doms.new_token()))
    elif ex["app"] != name:
        q("update domains set app=?, status=case when verified=1 then 'verified' else 'pending' end where domain=?", (name, dom))
    elif not ex["verified"]: q("update domains set created=? where domain=?", (time.time(), dom))   # re-adding renews your own pending claim
    row = q("select * from domains where domain=?", (dom,), one=True)
    return _verify(row, manual=False)
import re as _re
def doms_ok(d): return doms.valid_error(d) is None
@app.post("/v1/apps/{name}/domains/{domain}/verify")
def verify_domain(name: str, domain: str, a=Depends(acct)):
    own(name, a); domain = doms.normalize(domain); d = q("select * from domains where domain=? and app=? and account_id=?", (domain, name, a["id"]), one=True)
    if not d: raise HTTPException(404, "domain not found")
    try: doms.rate_check("verify", a["id"]); doms.verify_gap(domain)
    except doms.LimitError as e: raise _lim(e)
    return _verify(d)
@app.get("/v1/apps/{name}/domains")
def list_domains(name: str, a=Depends(acct)): own(name, a); return [_domain_view(d) for d in q("select * from domains where app=? and account_id=? order by created", (name, a["id"]))]
@app.get("/v1/domains")
def list_all_domains(a=Depends(acct)):
    """Every custom domain on the account with its status, plus the plan's limit."""
    return {"domains": [_domain_view(d) for d in q("select * from domains where account_id=? order by created", (a["id"],))], **_domain_quota(a),
            "edge": {"a": doms.edge_split(EDGE_IPS)[0], "aaaa": doms.edge_split(EDGE_IPS)[1]}}
@app.delete("/v1/apps/{name}/domains/{domain}")
def del_domain(name: str, domain: str, a=Depends(acct)):
    own(name, a); q("delete from domains where domain=? and app=? and account_id=?", (doms.normalize(domain), name, a["id"])); return {"ok": True}
SYSTEM_HOSTS = {API_HOST, MCP_HOST} | {h for d in APP_DOMAINS for h in (d, f"www.{d}", f"api.{d}", f"mcp.{d}")}
def pair_target(host):
    """If host is the www/apex twin of a verified custom domain (and isn't itself claimed), the canonical verified domain it redirects to.
    The target is always a verified domain taken from the DB, never from the request, so this can't be an open redirect."""
    if host in SYSTEM_HOSTS or _app_of(host): return None
    p = doms.partner(host)
    if not p or q("select 1 from domains where domain=? and verified=1", (host,), one=True): return None
    return p if q("select 1 from domains where domain=? and verified=1", (p,), one=True) else None
def _app_of(host):
    for d in APP_DOMAINS:
        if host.endswith("." + d) and host not in SYSTEM_HOSTS and "." not in host[:-len(d)-1] and not host.startswith("pg-"): return host[:-len(d)-1]
@app.get("/v1/tls/ask")
def tls_ask(domain: str):
    """Caddy on_demand_tls gate: only issue certs for our hosts, live app subdomains and verified custom domains."""
    d = domain.lower()
    if d in SYSTEM_HOSTS: return {"ok": True}
    if _app_of(d) and q("select 1 from apps where name=? and status!='destroyed'", (_app_of(d),), one=True): return {"ok": True}
    if q("select 1 from domains where domain=? and verified=1", (d,), one=True): return {"ok": True}
    if pair_target(d): return {"ok": True}        # www/apex twin of a verified domain (served as a 308 redirect)
    raise HTTPException(403, "unknown domain")
def key_hint(k):
    """Displayed key prefix everywhere (CLI, API, dashboard): 'pk_' + the first 4 key characters."""
    return (k or "")[:7] or None
def http_host_known(host):
    """Plain-HTTP redirect gate: platform hosts, any *.<our domain>, verified custom domains (and their www/apex twins)."""
    d = (host or "").split(":")[0].lower().rstrip(".")
    if not d: return False
    if d in SYSTEM_HOSTS or any(d == b or d.endswith("." + b) for b in APP_DOMAINS): return True
    try: tls_ask(d); return True
    except HTTPException: return False
@app.get("/v1/tls/http-redirect", include_in_schema=False)
def tls_http_redirect(request: Request):
    """Caddy's http:// catch-all proxies here (ACME http-01 challenges are answered by Caddy before this). Known hosts get a
    301 to https; unknown hosts get a 404 instead of a reflected redirect."""
    host = request.headers.get("host", ""); uri = request.headers.get("x-aloof-uri") or "/"
    if not uri.startswith("/") or uri.startswith("//"): uri = "/"
    if not http_host_known(host): return PlainTextResponse("not found", status_code=404)
    return RedirectResponse(f"https://{host.split(':')[0].lower()}{uri}", status_code=301)
def _wallet(a): return {"credit_cents": round(a.get("credit_cents") or 0, 4), "credit_usd": billing.usd(a.get("credit_cents"))}
@app.get("/v1/usage")
def usage(a=Depends(acct)): return engine.usage(a["id"]) | _wallet(a) | {"month": billing.month_summary(a["id"])}
# ---- Stripe (primary) ----
class CheckoutReq(BaseModel): credit_usd: float
@app.post("/v1/billing/checkout")
def billing_checkout(o: CheckoutReq, a=Depends(acct)):
    """Stripe Checkout for a prepaid USD top-up ($10–$5,000). Returns {url}; the wallet is credited by the webhook."""
    try: stripe_billing.require_payments_open(); return stripe_billing.create_topup_session(a, o.credit_usd)
    except ValueError as e: raise HTTPException(400, str(e))
    except RuntimeError as e: raise HTTPException(503, str(e))
class SubReq(BaseModel): tier: str
@app.post("/v1/billing/subscribe")
def billing_subscribe(o: SubReq, a=Depends(acct)):
    """Stripe Checkout (subscription) for the Hobby or Pro tier; the tier switches when Stripe confirms."""
    if o.tier in TIERS and not TIERS[o.tier]["fee_usd"]: raise HTTPException(400, "Free needs no subscription; cancel in the customer portal")
    try: stripe_billing.require_payments_open(); return stripe_billing.create_subscription_session(a, o.tier)
    except ValueError as e: raise HTTPException(400, str(e))
    except RuntimeError as e: raise HTTPException(503, str(e))
@app.post("/v1/billing/portal")
def billing_portal(a=Depends(acct)):
    """Stripe customer portal: payment methods, invoices (with tax), cancel or change the subscription."""
    try: return stripe_billing.portal(a)
    except RuntimeError as e: raise HTTPException(503, str(e))
@app.post("/v1/billing/stripe/webhook")
async def stripe_webhook(request: Request):
    code, body = stripe_billing.handle_webhook(await request.body(), request.headers.get("stripe-signature"))
    return JSONResponse(body, status_code=code)
# ---- Razorpay (optional INR top-ups, off unless PAAS_RAZORPAY_ENABLED=1) ----
def _rzp():
    if not rzp.enabled(): raise HTTPException(404, "Razorpay is disabled; use Stripe (/v1/billing/checkout)")
class OrderReq(BaseModel): credit_inr: int
@app.post("/v1/billing/razorpay/orders")
def billing_order(o: OrderReq, a=Depends(acct)):
    _rzp()
    try: return rzp.create_order(a, o.credit_inr)
    except ValueError as e: raise HTTPException(400, str(e))
    except RuntimeError as e: raise HTTPException(503, str(e))
class VerifyReq(BaseModel): razorpay_order_id: str; razorpay_payment_id: str; razorpay_signature: str
@app.post("/v1/billing/razorpay/verify")
def billing_verify(v: VerifyReq, a=Depends(acct)):
    _rzp()
    p = q("select * from payments where order_id=? and account_id=?", (v.razorpay_order_id, a["id"]), one=True)
    if not p: raise HTTPException(404, "order not found")
    if not rzp.verify_checkout_signature(v.razorpay_order_id, v.razorpay_payment_id, v.razorpay_signature): raise HTTPException(400, "invalid payment signature")
    return rzp.capture(v.razorpay_order_id, v.razorpay_payment_id, source="checkout")
@app.post("/v1/billing/razorpay/webhook")
async def billing_webhook(request: Request):
    if not rzp.enabled(): return JSONResponse({"error": "razorpay disabled"}, status_code=404)
    code, body = rzp.handle_webhook(await request.body(), request.headers.get("x-razorpay-signature"))
    return JSONResponse(body, status_code=code)
@app.get("/v1/billing/daily")
def billing_daily(days: int = 30, a=Depends(acct)):
    """Balance debits per UTC day (metered usage beyond included, and plan fees) for the last N days. Usage covered by the
    plan's included amount isn't in the ledger, so it isn't counted here; see /v1/billing month.usage for that."""
    days = max(1, min(days, 90)); since = time.time() - days * 86400; out = {}
    for r in q("select ts, amount_cents, kind from ledger where account_id=? and ts>? and amount_cents<0", (a["id"], since)):
        d = time.strftime("%Y-%m-%d", time.gmtime(r["ts"])); o = out.setdefault(d, {"usage_usd": 0.0, "plan_usd": 0.0})
        o["plan_usd" if r["kind"] == "plan" else "usage_usd"] += -r["amount_cents"] / 100
    return {"days": [{"date": time.strftime("%Y-%m-%d", time.gmtime(since + (i + 1) * 86400)), **{k: round(v, 2) for k, v in out.get(time.strftime("%Y-%m-%d", time.gmtime(since + (i + 1) * 86400)), {"usage_usd": 0.0, "plan_usd": 0.0}).items()}} for i in range(days)]}
@app.get("/v1/billing/runway")
def billing_runway(a=Depends(acct)):
    """Estimated hours of prepaid credit left at the last 6 hours' burn rate, plus alert settings. Drives the dashboard banner."""
    return billing.runway(a["id"])
class AlertPrefs(BaseModel): enabled: bool = True; threshold_usd: float | None = None
@app.put("/v1/billing/alerts")
def billing_alerts(p: AlertPrefs, a=Depends(acct)):
    """Low-balance emails (on by default: ~3 days left, <24h left, suspension). Optional extra alert when the balance falls to threshold_usd."""
    th = p.threshold_usd
    if th is not None and not (0 < th <= 5000): raise HTTPException(400, "threshold must be between $0 and $5,000, or empty")
    q("update accounts set lowbal_alerts=?, lowbal_threshold_usd=? where id=?", (int(p.enabled), th, a["id"])); return billing.runway(a["id"])
@app.get("/v1/billing")
def billing_summary(a=Depends(acct)):
    a = q("select * from accounts where id=?", (a["id"],), one=True)
    return _wallet(a) | {"currency": "USD", "stripe": stripe_billing.configured(), "payments_open": stripe_billing.payments_open(), "stripe_tax": stripe_billing.tax_enabled(), "topup_min_usd": stripe_billing.MIN_TOPUP, "topup_max_usd": stripe_billing.MAX_TOPUP, "rates": RATES, "razorpay": rzp.enabled(),
            "subscription": a.get("stripe_subscription"), "month": billing.month_summary(a["id"]), "tiers": TIERS, "runway": billing.runway(a["id"]),
            "profile": {k: a.get(k) for k in ("billing_name", "country", "tax_id")},
            "payments": q("select id,kind,credit_cents,amount_cents,tier,status,invoice_url,created from stripe_sessions where account_id=? order by created desc limit 50", (a["id"],)),
            "invoices": [r for r in q("select id,invoice_id,amount_cents,invoice_url,created from stripe_sessions where account_id=? and invoice_url is not null order by created desc", (a["id"],))],
            "ledger": q("select ts,amount_cents,kind,ref from ledger where account_id=? and amount_cents is not null order by id desc limit 50", (a["id"],))}
class TierReq(BaseModel): tier: str
@app.put("/v1/billing/tier")
def billing_tier(t: TierReq, a=Depends(acct)):
    """Switch tier paid from the prepaid wallet. Upgrades charge the fee difference now (pro-rated); downgrades apply immediately, no refund.
    With a Stripe subscription use /v1/billing/subscribe or the portal instead."""
    from .crypto import load_spec
    if t.tier not in TIERS: raise HTTPException(400, f"tier must be one of {list(TIERS)}")
    if a.get("stripe_subscription"): raise HTTPException(409, "tier is managed by your Stripe subscription; use the customer portal")
    old = TIERS.get(a.get("tier") or "free", TIERS["free"]); new = TIERS[t.tier]
    if new.get("plans"):
        bad = [r["name"] for r in q("select name, spec from apps where account_id=? and status!='destroyed'", (a["id"],)) if load_spec(r["spec"]).get("plan") not in new["plans"]]
        if bad: raise HTTPException(400, f"{t.tier} tier only runs {', '.join(new['plans'])}; change or destroy: {', '.join(bad)}")
    diff = (new["fee_usd"] - old["fee_usd"]) * 100
    if diff > 0:
        lt = time.localtime(); days = 30 - min(lt.tm_mday - 1, 29); charge = round(diff * days / 30, 2)
        if (a.get("credit_cents") or 0) < charge: raise HTTPException(402, f"insufficient credit: upgrade costs ${charge/100:.2f} now; top up first")
        q("update accounts set credit_cents=credit_cents-? where id=?", (charge, a["id"]))
        q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (a["id"], time.time(), -charge, "plan", f"upgrade to {t.tier} (pro-rated {days}d)"))
    q("update accounts set tier=?, month_key=? where id=?", (t.tier, billing.month_key(), a["id"]))
    if not new.get("hard_cap"): billing.uncap_account(a["id"], f"upgraded to {t.tier}")
    return {"ok": True, "tier": t.tier}
class Profile(BaseModel): billing_name: str|None=None; country: str|None=None; tax_id: str|None=None; gstin: str|None=None; billing_state: str|None=None
@app.put("/v1/billing/profile")
def billing_profile(p: Profile, a=Depends(acct)):
    """Name/country/tax ID for invoices. With Stripe Tax on, Checkout also collects the address and VAT/GST ID.
    gstin/billing_state are only used by the optional Razorpay INR invoices."""
    c = (p.country or "").upper() or None
    if c and not _re.match(r"^[A-Z]{2}$", c): raise HTTPException(400, "country must be an ISO 3166-1 alpha-2 code, e.g. US, DE, IN")
    if p.gstin and not _re.match(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", p.gstin.upper()): raise HTTPException(400, "invalid GSTIN format")
    st = rzp.STATES.get((p.gstin or "")[:2].upper()) or p.billing_state
    q("update accounts set billing_name=?, country=?, tax_id=?, gstin=?, billing_state=? where id=?",
      (p.billing_name, c, (p.tax_id or "").strip() or None, (p.gstin or "").upper() or None, st, a["id"]))
    return {"ok": True, "billing_state": st}
@app.get("/v1/billing/invoices/{inv_id}", response_class=HTMLResponse)
def invoice(inv_id: int, a=Depends(acct)):
    """Legacy Razorpay GST invoices (INR). Stripe invoices are linked from GET /v1/billing (invoice_url)."""
    inv = q("select * from invoices where id=? and account_id=?", (inv_id, a["id"]), one=True)
    if not inv: raise HTTPException(404, "invoice not found")
    return rzp.invoice_html(inv)
# ---- auth: password, magic link, sessions ----
class Creds(BaseModel): email: str; password: str
class RegisterReq(Attribution, Creds): attribution: dict | None = None
class MagicReq(Attribution): email: str; attribution: dict | None = None
def _login_response(a, body=None):
    r = JSONResponse(body or {"ok": True, "email": a["email"]}); r.set_cookie(auth.COOKIE, auth.new_session(a["id"]), max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    return r
ATTR_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "referrer", "landing")
def _save_attribution(account_id, attr):
    """First-touch UTM/referrer sent by the site with the signup (from sessionStorage, no cookies). Whitelisted and length-capped."""
    if not isinstance(attr, dict): return
    clean = {k: str(attr[k])[:500] for k in ATTR_KEYS if attr.get(k)}
    if clean:
        try: q("update accounts set signup_attribution=? where id=?", (json.dumps(clean), account_id))
        except Exception: pass
@app.post("/v1/auth/register")
def auth_register(c: RegisterReq):
    try: a = auth.register(c.email, c.password)
    except ValueError as e: raise HTTPException(400, str(e))
    except PermissionError as e: raise HTTPException(409, str(e))
    _save_attribution(a["id"], c.attribution)
    analytics.capture(a["id"], "signup_completed", {"method": "password"} | analytics.attribution(c))
    return _login_response(a, {"ok": True, "email": a["email"], "api_key": a["api_key"]})
# Soft login rate limit (in-memory, per process): LOGIN_LIMIT attempts per LOGIN_WINDOW s per client IP and per email -> 429 + Retry-After.
LOGIN_LIMIT = int(os.environ.get("PAAS_LOGIN_LIMIT", "10")); LOGIN_WINDOW = 60.0
from .netaddr import client_ip, rate_key
_login_hits = {}
def _ipk(request): return rate_key(client_ip(request))   # IPv6 bucketed per /64 so rotating addresses inside one allocation share a limit
def _rate_check(keys, now=None):
    now = now or time.time(); retry = 0
    if len(_login_hits) > 50000: _login_hits.clear()   # memory bound; worst case resets the window
    for k in keys:
        h = [t for t in _login_hits.get(k, []) if now - t < LOGIN_WINDOW]; _login_hits[k] = h
        if len(h) >= LOGIN_LIMIT: retry = max(retry, int(LOGIN_WINDOW - (now - h[0])) + 1)
    if retry: raise HTTPException(429, "too many login attempts; try again later", headers={"Retry-After": str(retry)})
    for k in keys: _login_hits[k].append(now)
@app.post("/v1/auth/login")
def auth_login(c: Creds, request: Request):
    _rate_check(("ip:" + _ipk(request), "email:" + (c.email or "").strip().lower()))
    try: a = auth.login(c.email, c.password)
    except PermissionError as e: raise HTTPException(401, str(e))
    analytics.identify(a["id"]); return _login_response(a)
@app.post("/v1/auth/magic")
def auth_magic(m: MagicReq, request: Request):
    _rate_check(("ip:" + _ipk(request), "email:" + (m.email or "").strip().lower()))
    try: auth.start_magic(m.email, PUBLIC_API_URL if SECURE_COOKIES else str(request.base_url), attribution=analytics.attribution(m))
    except ValueError as e: raise HTTPException(400, str(e))
    except auth.MailError: raise HTTPException(503, "email sign-in is temporarily unavailable; log in with your password or try again later")
    return {"ok": True, "detail": "If the address is valid, a sign-in link is on its way (valid 15 minutes)."}
@app.get("/auth/magic")
def auth_magic_finish(token: str):
    from .db import kv_get as _kg, kv_del as _kd
    pend = _kg("ml_pending", auth.sha256(token or ""))
    try: a = auth.finish_magic(token)
    except PermissionError:
        from . import www
        body = www.shell("Sign in", "This sign-in link doesn't work anymore",
                         "Links last 15 minutes and work once. If you already clicked it, you may be signed in.",
                         "<p><a class='btn pri' href='/login'>Get a new link</a> <a class='btn ghost' href='/dashboard'>Open dashboard</a></p>", narrow=True)
        return HTMLResponse(www.render("/auth/magic", "Sign-in link expired", "Request a new sign-in link.", body), 400)
    via = "magic_link"
    if pend:   # X sign-up without email: the link proved the address; attach the parked X identity
        _kd("ml_pending", auth.sha256(token or ""))
        try:
            if oauth_login.finish_pending(pend, a["id"]): via = "x"
        except oauth_login.AuthError: pass
    if a.get("_created"): analytics.capture(a["id"], "signup_completed", {"method": via} | (a.get("_attribution") or {}))
    else: analytics.identify(a["id"])
    r = RedirectResponse("/dashboard", 303); r.set_cookie(auth.COOKIE, auth.new_session(a["id"]), max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    return r
# ---- social login: Google / GitHub / X (see paas/oauth_login.py, docs/social-login.md) ----
from . import oauth_login
import urllib.parse as _up
def _auth_err(code, p, nxt):
    r = RedirectResponse("/login?" + _up.urlencode({"auth_error": code, "provider": p, "next": nxt}), 303)
    r.delete_cookie(oauth_login.STATE_COOKIE, path="/auth/"); return r
def _cookie_kw(age): return {"max_age": age, "httponly": True, "samesite": "lax", "secure": SECURE_COOKIES, "path": "/auth/"}
@app.get("/v1/auth/providers")
def auth_providers(): return {"providers": oauth_login.providers(), "magic_link": True}
@app.get("/auth/{p}/start")
def social_start(p: str, request: Request, next: str | None = None):
    nxt = auth.safe_next(next, "/dashboard")
    if p not in oauth_login.PROVIDERS: return _auth_err("provider_disabled", p[:20], nxt)
    base = oauth_login.base_url(); bh = _up.urlsplit(base).netloc
    if SECURE_COOKIES and bh and request.url.netloc != bh:   # one canonical host, so the state cookie and the session land where the callback is
        return RedirectResponse(f"{base}/auth/{p}/start?" + _up.urlencode({"next": nxt}), 303)
    try: _rate_check(("oauth-ip:" + _ipk(request),))
    except HTTPException: return _auth_err("rate_limited", p, nxt)
    cur = auth.session_account(request.cookies.get(auth.COOKIE))
    try: url, st = oauth_login.start(p, nxt, link_account=cur["id"] if cur else None)
    except oauth_login.AuthError as e: return _auth_err(e.code, p, nxt)
    r = RedirectResponse(url, 303); r.set_cookie(oauth_login.STATE_COOKIE, st, **_cookie_kw(oauth_login.STATE_TTL)); return r
@app.get("/auth/{p}/callback")
def social_callback(p: str, request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    nxt = auth.safe_next(oauth_login.peek_next(state), "/dashboard")
    if p not in oauth_login.PROVIDERS or not oauth_login.enabled(p): return _auth_err("provider_disabled", p[:20], nxt)
    try:
        s = oauth_login.take_state(p, state, request.cookies.get(oauth_login.STATE_COOKIE))
        if error: raise oauth_login.AuthError("cancelled" if error == "access_denied" else "provider_error")
        if not code: raise oauth_login.AuthError("provider_error")
        if s.get("link"):   # connect flow: the same signed-in account must finish it
            cur = auth.session_account(request.cookies.get(auth.COOKIE))
            if not cur or cur["id"] != s["link"]: raise oauth_login.AuthError("state_invalid")
        prof = oauth_login.profile(p, oauth_login._exchange(p, code, s["ver"]), s["nonce"])
        a, created, pid = oauth_login.resolve(p, prof, s.get("link"))
    except oauth_login.AuthError as e:
        logging.getLogger("aloof.auth").info("social login %s failed: %s (%s)", p, e.code, e)
        return _auth_err(e.code, p, nxt)
    except Exception as e:
        logging.getLogger("aloof.auth").warning("social login %s error: %s", p, type(e).__name__)
        return _auth_err("provider_error", p, nxt)
    if pid:
        r = RedirectResponse("/auth/x/email?" + _up.urlencode({"next": nxt}), 303)
        r.delete_cookie(oauth_login.STATE_COOKIE, path="/auth/"); r.set_cookie("aloof_oauth_pending", pid, **_cookie_kw(oauth_login.PENDING_TTL)); return r
    if created: analytics.capture(a["id"], "signup_completed", {"method": p})
    else: analytics.identify(a["id"])
    _audit(a["id"], None, "auth.login", p, _ip(request))
    r = RedirectResponse(nxt, 303); r.delete_cookie(oauth_login.STATE_COOKIE, path="/auth/")
    if not s.get("link"): r.set_cookie(auth.COOKIE, auth.new_session(a["id"]), max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    return r
@app.get("/auth/x/email", response_class=HTMLResponse)
def x_email_form(request: Request, next: str | None = None):
    nxt = auth.safe_next(next, "/dashboard")
    if not oauth_login.pending(request.cookies.get("aloof_oauth_pending")): return _auth_err("state_invalid", "x", nxt)
    from . import site
    return www.at("/auth/x/email", "default", site.x_email, BRAND)
class XEmailReq(BaseModel): email: str
@app.post("/auth/x/email")
def x_email_submit(b: XEmailReq, request: Request):
    """JSON {email} + X-Aloof-CSRF: 1 (sent by site.x_email). Needs the parked X identity (pending cookie from the callback)."""
    if request.headers.get("x-aloof-csrf") != "1": raise HTTPException(403, "missing X-Aloof-CSRF header")
    pid = request.cookies.get("aloof_oauth_pending")
    if not oauth_login.pending(pid): raise HTTPException(400, "Your X sign-in expired. Start again with Sign in with X.")
    em = (b.email or "").strip().lower()
    _rate_check(("ip:" + _ipk(request), "email:" + em))
    try: auth.start_magic(em, oauth_login.base_url() if SECURE_COOKIES else str(request.base_url), pending=pid)
    except ValueError: raise HTTPException(400, "That doesn't look like an email address.")
    except auth.MailError: raise HTTPException(503, "Email is temporarily unavailable; try again shortly.")
    return {"ok": True, "detail": f"Check {em} for a link to finish signing in (valid 15 minutes). Open it in this browser."}
@app.get("/v1/auth/identities")
def identities_list(a=Depends(acct)): return {"identities": oauth_login.list_identities(a["id"]), "providers": oauth_login.providers()}
@app.delete("/v1/auth/identities/{p}")
def identities_unlink(p: str, request: Request, a=Depends(acct)):
    if not oauth_login.unlink(a["id"], p): raise HTTPException(404, "that provider isn't connected")
    return {"ok": True, "note": "you can still sign in with an email link" + (" or your password" if a.get("password_hash") else "")}
@app.post("/v1/auth/logout")
def auth_logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE)); r = JSONResponse({"ok": True}); r.delete_cookie(auth.COOKIE, path="/"); return r
class NameReq(BaseModel): name: str | None = None
@app.put("/v1/auth/profile")
def auth_profile(p: NameReq, a=Depends(acct)):
    """Display name (also used as the invoice name)."""
    n = (p.name or "").strip()[:120] or None; q("update accounts set billing_name=? where id=?", (n, a["id"])); return {"ok": True, "name": n}
@app.get("/v1/auth/sessions")
def auth_sessions(request: Request, a=Depends(acct)):
    """Active browser sessions (no IP or user agent is stored). `current` marks the one making this request."""
    cur = auth.sha256(request.cookies.get(auth.COOKIE) or "")
    return [{"id": r["token_hash"][:12], "created": r["created"], "expires": r["expires"], "current": r["token_hash"] == cur}
            for r in q("select token_hash, created, expires from sessions where account_id=? and expires>? order by created desc", (a["id"], time.time()))]
@app.delete("/v1/auth/sessions/others")
def auth_sessions_revoke_others(request: Request, a=Depends(acct)):
    """Sign out every other browser session. API keys and deploy tokens are not affected."""
    cur = auth.sha256(request.cookies.get(auth.COOKIE) or "")
    n = len(q("select token_hash from sessions where account_id=? and token_hash!=?", (a["id"], cur)))
    q("delete from sessions where account_id=? and token_hash!=?", (a["id"], cur)); return {"ok": True, "revoked": n}
@app.get("/v1/auth/me")
def auth_me(a=Depends(acct)):
    return {"id": a["id"], "email": a["email"], "name": a.get("billing_name"), "created": a.get("created"), "tier": a.get("tier") or "free",
            "email_verified": bool(a["email_verified"]), "has_password": bool(a.get("password_hash")), "is_test": bool(a.get("is_test")),
            "api_key_prefix": key_hint(a.get("api_key_prefix")), **_wallet(a)}
@app.post("/v1/auth/api-key")
def auth_rotate(a=Depends(acct)): return {"api_key": engine.rotate_api_key(a["id"]), "detail": "previous key revoked; shown once"}
class KeyReq(BaseModel): label: str
@app.get("/v1/auth/keys")
def auth_keys(a=Depends(acct)):
    """Full-access API keys: the primary key (rotate-only) plus named extra keys. Scoped deploy tokens live at /v1/tokens."""
    ac = q("select id,api_key_prefix,created from accounts where id=?", (a["id"],), one=True)
    return [{"id": "primary", "label": "Default key", "prefix": key_hint(ac["api_key_prefix"]), "created": ac["created"], "last_used": None, "kind": "primary", "scope": "full access"}] + \
           [{"id": r["id"], "label": r["label"], "prefix": key_hint(r["prefix"]), "created": r["created"], "last_used": r["last_used"], "kind": "named", "scope": "full access"}
            for r in q("select * from api_keys where account_id=? order by created desc", (a["id"],))]
@app.post("/v1/auth/keys")
def auth_key_create(b: KeyReq, request: Request, a=Depends(acct)):
    label = (b.label or "").strip()[:60]
    if not label: raise HTTPException(400, "name the key")
    if label == "mcp-oauth": raise HTTPException(400, "that name is reserved")
    k = engine.create_named_key(a["id"], label, replace=False); _audit(a["id"], None, "key.create", label, _ip(request))
    return {"api_key": k, "prefix": key_hint(k), "label": label, "detail": "shown once"}
@app.get("/v1/auth/connected-apps")
def connected_apps(a=Depends(acct)):
    """OAuth/MCP clients (Claude, ChatGPT, Cursor, ...) that currently hold a token for this account."""
    from . import oauth_apps; return oauth_apps.list_apps(a["id"])
@app.delete("/v1/auth/connected-apps/{client_id:path}")
def connected_app_revoke(client_id: str, request: Request, a=Depends(acct)):
    """Disconnect an app: deletes its access and refresh tokens for this account (it must sign in again to reconnect)."""
    from . import oauth_apps; n = oauth_apps.revoke(a["id"], client_id)
    if not sum(n.values()): raise HTTPException(404, "no connected app with that client_id")
    _audit(a["id"], None, "oauth.revoke", client_id[:200], _ip(request))
    return {"ok": True, "tokens_revoked": n["access"] + n["refresh"], "codes_revoked": n["code"], "deploy_tokens_revoked": n.get("deploy_tokens", 0)}
@app.delete("/v1/auth/keys/{kid}")
def auth_key_revoke(kid: str, request: Request, a=Depends(acct)):
    if kid == "primary": raise HTTPException(400, "the default key can be rotated, not revoked")
    q("delete from api_keys where id=? and account_id=?", (kid, a["id"])); _audit(a["id"], None, "key.revoke", kid, _ip(request)); return {"ok": True}
@app.post("/v1/auth/mcp-key")
def auth_mcp_key(a=Depends(acct)):
    """Key bound to the MCP OAuth connector; doesn't revoke the CLI key. Re-login rotates only this one."""
    return {"api_key": engine.create_named_key(a["id"], "mcp-oauth")}
ICONS = {"/favicon.ico": ("favicon.ico", "image/x-icon"), "/icon.svg": ("icon.svg", "image/svg+xml"), "/favicon-16x16.png": ("favicon-16x16.png", "image/png"),
         "/favicon-32x32.png": ("favicon-32x32.png", "image/png"), "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
         "/icon-192.png": ("icon-192.png", "image/png"), "/email-logo.png": ("email-logo.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"), "/site.webmanifest": ("site.webmanifest", "application/manifest+json")}
def _icon_route(path, fn, mt):
    @app.get(path, include_in_schema=False)
    def _icon(): return FileResponse(os.path.join(ROOT, "paas", "static", "icons", fn), media_type=mt, headers={"Cache-Control": "public, max-age=86400"})
for _p, (_f, _m) in ICONS.items(): _icon_route(_p, _f, _m)
@app.get("/cli/aloof", response_class=PlainTextResponse)
def cli_download():
    return open(os.path.join(os.path.dirname(__file__), "..", "cli", "aloof")).read()
class PwReq(BaseModel): password: str
@app.post("/v1/auth/password")
def auth_password(p: PwReq, a=Depends(acct)):
    try: auth.set_password(a["id"], p.password); return {"ok": True}
    except ValueError as e: raise HTTPException(400, str(e))
# ---- nodes: agent registration, bootstrap, admin ----
def _admin_token():
    f = os.path.join(ROOT, "data", "admin_token")
    if os.environ.get("PAAS_ADMIN_TOKEN"): return os.environ["PAAS_ADMIN_TOKEN"]
    if not os.path.exists(f):   # first start: create one (0600) so admin endpoints work out of the box
        import secrets as _s; os.makedirs(os.path.dirname(f), exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, ("adm_" + _s.token_urlsafe(32)).encode()); os.close(fd)
    return open(f).read().strip()
ADMIN = _admin_token()
remote.direct_agent_guard()   # refuse PAAS_ALLOW_DIRECT_AGENT in production
def admin(authorization: str = Header(None)):
    import hmac as _h
    a = authorization or ""
    if not ADMIN or not a.startswith("Bearer ") or not _h.compare_digest(a[7:], ADMIN): raise HTTPException(401, "admin token required")
@app.post("/v1/nodes/register")
async def node_register(request: Request):
    try: body = await request.json()
    except Exception: raise HTTPException(400, "body must be JSON")
    try: return remote.register_node(body)
    except PermissionError as e: raise HTTPException(403, str(e))
    except LookupError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
@app.get("/v1/nodes/agent.py", response_class=PlainTextResponse)
def node_agent(): return open(os.path.join(ROOT, "paas", "agent", "agent.py")).read()
class NodeReq(BaseModel): pool: str="self-hosted"; plan: str="cpu-large"; gpu: bool=False; region: str|None=None
@app.post("/v1/admin/nodes", dependencies=[Depends(admin)])
def admin_add_node(n: NodeReq):
    """Create a join token for a manually-managed node (self-hosted / Yotta bare metal / any Ubuntu box)."""
    if n.pool not in PROVISIONERS: raise HTTPException(400, f"pool must be one of {list(PROVISIONERS)}")
    if n.plan not in PLANS: raise HTTPException(400, "plan must be a known plan id (see /v1/plans)")
    from . import regions as R
    try: region = R.canonical(n.region or R.home())
    except ValueError as e: raise HTTPException(400, str(e))
    node_id, join = remote.create_node_record(n.pool, n.plan, n.gpu, region)
    return {"node_id": node_id, "join_token": join, "region": region,
            "install": f"curl -fsSL '{PUBLIC_API_URL}/v1/nodes/bootstrap.sh?node={node_id}&token={join}' | sudo bash"}
@app.get("/v1/nodes/bootstrap.sh", response_class=PlainTextResponse)
def node_bootstrap(node: str, token: str):
    from .drivers import bootstrap
    from .crypto import sha256
    from .db import kv_get
    if kv_get("join", sha256(token)) != node: raise HTTPException(403, "invalid join token for this node")   # tokens are bound to their node
    n = q("select * from nodes where id=?", (node,), one=True)
    if not n: raise HTTPException(404, "unknown node")
    return bootstrap.render(node, n["provider"], n["plan"], token, bool(n["gpus"]))
@app.get("/v1/admin/nodes", dependencies=[Depends(admin)])
def admin_nodes(): return q("select id,provider,provider_ref,status,region,public_ip,wg_ip,vcpu,mem_mb,gpus,gpu_model,plan,created,last_seen,error from nodes order by created desc")
@app.get("/v1/admin/edge/wireguard", dependencies=[Depends(admin)], response_class=PlainTextResponse)
def admin_edge_wg():
    """wg-quick [Peer] blocks for the edge hub, one per registered node, grouped by region (render only; applies nothing)."""
    return remote.edge_wireguard_config()
@app.get("/v1/admin/edge/routes", dependencies=[Depends(admin)])
def admin_edge_routes():
    """Where the edge sends each running instance: region, node and WireGuard upstream."""
    return remote.edge_routes()
def paid_customers():
    """Accounts that have actually paid us: a paid Stripe top-up/invoice, an active subscription, or a paid Razorpay order."""
    return q("""select count(distinct id) as c from (
      select account_id as id from stripe_sessions where status='paid'
      union select id from accounts where stripe_subscription is not null and stripe_subscription != ''
      union select account_id as id from payments where status='paid') t""", one=True)["c"]
from . import www
@app.get("/v1/admin/metrics", dependencies=[Depends(www.require_admin)])
def admin_metrics():
    """Owner metrics (test accounts excluded). Legacy top-level keys kept; full dashboard data under "dashboard"."""
    m = www.metrics(); n = m["paid_customers"]; goal = _plans.GPU_UNLOCK_PAID_CUSTOMERS
    return {"backups": m["backups"], "paid_customers": n, "accounts": m["users"]["total"],
            "gpu_waitlist": m["waitlist"]["gpu"], "gpu_enabled": _plans.GPU_ENABLED, "gpu_unlock_at_paid_customers": goal,
            "note": "GPUs enabled (phase 2)." if _plans.GPU_ENABLED else
                    (f"Phase 1 (CPU only). GPU unlocks at {goal} paid customers: {max(0, goal - n)} to go." if n < goal else
                     f"{n} paid customers: GPU unlock reached. Set PAAS_GPU_ENABLED=1 plus HYPERSTACK_API_KEY / RUNPOD_API_KEY."),
            "dashboard": m}
@app.delete("/v1/admin/nodes/{node_id}", dependencies=[Depends(admin)])
def admin_drop_node(node_id: str):
    if not q("select 1 from nodes where id=?", (node_id,), one=True): raise HTTPException(404, "unknown node")
    remote.destroy_node(node_id, "admin"); return {"ok": True}
@app.get("/healthz")
def health(): return {"ok":True}
def _llms_gpu():
    if _plans.GPU_ENABLED:
        return "GPU per hour: " + ", ".join(f"{k} ${v['price_usd_hr']:.2f}" for k, v in PLANS.items() if v["kind"] == "gpu")
    return ("GPUs: COMING SOON (phase 2: " + ", ".join(k for k, v in PLANS.items() if v["kind"] == "gpu") + "). Not deployable yet; "
            "join the waitlist: POST {{API}}/v1/waitlist {\"email\":\"...\",\"gpu\":\"gpu-h100\"}")
def mcp_tool_names(): return site.mcp_tool_names()
@app.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    t = mcp_tool_names()
    return open(os.path.join(ROOT,"llms.txt")).read().replace("{{MCP_TOOL_COUNT}}", str(len(t))).replace("{{MCP_TOOLS}}", ", ".join(t)).replace("{{GPU}}", _llms_gpu()).replace("{{API}}", PUBLIC_API_URL).replace("{{MCP}}", PUBLIC_MCP_URL).replace("{{DOMAIN}}", URL_DOMAIN).replace("{{BRAND}}", BRAND)
def _llms_text(): return llms()
def _regions_md():
    from . import regions as RG
    rows = ["| Region | Location | Status | Provider |", "|---|---|---|---|"] + [f"| `{r['id']}` | {r['name']} | {'live' if r['live'] else 'coming soon (not live)'} | {r['provider']} |" for r in RG.status()]
    return "\n## Regions\n\nGenerated from `GET " + PUBLIC_API_URL + "/v1/regions` (live = has capacity now). New apps, volumes and Postgres are only accepted in live regions; an app's region can't change.\n\n" + "\n".join(rows) + "\n"
_MD_TYPE = "text/markdown; charset=utf-8"
@app.get("/llms-full.txt")
def llms_full():
    from . import llms_full as LF
    site_url = PUBLIC_API_URL.replace("://api.", "://www.", 1)
    body = LF.full(site.docs, BRAND, URL_DOMAIN, PUBLIC_API_URL, PUBLIC_MCP_URL, site_url, _llms_text()) + _regions_md()
    return Response(body, media_type="text/plain; charset=utf-8")
@app.get("/mcp-tools.md")
def mcp_tools_md():
    from . import llms_full as LF
    return Response(LF.mcp_tools_md(BRAND, PUBLIC_MCP_URL, PUBLIC_API_URL), media_type=_MD_TYPE)
@app.get("/mcp-tools.json")
def mcp_tools_json():
    from . import llms_full as LF
    from .mcp_tools import legacy_map
    return {"server": PUBLIC_MCP_URL, "tools": LF.mcp_tools(), "legacy": legacy_map(), "legacy_opt_in": f"{PUBLIC_MCP_URL}?tools=legacy or header X-Aloof-Tools: legacy"}
@app.get("/.well-known/mcp.json")
def mcp_manifest():
    m = json.load(open(os.path.join(ROOT,"mcp_server","manifest.json")))
    m["transports"][0]["url"] = PUBLIC_MCP_URL; m["transports"][1]["env"]["ALOOF_API_URL"] = PUBLIC_API_URL; m["docs"] = f"{PUBLIC_API_URL}/llms.txt"
    m["auth"]["oauth_protected_resource"] = PUBLIC_MCP_URL.rsplit("/mcp",1)[0] + "/.well-known/oauth-protected-resource/mcp"
    m["region"] = "Nuremberg, Germany (backups in Helsinki)"; m["tools"] = mcp_tool_names()   # from the server's registered tools
    return m
from .mcp_tools import mcp_tool_names
@app.get("/v1/config")
def config(): return {"brand": BRAND, "base_domain": BASE_DOMAIN, "api": PUBLIC_API_URL, "mcp": PUBLIC_MCP_URL, "app_url_pattern": f"https://<app>.{URL_DOMAIN}/", "app_domains": APP_DOMAINS,
                       "posthog": ({"key": site.posthog_key(), "host": site.posthog_host()} if site.posthog_key() else None),
                       "posthog_key": analytics.api_key() or None, "posthog_host": analytics.host() if analytics.enabled() else None}   # phc_ project key is public by design
from . import site
@app.get("/", response_class=HTMLResponse)
def landing(): return www.at("/", "default", site.landing, BRAND, URL_DOMAIN, PUBLIC_API_URL, PUBLIC_MCP_URL)
@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(): return www.at("/pricing", "default", site.pricing, BRAND)
@app.get("/docs", response_class=HTMLResponse)
@app.get("/docs/{page}", response_class=HTMLResponse)
def docs_page(page: str = "quickstart"):
    h = www.at("/docs" if page == "quickstart" else f"/docs/{page}", "default", site.docs, page, BRAND, URL_DOMAIN, PUBLIC_API_URL, PUBLIC_MCP_URL)
    if h is None: raise HTTPException(404, "no such docs page")
    return h
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    n = request.query_params.get("next")
    if n is not None and auth.safe_next(n, None) is None: return RedirectResponse("/login", 303)   # F1: drop unsafe next server-side
    if n and auth.session_account(request.cookies.get(auth.COOKIE)): return RedirectResponse(auth.safe_next(n, "/dashboard"), 303)   # already signed in: go where the link pointed
    return www.at("/login", "default", site.login, BRAND)
@app.get("/static/analytics.js", include_in_schema=False)
def static_analytics(): return FileResponse(os.path.join(ROOT, "paas", "static", "analytics.js"), media_type="text/javascript", headers={"Cache-Control": "public, max-age=3600"})
@app.get("/static/aloof.css", include_in_schema=False)
def static_css(): return FileResponse(os.path.join(ROOT, "paas", "static", "aloof.css"), media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})
@app.get("/privacy", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
def legal_page(request: Request): return www.at(request.url.path, "default", site.legal, request.url.path.strip("/"), BRAND)
SWAGGER_V = "5.17.14"; SWAGGER_SRI = {"js": "sha384-wmyclcVGX/WhUkdkATwhaK1X1JtiNrr2EoYJ+diV3vj4v6OC5yCeSu+yW13SYJep", "css": "sha384-wxLW6kwyHktdDGr6Pv1zgm/VGJh99lfUbzSn6HNHBENZlCN7W602k9VkGdxuFvPn"}
@app.get("/static/swagger/{f}", include_in_schema=False)
def swagger_asset(f: str):
    if f not in ("swagger-ui-bundle.js", "swagger-ui.css"): raise HTTPException(404, "Not Found")
    return FileResponse(os.path.join(ROOT, "paas", "static", "swagger", f), media_type="text/css" if f.endswith(".css") else "text/javascript", headers={"Cache-Control": "public, max-age=604800, immutable"})
@app.get("/api/docs", response_class=HTMLResponse, include_in_schema=False)
def api_docs():
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>API reference · {BRAND}</title>
<meta name=description content="Interactive reference for the {BRAND} REST API: apps, deploys, volumes, Postgres, domains, billing and keys. Authenticate with Authorization: Bearer.">
<link rel=icon href="/favicon.ico?v=3" sizes="32x32"><link rel=icon href="/icon.svg?v=3" type="image/svg+xml"><link rel=icon type="image/png" sizes="32x32" href="/favicon-32x32.png?v=3"><link rel=icon type="image/png" sizes="16x16" href="/favicon-16x16.png?v=3"><link rel=apple-touch-icon href="/apple-touch-icon.png?v=3">
<link rel=stylesheet href="/static/swagger/swagger-ui.css?v={SWAGGER_V}" integrity="{SWAGGER_SRI['css']}" crossorigin=anonymous><link rel=stylesheet href="/static/aloof.css?v=23">
<style>body{{margin:0;background:#fff}}.topbar{{display:none}}.abar{{display:flex;align-items:center;gap:14px;padding:14px 24px;background:#0a0807;color:#f6f1ec}}.abar .logo{{color:#f6f1ec!important}}.abar a{{color:#a9b0c3}}.abar .sp{{flex:1}}</style></head>
<body><div class=abar><a class=logo href="/"><i></i>{BRAND}</a><span style="color:#998b7e">API reference</span><span class=sp></span><a href="/docs/api">Guide</a><a href="/dashboard">Dashboard</a></div><div id=swagger-ui></div>
<script src="/static/swagger/swagger-ui-bundle.js?v={SWAGGER_V}" integrity="{SWAGGER_SRI['js']}" crossorigin=anonymous></script>
<script>SwaggerUIBundle({{url:"/api/openapi.json",dom_id:"#swagger-ui",deepLinking:true,persistAuthorization:false}})</script></body></html>"""
@app.get("/dashboard", response_class=HTMLResponse)
def dash(request: Request):
    a = auth.session_account(request.cookies.get(auth.COOKIE))
    if not a: return RedirectResponse("/login?next=/dashboard", 303)
    h = open(os.path.join(ROOT,"dashboard","index.html")).read().replace("{{BRAND}}", BRAND).replace("<!--ANALYTICS-->", "" if a.get("impersonated_by") else site.analytics_tag())
    if a.get("impersonated_by"): from .admin_ops import imp_banner; h = h.replace("<body>", "<body>" + imp_banner(a), 1)
    return HTMLResponse(h, headers={"X-Robots-Tag": "noindex, nofollow"})


# ---------------- Fly parity: releases/rollback, managed Postgres, snapshots, exec/console, log drains/history, audit ----------------
from . import parity, selfheal
from .db import audit as _audit
def _ip(request): return client_ip(request) if request.client else None
@app.get("/v1/apps/{name}/releases")
def app_releases(name: str, a=Depends(acct)): own(name, a); return engine.releases(name)
class SelfHealReq(BaseModel): enabled: bool
@app.get("/v1/apps/{name}/self-heal")
def app_selfheal_get(name: str, a=Depends(acct)): own(name, a); return selfheal.status(name)
@app.put("/v1/apps/{name}/self-heal")
def app_selfheal_put(name: str, b: SelfHealReq, request: Request, a=Depends(acct)):
    """Turn automatic rollback after a bad deploy on or off for this app (default on)."""
    own(name, a); selfheal.set_enabled(name, b.enabled)
    _audit(a["id"], name, "self-heal", "on" if b.enabled else "off", _ip(request)); return selfheal.status(name)
@app.get("/v1/apps/{name}/diagnose")
def app_diagnose(name: str, a=Depends(acct)):
    """Read-only diagnosis: recent errors, failed health checks, deploys/rollbacks/restarts, instance states and exit codes,
    and rule-based likely causes with suggested fixes. Secrets and env values are redacted."""
    own(name, a); from . import diagnose; return diagnose.diagnose(name) | (engine.release_state(name) or {})
class RollbackReq(BaseModel): version: int|None=None
@app.post("/v1/apps/{name}/rollback")
def app_rollback(name: str, request: Request, b: RollbackReq|None=None, a=Depends(acct)):
    """Redeploy an earlier release (default: the previous one) with zero downtime. No rebuild."""
    own(name, a)
    try: r = engine.rollback(name, b.version if b else None)
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], name, "rollback", f"to v{b.version}" if b and b.version else "previous", _ip(request)); return with_url(r)
# managed Postgres
class PgReq(BaseModel): name: str; plan: str="pg-dev"; region: str|None=None
def own_pg(n, a):
    d = q("select * from pg_dbs where name=?", (n,), one=True)
    if not d or d["account_id"] != a["id"]: raise HTTPException(404, "database not found")
    return d
@app.get("/v1/pg/plans")
def pg_plans(): return {"plans": [{"id": k} | {x: y for x, y in v.items() if x != "margin"} for k, v in _plans.PG_PLANS.items()],
                        "backups": {"schedule": "daily", "keep": parity.PG_KEEP, "usd_gb_month": RATES["backup_usd_gb_month"]}}
@app.post("/v1/pg")
def pg_create(b: PgReq, request: Request, a=Depends(acct)):
    try: r = parity.pg_create(a["id"], b.name, b.plan, b.region)
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], f"pg-{b.name}", "pg.create", b.plan, _ip(request)); return r
@app.get("/v1/pg")
def pg_list(a=Depends(acct)): return [parity.pg_view(d) for d in q("select * from pg_dbs where account_id=?", (a["id"],))]
@app.get("/v1/pg/{name}")
def pg_get(name: str, reveal: bool=False, request: Request=None, a=Depends(acct)):
    d = own_pg(name, a)
    if reveal: _audit(a["id"], d["app"], "pg.reveal_url", "", _ip(request))
    return parity.pg_view(d, reveal)
class PgAttach(BaseModel): app: str; var: str="DATABASE_URL"
@app.post("/v1/pg/{name}/attach")
def pg_attach(name: str, b: PgAttach, request: Request, a=Depends(acct)):
    own_pg(name, a); own(b.app, a)
    try: r = parity.pg_attach(name, b.app, b.var)
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], b.app, "pg.attach", f"{name} as ${b.var}", _ip(request)); return with_url(r)
@app.delete("/v1/pg/{name}/attach/{app_name}")
def pg_detach(name: str, app_name: str, a=Depends(acct)): own_pg(name, a); own(app_name, a); return with_url(parity.pg_detach(name, app_name))
@app.post("/v1/pg/{name}/backups")
def pg_backup(name: str, request: Request, a=Depends(acct)):
    own_pg(name, a)
    try: r = parity.pg_backup(name)
    except (ValueError, RuntimeError) as e: raise HTTPException(409, str(e))
    _audit(a["id"], f"pg-{name}", "pg.backup", r["id"], _ip(request)); return r
@app.get("/v1/pg/{name}/backups")
def pg_backups(name: str, a=Depends(acct)): own_pg(name, a); return parity.pg_view(own_pg(name, a))["backups"]
class RestoreReq(BaseModel): backup_id: str
@app.post("/v1/pg/{name}/restore")
def pg_restore(name: str, b: RestoreReq, request: Request, a=Depends(acct)):
    own_pg(name, a)
    try: r = parity.pg_restore(name, b.backup_id)
    except (ValueError, RuntimeError) as e: raise HTTPException(409, str(e))
    _audit(a["id"], f"pg-{name}", "pg.restore", b.backup_id, _ip(request)); return r
@app.delete("/v1/pg/{name}")
def pg_delete(name: str, request: Request, keep_backups: bool=False, a=Depends(acct)):
    own_pg(name, a); parity.pg_destroy(name, keep_backups); _audit(a["id"], f"pg-{name}", "pg.destroy", "", _ip(request)); return {"ok": True}
# volume snapshots
@app.post("/v1/volumes/{vol}/snapshots")
def snap_create(vol: str, request: Request, a=Depends(acct)):
    own_vol(vol, a)
    try: r = parity.snapshot_create(vol)
    except NotImplementedError as e: raise HTTPException(501, str(e))
    _audit(a["id"], None, "snapshot.create", f"{vol} {r['id']}", _ip(request)); return r
@app.get("/v1/volumes/{vol}/snapshots")
def snap_list(vol: str, a=Depends(acct)):
    own_vol(vol, a); return [parity._arc_view(r) for r in q("select * from archives where kind='snap' and owner=? order by created desc", (vol,))]
class SnapRestore(BaseModel): snapshot_id: str
@app.post("/v1/volumes/{vol}/restore")
def snap_restore(vol: str, b: SnapRestore, request: Request, a=Depends(acct)):
    own_vol(vol, a)
    try: r = parity.snapshot_restore(vol, b.snapshot_id)
    except ValueError as e: raise HTTPException(404, str(e))
    _audit(a["id"], None, "snapshot.restore", f"{vol} {b.snapshot_id}", _ip(request)); return r
@app.delete("/v1/volumes/{vol}/snapshots/{sid}")
def snap_delete(vol: str, sid: str, a=Depends(acct)):
    own_vol(vol, a); r = q("select * from archives where id=? and kind='snap' and owner=?", (sid, vol), one=True)
    if not r: raise HTTPException(404, "snapshot not found")
    parity.delete_archive(r); return {"ok": True}
# exec + interactive console (WebSocket), audited
class ExecReq(BaseModel): command: list[str]|str; instance: str|None=None
@app.post("/v1/apps/{name}/exec")
def app_exec(name: str, b: ExecReq, request: Request, a=Depends(acct)):
    own(name, a)
    try: return parity.exec_cmd(a["id"], name, b.command, _ip(request), b.instance)
    except NotImplementedError as e: raise HTTPException(501, str(e))
    except ValueError as e: raise HTTPException(409, str(e))
_SITE_ORIGINS = None
def _ws_account(ws: WebSocket):
    """Bearer API key (CLI), or the session cookie + an Origin on one of our own hosts (blocks cross-site WebSocket hijacking)."""
    key = (ws.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if key: return engine.account_by_key(key)
    origin = (ws.headers.get("origin") or "").lower(); host = origin.split("://", 1)[-1].split(":")[0]
    ok_hosts = SYSTEM_HOSTS | {"localhost", "127.0.0.1"} | {h.split("://", 1)[-1].split("/")[0] for h in (PUBLIC_API_URL, PUBLIC_PROXY, os.environ.get("PAAS_SITE_URL", ""))}
    if host not in ok_hosts: return None
    a = auth.session_account(ws.cookies.get(auth.COOKIE))
    return None if a and (a.get("impersonated_by") or a.get("suspended")) else a   # no consoles/streams in the admin's read-only view
@app.websocket("/v1/apps/{name}/console")
async def app_console(ws: WebSocket, name: str, instance: str|None=None):
    """Interactive shell in a running instance. Binary frames carry terminal bytes both ways; a text frame {"resize":[rows,cols]} resizes.
    Session open/close and each entered line are written to the audit log."""
    # Accept first, then close with a code + reason: closing before accept() turns into an HTTP 403 and the browser only sees 1006.
    a = _ws_account(ws); ap = engine.get_app(name); await ws.accept()
    if not a or not ap or ap["account_id"] != a["id"] or ap["status"] == "destroyed":
        await ws.close(code=4401, reason="not signed in, or no such app on this account"); return
    # same checks as REST exec (POST /v1/apps/{name}/exec): app-scoped deploy tokens only reach their app, and the caller needs write
    if a.get("_token") and not scope_ok(name, a):
        await ws.close(code=4403, reason=f"this deploy token is scoped to app {a['_scope_app']}"); return
    if caller_level(ws, a) < 2:
        await ws.close(code=4403, reason="insufficient scope: the console needs paas:write"); return
    if ap["status"] in ("suspended", "capped"):
        await ws.close(code=4409, reason="app is " + ("suspended (balance at $0): top up to resume" if ap["status"] == "suspended" else "paused at the Free plan's monthly cap")); return
    sel = lambda: q("select * from instances where app=? and status='running'" + (" and id=?" if instance else "") + " order by started", (name, *([instance] if instance else [])), one=True)
    i = sel()
    if not i and not instance and ap["status"] in ("running", "idle"):   # scaled to zero: wake it like an incoming request would
        await ws.send_bytes(b"Waking the app (it was scaled to zero)...\r\n")
        try: await asyncio.to_thread(controller.cold_start, name)
        except Exception as e: log.warning("console wake failed for %s: %s", name, e)
        i = sel()
    drv = get_drivers().get(i["provider"]) if i else None
    if not i or not hasattr(drv, "exec_socket"):
        await ws.close(code=4409, reason="no running instance" if not i else "console isn't supported on this node"); return
    ip = ws.client.host if ws.client else None; t0 = time.time()
    try: eid, sock = await asyncio.to_thread(drv.exec_socket, i["id"])
    except Exception as e:
        log.warning("console exec failed for %s: %s", name, e); await ws.close(code=1011, reason="couldn't start a shell in the instance"); return
    controller.M.begin(name)   # an open console counts as activity, so scale-to-zero doesn't kill the session
    _audit(a["id"], name, "console.open", f"instance {i['id']}", ip)
    async def pump():
        while True:
            data = await asyncio.to_thread(sock.recv, 65536)
            if not data: break
            await ws.send_bytes(data)
        await ws.close()
    task = asyncio.create_task(pump()); line = bytearray(); n = 0
    try:
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect": break
            if m.get("text"):
                try: c = json.loads(m["text"])
                except Exception: c = {"data": m["text"]}
                if "resize" in c: drv.exec_resize(eid, *c["resize"][:2]); continue
                data = str(c.get("data", "")).encode()
            else: data = m.get("bytes") or b""
            await asyncio.to_thread(sock.sendall, data); n += len(data)
            for ch in data:
                if ch in (10, 13):
                    if line.strip(): _audit(a["id"], name, "console.input", line.decode(errors="replace")[:500], ip)
                    line.clear()
                elif ch in (8, 127): line[-1:] = b""
                elif ch >= 32: line.append(ch)
    except Exception: pass
    finally:
        task.cancel()
        try: sock.shutdown(socket.SHUT_RDWR)   # wakes the reader thread blocked in recv()
        except Exception: pass
        try: sock.close()
        except Exception: pass
        controller.M.end(name)
        _audit(a["id"], name, "console.close", f"{time.time()-t0:.0f}s, {n} bytes in", ip)
@app.get("/v1/audit")
def audit_log(limit: int=100, app_name: str|None=None, a=Depends(acct)):
    return q("select ts, app, action, detail, ip from audit where account_id=?" + (" and app=?" if app_name else "") + " order by id desc limit ?",
             (a["id"], *([app_name] if app_name else []), min(limit, 1000)))
# logs: retention + drains
@app.get("/v1/apps/{name}/logs/history")
def logs_history(name: str, since: float=0, limit: int=1000, contains: str|None=None, a=Depends(acct)):
    own(name, a); t = TIERS.get(a.get("tier") or "free", TIERS["free"])
    return {"retention_days": t["log_retention_days"], "lines": parity.history(name, max(since, time.time() - t["log_retention_days"] * 86400), min(limit, 10000), contains)}
class DrainReq(BaseModel): url: str
@app.post("/v1/apps/{name}/drains")
def drain_add(name: str, b: DrainReq, request: Request, a=Depends(acct)):
    own(name, a)
    try: r = parity.drain_add(a["id"], name, b.url)
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], name, "drain.add", r["url"], _ip(request)); return r
@app.get("/v1/apps/{name}/drains")
def drain_list(name: str, a=Depends(acct)): own(name, a); return [parity.drain_view(d) for d in q("select * from drains where app=?", (name,))]
@app.delete("/v1/apps/{name}/drains/{did}")
def drain_del(name: str, did: str, a=Depends(acct)): own(name, a); q("delete from drains where id=? and app=?", (did, name)); return {"ok": True}

# ---- off-site backups (S3, client-side encrypted) ----
from . import offsite
def _need_offsite():
    if not offsite.enabled(): raise HTTPException(503, "off-site backups are not configured on this server yet")
@app.get("/v1/pg/{name}/offsite")
def pg_offsite_list(name: str, a=Depends(acct)): own_pg(name, a); return offsite.list_("pg", name)
class OffsiteReq(BaseModel): backup_id: str
@app.post("/v1/pg/{name}/offsite-restore")
def pg_offsite_restore(name: str, b: OffsiteReq, request: Request, a=Depends(acct)):
    own_pg(name, a); _need_offsite()
    try: r = offsite.restore_pg(name, b.backup_id)
    except (ValueError, RuntimeError) as e: raise HTTPException(409, str(e))
    _audit(a["id"], f"pg-{name}", "pg.offsite_restore", b.backup_id, _ip(request)); return r
@app.get("/v1/volumes/{vol}/offsite")
def vol_offsite_list(vol: str, a=Depends(acct)): own_vol(vol, a); return offsite.list_("vol", vol)
@app.post("/v1/volumes/{vol}/offsite-restore")
def vol_offsite_restore(vol: str, b: OffsiteReq, request: Request, a=Depends(acct)):
    own_vol(vol, a); _need_offsite()
    try: r = offsite.restore_vol(vol, b.backup_id)
    except (ValueError, RuntimeError) as e: raise HTTPException(409, str(e))
    _audit(a["id"], None, "snapshot.offsite_restore", f"{vol} {b.backup_id}", _ip(request)); return r
@app.get("/v1/admin/backups", dependencies=[Depends(admin)])
def admin_backups(): return offsite.status() | {"objects_list": [offsite.view(r) | {"owner": r["owner"]} for r in q("select * from offsite order by created desc limit 200")]}
@app.post("/v1/admin/backups/run", dependencies=[Depends(admin)])
def admin_backups_run(verify: bool=False):
    _need_offsite(); r = offsite.backup_all(force=True)
    if verify: r["verify"] = offsite.verify_all(force=True)
    return r
from . import devx
app.include_router(devx.R)
app.include_router(www.R)
from . import templating as _tpl; app.include_router(_tpl.R)
from . import waitlist as _wl; app.include_router(_wl.R)
from . import admin_ops as _aops; app.include_router(_aops.R)
try: _wl.migrate_kv()
except Exception as _e: logging.getLogger('paas').error('waitlist kv migration failed: %s', _e)
# ---------------- edge proxy (port 8080): <app>.BASE_DOMAIN or /_app/<app>/ ----------------
from . import lb
proxy = FastAPI(); _rr = {}; client = httpx.AsyncClient(timeout=60)
EGRESS = billing.EGRESS   # app -> bytes sent to clients since last meter tick
HOP = {"connection","keep-alive","transfer-encoding","te","trailer","upgrade","proxy-authorization","proxy-authenticate","host","content-length"}
def resolve_app(host, path):
    if _app_of(host): return _app_of(host), path
    d = q("select app from domains where domain=? and verified=1", (host,), one=True)
    if d: return d["app"], path
    if path.startswith("_app/"):
        _, name, *rest = path.split("/", 2)
        if name.startswith("pg-"): return None, path
        return name, (rest[0] if rest else "")
    return None, path
def _sh_record(name, status, t0, request=None, path="/"):
    try:
        ip = client_ip(request) if request else None
        selfheal.record(name, status, (time.monotonic() - t0) * 1000, ip, path)   # time to response headers
    except Exception: pass
@proxy.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def edge(path: str, request: Request):
    host = request.headers.get("host","").split(":")[0].lower(); name, path = resolve_app(host, path)
    if not name:
        tgt = pair_target(host)
        if tgt:
            qs = f"?{request.url.query}" if request.url.query else ""
            return Response(status_code=308, headers={"location": f"https://{tgt}{request.url.path or '/'}{qs}", "cache-control": "max-age=300"})
    if not name: return PlainTextResponse(f"{BRAND} edge: use https://<app>.{URL_DOMAIN}/, a verified custom domain, or /_app/<app>/", 404)
    insts = q("select id,host,port from instances where app=? and status='running'", (name,))
    if not insts:
        ap = q("select spec,status from apps where name=?", (name,), one=True)
        if ap and ap["status"] in ("idle","running") and json.loads(ap["spec"]).get("autoscale"):
            controller.M.begin(name)   # count the waiting request so the autoscaler doesn't scale us back down
            try: await asyncio.to_thread(controller.cold_start, name)
            except capacity.PlatformAtCapacity as e: return PlainTextResponse(str(e), 503, headers={"Retry-After": str(e.retry_after)})
            finally: controller.M.end(name)
            insts = q("select id,host,port from instances where app=? and status='running'", (name,))
        if not insts and ap and ap["status"] == "capped": return PlainTextResponse(f"app '{name}' is paused: free tier usage cap reached for this month", 402)
        if not insts: return PlainTextResponse(f"app '{name}' has no running instances", 503)
    i = lb.pick(name, insts); tried = set()
    qs = f"?{request.url.query}" if request.url.query else ""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    hdrs = {k:v for k,v in request.headers.items() if k.lower() not in HOP} | {"x-forwarded-host": host, "x-forwarded-proto": proto}
    held = {"m": True, "lb": None}
    def release():   # idempotent: every exit path (errors, cancellation, finished or abandoned body) releases its counters once
        if held["lb"]: lb.end(held["lb"]); held["lb"] = None
        if held["m"]: controller.M.end(name); held["m"] = False
    controller.M.begin(name); r = None; err = None; t0 = time.monotonic()
    try:
        body_bytes = await request.body()
        for attempt in range(3):
            url = f"http://{i['host']}:{i['port']}/{path}" + qs
            lb.begin(i["id"]); held["lb"] = i["id"]
            try:
                r = await client.send(client.build_request(request.method, url, headers=hdrs, content=body_bytes), stream=True); break
            except httpx.ConnectError as e:
                err = e; lb.end(i["id"]); held["lb"] = None; lb.failure(name, i["id"], insts, "connection refused"); tried.add(i["id"])
                # instance restarting / port remapped: refresh endpoints and retry on the least-loaded other instance
                await asyncio.sleep(0.5 * attempt)
                await asyncio.to_thread(controller.health_tick)
                insts = q("select id,host,port from instances where app=? and status='running'", (name,)) or insts
                i = lb.pick(name, insts, exclude=tried)
            except httpx.HTTPError as e:
                err = e; lb.end(i["id"]); held["lb"] = None
                if isinstance(e, httpx.TimeoutException): lb.failure(name, i["id"], insts, "timeout")
                break
        if r is None:
            release()
            if isinstance(err, httpx.ConnectError): _sh_record(name, 502, t0, request, path)   # timeouts / client aborts don't count
            return PlainTextResponse(f"upstream error: {err}", 502)
        _sh_record(name, r.status_code, t0, request, path)
        if r.status_code in lb.EJECT_STATUSES: lb.failure(name, i["id"], insts, f"HTTP {r.status_code}")
        elif r.status_code < 500: lb.success(i["id"])
    except BaseException:
        release()
        if r is not None: await r.aclose()
        raise
    async def body():
        try:
            async for chunk in r.aiter_raw():
                EGRESS[name] = EGRESS.get(name, 0) + len(chunk); yield chunk
        finally:
            try: await r.aclose()
            finally: release()
    gen = body(); weakref.finalize(gen, release)   # body never iterated (client gone before streaming): release on GC
    from starlette.background import BackgroundTask
    return StreamingResponse(gen, status_code=r.status_code, background=BackgroundTask(release),
                             headers={k:v for k,v in r.headers.items() if k.lower() not in HOP} | {"x-paas-instance": str(i["port"])})

# Branded 404 for browsers; API/JSON clients keep {"detail": ...}
from starlette.exceptions import HTTPException as _StarHTTP
from fastapi.exception_handlers import http_exception_handler as _default_http
@app.exception_handler(_StarHTTP)
async def _http_exc(request: Request, exc):
    p = request.url.path
    if exc.status_code == 404 and ("text/html" in request.headers.get("accept", "") or request.headers.get("sec-fetch-mode") == "navigate") and not p.startswith(("/v1/", "/api/", "/mcp", "/.well-known", "/_app/")):
        return HTMLResponse(site.not_found(BRAND), 404)
    return await _default_http(request, exc)

# HEAD wherever GET works: run the GET handler, drop the body (Starlette routes only register GET)
class _HeadAsGet:
    def __init__(self, a): self.a = a
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "HEAD": return await self.a(scope, receive, send)
        scope = dict(scope, method="GET")
        async def snd(m):
            if m["type"] == "http.response.body": m = {"type": "http.response.body", "body": b"", "more_body": m.get("more_body", False)}
            await send(m)
        await self.a(scope, receive, snd)
app.add_middleware(_HeadAsGet)
