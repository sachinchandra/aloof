"""Owner dashboard: one account's detail, and the owner's actions on it (suspend, credit adjustment, Stripe refund, read-only view),
plus admin 2FA enrollment. Every action needs the account's email typed back and writes an audit entry."""
import html, json, time
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from .db import q, audit
from . import auth, www
R = APIRouter()
BACK = "aloof_admin_back"; IMP_TTL = 30 * 60
def _acct(i):
    a = q("select * from accounts where id=?", (i,), one=True)
    if not a: raise HTTPException(404, "no such account")
    return a
def _typed(a, confirm):
    if (confirm or "").strip().lower() != (a["email"] or "").lower(): raise HTTPException(400, "type the account's email address to confirm")
def _who(ctx): return (ctx.get("account") or {}).get("id")
def _reason(r):
    r = (r or "").strip()
    if len(r) < 3: raise HTTPException(400, "a reason is required")
    return r[:300]
def _refundable(s):
    done = (q("select coalesce(sum(amount_cents),0) n from admin_refunds where session_id=?", (s["id"],), one=True) or {}).get("n") or 0
    return max(0, int(s["amount_cents"] or 0) - int(done)) if s["status"] == "paid" and s["kind"] in ("topup", "invoice") else 0
@R.get("/v1/admin/accounts/{account_id}")
def detail(account_id: int, _=Depends(www.require_admin)):
    a = _acct(account_id)
    apps = q("select name, status, region, created, updated, error from apps where account_id=? order by created desc", (account_id,))
    names = [x["name"] for x in apps]
    rel = [r for r in q("select app, version, status, reason, created from releases order by created desc limit 2000") if r["app"] in names][:25]
    ss = q("select id, kind, tier, status, amount_cents, created, updated from stripe_sessions where account_id=? order by created desc limit 50", (account_id,))
    for s in ss: s["refundable_cents"] = _refundable(s)
    return {"profile": {k: a.get(k) for k in ("id", "email", "created", "tier", "is_test", "is_admin", "suspended", "email_verified", "country", "billing_name", "stripe_subscription", "deleting")}
                       | {"destroyed_apps": sum(1 for x in apps if x["status"] == "destroyed")}
                       | {"credit_usd": round((a.get("credit_cents") or 0) / 100, 2), "totp": bool(a.get("totp_enabled"))},
            "apps": apps, "deploys": rel, "stripe": ss,
            "razorpay": q("select order_id, amount_paise, credit_inr, status, created from payments where account_id=? order by created desc limit 20", (account_id,)),
            "ledger": q("select ts, amount_cents, kind, ref from ledger where account_id=? order by ts desc limit 40", (account_id,)),
            "refunds": q("select session_id, amount_cents, reason, created from admin_refunds where account_id=? order by created desc", (account_id,)),
            "audit": q("select ts, account_id, app, action, substr(detail,1,200) detail from audit where account_id=? or detail like ? order by id desc limit 40", (account_id, f"account={account_id} %"))}
class Suspend(BaseModel): suspended: bool; confirm: str; reason: str = ""
@R.post("/v1/admin/accounts/{account_id}/suspend")
def suspend(account_id: int, b: Suspend, ctx=Depends(www.require_owner_2fa)):
    a = _acct(account_id); _typed(a, b.confirm); why = _reason(b.reason)
    if a.get("is_admin"): raise HTTPException(400, "the owner account can't be suspended")
    from . import engine, billing
    if b.suspended:
        q("update accounts set suspended=1 where id=?", (account_id,)); q("delete from sessions where account_id=?", (account_id,))
        from . import ws_registry; ws_registry.close_account(account_id, "account suspended")
        for x in q("select name from apps where account_id=? and status in ('running','idle')", (account_id,)):
            try: engine.scale(x["name"], 0, reason="account suspended")
            except Exception: pass
            q("update apps set status='suspended' where name=?", (x["name"],))
    else:
        q("update accounts set suspended=0, deleting=0 where id=?", (account_id,)); billing.resume_suspended(account_id)
    audit(_who(ctx), None, "admin.suspend" if b.suspended else "admin.unsuspend", f"account={account_id} email={a['email']} reason={why}")
    return {"ok": True, "suspended": b.suspended}
class Credit(BaseModel): amount_usd: float = Field(allow_inf_nan=False, ge=-1000, le=1000); confirm: str; reason: str = ""
@R.post("/v1/admin/accounts/{account_id}/credit")
def credit(account_id: int, b: Credit, ctx=Depends(www.require_owner_2fa)):
    a = _acct(account_id); _typed(a, b.confirm); why = _reason(b.reason)
    cents = round(b.amount_usd * 100)
    if not cents or abs(cents) > 100000: raise HTTPException(400, "amount must be non-zero and at most $1,000 either way")
    from . import billing
    (billing.credit if cents > 0 else billing.credit_rows)(account_id, cents, "admin_adjust", why)
    audit(_who(ctx), None, "admin.credit", f"account={account_id} email={a['email']} amount_usd={cents/100:.2f} reason={why}")
    return {"ok": True, "credit_usd": round((_acct(account_id)["credit_cents"] or 0) / 100, 2)}
class Refund(BaseModel): session_id: str; amount_usd: float = Field(allow_inf_nan=False, gt=0, le=100000); confirm: str; reason: str = ""
@R.post("/v1/admin/accounts/{account_id}/refund")
def refund(account_id: int, b: Refund, ctx=Depends(www.require_owner_2fa)):
    return do_refund(account_id, b, ctx)
def do_refund(account_id, b, ctx, client=None):
    """Refund a paid top-up or invoice through Stripe, never more than what's left of the original amount. A top-up refund also takes
    the refunded credit back out of the wallet."""
    a = _acct(account_id); _typed(a, b.confirm); why = _reason(b.reason)
    from .db import tx, PG
    from . import stripe_billing as sb
    failed = None
    with tx():   # row lock: concurrent refunds of one payment run one at a time, so the limit check and the write are atomic
        s = q("select * from stripe_sessions where id=? and account_id=?" + (" for update" if PG else ""), (b.session_id, account_id), one=True)
        if not s: raise HTTPException(404, "no such payment on this account")
        left = _refundable(s); cents = round(b.amount_usd * 100)
        if cents <= 0 or cents > left: raise HTTPException(400, f"refund must be between $0.01 and ${left/100:.2f}")
        pi = s.get("payment_intent")
        try:
            if not pi and s["kind"] == "invoice": pi = sb._call("GET", f"/invoices/{s['invoice_id'] or s['id']}", client=client).get("payment_intent")
            if not pi: raise HTTPException(400, "this payment has no Stripe payment to refund")
            r = sb._call("POST", "/refunds", {"payment_intent": pi, "amount": cents, "metadata": {"account_id": account_id, "reason": why}},
                         idem=f"admrefund-{s['id']}-{left}-{cents}", client=client)
        except HTTPException: raise
        except Exception as e: failed = e
        if not failed:
            q("insert into admin_refunds(id,session_id,account_id,amount_cents,reason,created) values(?,?,?,?,?,?)", (r.get("id"), s["id"], account_id, cents, why, time.time()))
            if s["kind"] == "topup":
                from . import billing; billing.credit_rows(account_id, -cents, "refund", f"refund {r.get('id')} of {s['id']}")
    if failed:
        audit(_who(ctx), None, "admin.refund_failed", f"account={account_id} session={s['id']} amount_usd={cents/100:.2f} error={str(failed)[:200]}")
        raise HTTPException(502, f"Stripe refused the refund: {str(failed)[:200]}")
    audit(_who(ctx), None, "admin.refund", f"account={account_id} email={a['email']} session={s['id']} refund={r.get('id')} amount_usd={cents/100:.2f} reason={why}")
    return {"ok": True, "refund": r.get("id"), "refundable_left_usd": round((left - cents) / 100, 2)}
class Imp(BaseModel): confirm: str; reason: str = ""
@R.post("/v1/admin/accounts/{account_id}/impersonate")
def impersonate(account_id: int, b: Imp, request: Request, ctx=Depends(www.require_owner_2fa)):
    """Read-only view as this account: a short session flagged impersonated_by, which the API refuses for every write."""
    if ctx.get("via") != "session": raise HTTPException(400, "sign in to the dashboard to use the read-only view")
    a = _acct(account_id); _typed(a, b.confirm); why = _reason(b.reason)
    if a.get("is_admin"): raise HTTPException(400, "that's the owner account")
    from .crypto import sha256
    import secrets
    tok = "s_" + secrets.token_urlsafe(32)
    q("insert into sessions(token_hash,account_id,expires,created,impersonated_by) values(?,?,?,?,?)", (sha256(tok), account_id, time.time() + IMP_TTL, time.time(), _who(ctx)))
    audit(_who(ctx), None, "admin.impersonate", f"account={account_id} email={a['email']} reason={why}")
    from .api import SECURE_COOKIES
    r = JSONResponse({"ok": True, "url": "/dashboard"})
    r.set_cookie(BACK, request.cookies.get(auth.COOKIE), max_age=IMP_TTL + 600, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    r.set_cookie(auth.COOKIE, tok, max_age=IMP_TTL, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/"); return r
@R.post("/v1/admin/impersonate/exit")
def imp_exit(request: Request):
    if request.headers.get("x-aloof-csrf") != "1": raise HTTPException(403, "missing X-Aloof-CSRF header")
    cur = auth.session_account(request.cookies.get(auth.COOKIE))
    if not cur or not cur.get("impersonated_by"): raise HTTPException(400, "not in a read-only view")
    auth.end_session(request.cookies.get(auth.COOKIE)); audit(cur["impersonated_by"], None, "admin.impersonate_exit", f"account={cur['id']} email={cur['email']}")
    back = request.cookies.get(BACK); o = auth.session_account(back)
    from .api import SECURE_COOKIES
    r = JSONResponse({"ok": True, "url": "/admin" if www.is_owner(o) else "/login"}); r.delete_cookie(BACK, path="/", secure=SECURE_COOKIES, httponly=True, samesite="lax")
    if www.is_owner(o): r.set_cookie(auth.COOKIE, back, max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/")
    else: r.delete_cookie(auth.COOKIE, path="/", secure=SECURE_COOKIES, httponly=True, samesite="lax")
    return r
def imp_banner(a):
    return ("<script>window.ALOOF_READONLY=1</script><div role=status style=\"position:sticky;top:0;z-index:9999;display:flex;gap:12px;align-items:center;justify-content:center;flex-wrap:wrap;"
            "padding:8px 14px;background:#f59e0b;color:#1c1206;font:600 14px/1.3 system-ui,sans-serif\">"
            f"<span>Read-only view of {html.escape(a['email'] or '')}. Changes are disabled. Secrets hidden while viewing as user.</span>"
            "<button type=button onclick=\"fetch('/v1/admin/impersonate/exit',{method:'POST',headers:{'X-Aloof-CSRF':'1'}}).then(r=>r.json()).then(j=>location=j.url||'/admin')\" "
            "style=\"font:inherit;padding:4px 12px;border-radius:6px;border:1px solid #1c1206;background:#fff7ed;color:#1c1206;cursor:pointer\">Exit view</button></div>")
# ---- admin 2FA (TOTP). Reachable by the owner's session even before the session is verified, so enrollment can never lock them out.
def owner_session(request: Request):
    a = www._session(request)
    if not www.is_owner(a): raise HTTPException(404, "Not Found")
    if request.method != "GET" and request.headers.get("x-aloof-csrf") != "1": raise HTTPException(403, "missing X-Aloof-CSRF header")
    return a
class Code(BaseModel): code: str
@R.get("/v1/admin/2fa")
def mfa_status(a=Depends(owner_session)):
    from . import mfa
    import os
    return mfa.status(a["id"]) | {"required": os.environ.get("PAAS_ADMIN_REQUIRE_2FA") == "1", "session_verified": bool(a.get("mfa_verified"))}
ALERT_TO = "sachin@aloof.si"
def _not_locked(a):
    from . import mfa
    until = mfa.locked_until(a["id"])
    if until: raise HTTPException(429, "too many wrong codes; 2FA is locked for 30 minutes", headers={"Retry-After": str(int(until - time.time()) + 1)})
def _failed(a):
    from . import mfa
    audit(a["id"], None, "admin.2fa_failed", "")
    if mfa.record_failure(a["id"]):
        audit(a["id"], None, "admin.2fa_locked", f"{mfa.LOCK_FAILS} failures in {mfa.LOCK_WINDOW // 60} min; locked {mfa.LOCK_FOR // 60} min")
        try:
            auth.send_mail(ALERT_TO, "Aloof admin 2FA locked after repeated wrong codes",
                           f"The owner account's 2FA had {mfa.LOCK_FAILS} wrong codes within {mfa.LOCK_WINDOW // 60} minutes and is locked for "
                           f"{mfa.LOCK_FOR // 60} minutes ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}).\n\nIf this wasn't you, someone has an "
                           "owner session: sign out all sessions and change the owner password.")
        except Exception: pass
@R.post("/v1/admin/2fa/start")
def mfa_start(a=Depends(owner_session)):
    from . import mfa
    try: s = mfa.start(a["id"])
    except PermissionError as e: raise HTTPException(409, str(e))
    audit(a["id"], None, "admin.2fa_start", ""); return {"secret": s, "uri": mfa.uri(s, a["email"])}
def _verify_session(request): q("update sessions set mfa_verified=1 where token_hash=?", (auth.sha256(request.cookies.get(auth.COOKIE) or ""),))
@R.post("/v1/admin/2fa/confirm")
def mfa_confirm(b: Code, request: Request, a=Depends(owner_session)):
    from . import mfa
    from .api import _rate_check; _rate_check((f"2fa:{a['id']}",))
    _not_locked(a)
    try: codes = mfa.confirm(a["id"], b.code)
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: _failed(a); raise HTTPException(400, str(e))
    _verify_session(request); audit(a["id"], None, "admin.2fa_enabled", ""); return {"ok": True, "recovery_codes": codes}
@R.post("/v1/admin/2fa/verify")
def mfa_verify(b: Code, request: Request, a=Depends(owner_session)):
    from . import mfa
    from .api import _rate_check; _rate_check((f"2fa:{a['id']}",))
    _not_locked(a)
    if not mfa.verify(a["id"], b.code): _failed(a); raise HTTPException(400, "that code didn't work")
    _verify_session(request); audit(a["id"], None, "admin.2fa_verified", ""); return {"ok": True}
@R.get("/admin/2fa", response_class=HTMLResponse, include_in_schema=False)
def mfa_page(request: Request):
    a = www._session(request)
    if not a: return RedirectResponse("/login?next=/admin/2fa", 303)
    if not www.is_owner(a): raise HTTPException(404, "Not Found")
    import os
    return open(os.path.join(www.ROOT, "dashboard", "admin2fa.html")).read()
# ---- promo codes (read-only from Stripe; terms live there) ----
def _stripe_get(path, client=None):
    from . import stripe_billing as sb
    return sb._call("GET", path, client=client)
def _promo_view(pc):
    c = pc.get("coupon") or {}; r = pc.get("restrictions") or {}
    return {"code": pc["code"], "id": pc["id"], "active": pc.get("active"), "used": pc.get("times_redeemed", 0), "max": pc.get("max_redemptions"),
            "remaining": (pc["max_redemptions"] - pc.get("times_redeemed", 0)) if pc.get("max_redemptions") else None,
            "expires_at": pc.get("expires_at"), "first_time_only": bool(r.get("first_time_transaction")), "minimum_amount": r.get("minimum_amount"),
            "customer_restricted": bool(pc.get("customer")), "percent_off": c.get("percent_off"), "amount_off": c.get("amount_off"),
            "duration": c.get("duration"), "duration_in_months": c.get("duration_in_months"), "applies_to_products": (c.get("applies_to") or {}).get("products") or []}
@R.get("/v1/admin/promos")
def promos(_=Depends(www.require_admin)): return promos_view()
def promos_view(client=None):
    """Active Stripe promotion codes with redemptions used/max, expiry and restrictions. GETs only; never changes Stripe."""
    try: d = _stripe_get("/promotion_codes?active=true&limit=100&expand[]=data.coupon.applies_to", client)
    except RuntimeError as e: raise HTTPException(503, f"Stripe unavailable: {e}")
    return {"promos": [_promo_view(p) for p in d.get("data", [])]}
def prior_paid(a, client=None):
    """Has this account ever paid anything? Local paid sessions, plus (when it has a Stripe customer) succeeded charges or paid
    invoices with a non-zero amount. Stripe's first_time_transaction rule rejects customers with any prior successful payment."""
    local = q("select count(*) n from stripe_sessions where account_id=? and status='paid' and coalesce(amount_cents,0)>0", (a["id"],), one=True)
    rzp = q("select count(*) n from payments where account_id=? and status='paid'", (a["id"],), one=True)
    out = {"local_paid_sessions": int((local or {}).get("n") or 0), "razorpay_paid": int((rzp or {}).get("n") or 0), "stripe_customer": bool(a.get("stripe_customer")), "stripe_paid_charges": 0, "stripe_paid_invoices": 0}
    if a.get("stripe_customer"):
        cu = a["stripe_customer"]
        out["stripe_paid_charges"] = sum(1 for c in _stripe_get(f"/charges?customer={cu}&limit=100", client).get("data", []) if c.get("status") == "succeeded" and c.get("amount", 0) > 0 and not c.get("refunded"))
        out["stripe_paid_invoices"] = sum(1 for i in _stripe_get(f"/invoices?customer={cu}&status=paid&limit=100", client).get("data", []) if i.get("amount_paid", 0) > 0)
    out["has_prior_paid"] = bool(out["local_paid_sessions"] or out["stripe_paid_charges"] or out["stripe_paid_invoices"])
    out["eligible_first_time_codes"] = not out["has_prior_paid"]   # Razorpay payments don't count for Stripe's rule (shown for context)
    return out
@R.get("/v1/admin/accounts/{account_id}/promo-eligibility")
def promo_eligibility(account_id: int, _=Depends(www.require_admin)): return promo_eligibility_view(account_id)
def promo_eligibility_view(account_id, client=None):
    a = _acct(account_id)
    try: return {"account_id": account_id, "email": a["email"]} | prior_paid(a, client)
    except RuntimeError as e: raise HTTPException(503, f"Stripe unavailable: {e}")
