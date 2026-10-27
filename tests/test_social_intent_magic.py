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
def test_connect_is_post_with_csrf(seen, monkeypatch):
    monkeypatch.setattr(oauth_login, "enabled", lambda p: True)
    c, a = signed_in()
    assert c.post("/auth/x/connect").status_code == 403 and "link" not in seen            # no CSRF header
    r = c.post("/auth/x/connect", headers={"X-Aloof-CSRF": "1"})
    assert r.status_code == 200 and r.json()["url"] and seen["link"] == a["id"] and oauth_login.STATE_COOKIE in r.headers["set-cookie"]
    assert TestClient(api.app).post("/auth/x/connect", headers={"X-Aloof-CSRF": "1"}).status_code == 401
def test_get_start_never_connects(seen):
    c, a = signed_in()
    for qs in ("intent=connect", "next=%2Fa&next=%2Fb", "intent=login&intent=connect"):
        r = c.get("/auth/x/start?" + qs, follow_redirects=False)
        assert "auth_error=state_invalid" in r.headers["location"], qs
    assert "link" not in seen
def test_magic_skips_test_addresses(monkeypatch):
    sent = []; monkeypatch.setattr(auth, "send_mail", lambda *a, **k: sent.append(a[0]))
    auth.start_magic("someone@example.com", "https://x"); assert sent == []
def test_magic_bounce_is_generic_200(monkeypatch):
    def boom(*a, **k): raise auth.MailBounce("550 bounced")
    monkeypatch.setattr(auth, "send_mail", boom); api._login_hits.clear()
    r = TestClient(api.app).post("/v1/auth/magic", json={"email": "real.person@gmail.com"})
    assert r.status_code == 200 and r.json()["ok"] is True and "bounced" not in r.text
def test_send_mail_recipient_refused_is_bounce(monkeypatch):
    import smtplib
    class S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, m): raise smtplib.SMTPRecipientsRefused({"bad@gmail.com": (550, b"no such user")})
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid"); monkeypatch.setattr(smtplib, "SMTP", S)
    with pytest.raises(auth.MailBounce): auth.send_mail("bad@gmail.com", "s", "b")
    def down(*a, **k): raise OSError("connection refused")
    monkeypatch.setattr(smtplib, "SMTP", down)
    with pytest.raises(auth.MailError) as e: auth.send_mail("bad@gmail.com", "s", "b")
    assert not isinstance(e.value, auth.MailBounce)   # outages stay MailError -> 503
