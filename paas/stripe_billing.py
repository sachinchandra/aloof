"""Stripe: primary payment provider (USD).
* Top-ups: POST /v1/billing/checkout {credit_usd} -> Checkout Session (mode=payment) for the credit amount (+ tax when Stripe Tax is on)
  -> webhook checkout.session.completed (or async_payment_succeeded) with payment_status=paid -> credit the wallet exactly once.
* Tiers: POST /v1/billing/subscribe {tier} -> Checkout Session (mode=subscription, STRIPE_PRICE_HOBBY/PRO) -> webhook sets the tier;
  invoice.paid renews it; customer.subscription.deleted / unpaid drops to Free.
* Customer portal: POST /v1/billing/portal -> billing_portal session (cards, invoices, cancel).
* Invoices/VAT: Stripe issues the invoice (invoice_creation for top-ups, subscription invoices). Set STRIPE_TAX=1 to enable
  automatic_tax (Stripe Tax computes VAT/GST/sales tax from the customer's address and collects tax IDs).
Webhooks are verified with the Stripe-Signature header (HMAC-SHA256 over "t.payload", 5 min tolerance) and de-duplicated by event id."""
import os, hmac, hashlib, json, time, httpx, logging
from urllib.parse import urlencode
from .db import q, tx
from . import billing
from .crypto import load_spec
log = logging.getLogger("paas.stripe")
E = lambda k, d=None: os.environ.get(k, d)
API = lambda: E("STRIPE_API_BASE", "https://api.stripe.com/v1")
MIN_TOPUP, MAX_TOPUP = 10, 5000
def configured(): return bool(E("STRIPE_SECRET_KEY"))
PAYMENTS_TTL = 600; _acct = {"t": 0.0, "open": None}
PAYMENTS_SOON = "Paid plans are opening soon: card payments aren't enabled on our Stripe account yet. Nothing was charged."
def payments_open(client=None, now=None):
    """True when Stripe can take card payments (account charges_enabled). Cached 10 min. If Stripe can't be reached, the last
    known answer is kept (or True on first boot) so a Stripe blip never hides billing; checkout will still report errors."""
    if not configured(): return False
    now = now or time.time()
    if _acct["open"] is not None and now - _acct["t"] < PAYMENTS_TTL: return _acct["open"]
    try:
        c = client or httpx.Client(timeout=10)
        r = c.get(f"{API()}/account", headers={"Authorization": f"Bearer {E('STRIPE_SECRET_KEY')}", "Stripe-Version": E("STRIPE_API_VERSION", "2024-06-20")})
        r.raise_for_status(); _acct.update(t=now, open=bool(r.json().get("charges_enabled")))
    except Exception as e:
        log.warning("stripe account check failed: %s", e); _acct.update(t=now, open=True if _acct["open"] is None else _acct["open"])
    return _acct["open"]
def payments_open_cached():
    """For page rendering: the cached answer when fresh, else a live check (cached 10 min). Unconfigured Stripe = closed."""
    return payments_open()
def require_payments_open():
    if not payments_open(): raise RuntimeError(PAYMENTS_SOON)
def tax_enabled(): return E("STRIPE_TAX", "0") == "1"
def tier_price(tier): return E(f"STRIPE_PRICE_{tier.upper()}")
def _flat(d, pre=""):
    """Stripe's form encoding: a[b][0][c]=v."""
    out = []
    for k, v in (d.items() if isinstance(d, dict) else enumerate(d)):
        key = f"{pre}[{k}]" if pre else str(k)
        if isinstance(v, (dict, list)): out += _flat(v, key)
        elif v is not None: out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out
def _call(method, path, data=None, idem=None, client=None):
    if not configured(): raise RuntimeError("Stripe not configured (STRIPE_SECRET_KEY)")
    c = client or httpx.Client(timeout=30)
    h = {"Authorization": f"Bearer {E('STRIPE_SECRET_KEY')}", "Stripe-Version": E("STRIPE_API_VERSION", "2024-06-20"),
         "Content-Type": "application/x-www-form-urlencoded"}
    if idem: h["Idempotency-Key"] = idem
    r = c.request(method, f"{API()}{path}", content=urlencode(_flat(data or {})), headers=h)
    if r.status_code >= 400:   # full Stripe error goes to the log (and /admin), not to the customer
        logging.getLogger("paas.stripe").error("stripe %s %s failed: %s %s", method, path, r.status_code, r.text[:500])
        raise RuntimeError("Card payments are temporarily unavailable. Nothing was charged; please try again later.")
    return r.json()
def _urls():
    base = E("PAAS_PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/dashboard#billing?paid={{CHECKOUT_SESSION_ID}}", f"{base}/dashboard#billing"
def ensure_customer(a, client=None):
    if a.get("stripe_customer"): return a["stripe_customer"]
    c = _call("POST", "/customers", {"email": a["email"], "name": a.get("billing_name") or None, "metadata": {"account_id": a["id"]}},
              idem=f"cust-{a['id']}", client=client)
    q("update accounts set stripe_customer=? where id=?", (c["id"], a["id"])); return c["id"]
def _tax(d):
    if tax_enabled(): d |= {"automatic_tax": {"enabled": True}, "tax_id_collection": {"enabled": True}, "customer_update": {"address": "auto", "name": "auto"}}
    return d
def create_topup_session(a, credit_usd, client=None):
    cents = int(round(float(credit_usd) * 100))
    if cents < MIN_TOPUP * 100 or cents > MAX_TOPUP * 100: raise ValueError(f"top-up must be between ${MIN_TOPUP} and ${MAX_TOPUP:,}")
    ok, cancel = _urls()
    d = _tax({"mode": "payment", "customer": ensure_customer(a, client), "client_reference_id": a["id"], "success_url": ok, "cancel_url": cancel,
              "line_items": [{"quantity": 1, "price_data": {"currency": "usd", "unit_amount": cents, "tax_behavior": "exclusive",
                              "product_data": {"name": "Aloof prepaid credit", "description": f"${cents/100:,.2f} of cloud usage credit"}}}],
              "invoice_creation": {"enabled": True}, "metadata": {"account_id": a["id"], "kind": "topup", "credit_cents": cents},
              "payment_intent_data": {"metadata": {"account_id": a["id"], "kind": "topup"}}})
    s = _call("POST", "/checkout/sessions", d, idem=f"topup-{a['id']}-{cents}-{int(time.time())}", client=client)
    q("insert into stripe_sessions(id,account_id,kind,credit_cents,amount_cents,status,created,updated) values(?,?,?,?,?,?,?,?)",
      (s["id"], a["id"], "topup", cents, cents, "created", time.time(), time.time()))
    return {"id": s["id"], "url": s.get("url"), "credit_usd": cents / 100, "tax": "calculated at checkout" if tax_enabled() else "not collected (STRIPE_TAX off)"}
def create_subscription_session(a, tier, client=None):
    from .plans import TIERS
    if tier not in TIERS or not TIERS[tier]["fee_usd"]: raise ValueError("tier must be hobby or pro")
    price = tier_price(tier)
    if not price: raise RuntimeError(f"STRIPE_PRICE_{tier.upper()} not set")
    ok, cancel = _urls()
    d = _tax({"mode": "subscription", "allow_promotion_codes": True, "payment_method_collection": "always", "customer": ensure_customer(a, client), "client_reference_id": a["id"], "success_url": ok, "cancel_url": cancel,
              "line_items": [{"price": price, "quantity": 1}], "metadata": {"account_id": a["id"], "kind": "subscription", "tier": tier},
              "subscription_data": {"metadata": {"account_id": a["id"], "tier": tier}}})
    s = _call("POST", "/checkout/sessions", d, idem=f"sub-{a['id']}-{tier}-{int(time.time())}", client=client)
    q("insert into stripe_sessions(id,account_id,kind,tier,status,created,updated) values(?,?,?,?,?,?,?)", (s["id"], a["id"], "subscription", tier, "created", time.time(), time.time()))
    return {"id": s["id"], "url": s.get("url"), "tier": tier}
def portal(a, client=None):
    _, ret = _urls()
    return {"url": _call("POST", "/billing_portal/sessions", {"customer": ensure_customer(a, client), "return_url": ret}, client=client)["url"]}
def verify_signature(raw: bytes, header: str, secret=None, tolerance=300, now=None):
    secret = secret or E("STRIPE_WEBHOOK_SECRET")
    if not secret or not header: return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    ts = next((v for k, v in parts if k == "t"), None); sigs = [v for k, v in parts if k == "v1"]
    if not ts or not sigs or not ts.isdigit(): return False
    if abs((now or time.time()) - int(ts)) > tolerance: return False
    exp = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(exp, s) for s in sigs)
def sign(raw: bytes, secret, ts=None):
    """Build a Stripe-Signature header (tests / local replays)."""
    ts = int(ts or time.time()); return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
def _acct_from(obj):
    md = obj.get("metadata") or {}
    if md.get("account_id"): return int(md["account_id"])
    if obj.get("client_reference_id"): return int(obj["client_reference_id"])
    r = q("select id from accounts where stripe_customer=?", (obj.get("customer"),), one=True) if obj.get("customer") else None
    return r["id"] if r else None
def fulfill_topup(s):
    """Credit exactly once per Checkout Session; the credited amount is the pre-tax subtotal we created the session for."""
    row = q("select * from stripe_sessions where id=?", (s["id"],), one=True)
    if not row or row["kind"] != "topup": return {"ok": False, "error": "unknown session"}
    if s.get("amount_subtotal") is not None and int(s["amount_subtotal"]) != int(row["amount_cents"]): return {"ok": False, "error": "amount mismatch"}
    with tx():   # status flip + wallet credit commit together; a failure rolls both back so Stripe's retry credits it
        won = q("update stripe_sessions set status='paid', payment_intent=?, invoice_id=?, updated=? where id=? and status!='paid' returning id",
                (s.get("payment_intent"), s.get("invoice"), time.time(), s["id"]))
        if not won: return {"ok": True, "duplicate": True}
        billing.credit_rows(row["account_id"], row["credit_cents"], "topup", f"stripe {s['id']}" + (f" (total incl. tax ${s['amount_total']/100:.2f})" if s.get("amount_total") else ""))
    try: billing.resume_suspended(row["account_id"])
    except Exception: log.exception("resume after top-up failed for account %s", row["account_id"])
    return {"ok": True, "credited_cents": row["credit_cents"]}
def set_tier(acct, tier, sub_id=None, why="stripe"):
    from .plans import TIERS
    if tier not in TIERS: return
    q("update accounts set tier=?, stripe_subscription=?, month_key=? where id=?", (tier, sub_id, billing.month_key(), acct))
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (acct, time.time(), 0, "plan", f"{tier} via {why}" + (f" {sub_id}" if sub_id else "")))
    if TIERS[tier].get("hard_cap"):     # dropped to Free: bigger apps can't keep running for free
        bad = [r["name"] for r in q("select name, spec from apps where account_id=? and status in ('running','idle')", (acct,))
               if load_spec(r["spec"]).get("plan") not in TIERS[tier]["plans"]]
        if bad: billing.cap_account(acct)
    else: billing.uncap_account(acct, f"{tier} plan")
def _rank(t): return {"free": 0, "hobby": 1, "pro": 2}.get(t or "free", 0)
def _track_upgrade(acct, tier, o):
    """plan_upgraded analytics: plan, amount_usd, interval (from the Checkout Session or Subscription). Never raises."""
    try:
        from . import analytics
        from .plans import TIERS
        amt = o.get("amount_total"); interval = None
        items = ((o.get("items") or {}).get("data") or [])
        if items:
            pr = items[0].get("price") or {}; interval = (pr.get("recurring") or {}).get("interval")
            if amt is None and pr.get("unit_amount") is not None: amt = pr["unit_amount"] * (items[0].get("quantity") or 1)
        usd = round(amt / 100, 2) if isinstance(amt, (int, float)) else TIERS.get(tier, {}).get("fee_usd")
        analytics.capture(acct, "plan_upgraded", {"plan": tier, "amount_usd": usd, "interval": interval or "month"})
    except Exception: pass
def handle_webhook(raw: bytes, sig_header):
    if not verify_signature(raw, sig_header): return 400, {"error": "invalid signature"}
    ev = json.loads(raw); eid = ev.get("id")
    if not eid: return 400, {"error": "no event id"}
    if not q("insert into stripe_events(id,type,received) values(?,?,?) on conflict(id) do nothing returning id", (eid, ev.get("type"), time.time())):
        return 200, {"ok": True, "duplicate_event": True}
    try: return _process(ev)
    except Exception:
        # F1: un-record the event so Stripe's retry is processed again instead of being answered "duplicate_event".
        log.exception("stripe webhook %s (%s) failed; event un-recorded for retry", eid, ev.get("type"))
        try: q("delete from stripe_events where id=?", (eid,))
        except Exception: log.exception("could not un-record stripe event %s", eid)
        return 500, {"error": "processing failed; retry"}
PAID_OK = ("paid", "no_payment_required")
def tier_for_price(price_id):
    """F3: map a Stripe price id to our tier using the configured STRIPE_PRICE_HOBBY / STRIPE_PRICE_PRO."""
    if not price_id: return None
    for tier, env in (("hobby", "STRIPE_PRICE_HOBBY"), ("pro", "STRIPE_PRICE_PRO")):
        if E(env) and E(env) == price_id: return tier
    return None
def _sub_tier(o):
    items = ((o.get("items") or {}).get("data") or [])
    pid = ((items[0].get("price") or {}).get("id")) if items else None
    tier = tier_for_price(pid)
    if not tier: log.warning("stripe subscription %s: unknown price id %r; plan unchanged", o.get("id"), pid)
    return tier
def _process(ev):
    t = ev.get("type"); o = (ev.get("data") or {}).get("object") or {}
    if t in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if o.get("mode") == "payment":
            return 200, (fulfill_topup(o) if o.get("payment_status") == "paid" else {"ok": True, "pending": o.get("payment_status")})
        if o.get("mode") == "subscription":
            acct = _acct_from(o); tier = (o.get("metadata") or {}).get("tier")
            if acct and tier and o.get("payment_status") not in PAID_OK:   # F2: async/delayed methods wait for async_payment_succeeded
                return 200, {"ok": True, "pending": o.get("payment_status")}
            if acct and tier:
                if o.get("customer"): q("update accounts set stripe_customer=? where id=? and stripe_customer is null", (o["customer"], acct))
                q("update stripe_sessions set status='paid', updated=? where id=?", (time.time(), o.get("id")))
                set_tier(acct, tier, o.get("subscription"), why="stripe checkout"); _track_upgrade(acct, tier, o)
                return 200, {"ok": True, "tier": tier}
        return 200, {"ok": True, "ignored": "session"}
    if t == "invoice.paid":
        acct = _acct_from(o)
        if acct:
            q("insert into stripe_sessions(id,account_id,kind,amount_cents,status,invoice_id,invoice_url,created,updated) values(?,?,?,?,?,?,?,?,?) on conflict(id) do nothing",
              (o.get("id"), acct, "invoice", o.get("amount_paid"), "paid", o.get("id"), o.get("hosted_invoice_url"), time.time(), time.time()))
            q("update stripe_sessions set invoice_url=? where invoice_id=? and kind='topup'", (o.get("hosted_invoice_url"), o.get("id")))
        return 200, {"ok": True}
    if t in ("customer.subscription.deleted", "customer.subscription.updated"):
        r = q("select id, tier from accounts where stripe_subscription=?", (o.get("id"),), one=True)
        if not r: return 200, {"ok": True, "ignored": "unknown subscription"}
        if t.endswith("deleted") or o.get("status") in ("canceled", "unpaid", "incomplete_expired"):
            set_tier(r["id"], "free", None, why=f"stripe {t} ({o.get('status')})"); return 200, {"ok": True, "tier": "free"}
        tier = _sub_tier(o)
        if tier and tier != r["tier"]:
            set_tier(r["id"], tier, o["id"], why="stripe plan change")
            if _rank(tier) > _rank(r["tier"]): _track_upgrade(r["id"], tier, o)
        return 200, {"ok": True}
    return 200, {"ok": True, "ignored": t}
