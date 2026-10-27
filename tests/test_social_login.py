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
