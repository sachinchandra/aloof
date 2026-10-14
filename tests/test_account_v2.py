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
