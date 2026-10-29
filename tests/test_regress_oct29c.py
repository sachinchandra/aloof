"""QA oct29 round 3: F8a (__Host- cookie, no same-session exception, legacy/tossed cookie stripped, tenant Set-Cookie filter),
F8b (identities linked while unverified are removed; no linking until verified), F8c (single transaction, sessions ended last)."""
import re, time, pytest
from fastapi.testclient import TestClient
from paas import api, auth, engine, oauth_login, db
from paas.db import q
def _n(): return time.time_ns()
@pytest.fixture
def mail(monkeypatch):
    s = []; monkeypatch.setattr(auth, "send_mail", lambda to, subj, b, html=None, headers=None: s.append(b) or "smtp"); return s
def _link(mail): return re.search(r"token=(ml_[\w-]+)", mail[-1])[1]
def _prof(): return {"sub": f"gh{_n()}", "email": "atk@evil.test", "email_verified": True, "name": "atk"}
def test_identity_linked_while_unverified_is_removed(mail):
    e = f"v{_n()}@realco.io"; a = engine.create_account(e, auth.hash_password("attacker-pw-1"), verified=False)
    q("update accounts set email_verified=1 where id=?", (a["id"],)); oauth_login.link(a["id"], "github", _prof())   # e.g. a row from before the gate
    q("update accounts set email_verified=0 where id=?", (a["id"],))
    auth.start_magic(e, "http://x"); auth.finish_magic(_link(mail))
    assert not q("select 1 from provider_identities where account_id=?", (a["id"],))
    assert q("select password_hash from accounts where id=?", (a["id"],), one=True)["password_hash"] is None
def test_link_blocked_until_verified(mail):
    e = f"u{_n()}@realco.io"; c = TestClient(api.app)
    assert c.post("/v1/auth/register", json={"email": e, "password": "attacker-pw-1"}).status_code == 200
    a = q("select * from accounts where email=?", (e,), one=True)
    with pytest.raises(oauth_login.AuthError): oauth_login.link(a["id"], "github", _prof())
    with pytest.raises(oauth_login.AuthError): oauth_login.resolve("github", _prof(), link_account=a["id"])
    r = c.post("/auth/github/connect", headers={"X-Aloof-CSRF": "1"}); assert r.status_code in (403, 404)
    if oauth_login.enabled("github"): assert r.status_code == 403
def test_single_transaction_rolls_back_and_ends_sessions_last(monkeypatch, mail):
    e = f"t{_n()}@realco.io"; a = engine.create_account(e, auth.hash_password("pw-pw-pw-pw-1"), verified=False)
    s = auth.new_session(a["id"]); order = []
    real_q = auth.q
    def spy(sql, *a_, **k):
        order.append(sql.split()[0] + " " + sql.split()[1] + " " + sql.split()[2]); return real_q(sql, *a_, **k)
    monkeypatch.setattr(auth, "q", spy); auth.secure_first_verification(a["id"]); monkeypatch.setattr(auth, "q", real_q)
    assert order[0] == "update accounts set"   # password cleared first
    assert order[-1] == "delete from sessions" and order.index("delete from provider_identities") < order.index("delete from sessions")
    assert auth.session_account(s) is None
    b = engine.create_account(f"r{_n()}@realco.io", auth.hash_password("pw-pw-pw-pw-1"), verified=False)
    def boom(_): raise RuntimeError("x")
    monkeypatch.setattr(engine, "rotate_api_key", boom)
    with pytest.raises(RuntimeError): auth.secure_first_verification(b["id"])
    assert q("select password_hash from accounts where id=?", (b["id"],), one=True)["password_hash"]   # rolled back: all or nothing
def test_host_cookie_name_and_legacy_strip(monkeypatch, mail):
    monkeypatch.setattr(auth, "COOKIE", "__Host-aloof_session")
    e = f"h{_n()}@realco.io"; engine.create_account(e, None, verified=True); auth.start_magic(e, "http://testserver")
    c = TestClient(api.app); c.cookies.set("aloof_session", "tossed")
    r = c.get(f"/auth/magic?token={_link(mail)}", follow_redirects=False)
    sc = r.headers.get_list("set-cookie")
    assert any(v.startswith("__Host-aloof_session=") and "Path=/" in v and "domain=" not in v.lower() for v in sc)
    assert any(v.startswith("aloof_session=;") and "Domain=" in v for v in sc) and any(v.startswith("aloof_session=;") and "Domain=" not in v for v in sc)
    c2 = TestClient(api.app); c2.cookies.set("aloof_session", auth.new_session(q("select id from accounts where email=?", (e,), one=True)["id"]))
    assert c2.get("/v1/auth/me").status_code == 401     # legacy name no longer accepted
def test_tenant_set_cookie_filter():
    d = api.BASE_DOMAIN
    for bad in ("aloof_session=x; Path=/", "__Host-aloof_session=x; Path=/; Secure", f"a=1; Domain={d}", f"a=1; Domain=.{d}; Path=/", f"a=1; domain={d.upper()}."):
        assert not api.tenant_cookie_ok(bad), bad
    for ok in ("a=1; Path=/", f"a=1; Domain=app.{d}", "sid=2; HttpOnly"): assert api.tenant_cookie_ok(ok), ok
