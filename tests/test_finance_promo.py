"""Finance prep Nov 3: one promo per person, past-due limit/restore, starting-credit clawback. All flags default off. Test fixtures only."""
import pytest
from fastapi.testclient import TestClient
from paas import api, stripe_billing as S, promo_policy as P
from paas.db import q
from test_unit import _acct_client, _stripe_ev
w = TestClient(api.app)

def _acct(email, sub):
    c = _acct_client(email); aid = c.get("/v1/auth/me").json()["id"]
    q("update accounts set stripe_customer=? where id=?", ("cus_" + sub, aid)); return c, aid
def _post(evid, typ, o): raw, h = _stripe_ev(evid, typ, o); return w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()
def _co(aid, sub, disc=True, tier="hobby"):
    o = {"id": "cs_" + sub, "mode": "subscription", "payment_status": "paid", "customer": "cus_" + sub, "subscription": "sub_" + sub,
         "metadata": {"account_id": str(aid), "tier": tier}, "total_details": {"amount_discount": 500 if disc else 0}}
    if disc: o["discounts"] = [{"promotion_code": "promo_FIRST1"}]
    return o

def test_normalize_email():
    assert P.normalize_email("J.Doe+aloof@GoogleMail.com") == "jdoe@gmail.com"
    assert P.normalize_email("j.doe+x@company.dev") == "j.doe@company.dev"   # dots only ignored where the provider ignores them

def test_approved_defaults(monkeypatch):
    for k in ("PAAS_PROMO_ONE_PER_PERSON", "PAAS_PAST_DUE_ACTION", "PAAS_STARTER_CREDIT_PROMO"): monkeypatch.delenv(k, raising=False)
    assert (P.one_per_person(), P.past_due_action(), P.starter_mode()) == ("flag", "limit", "keep")
    _, aid = _acct("fp-off@example.com", "fpoff")
    assert P.offer_promo_codes(aid, "hobby") and not P.offer_promo_codes(aid, "pro")

def test_repeat_by_normalized_email_flagged_and_blocked(monkeypatch):
    monkeypatch.setenv("PAAS_PROMO_ONE_PER_PERSON", "flag")
    _, a1 = _acct("rep.eat+one@gmail.com", "rep1"); _, a2 = _acct("repeat+two@googlemail.com", "rep2")
    assert _post("evt_rep1", "checkout.session.completed", _co(a1, "rep1"))["promo"] == "ok"
    r = _post("evt_rep2", "checkout.session.completed", _co(a2, "rep2"))
    assert r["promo"] == "repeat" and r["tier"] == "hobby"            # flag mode never takes the plan away
    assert q("select 1 from audit where action='billing.promo_repeat' and account_id=?", (a2,), one=True)
    assert P.offer_promo_codes(a2, "hobby")                            # flag: still offered
    monkeypatch.setenv("PAAS_PROMO_ONE_PER_PERSON", "block")
    assert not P.offer_promo_codes(a2, "hobby") and not P.offer_promo_codes(a1, "hobby")
    _, a3 = _acct("fresh@example.com", "rep3"); assert P.offer_promo_codes(a3, "hobby")

def test_repeat_by_card_fingerprint(monkeypatch):
    monkeypatch.setenv("PAAS_PROMO_ONE_PER_PERSON", "flag")
    monkeypatch.setattr(S, "_call", lambda m, path, d=None, idem=None, client=None: {"default_payment_method": {"card": {"fingerprint": "fp_same"}}})
    _, a1 = _acct("card-a@example.com", "card1"); _, a2 = _acct("card-b@example.com", "card2")
    assert _post("evt_card1", "checkout.session.completed", _co(a1, "card1"))["promo"] == "ok"
    assert _post("evt_card2", "checkout.session.completed", _co(a2, "card2"))["promo"] == "repeat"

def test_no_discount_records_nothing(monkeypatch):
    monkeypatch.setenv("PAAS_PROMO_ONE_PER_PERSON", "flag")
    _, a = _acct("nodisc@example.com", "nod")
    assert "promo" not in _post("evt_nod", "checkout.session.completed", _co(a, "nod", disc=False))
    assert not q("select 1 from promo_redemptions where account_id=?", (a,), one=True)

def test_starter_clawback(monkeypatch):
    monkeypatch.setenv("PAAS_STARTER_CREDIT_PROMO", "clawback")
    _, a = _acct("claw@example.com", "claw"); q("update accounts set credit_cents=320 where id=?", (a,))
    assert _post("evt_claw", "checkout.session.completed", _co(a, "claw"))["starter_clawback_cents"] == 320
    assert q("select credit_cents from accounts where id=?", (a,), one=True)["credit_cents"] == 0
    _, b = _acct("claw-paid@example.com", "clawp")   # topped up before: paid money is never touched
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,0,1000,'topup','x')", (b,)); q("update accounts set credit_cents=1500 where id=?", (b,))
    assert "starter_clawback_cents" not in _post("evt_clawp", "checkout.session.completed", _co(b, "clawp"))
    _, c = _acct("claw-nodisc@example.com", "clawn")
    assert "starter_clawback_cents" not in _post("evt_clawn", "checkout.session.completed", _co(c, "clawn", disc=False))

@pytest.mark.parametrize("mode", ["free", "limit", "none"])
def test_past_due_and_restore(monkeypatch, mode):
    monkeypatch.setenv("PAAS_PAST_DUE_ACTION", mode); s = "pd" + mode
    c, a = _acct(f"{s}@example.com", s)
    _post(f"evt_{s}0", "checkout.session.completed", _co(a, s, disc=False))
    capped = []; monkeypatch.setattr(S.billing, "cap_account", lambda acct: capped.append(acct))
    inv = {"id": "in_" + s, "customer": "cus_" + s, "subscription": "sub_" + s, "attempt_count": 1, "metadata": {}}
    assert _post(f"evt_{s}1", "invoice.payment_failed", inv)["past_due"] == mode
    tier = q("select tier, past_due_since from accounts where id=?", (a,), one=True)
    assert tier["past_due_since"] and tier["tier"] == ("free" if mode == "free" else "hobby")
    assert capped == ([a] if mode == "limit" else [])
    paid = dict(inv, id="in_" + s + "b", amount_paid=500, lines={"data": [{"price": {"id": "price_hobby"}}]})
    _post(f"evt_{s}2", "invoice.paid", paid)
    r = q("select tier, past_due_since, stripe_subscription from accounts where id=?", (a,), one=True)
    assert r["tier"] == "hobby" and not r["past_due_since"] and r["stripe_subscription"] == "sub_" + s

def test_subscription_status_past_due_then_active(monkeypatch):
    monkeypatch.setenv("PAAS_PAST_DUE_ACTION", "free")
    _, a = _acct("pdsub@example.com", "pdsub"); _post("evt_pds0", "checkout.session.completed", _co(a, "pdsub", disc=False))
    sub = {"id": "sub_pdsub", "status": "past_due", "metadata": {"tier": "hobby"}, "items": {"data": [{"price": {"id": "price_hobby"}}]}}
    assert _post("evt_pds1", "customer.subscription.updated", sub)["past_due"] == "free"
    assert q("select tier from accounts where id=?", (a,), one=True)["tier"] == "free"
    assert _post("evt_pds2", "customer.subscription.updated", dict(sub, status="active"))["past_due"] == "cleared"
    assert q("select tier from accounts where id=?", (a,), one=True)["tier"] == "hobby"

def test_payment_failed_on_non_subscription_invoice_ignored():
    _, a = _acct("pdx@example.com", "pdx")
    assert "ignored" in _post("evt_pdx", "invoice.payment_failed", {"id": "in_x", "customer": "cus_pdx", "metadata": {}})
