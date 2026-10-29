"""Support doc bugs 47 (sign-in doc follows the live provider list) and 50 (downgrade wording matches the billing code)."""
from fastapi.testclient import TestClient
from paas import api, oauth_login
c = TestClient(api.app)

def test_sign_in_doc_follows_enabled_providers(monkeypatch):
    monkeypatch.setattr(oauth_login, "enabled", lambda p: p == "x")
    h = c.get("/docs/sign-in").text
    assert "Continue with X" in h and "Continue with GitHub" not in h and "Continue with Google" not in h
    assert "Google and GitHub sign-in are coming soon." in h
    monkeypatch.setattr(oauth_login, "enabled", lambda p: False)
    h = c.get("/docs/sign-in").text
    assert "Continue with" not in h and "coming soon" in h

def test_downgrade_wording_matches_code():
    src = open(api.__file__).read()
    assert "downgrades apply immediately, no refund" in src and "tier only runs" in src
    for path in ("/pricing", "/docs/billing"):
        h = c.get(path).text
        assert "downgrades take effect immediately" in h.lower() or "take effect immediately, and the rest of this month" in h, path
        assert "end of the paid month" in h, path
