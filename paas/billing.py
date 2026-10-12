"""Prepaid INR wallet with Razorpay.
Flow: POST /v1/billing/orders {credit_inr} -> Razorpay order for credit + 18% GST -> Checkout (dashboard) ->
POST /v1/billing/verify (checkout signature, HMAC_SHA256(order_id|payment_id, key_secret)) and/or webhook
payment.captured / order.paid (X-Razorpay-Signature = HMAC_SHA256(raw_body, webhook_secret)) -> credit once (idempotent)
-> GST tax invoice (CGST+SGST intra-state, IGST inter-state; place of supply = customer's state).
Metering: every minute running instances are charged at plan rate; apps are suspended when the balance runs out."""
import os, hmac, hashlib, json, time, httpx, logging
from .db import q, event
from .plans import PLANS, GST_RATE
from .crypto import load_spec
log = logging.getLogger("paas.billing")
E = lambda k, d=None: os.environ.get(k, d)
RZP = lambda: E("RAZORPAY_API_BASE", "https://api.razorpay.com/v1")
SELLER = {"name": E("PAAS_SELLER_NAME", "Aloof (seller legal name not set)"), "gstin": E("PAAS_SELLER_GSTIN", ""), "state": E("PAAS_SELLER_STATE", ""),
          "address": E("PAAS_SELLER_ADDRESS", ""), "sac": "998315"}   # SAC 998315: hosting & IT infrastructure provisioning
STATES = {"01":"Jammu and Kashmir","02":"Himachal Pradesh","03":"Punjab","04":"Chandigarh","05":"Uttarakhand","06":"Haryana","07":"Delhi","08":"Rajasthan",
  "09":"Uttar Pradesh","10":"Bihar","11":"Sikkim","12":"Arunachal Pradesh","13":"Nagaland","14":"Manipur","15":"Mizoram","16":"Tripura","17":"Meghalaya",
  "18":"Assam","19":"West Bengal","20":"Jharkhand","21":"Odisha","22":"Chhattisgarh","23":"Madhya Pradesh","24":"Gujarat","26":"Dadra and Nagar Haveli and Daman and Diu",
  "27":"Maharashtra","29":"Karnataka","30":"Goa","31":"Lakshadweep","32":"Kerala","33":"Tamil Nadu","34":"Puducherry","35":"Andaman and Nicobar Islands",
  "36":"Telangana","37":"Andhra Pradesh","38":"Ladakh"}
def configured(): return bool(E("RAZORPAY_KEY_ID") and E("RAZORPAY_KEY_SECRET"))
def _auth(): return (E("RAZORPAY_KEY_ID"), E("RAZORPAY_KEY_SECRET"))
def create_order(account, credit_inr, client=None):
    if not configured(): raise RuntimeError("Razorpay not configured (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)")
    credit_inr = int(credit_inr)
    if credit_inr < 100 or credit_inr > 500000: raise ValueError("top-up must be between ₹100 and ₹5,00,000")
    amount_paise = round(credit_inr * (1 + GST_RATE) * 100)
    c = client or httpx.Client(timeout=30)
    r = c.post(f"{RZP()}/orders", auth=_auth(), json={"amount": amount_paise, "currency": "INR", "receipt": f"acct{account['id']}-{int(time.time())}",
                                                       "notes": {"account_id": str(account["id"]), "credit_inr": str(credit_inr)}})
    if r.status_code >= 400: raise RuntimeError(f"razorpay order failed: {r.status_code} {r.text[:200]}")
    o = r.json()
    q("insert into payments(order_id,account_id,amount_paise,credit_inr,status,created,updated) values(?,?,?,?,?,?,?)",
      (o["id"], account["id"], amount_paise, credit_inr, "created", time.time(), time.time()))
    return {"order_id": o["id"], "amount": amount_paise, "currency": "INR", "key_id": E("RAZORPAY_KEY_ID"), "credit_inr": credit_inr,
            "gst_inr": round(amount_paise / 100 - credit_inr, 2), "name": "Aloof", "description": f"Aloof credit ₹{credit_inr} + 18% GST",
            "prefill": {"email": account["email"]}}
def verify_checkout_signature(order_id, payment_id, signature):
    exp = hmac.new(E("RAZORPAY_KEY_SECRET", "").encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, signature or "")
def verify_webhook_signature(raw: bytes, signature):
    secret = E("RAZORPAY_WEBHOOK_SECRET")
    if not secret: return False
    return hmac.compare_digest(hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), signature or "")
def capture(order_id, payment_id, amount_paise=None, source="checkout"):
    """Credit the wallet exactly once per order (atomic conditional update), then issue the GST invoice."""
    p = q("select * from payments where order_id=?", (order_id,), one=True)
    if not p: return {"ok": False, "error": "unknown order"}
    if amount_paise is not None and int(amount_paise) != p["amount_paise"]: return {"ok": False, "error": "amount mismatch"}
    won = q("update payments set status='paid', payment_id=?, updated=? where order_id=? and status!='paid' returning order_id", (payment_id, time.time(), order_id))
    if not won: return {"ok": True, "duplicate": True, "invoice": invoice_for(order_id)}
    q("update accounts set credit_inr = credit_inr + ? where id=?", (p["credit_inr"], p["account_id"]))
    q("insert into ledger(account_id,ts,amount_inr,kind,ref) values(?,?,?,?,?)", (p["account_id"], time.time(), p["credit_inr"], "topup", f"{order_id}/{payment_id} via {source}"))
    inv = make_invoice(p, payment_id); resume_suspended(p["account_id"])
    return {"ok": True, "credited_inr": p["credit_inr"], "invoice": inv}
def handle_webhook(raw: bytes, signature):
    if not verify_webhook_signature(raw, signature): return 400, {"error": "invalid signature"}
    ev = json.loads(raw); eid = ev.get("id") or ev.get("event_id")
    if eid:
        if q("select 1 from webhook_events where id=?", (eid,), one=True): return 200, {"ok": True, "duplicate_event": True}
        q("insert into webhook_events(id,received) values(?,?) on conflict(id) do nothing", (eid, time.time()))
    kind = ev.get("event"); pay = ((ev.get("payload") or {}).get("payment") or {}).get("entity") or {}
    if kind in ("payment.captured", "order.paid"):
        oid = pay.get("order_id") or (((ev.get("payload") or {}).get("order") or {}).get("entity") or {}).get("id")
        return 200, capture(oid, pay.get("id"), pay.get("amount"), source=f"webhook:{kind}")
    if kind == "payment.failed":
        q("update payments set status='failed', updated=? where order_id=? and status='created'", (time.time(), pay.get("order_id"))); return 200, {"ok": True}
    return 200, {"ok": True, "ignored": kind}
def fy(ts):
    t = time.localtime(ts); y = t.tm_year if t.tm_mon >= 4 else t.tm_year - 1; return f"{y}-{str(y + 1)[2:]}"
def make_invoice(p, payment_id):
    a = q("select * from accounts where id=?", (p["account_id"],), one=True); now = time.time()
    taxable = round(p["credit_inr"], 2); total = round(p["amount_paise"] / 100, 2); tax = round(total - taxable, 2)
    cust_state = (STATES.get((a.get("gstin") or "")[:2]) or a.get("billing_state") or "").strip()
    intra = bool(SELLER["state"]) and cust_state.lower() == SELLER["state"].lower()
    cgst = sgst = round(tax / 2, 2) if intra else 0.0; igst = 0.0 if intra else tax
    n = (q("select count(*) as c from invoices where number like ?", (f"ALF/{fy(now)}/%",), one=True)["c"] or 0) + 1
    number = f"ALF/{fy(now)}/{n:06d}"   # GST rule 46: <=16 chars, unique per financial year
    q("insert into invoices(number,account_id,order_id,payment_id,taxable_inr,cgst,sgst,igst,total_inr,place_of_supply,gstin,billing_name,created) "
      "values(?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict(order_id) do nothing",
      (number, a["id"], p["order_id"], payment_id, taxable, cgst, sgst, igst, total, cust_state or "unspecified", a.get("gstin"), a.get("billing_name") or a["email"], now))
    return invoice_for(p["order_id"])
def invoice_for(order_id): return q("select * from invoices where order_id=?", (order_id,), one=True)
def invoice_html(inv):
    s = SELLER; rows = "".join(f"<tr><td>{k}</td><td style='text-align:right'>₹{v:,.2f}</td></tr>" for k, v in
        [("Taxable value (cloud credit)", inv["taxable_inr"])] + ([("CGST @9%", inv["cgst"]), ("SGST @9%", inv["sgst"])] if inv["cgst"] else [("IGST @18%", inv["igst"])]) + [("Total", inv["total_inr"])])
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Invoice {inv['number']}</title><style>body{{font-family:system-ui;max-width:720px;margin:30px auto}}
td,th{{padding:6px;border-bottom:1px solid #ddd}}table{{width:100%;border-collapse:collapse}}</style></head><body>
<h2>Tax Invoice</h2><p><b>{s['name']}</b><br>{s['address']}<br>GSTIN: {s['gstin'] or '<i>not set</i>'} · State: {s['state'] or '<i>not set</i>'}</p>
<p>Invoice no: <b>{inv['number']}</b><br>Date: {time.strftime('%d %b %Y', time.localtime(inv['created']))}<br>Payment: {inv['payment_id']} (order {inv['order_id']})</p>
<p>Bill to: <b>{inv['billing_name']}</b><br>GSTIN: {inv['gstin'] or 'Unregistered'}<br>Place of supply: {inv['place_of_supply']}</p>
<table><tr><th style='text-align:left'>Description (SAC {s['sac']})</th><th style='text-align:right'>Amount</th></tr>{rows}</table>
<p style="color:#888;font-size:12px">Prepaid credit for Aloof cloud hosting services. Computer-generated invoice.</p></body></html>"""
EGRESS = {}   # filled by the edge proxy: app -> bytes since last tick
def month_key(ts=None): return time.strftime("%Y-%m", time.localtime(ts or time.time()))
def _bump(acct, month, kind, qty, cost):
    q("insert into usage_month(account_id,month,kind,qty,cost_inr) values(?,?,?,?,?) on conflict(account_id,month,kind) do update set "
      "qty=usage_month.qty+excluded.qty, cost_inr=usage_month.cost_inr+excluded.cost_inr", (acct, month, kind, qty, cost))
def _month(acct, month, kind):
    r = q("select qty, cost_inr from usage_month where account_id=? and month=? and kind=?", (acct, month, kind), one=True)
    return (r["qty"], r["cost_inr"]) if r else (0.0, 0.0)
def _debit(acct, amount, kind, ref, now):
    if amount <= 0: return
    q("update accounts set credit_inr = credit_inr - ? where id=?", (amount, acct))
    q("insert into ledger(account_id,ts,amount_inr,kind,ref) values(?,?,?,?,?)", (acct, now, -round(amount, 4), kind, ref))
def meter_tick(now=None):
    """Meter compute (per second), volumes (GB-month, provisioned size) and egress (per GB beyond the tier allowance).
    Tier fee is charged at the start of each month; usage up to the tier's included amount is not deducted."""
    from .plans import TIERS, RATES, HOURS_MONTH
    now = now or time.time(); mk = month_key(now); charges = {}   # acct -> {kind: (qty, cost)}
    add = lambda a, k, qty, cost: charges.setdefault(a, {}).__setitem__(k, tuple(x + y for x, y in zip(charges.get(a, {}).get(k, (0, 0)), (qty, cost))))
    for i in q("select i.id, i.started, a.account_id, a.spec from instances i join apps a on a.name=i.app where i.status='running'"):
        m = q("select last_billed from meter where instance_id=?", (i["id"],), one=True); last = m["last_billed"] if m else i["started"]
        hrs = max(0, now - last) / 3600
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (i["id"], now))
        add(i["account_id"], "compute", hrs, hrs * PLANS[load_spec(i["spec"])["plan"]]["price_inr_hr"])
    for v in q("select name, account_id, size_gb, created from volumes"):
        key = "vol:" + v["name"]; m = q("select last_billed from meter where instance_id=?", (key,), one=True); last = m["last_billed"] if m else v["created"]
        gbm = v["size_gb"] * max(0, now - last) / (HOURS_MONTH * 3600)
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (key, now))
        add(v["account_id"], "volume", gbm, gbm * RATES["volume_inr_gb_month"])
    drained = {k: EGRESS.pop(k) for k in list(EGRESS)}
    for app, nbytes in drained.items():
        a = q("select account_id from apps where name=?", (app,), one=True)
        if not a: continue
        acct = a["account_id"]; tier = TIERS.get((q("select tier from accounts where id=?", (acct,), one=True) or {}).get("tier") or "free", TIERS["free"])
        gb = nbytes / 1e9; used_gb, _ = _month(acct, mk, "egress"); free_left = max(0, tier["egress_gb"] - used_gb)
        add(acct, "egress", gb, max(0, gb - free_left) * RATES["egress_inr_gb"])
    # monthly tier fee (once per account per month)
    for a in q("select id, tier, month_key from accounts where month_key is null or month_key != ?", (mk,)):
        tier = TIERS.get(a["tier"] or "free", TIERS["free"])
        q("update accounts set month_key=? where id=?", (mk, a["id"])); uncap_account(a["id"], "new month")
        if tier["fee_inr"]: _debit(a["id"], tier["fee_inr"], "plan", f"{a['tier']} plan {mk}", now)
    for acct, kinds in charges.items():
        tier = TIERS.get((q("select tier from accounts where id=?", (acct,), one=True) or {}).get("tier") or "free", TIERS["free"])
        before = sum(_month(acct, mk, k)[1] for k in ("compute", "volume", "egress"))
        for k, (qty, cost) in kinds.items(): _bump(acct, mk, k, qty, cost)
        total = sum(c for _, c in kinds.values()); after = before + total
        billable = max(0, after - tier["included_usage_inr"]) - max(0, before - tier["included_usage_inr"])
        if tier.get("hard_cap"):     # free tier never draws on the wallet; at the cap its apps sleep until next month/upgrade
            if after >= tier["included_usage_inr"]: cap_account(acct)
            continue
        if billable > 0:
            _debit(acct, billable, "usage", "metered " + ", ".join(f"{k} {c:.2f}" for k, (_, c) in kinds.items() if c), now)
    if E("PAAS_SUSPEND_ON_ZERO", "1") == "1":
        from . import engine
        for a in q("select id from accounts where credit_inr <= 0"):
            for app in q("select name from apps where account_id=? and status in ('running','idle')", (a["id"],)):
                engine.scale(app["name"], 0, reason="suspended: balance exhausted")
                q("update apps set status='suspended' where name=?", (app["name"],)); event(app["name"], "suspended: prepaid balance exhausted; top up to resume")
def cap_account(acct):
    from . import engine
    for app in q("select name from apps where account_id=? and status in ('running','idle')", (acct,)):
        engine.scale(app["name"], 0, reason="free tier cap reached")
        q("update apps set status='capped' where name=?", (app["name"],))
        event(app["name"], "paused: free tier included usage used up for this month; upgrade or wait for next month")
def uncap_account(acct, why):
    for app in q("select name from apps where account_id=? and status='capped'", (acct,)):
        q("update apps set status='idle', updated=? where name=?", (time.time(), app["name"])); event(app["name"], f"unpaused ({why}); wakes on next request")
def month_summary(account_id, mk=None):
    from .plans import TIERS
    mk = mk or month_key(); a = q("select tier from accounts where id=?", (account_id,), one=True); t = TIERS.get((a or {}).get("tier") or "free", TIERS["free"])
    rows = {r["kind"]: {"qty": round(r["qty"], 4), "cost_inr": round(r["cost_inr"], 2)} for r in q("select kind, qty, cost_inr from usage_month where account_id=? and month=?", (account_id, mk))}
    used = sum(r["cost_inr"] for r in rows.values())
    return {"month": mk, "tier": (a or {}).get("tier") or "free", "fee_inr": t["fee_inr"], "included_usage_inr": t["included_usage_inr"],
            "egress_included_gb": t["egress_gb"], "usage": rows, "usage_total_inr": round(used, 2), "billable_inr": round(max(0, used - t["included_usage_inr"]), 2)}
def resume_suspended(account_id):
    from . import engine
    for app in q("select name from apps where account_id=? and status='suspended'", (account_id,)):
        q("update apps set status='running' where name=?", (app["name"],)); event(app["name"], "resumed after top-up")
        sp = load_spec(q("select spec from apps where name=?", (app["name"],), one=True)["spec"])
        engine.scale(app["name"], max(1, (sp.get("autoscale") or {}).get("min", 1)), reason="resume")
