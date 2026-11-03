"""Promotion codes are accepted on Hobby subscription checkout only (finance Nov 3), never on Pro or credit top-ups."""
import pytest
from paas import stripe_billing as S

@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(S, "_call", lambda m, path, d, idem=None, client=None: out.append(d) or {"id": f"cs_test_{__import__('uuid').uuid4().hex}", "url": "https://checkout.stripe.test/x"})
    monkeypatch.setattr(S, "ensure_customer", lambda a, client=None: "cus_test")
    monkeypatch.setattr(S, "tier_price", lambda tier: f"price_test_{tier}")
    return out

@pytest.mark.parametrize("tier,ok", [("hobby", True), ("pro", False)])
def test_subscription_promo_codes_hobby_only(sent, tier, ok):
    S.create_subscription_session({"id": 9001, "email": "promo@example.in"}, tier)
    assert sent[-1]["mode"] == "subscription" and sent[-1]["allow_promotion_codes"] is ok and sent[-1]["payment_method_collection"] == "always"

def test_topup_has_no_promo_codes(sent):
    S.create_topup_session({"id": 9002, "email": "promo2@example.in"}, 10)
    assert sent[-1]["mode"] == "payment" and "allow_promotion_codes" not in sent[-1]
