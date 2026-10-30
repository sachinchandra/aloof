"""/changelog: rendered from content/changelog/*.md; off (404, no sitemap/footer) until PUBLISHED; no social sign-in entries."""
import glob, re
from fastapi.testclient import TestClient
from paas.api import app
from paas import www_changelog as ch, www, site
c = TestClient(app)
def test_unpublished_is_404_and_hidden(monkeypatch):
    monkeypatch.setattr(ch, "PUBLISHED", False)
    r = c.get("/changelog", follow_redirects=False); assert r.status_code == 301 and r.headers["location"] == "/"
    assert c.get("/changelog.md").status_code == 404
    assert "/changelog" not in www.urls() and "/changelog" not in site.footer("Aloof")
def test_published_renders(monkeypatch):
    monkeypatch.setattr(ch, "PUBLISHED", True)
    r = c.get("/changelog"); assert r.status_code == 200 and "MCP v2: 25 tools instead of 42" in r.text and "<h3>" in r.text
    m = c.get("/changelog.md"); assert m.status_code == 200 and m.headers["content-type"].startswith("text/markdown")
    assert "/changelog" in www.urls() and 'href="/changelog"' in site.footer("Aloof")
def test_no_social_signin_and_checked_numbers():
    md = "".join(open(f).read() for f in glob.glob("content/changelog/*.md"))
    assert not re.search(r"Sign in with X|X account|Google|GitHub sign-in", md)
    from paas.mcp_tools import mcp_tool_names; assert f"{len(mcp_tool_names())} tools" in md
