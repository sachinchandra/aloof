import os, json, itertools, httpx, time, asyncio, socket
from fastapi import FastAPI, Header, HTTPException, Request, Depends, Cookie, WebSocket
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse, StreamingResponse, Response, RedirectResponse, JSONResponse
from pydantic import BaseModel
from . import engine
from .db import q
from .plans import PLANS, REGIONS, REGION_NOTE, TIERS, RATES
from . import plans as _plans
from .drivers import get_drivers
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
def acct(request: Request, authorization: str = Header(None)):
    """Bearer API key (CLI/MCP/automation) or session cookie (dashboard). Cookie-authenticated writes need the
    X-Aloof-CSRF header, which cross-site forms cannot set (SameSite=Lax is the first line of defence)."""
    key = (authorization or "").removeprefix("Bearer ").strip()
    if key:
        a = engine.account_by_key(key)
        if not a: raise HTTPException(401, "invalid API key")
        if a.get("_token"): scope_check(request.url.path, a)
        return a
    a = auth.session_account(request.cookies.get(auth.COOKIE))
    if not a: raise HTTPException(401, "not authenticated: send Authorization: Bearer pk_... or log in at /login")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-aloof-csrf") != "1": raise HTTPException(403, "missing X-Aloof-CSRF header")
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
class Signup(BaseModel): email: str
class Source(BaseModel):
    image: str|None=None; git: str|None=None; ref: str|None=None; subdir: str|None=None; path: str|None=None
    upload: str|None=None; dockerfile: str|None=None; build_args: dict[str,str]|None=None; github_installation: int|None=None
class DeployReq(BaseModel):
    name: str; source: Source; plan: str="shared-1x-1gb"; port: int=8080; env: dict[str,str]={}; instances: int=1
    command: list[str]|None=None; region: str="local"; provider: str|None=None
    volumes: list[dict]=[]; autoscale: dict|None=None; health: dict|None=None
    processes: dict[str, str|list[str]]|None=None; process_counts: dict[str,int]|None=None; cron: list[dict]|None=None; release_command: str|list[str]|None=None
@app.post("/v1/signup")
def signup(s: Signup):
    """API-only signup (CLI/MCP): returns an API key once. Web users register with a password or magic link."""
    try: e = auth._check_email(s.email)
    except ValueError as x: raise HTTPException(400, str(x))
    if q("select 1 from accounts where email=?", (e,), one=True): raise HTTPException(409, "exists")
    a = engine.create_account(e); return {"email":a["email"],"api_key":a["api_key"],"credit_usd":billing.usd(a.get("credit_cents"))}
@app.get("/v1/plans")
def plans(legacy: bool = False):
    """Pricing v3 (USD excl. tax/VAT): per-second plans, usage rates, workspace tiers and regions."""
    """GPU plans are only listed once GPUs are enabled (phase 2); until then `gpu` says they're coming soon, with the waitlist."""
    out = [{"id": k, **{x: y for x, y in v.items() if x not in ("providers",)}} for k, v in PLANS.items()
           if (legacy or not v["legacy"]) and (v["kind"] != "gpu" or _plans.GPU_ENABLED)]
    gpu = _plans.gpu_status() | ({} if _plans.GPU_ENABLED else {"coming_soon": [{"id": k, "gpu": v["gpu"]} for k, v in PLANS.items() if v["kind"] == "gpu"]})
    return {"currency": "USD", "tax": "excluded; VAT/GST/sales tax added at checkout where applicable", "billing": "per second while running; volumes per provisioned GB-month; egress per GB (counts toward included usage)",
            "plans": out, "gpu": gpu, "rates": RATES, "tiers": TIERS, "regions": REGIONS, "region_note": REGION_NOTE}
class WaitlistReq(BaseModel): email: str; gpu: str | None = None; note: str | None = None; kind: str = "gpu"; region: str | None = None; company: str | None = None
@app.post("/v1/waitlist")
def waitlist(w: WaitlistReq):
    """Waitlists: kind=gpu (phase 2 GPUs), kind=region (a coming-soon region), kind=design_partner. Idempotent per email (and region)."""
    try: e = auth._check_email(w.email)
    except ValueError as x: raise HTTPException(400, str(x))
    from .db import kv_put
    from .www_content import SOON_REGIONS
    if w.kind == "region":
        codes = {c: l for c, l, _, _ in SOON_REGIONS}
        if w.region not in codes: raise HTTPException(400, f"region must be one of {list(codes)}")
        kv_put("region_waitlist", f"{e}|{w.region}", json.dumps({"region": w.region, "ts": time.time()}))
        return {"ok": True, "message": f"You're on the waitlist for {codes[w.region]}. We'll email you once, when it's live."}
    if w.kind == "design_partner":
        kv_put("design_partner", e, json.dumps({"company": (w.company or "")[:200], "note": (w.note or "")[:1000], "ts": time.time()}))
        return {"ok": True, "message": "Thanks. We read every application and will reply by email."}
    if w.kind != "gpu": raise HTTPException(400, "kind must be gpu, region or design_partner")
    if w.gpu and (w.gpu not in PLANS or PLANS[w.gpu]["kind"] != "gpu"): raise HTTPException(400, "gpu must be a GPU plan id")
    kv_put("gpu_waitlist", e, json.dumps({"gpu": w.gpu, "note": (w.note or "")[:500], "ts": time.time()}))
    return {"ok": True, "message": "You're on the GPU waitlist. We'll email you when GPUs launch."}
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
def get_app(name: str, a=Depends(acct)): return with_url(own(name, a))
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
def delete(name: str, a=Depends(acct)): own(name, a); engine.destroy(name); return {"ok":True}
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
    own(name, a); return with_url(engine.update_spec(name, lambda s: s.__setitem__("health", h.model_dump())))
# ---- volumes ----
class VolReq(BaseModel): name: str; size_gb: int=1
@app.post("/v1/volumes")
def vol_create(v: VolReq, a=Depends(acct)):
    try: return engine.create_volume(a["id"], v.name, v.size_gb)
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
@app.get("/v1/volumes")
def vol_list(a=Depends(acct)): return q("select name,size_gb,app,mount_path,node_id,created from volumes where account_id=?", (a["id"],))
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
# ---- custom domains (TXT ownership + routing) + on-demand TLS gate ----
EDGE_IPS = [x for x in os.environ.get("PAAS_EDGE_IPS", "127.0.0.1").split(",") if x]
class DomainReq(BaseModel): domain: str
def _domain_view(d):
    return {"domain": d["domain"], "app": d["app"], "verified": bool(d["verified"]), "owned": bool(d["owned"]), "routed": bool(d["routed"]),
            "dns_records": doms.instructions(d["domain"], d["app"], BASE_DOMAIN, d["txt_token"], EDGE_IPS), "https": f"https://{d['domain']}/"}
def _verify(d):
    r = doms.check(d["domain"], d["app"], BASE_DOMAIN, d["txt_token"], EDGE_IPS)
    q("update domains set owned=?, routed=?, verified=? where domain=?", (int(r["owned"]), int(r["routed"]), int(r["verified"]), d["domain"]))
    return _domain_view(q("select * from domains where domain=?", (d["domain"],), one=True)) | {"detail": r["detail"]}
@app.post("/v1/apps/{name}/domains")
def add_domain(name: str, d: DomainReq, a=Depends(acct)):
    own(name, a); dom = d.domain.lower().strip().strip(".")
    if not doms_ok(dom): raise HTTPException(400, "invalid domain")
    if dom == BASE_DOMAIN or dom.endswith("." + BASE_DOMAIN): raise HTTPException(400, f"subdomains of {BASE_DOMAIN} are assigned automatically")
    ex = q("select * from domains where domain=?", (dom,), one=True)
    if ex and ex["account_id"] != a["id"]: raise HTTPException(409, "domain claimed by another account")
    if not ex: q("insert into domains(domain,app,account_id,verified,created,txt_token,owned,routed) values(?,?,?,0,?,?,0,0)", (dom, name, a["id"], time.time(), doms.new_token()))
    else: q("update domains set app=? where domain=?", (name, dom))
    return _verify(q("select * from domains where domain=?", (dom,), one=True))
import re as _re
def doms_ok(d): return bool(_re.match(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", d))
@app.post("/v1/apps/{name}/domains/{domain}/verify")
def verify_domain(name: str, domain: str, a=Depends(acct)):
    own(name, a); d = q("select * from domains where domain=? and app=?", (domain, name), one=True)
    if not d: raise HTTPException(404, "domain not found")
    return _verify(d)
@app.get("/v1/apps/{name}/domains")
def list_domains(name: str, a=Depends(acct)): own(name, a); return [_domain_view(d) for d in q("select * from domains where app=?", (name,))]
@app.delete("/v1/apps/{name}/domains/{domain}")
def del_domain(name: str, domain: str, a=Depends(acct)): own(name, a); q("delete from domains where domain=? and app=?", (domain, name)); return {"ok": True}
SYSTEM_HOSTS = {API_HOST, MCP_HOST} | {h for d in APP_DOMAINS for h in (d, f"www.{d}", f"api.{d}", f"mcp.{d}")}
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
    raise HTTPException(403, "unknown domain")
def _wallet(a): return {"credit_cents": round(a.get("credit_cents") or 0, 4), "credit_usd": billing.usd(a.get("credit_cents"))}
@app.get("/v1/usage")
def usage(a=Depends(acct)): return engine.usage(a["id"]) | _wallet(a) | {"month": billing.month_summary(a["id"])}
# ---- Stripe (primary) ----
class CheckoutReq(BaseModel): credit_usd: float
@app.post("/v1/billing/checkout")
def billing_checkout(o: CheckoutReq, a=Depends(acct)):
    """Stripe Checkout for a prepaid USD top-up ($10–$5,000). Returns {url}; the wallet is credited by the webhook."""
    try: return stripe_billing.create_topup_session(a, o.credit_usd)
    except ValueError as e: raise HTTPException(400, str(e))
    except RuntimeError as e: raise HTTPException(503, str(e))
class SubReq(BaseModel): tier: str
@app.post("/v1/billing/subscribe")
def billing_subscribe(o: SubReq, a=Depends(acct)):
    """Stripe Checkout (subscription) for the Hobby or Pro tier; the tier switches when Stripe confirms."""
    if o.tier in TIERS and not TIERS[o.tier]["fee_usd"]: raise HTTPException(400, "Free needs no subscription; cancel in the customer portal")
    try: return stripe_billing.create_subscription_session(a, o.tier)
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
@app.get("/v1/billing")
def billing_summary(a=Depends(acct)):
    a = q("select * from accounts where id=?", (a["id"],), one=True)
    return _wallet(a) | {"currency": "USD", "stripe": stripe_billing.configured(), "stripe_tax": stripe_billing.tax_enabled(), "topup_min_usd": stripe_billing.MIN_TOPUP, "topup_max_usd": stripe_billing.MAX_TOPUP, "rates": RATES, "razorpay": rzp.enabled(),
            "subscription": a.get("stripe_subscription"), "month": billing.month_summary(a["id"]), "tiers": TIERS,
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
class MagicReq(BaseModel): email: str
def _login_response(a, body=None):
    r = JSONResponse(body or {"ok": True, "email": a["email"]}); r.set_cookie(auth.COOKIE, auth.new_session(a["id"]), max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    return r
@app.post("/v1/auth/register")
def auth_register(c: Creds):
    try: a = auth.register(c.email, c.password)
    except ValueError as e: raise HTTPException(400, str(e))
    except PermissionError as e: raise HTTPException(409, str(e))
    return _login_response(a, {"ok": True, "email": a["email"], "api_key": a["api_key"]})
# Soft login rate limit (in-memory, per process): LOGIN_LIMIT attempts per LOGIN_WINDOW s per client IP and per email -> 429 + Retry-After.
LOGIN_LIMIT = int(os.environ.get("PAAS_LOGIN_LIMIT", "10")); LOGIN_WINDOW = 60.0
TRUSTED_PROXIES = {x.strip() for x in os.environ.get("PAAS_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if x.strip()}
_login_hits = {}
def client_ip(request: Request):
    """Peer address; X-Forwarded-For is honoured only when the peer is our own proxy (Caddy appends the real client last)."""
    peer = request.client.host if request.client else ""
    if peer in TRUSTED_PROXIES:
        xff = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
        if xff: return xff[-1]
    return peer
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
    _rate_check(("ip:" + client_ip(request), "email:" + (c.email or "").strip().lower()))
    try: return _login_response(auth.login(c.email, c.password))
    except PermissionError as e: raise HTTPException(401, str(e))
@app.post("/v1/auth/magic")
def auth_magic(m: MagicReq, request: Request):
    try: auth.start_magic(m.email, PUBLIC_API_URL if SECURE_COOKIES else str(request.base_url))
    except ValueError as e: raise HTTPException(400, str(e))
    return {"ok": True, "detail": "If the address is valid, a sign-in link is on its way (valid 15 minutes)."}
@app.get("/auth/magic")
def auth_magic_finish(token: str):
    try: a = auth.finish_magic(token)
    except PermissionError as e: return HTMLResponse(f"<p>{e}. <a href='/login'>Request a new link</a></p>", 400)
    r = RedirectResponse("/dashboard", 303); r.set_cookie(auth.COOKIE, auth.new_session(a["id"]), max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    return r
@app.post("/v1/auth/logout")
def auth_logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE)); r = JSONResponse({"ok": True}); r.delete_cookie(auth.COOKIE, path="/"); return r
@app.get("/v1/auth/me")
def auth_me(a=Depends(acct)):
    return {"id": a["id"], "email": a["email"], "email_verified": bool(a["email_verified"]), "has_password": bool(a.get("password_hash")),
            "api_key_prefix": a.get("api_key_prefix"), **_wallet(a)}
@app.post("/v1/auth/api-key")
def auth_rotate(a=Depends(acct)): return {"api_key": engine.rotate_api_key(a["id"]), "detail": "previous key revoked; shown once"}
class KeyReq(BaseModel): label: str
@app.get("/v1/auth/keys")
def auth_keys(a=Depends(acct)):
    """Full-access API keys: the primary key (rotate-only) plus named extra keys. Scoped deploy tokens live at /v1/tokens."""
    ac = q("select id,api_key_prefix,created from accounts where id=?", (a["id"],), one=True)
    return [{"id": "primary", "label": "Default key", "prefix": ac["api_key_prefix"], "created": ac["created"], "last_used": None, "kind": "primary", "scope": "full access"}] + \
           [{"id": r["id"], "label": r["label"], "prefix": r["prefix"], "created": r["created"], "last_used": r["last_used"], "kind": "named", "scope": "full access"}
            for r in q("select * from api_keys where account_id=? order by created desc", (a["id"],))]
@app.post("/v1/auth/keys")
def auth_key_create(b: KeyReq, request: Request, a=Depends(acct)):
    label = (b.label or "").strip()[:60]
    if not label: raise HTTPException(400, "name the key")
    if label == "mcp-oauth": raise HTTPException(400, "that name is reserved")
    k = engine.create_named_key(a["id"], label, replace=False); _audit(a["id"], None, "key.create", label, _ip(request))
    return {"api_key": k, "prefix": k[:10], "label": label, "detail": "shown once"}
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
         "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"), "/site.webmanifest": ("site.webmanifest", "application/manifest+json")}
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
def admin(authorization: str = Header(None)):
    import hmac as _h
    if not ADMIN or not _h.compare_digest((authorization or "").removeprefix("Bearer "), ADMIN): raise HTTPException(401, "admin token required")
@app.post("/v1/nodes/register")
async def node_register(request: Request):
    try: return remote.register_node(await request.json())
    except PermissionError as e: raise HTTPException(403, str(e))
@app.get("/v1/nodes/agent.py", response_class=PlainTextResponse)
def node_agent(): return open(os.path.join(ROOT, "paas", "agent", "agent.py")).read()
class NodeReq(BaseModel): pool: str="self-hosted"; plan: str="cpu-large"; gpu: bool=False
@app.post("/v1/admin/nodes", dependencies=[Depends(admin)])
def admin_add_node(n: NodeReq):
    """Create a join token for a manually-managed node (self-hosted / Yotta bare metal / any Ubuntu box)."""
    if n.pool not in PROVISIONERS: raise HTTPException(400, f"pool must be one of {list(PROVISIONERS)}")
    node_id, join = remote.create_node_record(n.pool, n.plan, n.gpu)
    return {"node_id": node_id, "join_token": join,
            "install": f"curl -fsSL '{PUBLIC_API_URL}/v1/nodes/bootstrap.sh?node={node_id}&token={join}' | sudo bash"}
@app.get("/v1/nodes/bootstrap.sh", response_class=PlainTextResponse)
def node_bootstrap(node: str, token: str):
    from .drivers import bootstrap
    from .crypto import sha256
    if q("select v from kv where kind='join' and k=?", (sha256(token),), one=True) is None: raise HTTPException(403, "invalid join token")
    n = q("select * from nodes where id=?", (node,), one=True)
    if not n: raise HTTPException(404, "unknown node")
    return bootstrap.render(node, n["provider"], n["plan"], token, bool(n["gpus"]))
@app.get("/v1/admin/nodes", dependencies=[Depends(admin)])
def admin_nodes(): return q("select id,provider,provider_ref,status,public_ip,wg_ip,vcpu,mem_mb,gpus,gpu_model,plan,created,last_seen,error from nodes order by created desc")
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
def admin_drop_node(node_id: str): remote.destroy_node(node_id, "admin"); return {"ok": True}
@app.get("/healthz")
def health(): return {"ok":True}
def _llms_gpu():
    if _plans.GPU_ENABLED:
        return "GPU per hour: " + ", ".join(f"{k} ${v['price_usd_hr']:.2f}" for k, v in PLANS.items() if v["kind"] == "gpu")
    return ("GPUs: COMING SOON (phase 2: " + ", ".join(k for k, v in PLANS.items() if v["kind"] == "gpu") + "). Not deployable yet; "
            "join the waitlist: POST {{API}}/v1/waitlist {\"email\":\"...\",\"gpu\":\"gpu-h100\"}")
@app.get("/llms.txt", response_class=PlainTextResponse)
def llms(): return open(os.path.join(ROOT,"llms.txt")).read().replace("{{GPU}}", _llms_gpu()).replace("{{API}}", PUBLIC_API_URL).replace("{{MCP}}", PUBLIC_MCP_URL).replace("{{DOMAIN}}", URL_DOMAIN).replace("{{BRAND}}", BRAND)
@app.get("/.well-known/mcp.json")
def mcp_manifest():
    m = json.load(open(os.path.join(ROOT,"mcp_server","manifest.json")))
    m["transports"][0]["url"] = PUBLIC_MCP_URL; m["transports"][1]["env"]["ALOOF_API_URL"] = PUBLIC_API_URL; m["docs"] = f"{PUBLIC_API_URL}/llms.txt"
    m["auth"]["oauth_protected_resource"] = PUBLIC_MCP_URL.rsplit("/mcp",1)[0] + "/.well-known/oauth-protected-resource/mcp"
    return m
@app.get("/v1/config")
def config(): return {"brand": BRAND, "base_domain": BASE_DOMAIN, "api": PUBLIC_API_URL, "mcp": PUBLIC_MCP_URL, "app_url_pattern": f"https://<app>.{URL_DOMAIN}/", "app_domains": APP_DOMAINS}
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
def login_page(): return www.at("/login", "default", site.login, BRAND)
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
<link rel=icon href="/favicon.ico" sizes="any"><link rel=icon href="/icon.svg" type="image/svg+xml"><link rel=apple-touch-icon href="/apple-touch-icon.png">
<link rel=stylesheet href="/static/swagger/swagger-ui.css?v={SWAGGER_V}" integrity="{SWAGGER_SRI['css']}" crossorigin=anonymous><link rel=stylesheet href="/static/aloof.css?v=8">
<style>body{{margin:0;background:#fff}}.topbar{{display:none}}.abar{{display:flex;align-items:center;gap:14px;padding:14px 24px;background:#0b0c10;color:#eef0f6}}.abar .logo{{color:#eef0f6!important}}.abar a{{color:#a9b0c3}}.abar .sp{{flex:1}}</style></head>
<body><div class=abar><a class=logo href="/"><i></i>{BRAND}</a><span style="color:#8b93a7">API reference</span><span class=sp></span><a href="/docs/api">Guide</a><a href="/dashboard">Dashboard</a></div><div id=swagger-ui></div>
<script src="/static/swagger/swagger-ui-bundle.js?v={SWAGGER_V}" integrity="{SWAGGER_SRI['js']}" crossorigin=anonymous></script>
<script>SwaggerUIBundle({{url:"/api/openapi.json",dom_id:"#swagger-ui",deepLinking:true,persistAuthorization:false}})</script></body></html>"""
@app.get("/dashboard", response_class=HTMLResponse)
def dash(request: Request):
    if not auth.session_account(request.cookies.get(auth.COOKIE)): return RedirectResponse("/login?next=/dashboard", 303)
    return HTMLResponse(open(os.path.join(ROOT,"dashboard","index.html")).read().replace("{{BRAND}}", BRAND), headers={"X-Robots-Tag": "noindex, nofollow"})


# ---------------- Fly parity: releases/rollback, managed Postgres, snapshots, exec/console, log drains/history, audit ----------------
from . import parity
from .db import audit as _audit
def _ip(request): return request.client.host if request.client else None
@app.get("/v1/apps/{name}/releases")
def app_releases(name: str, a=Depends(acct)): own(name, a); return engine.releases(name)
class RollbackReq(BaseModel): version: int|None=None
@app.post("/v1/apps/{name}/rollback")
def app_rollback(name: str, request: Request, b: RollbackReq|None=None, a=Depends(acct)):
    """Redeploy an earlier release (default: the previous one) with zero downtime. No rebuild."""
    own(name, a)
    try: r = engine.rollback(name, b.version if b else None)
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], name, "rollback", f"to v{b.version}" if b and b.version else "previous", _ip(request)); return with_url(r)
# managed Postgres
class PgReq(BaseModel): name: str; plan: str="pg-dev"
def own_pg(n, a):
    d = q("select * from pg_dbs where name=?", (n,), one=True)
    if not d or d["account_id"] != a["id"]: raise HTTPException(404, "database not found")
    return d
@app.get("/v1/pg/plans")
def pg_plans(): return {"plans": [{"id": k} | {x: y for x, y in v.items() if x != "margin"} for k, v in _plans.PG_PLANS.items()],
                        "backups": {"schedule": "daily", "keep": parity.PG_KEEP, "usd_gb_month": RATES["backup_usd_gb_month"]}}
@app.post("/v1/pg")
def pg_create(b: PgReq, request: Request, a=Depends(acct)):
    try: r = parity.pg_create(a["id"], b.name, b.plan)
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
    return auth.session_account(ws.cookies.get(auth.COOKIE))
@app.websocket("/v1/apps/{name}/console")
async def app_console(ws: WebSocket, name: str, instance: str|None=None):
    """Interactive shell in a running instance. Binary frames carry terminal bytes both ways; a text frame {"resize":[rows,cols]} resizes.
    Session open/close and each entered line are written to the audit log."""
    a = _ws_account(ws); ap = engine.get_app(name)
    if not a or not ap or ap["account_id"] != a["id"] or ap["status"] == "destroyed": await ws.close(code=4401); return
    i = q("select * from instances where app=? and status='running'" + (" and id=?" if instance else "") + " order by started", (name, *([instance] if instance else [])), one=True)
    drv = get_drivers().get(i["provider"]) if i else None
    if not i or not hasattr(drv, "exec_socket"): await ws.close(code=4409, reason="no running instance (or console unsupported on this node)"); return
    await ws.accept(); ip = ws.client.host if ws.client else None; t0 = time.time()
    eid, sock = await asyncio.to_thread(drv.exec_socket, i["id"])
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
# ---------------- edge proxy (port 8080): <app>.BASE_DOMAIN or /_app/<app>/ ----------------
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
@proxy.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def edge(path: str, request: Request):
    host = request.headers.get("host","").split(":")[0].lower(); name, path = resolve_app(host, path)
    if not name: return PlainTextResponse(f"{BRAND} edge: use https://<app>.{URL_DOMAIN}/, a verified custom domain, or /_app/<app>/", 404)
    insts = q("select host,port from instances where app=? and status='running'", (name,))
    if not insts:
        ap = q("select spec,status from apps where name=?", (name,), one=True)
        if ap and ap["status"] in ("idle","running") and json.loads(ap["spec"]).get("autoscale"):
            controller.M.begin(name)   # count the waiting request so the autoscaler doesn't scale us back down
            try: await asyncio.to_thread(controller.cold_start, name)
            finally: controller.M.end(name)
            insts = q("select host,port from instances where app=? and status='running'", (name,))
        if not insts and ap and ap["status"] == "capped": return PlainTextResponse(f"app '{name}' is paused: free tier usage cap reached for this month", 402)
        if not insts: return PlainTextResponse(f"app '{name}' has no running instances", 503)
    it = _rr.setdefault(name, itertools.count()); i = insts[next(it) % len(insts)]
    url = f"http://{i['host']}:{i['port']}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    hdrs = {k:v for k,v in request.headers.items() if k.lower() not in HOP} | {"x-forwarded-host": host, "x-forwarded-proto": proto}
    controller.M.begin(name); body_bytes = await request.body(); r = None
    for attempt in range(3):
        try:
            r = await client.send(client.build_request(request.method, url, headers=hdrs, content=body_bytes), stream=True); break
        except httpx.ConnectError as e:
            err = e   # instance restarting / port remapped: refresh endpoints and retry another or the same instance
            await asyncio.sleep(0.5 * attempt)
            await asyncio.to_thread(controller.health_tick)
            insts = q("select host,port from instances where app=? and status='running'", (name,)) or insts
            i = insts[next(it) % len(insts)]
            url = f"http://{i['host']}:{i['port']}/{path}" + (f"?{request.url.query}" if request.url.query else "")
        except httpx.HTTPError as e:
            err = e; break
    if r is None:
        controller.M.end(name); return PlainTextResponse(f"upstream error: {err}", 502)
    async def body():
        try:
            async for chunk in r.aiter_raw():
                EGRESS[name] = EGRESS.get(name, 0) + len(chunk); yield chunk
        finally:
            await r.aclose(); controller.M.end(name)
    return StreamingResponse(body(), status_code=r.status_code, headers={k:v for k,v in r.headers.items() if k.lower() not in HOP} | {"x-paas-instance": str(i["port"])})

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
