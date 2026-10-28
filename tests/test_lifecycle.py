"""Lifecycle emails: off by default, idempotent, skip test/opted-out, 1/day cap, one-click unsubscribe, one welcome per signup."""
import time, pytest
from fastapi.testclient import TestClient
from paas import api, auth, engine, lifecycle, analytics
from paas.db import q
@pytest.fixture
def mail(monkeypatch):
    sent = []; monkeypatch.setattr(auth, "send_mail", lambda to, s, b, html=None, headers=None: sent.append((to, s, b, html, headers)) or "smtp")
    monkeypatch.setattr(analytics, "capture", lambda *a, **k: None); return sent
def real(age_days=0, **kw):
    a = engine.create_account(f"lc{engine.secrets.token_hex(4)}@realco.io")
    q("update accounts set created=? where id=?", (time.time() - age_days * 86400, a["id"]))
    for k, v in kw.items(): q(f"update accounts set {k}=? where id=?", (v, a["id"]))
    return a
def test_off_by_default(mail, monkeypatch):
    monkeypatch.delenv("LIFECYCLE_EMAILS_ENABLED", raising=False)
    a = real(); assert lifecycle.tick() == 0 and lifecycle.send(a["id"], "welcome_onboarding") == "skipped" and mail == []
    assert not q("select 1 from lifecycle_mail where account_id=?", (a["id"],), one=True)
def test_send_idempotent_cap_and_skips(mail, monkeypatch):
    monkeypatch.setenv("LIFECYCLE_EMAILS_ENABLED", "1")
    a = real(1.5)
    assert lifecycle.send(a["id"], "no_deploy_24h") == "sent" and lifecycle.send(a["id"], "no_deploy_24h") == "skipped"
    assert lifecycle.send(a["id"], "day4_tip") == "skipped"            # 1 per 24h
    h = mail[-1][4]; assert h["List-Unsubscribe"].startswith("<") and h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "utm_campaign=onboarding_v1" in mail[-1][2] and "utm_content=e2" in mail[-1][2] and "Unsubscribe" in mail[-1][3]
    t = real(0.1, is_test=1); o = real(0.1, email_optout=1)
    assert lifecycle.send(t["id"], "welcome_onboarding") == "skipped" and lifecycle.send(o["id"], "welcome_onboarding") == "skipped"
def test_tick_rules(mail, monkeypatch):
    monkeypatch.setenv("LIFECYCLE_EMAILS_ENABLED", "1")
    new, nud, tip = real(0.01), real(1.5), real(4.5)
    lifecycle.tick()
    kinds = {r["account_id"]: r["kind"] for r in q("select account_id, kind from lifecycle_mail where account_id in (?,?,?) and status='sent'", (new["id"], nud["id"], tip["id"]))}
    assert kinds[new["id"]] == "welcome_onboarding" and kinds[tip["id"]] == "day4_tip" and kinds[nud["id"]] in ("welcome_onboarding", "no_deploy_24h")
    n = len(mail); lifecycle.tick(); assert len(mail) == n             # nothing twice
def test_one_click_unsubscribe(mail):
    a = real(); u = lifecycle.unsub_url(a["id"]).split("/email/unsubscribe")[1]
    c = TestClient(api.app)
    assert c.post("/email/unsubscribe" + u.replace(lifecycle.token(a["id"]), "0" * 32), data={"List-Unsubscribe": "One-Click"}).status_code == 400
    assert c.get("/email/unsubscribe" + u).status_code == 200
    r = c.post("/email/unsubscribe" + u, data={"List-Unsubscribe": "One-Click"}); assert r.status_code == 200
    assert q("select email_optout from accounts where id=?", (a["id"],), one=True)["email_optout"] == 1
def test_dashboard_toggle(mail):
    a = real(); c = TestClient(api.app); c.cookies.set(auth.COOKIE, auth.new_session(a["id"]))
    assert c.put("/v1/account/email-prefs", json={"onboarding": False}, headers={"X-Aloof-CSRF": "1"}).status_code == 200 and c.get("/v1/auth/me").json()["email_optout"] is True
    c.put("/v1/account/email-prefs", json={"onboarding": True}, headers={"X-Aloof-CSRF": "1"}); assert c.get("/v1/auth/me").json()["email_optout"] is False
def test_one_welcome_per_signup(mail, monkeypatch):
    monkeypatch.delenv("LIFECYCLE_EMAILS_ENABLED", raising=False)
    auth.register(f"pw{engine.secrets.token_hex(3)}@realco.io", "x" * 12)
    assert [m[1] for m in mail] == ["Welcome to Aloof"] and "Your Aloof account is ready" in mail[0][2]      # legacy key email while off
    mail.clear(); monkeypatch.setenv("LIFECYCLE_EMAILS_ENABLED", "1")
    a = auth.register(f"pw{engine.secrets.token_hex(3)}@realco.io", "x" * 12)
    assert len(mail) == 1 and a["api_key"] in mail[0][2] and "I'm Sachin" in mail[0][2]                      # onboarding welcome with key block
    mail.clear(); assert lifecycle.welcome(a["id"]) == "skipped" and mail == []
