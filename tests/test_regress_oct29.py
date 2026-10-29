"""QA oct29: F1 is_test never blocks transactional mail; F2 internal domains test only when verified + reserved staff local-parts;
F3 no URL is_test opt-in (in test_click_tracking); F4 analytics URLs keep only utm_* (in test_click_tracking + JS helper)."""
import time, pytest
from fastapi.testclient import TestClient
from paas import api, auth, engine, www, analytics
from paas.db import q
c = TestClient(api.app)
@pytest.fixture
def mail(monkeypatch):
    s = []; monkeypatch.setattr(auth, "send_mail", lambda to, subj, b, html=None, headers=None: s.append((to, subj)) or "smtp"); return s
def _n(): return time.time_ns()
def test_magic_link_sends_to_verified_internal_test_account(mail):
    e = f"qa{_n()}@aloof.si"; a = engine.create_account(e, None, verified=True); assert a["is_test"] == 1
    auth.start_magic(e, "https://api.aloof.si"); assert [m[0] for m in mail] == [e]
    auth.start_magic(f"owner{_n()}@enigmaticitsolutions.com", "https://api.aloof.si"); assert len(mail) == 2
def test_admin_flagged_test_account_still_gets_magic_link(mail):
    e = f"cust{_n()}@realco.io"; a = engine.create_account(e); q("update accounts set is_test=1 where id=?", (a["id"],))
    auth.start_magic(e, "https://x"); assert len(mail) == 1
def test_undeliverable_domains_still_skipped(mail):
    auth.start_magic(f"x{_n()}@example.com", "https://x"); auth.register(f"y{_n()}@foo.test", "x" * 12); assert mail == []
def test_password_welcome_sent_to_internal(mail):
    auth.register(f"dev{_n()}@aloof.si", "x" * 12); assert len(mail) == 1
def test_internal_unverified_is_real_until_verified(mail, monkeypatch):
    sent = []; monkeypatch.setattr(analytics, "identify", lambda i: sent.append(i) or True)
    e = f"dev{_n()}@aloof.si"; a = auth.register(e, "x" * 12)
    assert q("select is_test from accounts where id=?", (a["id"],), one=True)["is_test"] == 0
    tok = None
    def grab(to, subj, b, html=None, headers=None):
        nonlocal tok; import re; tok = re.search(r"token=(ml_[\w-]+)", b)[1]
    monkeypatch.setattr(auth, "send_mail", grab); auth.start_magic(e, "https://x"); auth.finish_magic(tok)
    assert q("select is_test from accounts where id=?", (a["id"],), one=True)["is_test"] == 1 and a["id"] in sent
def test_oauth_verified_internal_is_test():
    assert engine.create_account(f"g{_n()}@aloof.si", None, verified=True)["is_test"] == 1
def test_reserved_staff_local_parts(mail):
    for e in ("support@aloof.si", "Billing+x@Aloof.si", "sachin@enigmaticitsolutions.com", "postmaster@aloof.si"):
        r = c.post("/v1/auth/register", json={"email": e, "password": "x" * 12}); assert r.status_code == 400 and "reserved" in r.text, e
        assert c.post("/v1/signup", json={"email": e}).status_code == 400
    assert not engine.reserved_staff_email("support@realco.io") and not engine.reserved_staff_email("supportx@aloof.si")
def test_mark_flags_recomputes_both_ways(monkeypatch):
    sent = []; monkeypatch.setattr(www, "sync_test_flags", lambda ids: sent.extend(ids))
    u = engine.create_account(f"u{_n()}@aloof.si"); q("update accounts set is_test=1 where id=?", (u["id"],))      # old rule: unverified but test
    v = engine.create_account(f"v{_n()}@aloof.si"); q("update accounts set email_verified=1, is_test=0 where id=?", (v["id"],))
    r = engine.create_account(f"r{_n()}@realco.io"); q("update accounts set is_test=1 where id=?", (r["id"],))   # admin flag kept
    ch = www.mark_flags()
    g = lambda a: q("select is_test from accounts where id=?", (a["id"],), one=True)["is_test"]
    assert g(u) == 0 and g(v) == 1 and g(r) == 1 and {u["id"], v["id"]} <= set(ch) and r["id"] not in ch and set(ch) <= set(sent)
def test_js_url_cleaner_keeps_only_utm():
    import shutil, subprocess, os
    if not shutil.which("node"): pytest.skip("node not installed")
    js = open(os.path.join(os.path.dirname(api.__file__), "static", "analytics.js")).read()
    fn = js[js.index("function cu(u)"):js.index("\nfunction send(")]
    out = subprocess.run(["node", "-e", "var location={href:'https://www.aloof.si/'};" + fn + ";console.log(cu('https://www.aloof.si/email/unsubscribe?a=1&t=SECRET&utm_source=email&next=/x#h'))"], capture_output=True, text=True).stdout.strip()
    assert out == "https://www.aloof.si/email/unsubscribe?utm_source=email"
