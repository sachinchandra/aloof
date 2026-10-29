"""QA oct29 round 4: F9 every platform cookie is __Host- (Secure, Path=/, no Domain), old names ignored; OAuth flows end to end
in secure mode for Google, GitHub and X (incl. X without email); F8c row lock in password login and first verification."""
import re, time, pytest
from urllib.parse import urlsplit, parse_qs
from fastapi.testclient import TestClient
from paas import api, auth, oauth_login, admin_ops, engine
from paas.db import q
def _n(): return time.time_ns()
@pytest.fixture
def secure(monkeypatch):
    monkeypatch.setattr(api, "SECURE_COOKIES", True); monkeypatch.setattr(auth, "COOKIE", "__Host-aloof_session")
    monkeypatch.setattr(oauth_login, "STATE_COOKIE", "__Host-aloof_oauth_state"); monkeypatch.setattr(api, "PENDING_COOKIE", "__Host-aloof_oauth_pending")
    monkeypatch.setattr(api, "ATTR_COOKIE", "__Host-aloof_attr"); monkeypatch.setattr(admin_ops, "BACK", "__Host-aloof_admin_back")
    monkeypatch.setenv("PAAS_OAUTH_BASE_URL", "https://testserver")
    for p, v in oauth_login.PROVIDERS.items():
        monkeypatch.setenv(v["env"] + "_CLIENT_ID", "cid"); monkeypatch.setenv(v["env"] + "_CLIENT_SECRET", "sec"); monkeypatch.delenv(f"AUTH_{p.upper()}_ENABLED", raising=False)
    monkeypatch.setattr(oauth_login, "_exchange", lambda p, code, ver: {"access_token": "at"})
    sent = []; monkeypatch.setattr(auth, "send_mail", lambda to, s, b, html=None, headers=None: sent.append(b) or "smtp")
    api.netaddr._lim.clear() if hasattr(api, "netaddr") else None
    return sent
def _host_ok(r):
    sc = r.headers.get_list("set-cookie")
    for v in sc:
        name = v.split("=", 1)[0]; low = v.lower()
        assert name.startswith("__Host-"), v
        assert "secure" in low and "path=/" in low and "path=/auth" not in low and "domain=" not in low, v
    return {v.split("=", 1)[0]: v.split("=", 1)[1].split(";")[0] for v in sc}
def _start(c, p):
    r = c.get(f"/auth/{p}/start?next=/dashboard&utm_source=t", follow_redirects=False); assert r.status_code == 303, r.text
    ck = _host_ok(r); assert "__Host-aloof_oauth_state" in ck and "__Host-aloof_attr" in ck
    return parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
@pytest.mark.parametrize("p", ["google", "github", "x"])
def test_oauth_signup_e2e_host_cookies(secure, monkeypatch, p):
    em = f"{p}{_n()}@realco.io"
    monkeypatch.setattr(oauth_login, "profile", lambda p_, tok, nonce: {"sub": f"s{_n()}", "email": em, "email_verified": True, "name": "n"})
    c = TestClient(api.app, base_url="https://testserver"); st = _start(c, p)
    r = c.get(f"/auth/{p}/callback?code=c&state={st}", follow_redirects=False); assert r.status_code == 303 and r.headers["location"] == "/dashboard", r.headers.get("location")
    ck = _host_ok(r); assert ck.get("__Host-aloof_session", "").startswith("s_")
    assert c.get("/v1/auth/me").json()["email"] == em
def test_x_without_email_pending_cookie(secure, monkeypatch):
    sub = f"x{_n()}"; monkeypatch.setattr(oauth_login, "profile", lambda p_, tok, nonce: {"sub": sub, "email": None, "email_verified": False, "name": "n"})
    c = TestClient(api.app, base_url="https://testserver"); st = _start(c, "x")
    r = c.get(f"/auth/x/callback?code=c&state={st}", follow_redirects=False); assert r.headers["location"].startswith("/auth/x/email")
    assert "__Host-aloof_oauth_pending" in _host_ok(r)
    assert c.get("/auth/x/email").status_code == 200
    em = f"xe{_n()}@realco.io"; assert c.post("/auth/x/email", json={"email": em}, headers={"X-Aloof-CSRF": "1"}).status_code == 200
    tok = re.search(r"token=(ml_[\w-]+)", secure[-1])[1]; c.get(f"/auth/magic?token={tok}", follow_redirects=False)
    a = q("select id from accounts where email=?", (em,), one=True); assert q("select 1 from provider_identities where account_id=? and subject=?", (a["id"], sub))
def test_old_cookie_names_ignored(secure, monkeypatch):
    monkeypatch.setattr(oauth_login, "profile", lambda p_, tok, nonce: {"sub": f"s{_n()}", "email": f"o{_n()}@realco.io", "email_verified": True, "name": "n"})
    c = TestClient(api.app, base_url="https://testserver"); st = _start(c, "github")
    c.cookies.clear(); c.cookies.set("aloof_oauth_state", st)          # tossed by tenant JS under the old name
    r = c.get(f"/auth/github/callback?code=c&state={st}", follow_redirects=False); assert "__Host-aloof_session" not in r.headers.get("set-cookie", "")
    c2 = TestClient(api.app, base_url="https://testserver"); c2.cookies.set("aloof_oauth_pending", "anything")
    assert c2.post("/auth/x/email", json={"email": "a@realco.io"}, headers={"X-Aloof-CSRF": "1"}).status_code == 400
def test_logout_clears_host_cookie(secure):
    e = f"l{_n()}@realco.io"; engine.create_account(e, auth.hash_password("pw-pw-pw-pw-1"), verified=True)
    c = TestClient(api.app, base_url="https://testserver"); r = c.post("/v1/auth/login", json={"email": e, "password": "pw-pw-pw-pw-1"}); _host_ok(r)
    r = c.post("/v1/auth/logout", headers={"X-Aloof-CSRF": "1"}); _host_ok(r); assert c.get("/v1/auth/me").status_code == 401
def test_row_lock_sql(monkeypatch):
    from paas import db
    seen = []; real = auth.q
    monkeypatch.setattr(auth, "q", lambda sql, *a, **k: seen.append(sql) or real(sql.replace(" for update", ""), *a, **k))
    monkeypatch.setattr(db, "PG", True)
    e = f"k{_n()}@realco.io"; a = engine.create_account(e, auth.hash_password("pw-pw-pw-pw-1"), verified=False)
    auth.login(e, "pw-pw-pw-pw-1", session=True); auth.secure_first_verification(a["id"])
    assert sum(s.endswith(" for update") for s in seen) == 2
def test_no_unprefixed_platform_cookie_names_in_source():
    import pathlib
    src = "".join(pathlib.Path(f).read_text() for f in ("paas/api.py", "paas/admin_ops.py", "paas/www.py", "paas/oauth_login.py"))
    assert not re.search(r'set_cookie\("aloof_', src)
