"""Rate-limit keying: IPv6 per /64, IPv4 as-is; X-Forwarded-For only from a trusted proxy (loopback Caddy)."""
from types import SimpleNamespace as NS
import pytest
from paas.netaddr import client_ip, rate_key
from paas import api
def req(peer, xff=None): return NS(client=NS(host=peer), headers={"x-forwarded-for": xff} if xff else {})
def test_rate_key():
    assert rate_key("203.0.113.7") == "203.0.113.7"
    assert rate_key("2001:db8:1:2:aaaa::1") == rate_key("2001:db8:1:2:ffff:1:2:3") == "2001:db8:1:2::/64"
    assert rate_key("2001:db8:1:3::1") != rate_key("2001:db8:1:2::1")
    assert rate_key("::ffff:203.0.113.7") == "203.0.113.7"
def test_xff_only_from_trusted_proxy(monkeypatch):
    monkeypatch.delenv("PAAS_TRUSTED_PROXIES", raising=False); monkeypatch.delenv("PAAS_EDGE_IPS", raising=False)
    assert client_ip(req("127.0.0.1", "6.6.6.6, 2001:db8::5")) == "2001:db8::5"      # Caddy's appended hop wins
    assert client_ip(req("::1", "198.51.100.1")) == "198.51.100.1"
    assert client_ip(req("198.51.100.9", "1.2.3.4")) == "198.51.100.9"               # direct hit: header ignored
    assert client_ip(req("testclient", "1.2.3.4")) == "testclient"
    monkeypatch.setenv("PAAS_TRUSTED_PROXIES", "10.0.0.2")
    assert client_ip(req("10.0.0.2", "1.2.3.4")) == "1.2.3.4"
def test_rotating_ipv6_hits_one_limit(monkeypatch):
    """Login, magic link and social start all count against the /64, so rotating the low 64 bits doesn't reset the limit."""
    from fastapi.testclient import TestClient
    import itertools
    n = itertools.count(1)
    monkeypatch.setattr(api, "client_ip", lambda r: f"2001:db8:9:9::{next(n):x}")
    monkeypatch.setattr(api.auth, "start_magic", lambda *a, **k: None)
    c = TestClient(api.app)
    for ep in ("login", "magic"):
        api._login_hits.clear(); codes = []
        for i in range(api.LOGIN_LIMIT + 1):
            body = {"email": f"u{i}@example.com", "password": "x" * 12}
            codes.append(c.post(f"/v1/auth/{ep}", json=body).status_code)
        assert 429 not in codes[:-1] and codes[-1] == 429, (ep, codes)
    api._login_hits.clear()
    for i in range(api.LOGIN_LIMIT): api._rate_check(("oauth-ip:" + api._ipk(None),))
    r = c.get("/auth/github/start", follow_redirects=False)
    assert "auth_error=rate_limited" in r.headers.get("location", ""), r.headers.get("location")
    api._login_hits.clear()
