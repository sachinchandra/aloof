"""Connect needs intent=connect from a signed-in user; the login page always signs in. Magic link: no mail to test addresses, bounces stay generic."""
import pytest
from fastapi.testclient import TestClient
from paas import api, auth, oauth_login, engine
@pytest.fixture
def seen(monkeypatch):
    got = {}
    def fake_start(p, nxt, link_account=None): got["link"] = link_account; return "https://x.example/authorize", "st"
    monkeypatch.setattr(oauth_login, "start", fake_start); monkeypatch.setattr(api, "SECURE_COOKIES", False)
    api._login_hits.clear(); return got
def signed_in():
    a = engine.create_account(f"intent{engine.secrets.token_hex(3)}@aloof-test.dev", None, verified=True)
    c = TestClient(api.app); c.cookies.set(auth.COOKIE, auth.new_session(a["id"])); return c, a
def test_login_page_with_session_is_a_login(seen):
    c, a = signed_in(); r = c.get("/auth/x/start", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and seen["link"] is None
def test_connect_intent_links_current_account(seen):
    c, a = signed_in(); c.get("/auth/x/start?intent=connect", follow_redirects=False); assert seen["link"] == a["id"]
def test_connect_intent_without_session_refused(seen):
    r = TestClient(api.app).get("/auth/x/start?intent=connect", follow_redirects=False)
    assert "auth_error=state_invalid" in r.headers["location"] and "link" not in seen
def test_magic_skips_test_addresses(monkeypatch):
    sent = []; monkeypatch.setattr(auth, "send_mail", lambda *a, **k: sent.append(a[0]))
    auth.start_magic("someone@example.com", "https://x"); assert sent == []
def test_magic_bounce_is_generic_200(monkeypatch):
    def boom(*a, **k): raise auth.MailBounce("550 bounced")
    monkeypatch.setattr(auth, "send_mail", boom); api._login_hits.clear()
    r = TestClient(api.app).post("/v1/auth/magic", json={"email": "real.person@gmail.com"})
    assert r.status_code == 200 and r.json()["ok"] is True and "bounced" not in r.text
