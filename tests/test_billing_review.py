"""Billing review Oct 22: F1 (event recorded only after success), F2 (paid-only grants), F3 (price id -> tier), F5 (tax wording). Test fixtures only."""
import json
from fastapi.testclient import TestClient
from paas import api, stripe_billing
from paas.db import q
from test_unit import _acct_client, _stripe_ev

def _sub_acct(email, sub):
    c = _acct_client(email); aid = c.get("/v1/auth/me").json()["id"]
    q("update accounts set stripe_customer=? where id=?", ("cus_" + sub, aid)); return c, aid

def test_f2_unpaid_subscription_checkout_waits_for_async_success():
    c, aid = _sub_acct("f2@example.com", "f2"); w = TestClient(api.app)
    o = {"id": "cs_f2", "mode": "subscription", "payment_status": "unpaid", "customer": "cus_f2", "subscription": "sub_f2", "metadata": {"account_id": str(aid), "tier": "pro"}}
    raw, h = _stripe_ev("evt_f2a", "checkout.session.completed", o)
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["pending"] == "unpaid"
    assert c.get("/v1/billing").json()["month"]["tier"] == "free"
    raw, h = _stripe_ev("evt_f2b", "checkout.session.async_payment_succeeded", dict(o, payment_status="paid"))
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["tier"] == "pro"

def test_f3_portal_plan_change_uses_price_id():
    c, aid = _sub_acct("f3@example.com", "f3"); w = TestClient(api.app)
    raw, h = _stripe_ev("evt_f3a", "checkout.session.completed", {"id": "cs_f3", "mode": "subscription", "payment_status": "paid", "customer": "cus_f3",
                                                                    "subscription": "sub_f3", "metadata": {"account_id": str(aid), "tier": "pro"}})
    w.post("/v1/billing/stripe/webhook", content=raw, headers=h)
    # portal downgrade: metadata still says pro, the price is Hobby
    sub = {"id": "sub_f3", "status": "active", "metadata": {"tier": "pro"}, "items": {"data": [{"price": {"id": "price_hobby"}, "quantity": 1}]}}
    raw, h = _stripe_ev("evt_f3b", "customer.subscription.updated", sub)
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).status_code == 200
    assert c.get("/v1/billing").json()["month"]["tier"] == "hobby"
    raw, h = _stripe_ev("evt_f3c", "customer.subscription.updated", dict(sub, items={"data": [{"price": {"id": "price_pro"}}]}, metadata={}))
    w.post("/v1/billing/stripe/webhook", content=raw, headers=h); assert c.get("/v1/billing").json()["month"]["tier"] == "pro"
    raw, h = _stripe_ev("evt_f3d", "customer.subscription.updated", dict(sub, items={"data": [{"price": {"id": "price_unknown"}}]}, metadata={}))
    w.post("/v1/billing/stripe/webhook", content=raw, headers=h); assert c.get("/v1/billing").json()["month"]["tier"] == "pro"   # unknown price: unchanged
    raw, h = _stripe_ev("evt_f3e", "customer.subscription.deleted", dict(sub, status="canceled"))
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["tier"] == "free"
    assert stripe_billing.tier_for_price("price_hobby") == "hobby" and stripe_billing.tier_for_price(None) is None

def test_f1_failed_processing_is_retried_not_deduped(monkeypatch):
    c, aid = _sub_acct("f1@example.com", "f1"); w = TestClient(api.app, raise_server_exceptions=False)
    o = {"id": "cs_f1", "mode": "subscription", "payment_status": "paid", "customer": "cus_f1", "subscription": "sub_f1", "metadata": {"account_id": str(aid), "tier": "hobby"}}
    raw, h = _stripe_ev("evt_f1", "checkout.session.completed", o)
    real = stripe_billing.set_tier
    def boom(*a, **k): raise RuntimeError("db blip")
    monkeypatch.setattr(stripe_billing, "set_tier", boom)
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).status_code == 500
    assert not q("select id from stripe_events where id='evt_f1'")
    monkeypatch.setattr(stripe_billing, "set_tier", real)
    r = w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()                        # Stripe retry
    assert r.get("tier") == "hobby" and not r.get("duplicate_event")
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["duplicate_event"]

def test_f5_no_tax_collected_claims():
    from paas import site
    t = TestClient(api.app).get("/v1/plans").json()["tax"]
    assert "added at checkout" not in t and "taxes may apply where required" in t and "USD" in t
    src = open(site.__file__).read()
    assert "added at checkout" not in src and "shown at checkout" not in src

def test_f1_topup_credit_failure_rolls_back_status_and_retry_credits(monkeypatch):
    from paas import billing
    c, aid = _sub_acct("f1t@example.com", "f1t"); w = TestClient(api.app, raise_server_exceptions=False)
    q("insert into stripe_sessions(id,account_id,kind,amount_cents,credit_cents,status,created,updated) values(?,?,?,?,?,?,?,?)",
      ("cs_f1t", aid, "topup", 2500, 2500, "created", 0, 0))
    before = c.get("/v1/billing").json()["credit_cents"]
    obj = {"id": "cs_f1t", "mode": "payment", "payment_status": "paid", "amount_subtotal": 2500, "customer": "cus_f1t"}
    raw, h = _stripe_ev("evt_f1t", "checkout.session.completed", obj)
    real = billing.credit_rows
    def half(acct, cents, kind, ref):
        q("update accounts set credit_cents = credit_cents + ? where id=?", (cents, acct)); raise RuntimeError("ledger insert failed")
    monkeypatch.setattr(billing, "credit_rows", half)
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).status_code == 500
    assert q("select status from stripe_sessions where id='cs_f1t'", one=True)["status"] == "created"      # rolled back
    assert c.get("/v1/billing").json()["credit_cents"] == before                                           # partial credit rolled back
    monkeypatch.setattr(billing, "credit_rows", real)
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["credited_cents"] == 2500
    assert c.get("/v1/billing").json()["credit_cents"] == before + 2500
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["duplicate_event"]

def test_f3_unknown_price_ignores_stale_metadata(caplog):
    c, aid = _sub_acct("f3u@example.com", "f3u"); w = TestClient(api.app)
    raw, h = _stripe_ev("evt_f3u1", "checkout.session.completed", {"id": "cs_f3u", "mode": "subscription", "payment_status": "paid", "customer": "cus_f3u",
                                                                     "subscription": "sub_f3u", "metadata": {"account_id": str(aid), "tier": "hobby"}})
    w.post("/v1/billing/stripe/webhook", content=raw, headers=h)
    raw, h = _stripe_ev("evt_f3u2", "customer.subscription.updated", {"id": "sub_f3u", "status": "active", "items": {"data": [{"price": {"id": "price_pro"}}]}})
    w.post("/v1/billing/stripe/webhook", content=raw, headers=h); assert c.get("/v1/billing").json()["month"]["tier"] == "pro"
    raw, h = _stripe_ev("evt_f3u3", "customer.subscription.updated", {"id": "sub_f3u", "status": "active", "metadata": {"tier": "hobby"},
                                                                       "items": {"data": [{"price": {"id": "price_new_unconfigured"}}]}})
    with caplog.at_level("WARNING"): w.post("/v1/billing/stripe/webhook", content=raw, headers=h)
    assert c.get("/v1/billing").json()["month"]["tier"] == "pro" and "unknown price id" in caplog.text

def test_v13_idempotent_ddl_and_docs_wording():
    from paas import db, site
    sql = dict(db.MIGRATIONS)[13]
    assert "add column if not exists acct" in sql and "create index if not exists" in sql
    db.executescript("alter table kv add column if not exists acct TEXT" if db.PG else "select 1")
    db.migrate()                                                                                        # re-run is a no-op
    src = open(site.__file__).read(); assert "exclude tax" not in src
    page = TestClient(api.app).get("/docs/billing").text
    assert "taxes may apply where required" in page and "exclude tax" not in page
