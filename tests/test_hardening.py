"""QA 2026-10-13: login rate limit, is_test at signup, security headers."""
import os
from fastapi.testclient import TestClient
from paas import api, engine, auth
from paas.db import q
c = TestClient(api.app)
def setup_function(_): api._login_hits.clear()
def test_login_rate_limit_per_email():
    auth.register("rl-victim@realmail.dev", "correct-horse-1")
    codes = [TestClient(api.app, client=(f"198.51.100.{i}", 1)).post("/v1/auth/login", json={"email": "rl-victim@realmail.dev", "password": "wrong-pass-xx"}).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    r = c.post("/v1/auth/login", json={"email": "RL-victim@realmail.dev", "password": "correct-horse-1"})
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 61
def test_login_rate_limit_per_ip_and_xff():
    peer = TestClient(api.app, client=("203.0.113.7", 1))
    codes = [peer.post("/v1/auth/login", json={"email": f"u{i}@x.dev", "password": "wrong-pass-xx"}).status_code for i in range(11)]
    assert codes[-1] == 429 and codes[:10] == [401] * 10
    # untrusted peer: spoofed X-Forwarded-For is ignored, still limited
    assert peer.post("/v1/auth/login", json={"email": "z@x.dev", "password": "p"*10}, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 429
    # trusted proxy: last XFF hop is the client, so a different client is not limited
    prox = TestClient(api.app, client=("127.0.0.1", 1))
    assert prox.post("/v1/auth/login", json={"email": "y@x.dev", "password": "p"*10}, headers={"X-Forwarded-For": "6.6.6.6, 192.0.2.9"}).status_code == 401
    assert api.client_ip.__doc__
def test_window_expires():
    for _ in range(10): api._rate_check(("ip:t",), now=1000.0)
    try: api._rate_check(("ip:t",), now=1030.0); assert False
    except Exception as e: assert e.status_code == 429
    api._rate_check(("ip:t",), now=1061.0)
def test_is_test_at_creation():
    for e, want in [("a@example.com", 1), ("b@example.org", 1), ("c@foo.test", 1), ("d@bar.invalid", 1), ("e@localhost", 1),
                    ("f@gmail.com", 0), ("g@example.co.uk", 0), ("h@notexample.org.in", 0)]:
        engine.create_account(e); assert q("select is_test from accounts where email=?", (e,), one=True)["is_test"] == want, e
    r = c.post("/v1/auth/register", json={"email": "reg@example.org", "password": "long-enough-pw"})
    assert r.status_code == 200 and q("select is_test from accounts where email=?", ("reg@example.org",), one=True)["is_test"] == 1
def test_security_headers():
    h = c.get("/").headers
    assert h["strict-transport-security"].startswith("max-age=31536000") and h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in c.get("/v1/plans").headers["content-security-policy"]
def test_mcp_manifest_region_and_tools():
    m = c.get("/.well-known/mcp.json").json()
    assert m["region"] == "Nuremberg, Germany (backups in Helsinki)" and "Finland" not in m["description"]
    src = open(os.path.join(os.path.dirname(__file__), "..", "mcp_server", "server.py")).read()
    assert len(m["tools"]) == src.count("@mcp.tool(") and "deploy_app" in m["tools"]
    from paas import www_blog
    post = next(p for p in www_blog.posts() if p["slug"] == "mcp-let-your-agent-deploy")
    assert f"{len(m['tools'])} tools cover" in post["html"] and "__MCP" not in post["html"]
def test_pricing_seats_truthful():
    from paas.plans import TIERS
    assert TIERS["pro"]["seats"] == 1
    h = c.get("/pricing").text; assert "1 seat" in h
    for bad in ("Unlimited seats", "unlimited seats", "for teams", "For teams", "per workspace"):
        assert bad not in h and bad not in c.get("/").text, bad
    for comp in ("Fly.io", "Render", "Heroku", "Railway"): assert comp not in h, comp
    assert "Nuremberg, Germany" in h and "$24.97" in h and "$4.97" in h and "$6.48" in h
def test_cli_default_api():
    src = open(os.path.join(os.path.dirname(__file__), "..", "cli", "aloof")).read()
    assert 'DEFAULT_API = "https://api.aloof.si"' in src and "localhost:8000" not in src
