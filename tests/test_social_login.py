import os, re
from paas import site

L = site.login("Aloof")

def test_sso_container_hidden_by_default():
    assert re.search(r'<div class=sso id=sso[^>]*hidden>', L)
    assert re.search(r'<div class=ordiv id=ordiv hidden>', L)
    assert "id=autherr" not in L          # no empty error box
    assert "/auth/github/start" not in L

PV = [{"id": "github", "enabled": False}, {"id": "google", "enabled": True}, {"id": "x", "enabled": True}]
def test_buttons_server_rendered_enabled_only_in_order():
    h = site.login("Aloof", PV, "/dashboard/settings")
    assert "/auth/github/start" not in h
    assert h.index('href="/auth/google/start?next=%2Fdashboard%2Fsettings"') < h.index('href="/auth/x/start?next=%2Fdashboard%2Fsettings"')
    assert not re.search(r'<div class=sso id=sso[^>]*hidden>', h) and "Continue with X" in h

def test_error_rendered_server_side_named_and_generic():
    for c in site.AUTH_ERRORS:
        n = site.login("Aloof", PV, None, c, "x"); g = site.login("Aloof", PV, None, c, None)
        assert "id=autherr" in n and "that provider" not in n and "that provider" not in g and "{p}" not in n + g
    assert "Your email address at X isn't verified" in site.auth_error_text("email_unverified", "x")
    assert "GitHub" in site.auth_error_text("cancelled", "github") and "Google" in site.auth_error_text("provider_error", "google")
    assert site.auth_error_text("bogus", "x") == site.AUTH_ERROR_FALLBACK
    assert "&lt;b&gt;" in site.login("Aloof", PV, None, "cancelled", "<b>") or "<b>" not in site.auth_error_text("cancelled", "<b>")

def test_sso_before_email_form():
    assert L.index("id=sso") < L.index('type=email')

def test_every_error_code_has_message():
    for c in ["cancelled","email_unverified","email_linked_other","state_invalid","provider_error","rate_limited","provider_disabled"]:
        assert c in site.AUTH_ERRORS and all(x.strip() for x in site.AUTH_ERRORS[c])
    for m in site.AUTH_ERRORS.values():
        assert "babysit" not in " ".join(m).lower() and "{p}" not in m[1]


def test_no_env_flag():
    src = open(site.__file__).read()
    block = src[src.index("SSO_MARKS"):src.index("def login(")]
    assert "os.environ" not in block and "getenv" not in block

def test_brand_marks_and_ember_focus():
    L = site.login("Aloof", PV)
    assert all(c in L for c in ("#EA4335","#4285F4","#FBBC05","#34A853"))
    assert "min-height:48px" in L or "min-height: 48px" in L
    assert "outline:3px solid" in L

def test_x_email_page():
    h = site.x_email("Aloof")
    assert "One more step" in h and 'type=email' in h and "X-Aloof-CSRF" in h and 'href="/login"' in h or "href=/login" in h

def test_already_linked_and_email_linked_other_copy():
    assert "already_linked" in site.AUTH_ERRORS and "different Aloof account" in site.AUTH_ERRORS["already_linked"][0]
    m = site.AUTH_ERRORS["email_linked_other"][1]
    assert "email sign-in link" in m and "confirms" in m   # fits the unverified-account case too
    assert "no_email" not in site.AUTH_ERRORS

def test_x_email_handles_statuses_and_restart_keeps_next():
    h = site.x_email("Aloof")
    assert "fetch('/auth/x/email'" in h and "location.search" in h
    for c in ("403:", "400:", "429:", "503:"): assert c in h
    assert "Start X sign-in again" in h and "'/auth/x/start'+q" in h and "'/login'+q" in h
    assert "\x00" not in h

def test_x_email_mounted_and_requires_pending():
    from fastapi.testclient import TestClient
    from paas.api import app
    r = TestClient(app).get("/auth/x/email?next=/dashboard", follow_redirects=False)
    assert r.status_code == 303 and "auth_error=state_invalid" in r.headers["location"]

def test_dashboard_signin_methods_card():
    d = open(os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")).read()
    assert "id=s-ids" in d and "aria-busy=true" in d          # loading state
    assert "No accounts connected yet" in d                    # empty
    assert "Couldn\\'t load your sign-in methods" in d          # error
    assert "identConnect(this" in d and "api('POST','/auth/'" in d and "p.enabled?" in d
    assert "ask(''" in d.split("async function identUnlink")[1][:400]   # confirm before DELETE
    assert "#EA4335" in d and "ID_ORDER=['github','google','x']" in d
