from fastapi.testclient import TestClient
from paas import api, site

def _c(): return TestClient(api.app)
def test_signup_redirects_to_register_tab():
    r = _c().get("/signup", follow_redirects=False); assert r.status_code == 303 and r.headers["location"] == "/login#register"
    r = _c().get("/signup?next=/deploy?image=nginx", follow_redirects=False); assert r.headers["location"].startswith("/login?next=%2Fdeploy") and r.headers["location"].endswith("#register")
    assert _c().get("/signup?next=//evil.com", follow_redirects=False).headers["location"] == "/login#register"

def test_dashboard_subpaths_signed_out_go_to_login():
    r = _c().get("/dashboard/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fdashboard%2Fsettings"
    assert _c().get("/dashboard/../../etc", follow_redirects=False).status_code in (303, 404)
    assert _c().get("/dashboard/whatever", follow_redirects=False).headers["location"] == "/login?next=%2Fdashboard"

def test_dashboard_subpaths_signed_in_go_to_tab():
    c = _c(); r = c.post("/v1/auth/register", json={"email": "ux28@example.com", "password": "correct-horse-9"}); assert r.status_code == 200
    assert c.get("/dashboard/settings", follow_redirects=False).headers["location"] == "/dashboard#account"
    assert c.get("/dashboard/billing", follow_redirects=False).headers["location"] == "/dashboard#billing"

def test_login_page_renders_error_and_no_template_leak():
    h = _c().get("/login?auth_error=email_unverified&provider=x").text
    assert "Your email address at X isn't verified" in h and "that provider" not in h
    h = _c().get("/login?auth_error=email_unverified").text
    assert "that provider" not in h and "isn't verified" in h

def test_sign_in_docs():
    h = _c().get("/docs/sign-in").text
    assert "/v1/auth/identities" in h and "aloof login" in h and "Sign-in methods" in h and "babysit" not in h.lower()

def test_access_labels_match_authorize():
    from paas import oauth_apps
    from mcp_server import oauth
    assert [v[1] for v in oauth_apps.ACCESS.values()] == [v[1] for v in oauth.LEVEL_OPTS.values()]
