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
