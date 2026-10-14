from fastapi.testclient import TestClient
from paas import api
def _login(email):
    c = TestClient(api.app); assert c.post("/v1/auth/register", json={"email": email, "password": "demo-password-123"}).status_code == 200; return c
def test_profile_and_sessions():
    a = _login("acct2@realmail.dev"); b = TestClient(api.app)
    assert b.post("/v1/auth/login", json={"email": "acct2@realmail.dev", "password": "demo-password-123"}).status_code == 200
    h = {"X-Aloof-CSRF": "1"}
    assert a.put("/v1/auth/profile", json={"name": "  Sachin C  "}, headers=h).json()["name"] == "Sachin C"
    me = a.get("/v1/auth/me").json(); assert me["name"] == "Sachin C" and me["tier"] == "free" and me["created"]
    s = a.get("/v1/auth/sessions").json(); assert len(s) == 2 and sum(x["current"] for x in s) == 1
    assert a.delete("/v1/auth/sessions/others", headers=h).json()["revoked"] == 1
    assert b.get("/v1/auth/me").status_code == 401 and a.get("/v1/auth/me").status_code == 200
def test_account_markup():
    import os
    d = open(os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")).read()
    for x in ("id=s-profile", "id=s-security", "id=s-notify", "Danger zone", "/docs/billing#delete", "Sign out other sessions"): assert x in d, x
def test_billing_daily():
    import time
    from paas.db import q
    c = _login("bill2@realmail.dev"); aid = c.get("/v1/auth/me").json()["id"]
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (aid, time.time() - 60, -250, "usage", "t"))
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (aid, time.time() - 30, 1000, "topup", "t"))
    d = c.get("/v1/billing/daily?days=7").json()["days"]; assert len(d) == 7 and d[-1]["usage_usd"] == 2.5 and sum(x["plan_usd"] for x in d) == 0
def test_console_closes_with_reason_and_analytics_js():
    import pytest
    from starlette.websockets import WebSocketDisconnect
    c = TestClient(api.app)
    with c.websocket_connect("/v1/apps/nope/console", headers={"origin": "http://localhost"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e: ws.receive_bytes()
    assert e.value.code == 4401 and "not signed in" in (e.value.reason or "")
    r = c.get("/static/analytics.js"); assert r.status_code == 200 and "javascript" in r.headers["content-type"]
