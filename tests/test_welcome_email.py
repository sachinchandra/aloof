"""Welcome email at signup (PAAS_WELCOME_EMAIL, default off). send_mail is always faked: no real email."""
import ast, inspect, pytest
from paas import welcome, engine, lifecycle, main, controller
from paas.db import q
SENT = []
@pytest.fixture(autouse=True)
def fake_mail(monkeypatch):
    SENT.clear(); from paas import auth
    monkeypatch.setattr(auth, "send_mail", lambda to, subj, body, html=None, headers=None: SENT.append((to, subj, body, headers)) or "smtp")
    monkeypatch.delenv("LIFECYCLE_EMAILS_ENABLED", raising=False)
    yield
    q("delete from lifecycle_mail where kind='welcome'"); q("delete from accounts where email like 'wel5-%'")
def _mk(tag): a = engine.create_account(f"wel5-{tag}@welcometest.io", None, verified=True); q("update accounts set is_test=0 where id=?", (a["id"],)); return a
def _row(aid): return q("select status from lifecycle_mail where account_id=? and kind='welcome'", (aid,), one=True)

def test_flag_off_by_default_queues_and_sends_nothing(monkeypatch):
    monkeypatch.delenv("PAAS_WELCOME_EMAIL", raising=False)
    a = _mk("off"); assert not welcome.enabled() and _row(a["id"]) is None
    assert welcome.tick() == 0 and SENT == []

def test_flag_on_queues_at_signup_then_tick_sends_template(monkeypatch):
    monkeypatch.setenv("PAAS_WELCOME_EMAIL", "1")
    a = _mk("on"); assert _row(a["id"])["status"] == "queued" and SENT == []   # signup only queues
    assert welcome.tick() == 1 and _row(a["id"])["status"] == "sent"
    to, subj, body, h = SENT[0]
    assert to == a["email"] and subj == "Welcome to Aloof - your first deploy" and "/dashboard" in body and "unsubscribe" in body and "{" not in body and "# Marketing" not in body
    assert "List-Unsubscribe" in h and a["api_key"] not in body
    assert welcome.tick() == 0 and len(SENT) == 1   # once only

@pytest.mark.parametrize("path", ["api", "oauth"])
def test_every_signup_method_goes_through_create_account(path):
    from paas import api, oauth_login, auth
    src = inspect.getsource(api.signup) if path == "api" else inspect.getsource(oauth_login)
    assert "engine.create_account(" in src
    assert "engine.create_account(" in inspect.getsource(auth.register) and inspect.getsource(auth).count("engine.create_account(") >= 2

def test_skips_test_optout_and_when_lifecycle_on(monkeypatch):
    monkeypatch.setenv("PAAS_WELCOME_EMAIL", "1")
    t = _mk("test"); q("update accounts set is_test=1 where id=?", (t["id"],))
    o = _mk("opt"); q("update accounts set email_optout=1 where id=?", (o["id"],))
    u = engine.create_account("wel5-x@example.com"); q("update accounts set is_test=0 where id=?", (u["id"],))
    monkeypatch.setattr(lifecycle, "enabled", lambda: True); l = _mk("lc")
    assert welcome.tick() == 0 and SENT == [] and all(_row(x["id"])["status"] == "skipped" for x in (t, o, u, l))

def test_send_failure_marked_failed_never_raises(monkeypatch):
    monkeypatch.setenv("PAAS_WELCOME_EMAIL", "1"); from paas import auth
    def boom(*a, **k): raise auth.MailError("down")
    monkeypatch.setattr(auth, "send_mail", boom)
    a = _mk("fail"); assert welcome.tick() == 0 and _row(a["id"])["status"] == "failed"

def test_template_placeholder_editable(tmp_path):
    p = tmp_path / "w.txt"; p.write_text("Subject: Hey {dashboard_url}\n# note\nGo: {docs_url}\n")
    s, b = welcome.render(1, str(p)); assert s.startswith("Hey ") and "/docs" in b and "note" not in b
    p.write_text("no subject\n")
    with pytest.raises(ValueError): welcome.render(1, str(p))

def test_send_only_in_background_process():
    assert "_wel.tick" in inspect.getsource(controller.start)
    src = inspect.getsource(main.main); bg = src.index('PAAS_BACKGROUND", "1") == "1"')
    assert src.index("controller.start()") > bg and src.count("controller.start()") == 1
    assert "send_mail" not in inspect.getsource(welcome.queue)
