import os, re
from paas import site

L = site.login("Aloof")

def test_sso_container_hidden_by_default():
    assert re.search(r'<div class=sso id=sso[^>]*hidden>', L)
    assert re.search(r'<div class=ordiv id=ordiv hidden>', L)
    assert re.search(r'<p id=autherr[^>]*hidden>', L)
    # no provider buttons in server HTML: only rendered when endpoint says enabled
    assert "/auth/github/start" not in L.split("<script")[0]

def test_fetches_providers_endpoint_once_and_respects_enabled():
    assert L.count("/v1/auth/providers") == 1
    assert ".enabled" in L

def test_order_github_google_x():
    i = L.index("['github','google','x']") if "['github','google','x']" in L else L.index('["github","google","x"]')
    assert i > 0

def test_sso_before_email_form():
    assert L.index("id=sso") < L.index('type=email')

def test_every_error_code_has_message():
    for c in ["cancelled","email_unverified","email_linked_other","state_invalid","provider_error","rate_limited","provider_disabled"]:
        assert c in site.AUTH_ERRORS and site.AUTH_ERRORS[c].strip()
    for m in site.AUTH_ERRORS.values():
        assert "babysit" not in m.lower()

def test_start_route_and_next_passthrough():
    assert "/auth/'+" in L or "/auth/" in L
    assert "/start" in L and "next=" in L

def test_no_env_flag():
    src = open(site.__file__).read()
    block = src[src.index("SSO_MARKS"):src.index("def login(")]
    assert "os.environ" not in block and "getenv" not in block

def test_brand_marks_and_ember_focus():
    assert all(c in L for c in ("#EA4335","#4285F4","#FBBC05","#34A853"))
    assert "min-height:48px" in L or "min-height: 48px" in L
    assert "outline:3px solid" in L

def test_x_email_page():
    h = site.x_email("Aloof")
    assert "One more step" in h and 'type=email' in h and "X-Aloof-CSRF" in h and 'href="/login"' in h or "href=/login" in h

def test_already_linked_and_email_linked_other_copy():
    assert "already_linked" in site.AUTH_ERRORS and "different Aloof account" in site.AUTH_ERRORS["already_linked"]
    m = site.AUTH_ERRORS["email_linked_other"]
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
    assert "/start?next=%2Fdashboard%23account" in d and "p.enabled?" in d
    assert "ask(''" in d.split("async function identUnlink")[1][:400]   # confirm before DELETE
    assert "#EA4335" in d and "ID_ORDER=['github','google','x']" in d
