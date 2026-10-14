"""Payments-closed UX (Stripe charges not enabled) and waiting for the platform database at startup."""
import httpx, pytest, psycopg
from fastapi.testclient import TestClient
from paas import api, stripe_billing as sb, engine, db
class _C:
    def __init__(self, resp): self.resp, self.n = resp, 0
    def get(self, *a, **k):
        self.n += 1
        if isinstance(self.resp, Exception): raise self.resp
        return httpx.Response(200, json=self.resp, request=httpx.Request("GET", "https://api.stripe.com/v1/account"))
@pytest.fixture(autouse=True)
def reset(): sb._acct.update(t=0.0, open=None); yield; sb._acct.update(t=0.0, open=None)
def test_payments_open_cached():
    c = _C({"charges_enabled": False})
    assert sb.payments_open(client=c, now=1000) is False and sb.payments_open(client=c, now=1500) is False and c.n == 1
    c.resp = {"charges_enabled": True}; assert sb.payments_open(client=c, now=1000 + sb.PAYMENTS_TTL + 1) is True and c.n == 2
def test_payments_open_stripe_down_keeps_last():
    assert sb.payments_open(client=_C(httpx.ConnectError("x")), now=10) is True   # first boot: don't hide billing on a blip
    sb._acct.update(t=0, open=False); assert sb.payments_open(client=_C(httpx.ConnectError("x")), now=10**6) is False
def test_checkout_503_and_billing_flag_when_closed(monkeypatch):
    monkeypatch.setattr(sb, "payments_open", lambda client=None, now=None: False)
    called = []; monkeypatch.setattr(sb, "create_topup_session", lambda *a: called.append(1))
    a = engine.create_account("pay-closed@x.dev"); h = {"Authorization": f"Bearer {a['api_key']}"}; c = TestClient(api.app)
    for path, body in [("/v1/billing/checkout", {"credit_usd": 25}), ("/v1/billing/subscribe", {"tier": "hobby"})]:
        r = c.post(path, json=body, headers=h); assert r.status_code == 503 and "opening soon" in r.json()["detail"] and "Nothing was charged" in r.json()["detail"]
    assert not called and c.get("/v1/billing", headers=h).json()["payments_open"] is False
def test_dashboard_has_soon_note():
    s = open("dashboard/index.html").read(); assert "Paid plans opening soon" in s and "b.payments_open!==false" in s
def test_wait_for_db_retries_then_succeeds():
    t = [0.0]; slept = []; n = [0]
    def connect():
        n[0] += 1
        if n[0] < 4: raise psycopg.OperationalError("connection refused")
    assert db.wait_for_db("postgres://x", timeout=60, connect=connect, sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)), now=lambda: t[0]) == 4
    assert slept == [0.5, 1.0, 2.0]
def test_wait_for_db_times_out():
    t = [0.0]
    def connect(): raise psycopg.OperationalError("refused")
    with pytest.raises(RuntimeError, match="not reachable after 10s"):
        db.wait_for_db("postgres://x", timeout=10, connect=connect, sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0])
