"""QA oct29 round 2: F8 pre-account takeover, F2 email normalisation, F6 unsubscribe rate limit, F7 waitlist one-click, no-store/no analytics."""
import re, time, pytest
from fastapi.testclient import TestClient
from paas import api, auth, engine, waitlist, lifecycle, netaddr
from paas.db import q
def _n(): return time.time_ns()
@pytest.fixture
def mail(monkeypatch):
    s = []; monkeypatch.setattr(auth, "send_mail", lambda to, subj, b, html=None, headers=None: s.append((to, subj, b, headers)) or "smtp"); return s
@pytest.fixture(autouse=True)
def _reset_limits(): netaddr._lim.clear(); yield; netaddr._lim.clear()
def _link(mail): return re.search(r"token=(ml_[\w-]+)", mail[-1][2])[1]
def test_preaccount_takeover_attacker_loses_everything(mail):
    e = f"victim{_n()}@realco.io"; atk = TestClient(api.app)
    assert atk.post("/v1/auth/register", json={"email": e, "password": "attacker-pw-1"}).status_code == 200
    a = q("select * from accounts where email=?", (e,), one=True); old = atk.get("/v1/auth/me").json()
    k2 = engine.create_named_key(a["id"], "extra"); assert engine.account_by_key(k2 if isinstance(k2, str) else k2["key"])
    mail.clear(); auth.start_magic(e, "http://testserver"); tok = _link(mail)
    vic = TestClient(api.app); r = vic.get(f"/auth/magic?token={tok}", follow_redirects=False); assert r.status_code == 303
    a2 = q("select * from accounts where id=?", (a["id"],), one=True)
    assert a2["email_verified"] == 1 and a2["password_hash"] is None and a2["api_key_hash"] != a["api_key_hash"]
    assert not q("select 1 from api_keys where account_id=?", (a["id"],)) and not q("select 1 from deploy_tokens where account_id=?", (a["id"],))
    assert atk.get("/v1/auth/me").status_code == 401                                   # attacker session ended
    with pytest.raises(PermissionError): auth.login(e, "attacker-pw-1")
    me = vic.get("/v1/auth/me").json(); assert me["new_api_key"].startswith("pk_") and vic.get("/v1/auth/me").json()["new_api_key"] is None
    assert engine.account_by_key(me["new_api_key"])["id"] == a["id"]
def test_same_session_verify_keeps_password_rotates_key(mail):
    e = f"own{_n()}@realco.io"; c = TestClient(api.app)
    key = c.post("/v1/auth/register", json={"email": e, "password": "my-own-pw-1"}).json().get("api_key")
    mail.clear(); auth.start_magic(e, "http://testserver"); c.get(f"/auth/magic?token={_link(mail)}", follow_redirects=False)
    a = q("select * from accounts where email=?", (e,), one=True); assert a["email_verified"] == 1 and a["password_hash"]
    auth.login(e, "my-own-pw-1"); nk = c.get("/v1/auth/me").json()["new_api_key"]
    assert nk and (key is None or engine.account_by_key(key) is None)
def test_verified_account_magic_login_changes_nothing(mail):
    e = f"v{_n()}@realco.io"; a = engine.create_account(e, auth.hash_password("pw-pw-pw-pw-1"), verified=True)
    auth.start_magic(e, "http://x"); auth.finish_magic(_link(mail))
    assert engine.account_by_key(a["api_key"])["id"] == a["id"] and q("select password_hash from accounts where id=?", (a["id"],), one=True)["password_hash"]
@pytest.mark.parametrize("bad", ["\u0455upport@aloof.si", "sup\u200bport@aloof.si", "sup..port@aloof.si", '"support"@aloof.si',
                                 "support%40aloof.si@aloof.si", "a@b@aloof.si", "x@al\u043eof.si", ".x@aloof.si", "x.@aloof.si", "x@aloof..si"])
def test_email_rejected(bad):
    with pytest.raises(ValueError): auth._check_email(bad)
def test_email_normalised_then_reserved():
    assert auth._check_email("Support@Aloof.si.") == "support@aloof.si"
    assert auth._check_email("support@\uff41\uff4c\uff4f\uff4f\uff46.\uff53\uff49") == "support@aloof.si"
    for e in ("support@aloof.si.", "sup.port@aloof.si", "s.u.p.p.o.r.t+x@aloof.si", "team@aloof.si", "info@enigmaticitsolutions.com", "ops@aloof.si", "support@\uff41\uff4c\uff4f\uff4f\uff46.\uff53\uff49"):
        r = TestClient(api.app).post("/v1/auth/register", json={"email": e, "password": "x" * 12}); assert r.status_code == 400, e
def test_unsubscribe_rate_limited():
    c = TestClient(api.app)
    for path in ("/email/unsubscribe?a=1&t=x", "/waitlist/unsubscribe?id=1&t=x"):
        netaddr._lim.clear(); codes = [c.get(path).status_code for _ in range(31)]
        assert codes[0] == 400 and codes[-1] == 429
        netaddr._lim.clear(); codes = [c.post(path, data={"List-Unsubscribe": "One-Click"}).status_code for _ in range(31)]
        assert codes[0] == 400 and codes[-1] == 429
def test_waitlist_one_click_and_headers(mail, monkeypatch):
    c = TestClient(api.app); e = f"w{_n()}@realco.io"
    q("insert into waitlist(kind,email,created,updated,is_test) values(?,?,?,?,0)", ("gpu", e, time.time(), time.time()))
    r = q("select id from waitlist where email=?", (e,), one=True); waitlist.send_emails(r["id"])
    h = [m[3] for m in mail if m[0] == e][0]; assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and h["List-Unsubscribe"].startswith("<")
    u = waitlist.unsub_url(r["id"], e).split("/waitlist/unsubscribe")[1]
    bad = c.post("/waitlist/unsubscribe?id=%d&t=%s" % (r["id"], "0" * 32), data={"List-Unsubscribe": "One-Click"}); assert bad.status_code == 400
    assert c.post("/waitlist/unsubscribe", data={"id": r["id"], "t": "nope"}).status_code == 400            # form, invalid: not "removed"
    g = c.get("/waitlist/unsubscribe" + u); assert g.status_code == 200 and g.headers["cache-control"] == "no-store" and "analytics.js" not in g.text
    ok = c.post("/waitlist/unsubscribe" + u, data={"List-Unsubscribe": "One-Click"}); assert ok.status_code == 200 and ok.text == "ok"
    assert not q("select 1 from waitlist where id=?", (r["id"],))
def test_email_unsubscribe_same_behaviour(monkeypatch):
    import paas.site as S; monkeypatch.setattr(S, "analytics_tag", lambda: "<script src=/static/analytics.js?v=9></script>")
    c = TestClient(api.app); a = engine.create_account(f"l{_n()}@realco.io"); u = lifecycle.unsub_url(a["id"]).split("/email/unsubscribe")[1]
    g = c.get("/email/unsubscribe" + u); assert g.status_code == 200 and g.headers["cache-control"] == "no-store" and "analytics.js" not in g.text
    assert c.post("/email/unsubscribe", data={"a": a["id"], "t": "nope"}).status_code == 400
    assert c.get("/email/unsubscribe?a=1&t=x").status_code == 400
    assert "analytics.js" in c.get("/pricing").text                                                         # other pages still load it
def test_docs():
    t = TestClient(api.app)
    from paas.contact import CONTACT_EMAIL   # site copy says privacy@aloof.si; contact.apply maps role addresses to the one contact mailbox
    b = t.get("/docs/billing").text; assert f'Email <a href="mailto:{CONTACT_EMAIL}">' in b and "Request it through <a href=\"/docs/support\">support</a>" not in t.get("/docs/billing").text
    assert "never queued" in t.get("/docs/sign-in").text
