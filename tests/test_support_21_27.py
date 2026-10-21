"""Support bugs 21-27: pricing CTAs follow real payment state, template region label, /docs/mcp OAuth coverage matches the code,
branded and honest Authorize page with a path for email-link users, footer tap targets, dashboard hash routing."""
import os, sys, re
from fastapi.testclient import TestClient
from paas.api import app
from paas import stripe_billing
c = TestClient(app)
ROOT = os.path.join(os.path.dirname(__file__), "..")

def test_pricing_soon_when_cards_closed(monkeypatch):
    monkeypatch.setattr(stripe_billing, "payments_open_cached", lambda: False)
    h = c.get("/pricing").text
    assert h.count("Paid plans opening soon") >= 2 and "Start with Hobby" not in h and "Start with Pro" not in h and "Start free" in h
def test_pricing_normal_when_cards_open(monkeypatch):
    monkeypatch.setattr(stripe_billing, "payments_open_cached", lambda: True)
    h = c.get("/pricing").text
    assert "Start with Hobby" in h and "Start with Pro" in h and "pp-soon" not in h
def test_template_region_label():
    from paas.templating import _region_label
    assert _region_label("nbg1") == "Nuremberg (nbg1)" and _region_label("eu-de") == "Nuremberg (nbg1)"
def test_docs_mcp_oauth_values_match_code():
    sys.path.insert(0, os.path.join(ROOT, "mcp_server")); import oauth
    h = c.get("/docs/mcp").text
    assert oauth.ACCESS_TTL == 3600 and "Access token: 1 hour" in h
    assert oauth.REFRESH_TTL == 30 * 86400 and "Refresh token: 30 days" in h
    assert oauth.CODE_TTL == 300 and "5 minutes, single use" in h
    assert oauth.REG_PER_IP == 10 and oauth.REG_WINDOW == 3600 and "10 per IP address per hour" in h
    assert oauth.UNUSED_CLIENT_TTL == 86400 and "deleted after 24 hours" in h
    assert oauth.PAGE_TTL_MIN == 10 and "valid for 10 minutes" in h
    for v in oauth.VERIFIED_HOSTS: assert v in h, v
    for sch in oauth.REDIRECT_SCHEMES: assert f"<code>{sch}:</code>" in h, sch
    for anchor in ("authorize", "badges", "tokens", "limits", "redirects", "revoke", "no-password"): assert f"id={anchor}" in h
    assert "full access" in h and "scoped token" not in h
def test_authorize_page_branded_and_honest():
    sys.path.insert(0, os.path.join(ROOT, "mcp_server")); import oauth
    page = oauth.PAGE.format(client="X", badge=oauth.VERIFIED_BADGE, target="https://claude.ai", css=oauth.CSS, mark=oauth.MARK, www="https://www.aloof.si",
                             ttl=10, email="", key_open="", req="r", err="")
    assert "full access to your Aloof account" in page and "#ff7a1a" in page and "<svg" in page
    assert "/dashboard%23keys" in page and "email link" in page and "API key" in page
    assert not re.search(r"#(7c3aed|6d28d9|8b5cf6|a78bfa)|violet|purple", page, re.I) and "babysit" not in page.lower()
    assert "Request expired" in oauth.EXPIRED and "{" not in oauth.EXPIRED.split("<style>")[0]
def test_footer_tap_targets_and_hash_routing():
    css = open(os.path.join(ROOT, "paas/static/aloof.css")).read(); assert "footer.f .cols a:not(.logo){min-height:40px" in css
    assert "function goHash()" in open(os.path.join(ROOT, "dashboard/index.html")).read()
