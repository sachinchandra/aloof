"""Admin promo view and first-time eligibility: Stripe GETs only."""
import pytest
from fastapi import HTTPException
from paas import admin_ops, engine
from paas.db import q
PC = {"data": [{"id": "promo_x", "code": "FIRST1", "active": True, "times_redeemed": 2, "max_redemptions": 50, "expires_at": 1798750740,
                "restrictions": {"first_time_transaction": True}, "coupon": {"percent_off": 100.0, "duration": "repeating", "duration_in_months": 1,
                "applies_to": {"products": ["prod_hobby"]}}}]}
@pytest.fixture
def calls(monkeypatch):
    seen = []
    def fake(method, path, data=None, idem=None, client=None):
        seen.append((method, path)); assert method == "GET"
        if path.startswith("/promotion_codes"): return PC
        if path.startswith("/charges"): return {"data": [{"status": "succeeded", "amount": 900, "refunded": False}] if "cus_paid" in path else []}
        if path.startswith("/invoices"): return {"data": []}
    from paas import stripe_billing; monkeypatch.setattr(stripe_billing, "_call", fake); return seen
def test_promos_listed(calls):
    p = admin_ops.promos(None)["promos"][0]
    assert p["code"] == "FIRST1" and p["used"] == 2 and p["max"] == 50 and p["remaining"] == 48 and p["first_time_only"] and p["applies_to_products"] == ["prod_hobby"]
    assert all(m == "GET" for m, _ in calls)
def test_eligibility(calls):
    new = engine.create_account("promo-new@aloof-test.dev"); paid = engine.create_account("promo-paid@aloof-test.dev")
    q("update accounts set stripe_customer='cus_paid' where id=?", (paid["id"],)); q("update accounts set stripe_customer='cus_new' where id=?", (new["id"],))
    assert admin_ops.promo_eligibility(new["id"], None)["eligible_first_time_codes"] is True
    e = admin_ops.promo_eligibility(paid["id"], None); assert e["has_prior_paid"] and not e["eligible_first_time_codes"] and e["stripe_paid_charges"] == 1
    nostripe = engine.create_account("promo-none@aloof-test.dev")
    assert admin_ops.promo_eligibility(nostripe["id"], None)["eligible_first_time_codes"] is True
    assert all(m == "GET" for m, _ in calls)

def test_promo_routes_ignore_client_query_param(monkeypatch):
    """?client= must not reach the Stripe client (test hook is internal only), like refund in c348d33."""
    import inspect, time
    from test_admin_cockpit import owner
    from paas import stripe_billing
    assert "client" not in inspect.signature(admin_ops.promos).parameters and "client" not in inspect.signature(admin_ops.promo_eligibility).parameters
    got = []
    def fake(method, path, data=None, idem=None, client=None):
        got.append(client)
        return PC if path.startswith("/promotion_codes") else {"data": []}
    monkeypatch.setattr(stripe_billing, "_call", fake)
    o = owner(); a = engine.create_account(f"promo-q{time.time_ns()}@aloof-test.dev"); q("update accounts set stripe_customer='cus_q' where id=?", (a["id"],))
    assert o.get("/v1/admin/promos?client=evil").status_code == 200
    assert o.get(f"/v1/admin/accounts/{a['id']}/promo-eligibility?client=evil").status_code == 200
    assert got and all(c is None for c in got)
