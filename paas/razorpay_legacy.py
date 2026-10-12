"""OPTIONAL legacy Razorpay (INR) top-ups, disabled unless PAAS_RAZORPAY_ENABLED=1 and keys are set. Stripe (paas/stripe_billing.py)
is the primary provider. INR payments are converted to the USD-cent wallet at PAAS_USD_INR; the INR GST invoice is kept for Indian buyers.
Flow: POST /v1/billing/razorpay/orders {credit_inr} -> order for credit + 18% GST -> Checkout -> /v1/billing/razorpay/verify and/or
webhook payment.captured / order.paid (X-Razorpay-Signature = HMAC_SHA256(raw_body, webhook_secret)) -> credit once (idempotent)."""
import os, hmac, hashlib, json, time, httpx
from .db import q
from .plans import USD_INR
E = lambda k, d=None: os.environ.get(k, d)
GST_RATE = 0.18
RZP = lambda: E("RAZORPAY_API_BASE", "https://api.razorpay.com/v1")
SELLER = {"name": E("PAAS_SELLER_NAME", "Aloof (seller legal name not set)"), "gstin": E("PAAS_SELLER_GSTIN", ""), "state": E("PAAS_SELLER_STATE", ""),
          "address": E("PAAS_SELLER_ADDRESS", ""), "sac": "998315"}
STATES = {"01":"Jammu and Kashmir","02":"Himachal Pradesh","03":"Punjab","04":"Chandigarh","05":"Uttarakhand","06":"Haryana","07":"Delhi","08":"Rajasthan",
  "09":"Uttar Pradesh","10":"Bihar","11":"Sikkim","12":"Arunachal Pradesh","13":"Nagaland","14":"Manipur","15":"Mizoram","16":"Tripura","17":"Meghalaya",
  "18":"Assam","19":"West Bengal","20":"Jharkhand","21":"Odisha","22":"Chhattisgarh","23":"Madhya Pradesh","24":"Gujarat","26":"Dadra and Nagar Haveli and Daman and Diu",
  "27":"Maharashtra","29":"Karnataka","30":"Goa","31":"Lakshadweep","32":"Kerala","33":"Tamil Nadu","34":"Puducherry","35":"Andaman and Nicobar Islands",
  "36":"Telangana","37":"Andhra Pradesh","38":"Ladakh"}
def enabled(): return E("PAAS_RAZORPAY_ENABLED", "0") == "1" and configured()
def configured(): return bool(E("RAZORPAY_KEY_ID") and E("RAZORPAY_KEY_SECRET"))
def _auth(): return (E("RAZORPAY_KEY_ID"), E("RAZORPAY_KEY_SECRET"))
def create_order(account, credit_inr, client=None):
    if not enabled(): raise RuntimeError("Razorpay is disabled (set PAAS_RAZORPAY_ENABLED=1)")
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
    from .billing import credit
    cents = round(p["credit_inr"] * 100.0 / USD_INR, 2)
    credit(p["account_id"], cents, "topup", f"razorpay {order_id}/{payment_id} via {source} (₹{p['credit_inr']} @ ₹{USD_INR:g}/$)")
    inv = make_invoice(p, payment_id)
    return {"ok": True, "credited_inr": p["credit_inr"], "credited_cents": cents, "invoice": inv}
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
