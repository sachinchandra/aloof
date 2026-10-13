import os, json, itertools, httpx, time, asyncio
from fastapi import FastAPI, Header, HTTPException, Request, Depends, Cookie
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
# Public hostnames. Go-live: PAAS_BASE_DOMAIN=aloof.si -> <app>.aloof.si, api.aloof.si, mcp.aloof.si
API_HOST = os.environ.get("PAAS_API_HOST", f"api.{BASE_DOMAIN}"); MCP_HOST = os.environ.get("PAAS_MCP_HOST", f"mcp.{BASE_DOMAIN}")
PUBLIC_API_URL = os.environ.get("PAAS_PUBLIC_API_URL", f"https://{API_HOST}"); PUBLIC_MCP_URL = os.environ.get("PAAS_PUBLIC_MCP_URL", f"https://{MCP_HOST}/mcp")
ROOT = os.path.dirname(os.path.dirname(__file__))
app = FastAPI(title=f"{BRAND} control plane", version="0.3", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
SECURE_COOKIES = PUBLIC_API_URL.startswith("https://") and os.environ.get("PAAS_INSECURE_COOKIES") != "1"
def acct(request: Request, authorization: str = Header(None)):
    """Bearer API key (CLI/MCP/automation) or session cookie (dashboard). Cookie-authenticated writes need the
    X-Aloof-CSRF header, which cross-site forms cannot set (SameSite=Lax is the first line of defence)."""
    key = (authorization or "").removeprefix("Bearer ").strip()
    if key:
        a = engine.account_by_key(key)
        if not a: raise HTTPException(401, "invalid API key")
        return a
    a = auth.session_account(request.cookies.get(auth.COOKIE))
    if not a: raise HTTPException(401, "not authenticated: send Authorization: Bearer pk_... or log in at /login")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-aloof-csrf") != "1": raise HTTPException(403, "missing X-Aloof-CSRF header")
    return a
def own(name, a):
    ap = engine.get_app(name)
    if not ap or ap["account_id"] != a["id"] or ap["status"]=="destroyed": raise HTTPException(404, "app not found")
    return ap
def with_url(ap): ap["url"] = f"https://{ap['name']}.{BASE_DOMAIN}/"; ap["http_url"] = f"http://{ap['name']}.{BASE_DOMAIN}:8080/"; ap["path_url"] = f"{PUBLIC_PROXY}/_app/{ap['name']}/"; return ap
class Signup(BaseModel): email: str
class Source(BaseModel): image: str|None=None; git: str|None=None; ref: str|None=None; subdir: str|None=None; path: str|None=None
class DeployReq(BaseModel):
    name: str; source: Source; plan: str="shared-1x-1gb"; port: int=8080; env: dict[str,str]={}; instances: int=1
    command: list[str]|None=None; region: str="local"; provider: str|None=None
    volumes: list[dict]=[]; autoscale: dict|None=None; health: dict|None=None
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
class WaitlistReq(BaseModel): email: str; gpu: str | None = None; note: str | None = None
@app.post("/v1/waitlist")
def waitlist(w: WaitlistReq):
    """GPU waitlist (phase 2). Idempotent per email."""
    try: e = auth._check_email(w.email)
    except ValueError as x: raise HTTPException(400, str(x))
    if w.gpu and (w.gpu not in PLANS or PLANS[w.gpu]["kind"] != "gpu"): raise HTTPException(400, "gpu must be a GPU plan id")
    from .db import kv_put
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
    spec = r.model_dump(exclude_unset=True) | {"name": r.name, "source": r.source.model_dump()}; spec["source"] = {k:v for k,v in spec["source"].items() if v}
    try: return with_url(engine.deploy(a["id"], spec))
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
@app.get("/v1/apps")
def list_apps(a=Depends(acct)): return [with_url(engine.get_app(r["name"])) for r in q("select name from apps where account_id=? and status!='destroyed'", (a["id"],))]
@app.get("/v1/apps/{name}")
def get_app(name: str, a=Depends(acct)): return with_url(own(name, a))
@app.get("/v1/apps/{name}/logs", response_class=PlainTextResponse)
def logs(name: str, tail: int=200, a=Depends(acct)): own(name, a); return engine.logs(name, tail)
class Scale(BaseModel): instances: int
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
SYSTEM_HOSTS = {API_HOST, MCP_HOST, BASE_DOMAIN, f"www.{BASE_DOMAIN}"}
@app.get("/v1/tls/ask")
def tls_ask(domain: str):
    """Caddy on_demand_tls gate: only issue certs for our hosts, live app subdomains and verified custom domains."""
    d = domain.lower()
    if d in SYSTEM_HOSTS: return {"ok": True}
    if d.endswith("." + BASE_DOMAIN) and q("select 1 from apps where name=? and status!='destroyed'", (d[:-len(BASE_DOMAIN)-1],), one=True): return {"ok": True}
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
    return _wallet(a) | {"currency": "USD", "stripe": stripe_billing.configured(), "stripe_tax": stripe_billing.tax_enabled(), "razorpay": rzp.enabled(),
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
@app.post("/v1/auth/login")
def auth_login(c: Creds):
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
@app.post("/v1/auth/mcp-key")
def auth_mcp_key(a=Depends(acct)):
    """Key bound to the MCP OAuth connector; doesn't revoke the CLI key. Re-login rotates only this one."""
    return {"api_key": engine.create_named_key(a["id"], "mcp-oauth")}
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#111"/><text x="16" y="23" font-size="20" text-anchor="middle" fill="#fff" font-family="sans-serif" font-weight="700">a</text></svg>', media_type="image/svg+xml")
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
    return os.environ.get("PAAS_ADMIN_TOKEN") or (open(f).read().strip() if os.path.exists(f) else None)
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
@app.get("/v1/admin/metrics", dependencies=[Depends(admin)])
def admin_metrics():
    n = paid_customers(); goal = _plans.GPU_UNLOCK_PAID_CUSTOMERS
    return {"paid_customers": n, "accounts": q("select count(*) as c from accounts", one=True)["c"],
            "gpu_waitlist": q("select count(*) as c from kv where kind='gpu_waitlist'", one=True)["c"],
            "gpu_enabled": _plans.GPU_ENABLED, "gpu_unlock_at_paid_customers": goal,
            "note": "GPUs enabled (phase 2)." if _plans.GPU_ENABLED else
                    (f"Phase 1 (CPU only). GPU unlocks at {goal} paid customers: {max(0, goal - n)} to go." if n < goal else
                     f"{n} paid customers: GPU unlock reached. Set PAAS_GPU_ENABLED=1 plus HYPERSTACK_API_KEY / RUNPOD_API_KEY.")}
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
def llms(): return open(os.path.join(ROOT,"llms.txt")).read().replace("{{GPU}}", _llms_gpu()).replace("{{API}}", PUBLIC_API_URL).replace("{{MCP}}", PUBLIC_MCP_URL).replace("{{DOMAIN}}", BASE_DOMAIN).replace("{{BRAND}}", BRAND)
@app.get("/.well-known/mcp.json")
def mcp_manifest():
    m = json.load(open(os.path.join(ROOT,"mcp_server","manifest.json")))
    m["transports"][0]["url"] = PUBLIC_MCP_URL; m["transports"][1]["env"]["ALOOF_API_URL"] = PUBLIC_API_URL; m["docs"] = f"{PUBLIC_API_URL}/llms.txt"
    m["auth"]["oauth_protected_resource"] = PUBLIC_MCP_URL.rsplit("/mcp",1)[0] + "/.well-known/oauth-protected-resource/mcp"
    return m
@app.get("/v1/config")
def config(): return {"brand": BRAND, "base_domain": BASE_DOMAIN, "api": PUBLIC_API_URL, "mcp": PUBLIC_MCP_URL, "app_url_pattern": f"https://<app>.{BASE_DOMAIN}/"}
from . import site
@app.get("/", response_class=HTMLResponse)
def landing(): return site.landing(BRAND, BASE_DOMAIN, PUBLIC_API_URL, PUBLIC_MCP_URL)
@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(): return site.pricing(BRAND)
@app.get("/docs", response_class=HTMLResponse)
@app.get("/docs/{page}", response_class=HTMLResponse)
def docs_page(page: str = "quickstart"):
    h = site.docs(page, BRAND, BASE_DOMAIN, PUBLIC_API_URL, PUBLIC_MCP_URL)
    if h is None: raise HTTPException(404, "no such docs page")
    return h
@app.get("/login", response_class=HTMLResponse)
def login_page(): return site.login(BRAND)
@app.get("/dashboard", response_class=HTMLResponse)
def dash(): return open(os.path.join(ROOT,"dashboard","index.html")).read().replace("{{BRAND}}", BRAND)

# ---------------- edge proxy (port 8080): <app>.BASE_DOMAIN or /_app/<app>/ ----------------
proxy = FastAPI(); _rr = {}; client = httpx.AsyncClient(timeout=60)
EGRESS = billing.EGRESS   # app -> bytes sent to clients since last meter tick
HOP = {"connection","keep-alive","transfer-encoding","te","trailer","upgrade","proxy-authorization","proxy-authenticate","host","content-length"}
def resolve_app(host, path):
    if host.endswith("." + BASE_DOMAIN) and host not in SYSTEM_HOSTS: return host[:-len(BASE_DOMAIN)-1], path
    d = q("select app from domains where domain=? and verified=1", (host,), one=True)
    if d: return d["app"], path
    if path.startswith("_app/"):
        _, name, *rest = path.split("/", 2); return name, (rest[0] if rest else "")
    return None, path
@proxy.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def edge(path: str, request: Request):
    host = request.headers.get("host","").split(":")[0].lower(); name, path = resolve_app(host, path)
    if not name: return PlainTextResponse(f"{BRAND} edge: use https://<app>.{BASE_DOMAIN}/, a verified custom domain, or /_app/<app>/", 404)
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
