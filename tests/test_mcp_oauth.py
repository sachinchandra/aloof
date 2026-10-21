"""MCP OAuth: metadata (public clients, CIMD, docs URL), public-client PKCE flow, Client ID Metadata Documents with SSRF guards,
DCR with client secrets still working. In-process against the MCP Starlette app; the control plane is stubbed."""
import os, sys, re, json, base64, hashlib, secrets, importlib
from urllib.parse import urlparse, parse_qs
import pytest
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
@pytest.fixture(scope="module")
def srv():
    os.environ["MCP_PUBLIC_URL"] = "https://mcp.aloof.test"; os.environ.pop("PAAS_PUBLIC_API_URL", None)
    argv = sys.argv; sys.argv = ["server.py", "--http"]
    try:
        import oauth; server = importlib.import_module("server")
    finally: sys.argv = argv
    from starlette.testclient import TestClient
    oauth.check_api_key = lambda k: k == "pk_good"
    with TestClient(server.mcp.streamable_http_app(), base_url="https://mcp.aloof.test") as c: yield c, oauth, server
REDIRECT = "http://localhost:3999/callback"
def pkce():
    v = secrets.token_urlsafe(48); return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
def authorize(c, client_id, chal, redirect=REDIRECT):
    r = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": chal,
                                     "code_challenge_method": "S256", "state": "st", "scope": "paas"}, follow_redirects=False)
    if r.status_code != 302 or "/oauth/login" not in r.headers["location"]: return r, None
    lp = r.headers["location"]; page = c.get(lp.replace("https://mcp.aloof.test", "")).text
    req = re.search(r'name="req" value="([^"]+)"', page).group(1)
    r = c.post("/oauth/login", data={"req": req, "action": "approve", "api_key": "pk_good"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query); assert q["state"] == ["st"]; return page, q["code"][0]
def test_metadata(srv):
    c, _, server = srv
    m = c.get("/.well-known/oauth-authorization-server").json()
    assert set(m["token_endpoint_auth_methods_supported"]) == {"client_secret_post", "client_secret_basic", "none"}
    assert m["client_id_metadata_document_supported"] is True and m["code_challenge_methods_supported"] == ["S256"]
    assert m["service_documentation"] == "https://api.aloof.test/llms.txt" and "localhost" not in json.dumps(m)
    assert m["registration_endpoint"].endswith("/register")
def test_docs_url_never_localhost(srv, monkeypatch):
    _, _, server = srv
    monkeypatch.setattr(server, "PUBLIC", "http://localhost:8001"); assert server.docs_url() is None
    monkeypatch.setenv("PAAS_PUBLIC_API_URL", "https://api.example.si"); assert server.docs_url() == "https://api.example.si/llms.txt"
    monkeypatch.setenv("PAAS_PUBLIC_API_URL", "http://localhost:8000"); assert server.docs_url() is None
def test_public_client_dcr_pkce_flow(srv):
    c, oauth, _ = srv
    reg = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "pub", "token_endpoint_auth_method": "none",
                                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).json()
    assert "client_secret" not in reg or not reg["client_secret"]
    v, ch = pkce(); _, code = authorize(c, reg["client_id"], ch)
    assert c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": reg["client_id"], "code_verifier": "x" * 50}).status_code == 400
    _, code = authorize(c, reg["client_id"], ch)
    t = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": reg["client_id"], "code_verifier": v}).json()
    assert t["access_token"].startswith("mcp_at_") and t["refresh_token"]
    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": reg["client_id"]}).json()
    assert r["access_token"].startswith("mcp_at_")
    # PKCE is mandatory: no code_challenge -> error
    r = c.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s"}, follow_redirects=False)
    assert "error=invalid_request" in r.headers.get("location", "") or r.status_code == 400
@pytest.fixture(autouse=True)
def _reset_reg_limit():
    import oauth; oauth.reset_reg_hits(); yield; oauth.reset_reg_hits()
def _reg(c, uris, **kw):
    return c.post("/register", json={"redirect_uris": uris, "client_name": "x", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]} | kw)
def test_dcr_never_issues_secret(srv):
    c, oauth, _ = srv
    for meth in (None, "client_secret_post", "client_secret_basic", "none"):
        r = _reg(c, ["https://claude.ai/api/mcp/auth_callback"], **({"token_endpoint_auth_method": meth} if meth else {}))
        assert r.status_code == 201, r.text
        reg = r.json(); assert not reg.get("client_secret") and not reg.get("client_secret_expires_at") and reg["token_endpoint_auth_method"] == "none"
        stored = oauth.S.get("client", reg["client_id"]); assert not stored.get("client_secret") and stored["aloof_dcr"] is True
    # a client that still sends a (made-up) secret is fine; PKCE is the proof
    v, ch = pkce(); _, code = authorize(c, reg["client_id"], ch, "https://claude.ai/api/mcp/auth_callback")
    d = {"grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "client_id": reg["client_id"], "code_verifier": v, "client_secret": "whatever"}
    assert c.post("/token", data=d).json()["access_token"].startswith("mcp_at_")
def test_legacy_confidential_client_still_works(srv):
    """Clients registered before the fix that hold a secret keep working (and still need it)."""
    c, oauth, _ = srv
    cid, sec = "legacy-" + secrets.token_hex(4), secrets.token_hex(32)
    oauth.S.put("client", cid, {"client_id": cid, "client_secret": sec, "client_id_issued_at": 1, "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "token_endpoint_auth_method": "client_secret_post", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": "paas", "client_name": "Claude"})
    v, ch = pkce(); _, code = authorize(c, cid, ch, "https://claude.ai/api/mcp/auth_callback")
    d = {"grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "client_id": cid, "code_verifier": v}
    assert c.post("/token", data=d).status_code == 401   # secret required
    t = c.post("/token", data=d | {"client_secret": sec}).json(); assert t["access_token"]
    assert c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid, "client_secret": sec}).json()["access_token"]
    assert oauth.S.get("client", cid)["aloof_used"] is True
BAD_REDIRECTS = ["javascript:alert(1)", "javascript://claude.ai/%0aalert(1)", "data:text/html,<script>alert(1)</script>", "file:///etc/passwd",
    "vbscript:msgbox(1)", "http://evil.example.com/cb", "http://127.0.0.1.evil.com/cb", "https://*.example.com/cb", "https://evil.example.com/cb#x",
    "https://claude.ai@evil.example.com/cb", "https://user:pw@evil.example.com/cb", "myapp://callback", "com.evil.app:/cb", "ftp://x.example.com/cb",
    "https://evil.example.com/%2A"]
@pytest.mark.parametrize("uri", BAD_REDIRECTS)
def test_dcr_rejects_bad_redirects(srv, uri):
    c, oauth, _ = srv; n = oauth.client_count()
    r = _reg(c, ["https://claude.ai/api/mcp/auth_callback", uri]); assert r.status_code == 400, (uri, r.text)
    assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata") and oauth.client_count() == n
    assert oauth.redirect_problem(uri) is not None or r.json()["error"] == "invalid_client_metadata"
GOOD_REDIRECTS = ["https://claude.ai/api/mcp/auth_callback", "https://chatgpt.com/connector_platform_oauth_redirect", "http://127.0.0.1:33418/",
    "http://localhost:6274/oauth/callback", "http://[::1]:8080/cb", "cursor://anysphere.cursor-retrieval/oauth/user-aloof/callback",
    "vscode://vscode.github-authentication/did-authenticate", "https://vscode.dev/redirect", "https://glama.ai/api/mcp/oauth/callback"]
@pytest.mark.parametrize("uri", GOOD_REDIRECTS)
def test_dcr_accepts_known_client_redirects(srv, uri):
    c, _, _ = srv; r = _reg(c, [uri]); assert r.status_code == 201, r.text
def test_dcr_too_many_redirects(srv):
    c, _, _ = srv; assert _reg(c, [f"https://a{i}.example.com/cb" for i in range(11)]).status_code == 400
def test_authorize_exact_match_and_loopback_port(srv):
    c, oauth, _ = srv
    reg = _reg(c, ["https://chatgpt.com/cb", "http://127.0.0.1/cb"]).json(); v, ch = pkce()
    for bad in ["https://chatgpt.com/cb/x", "https://chatgpt.com/cb?x=1", "https://chatgpt.com/CB", "https://evil.example.com/cb", "http://127.0.0.1:5000/other", "http://localhost:5000/cb"]:
        r, code = authorize(c, reg["client_id"], ch, bad); assert code is None and r.status_code == 400, bad
    _, code = authorize(c, reg["client_id"], ch, "http://127.0.0.1:51234/cb"); assert code   # RFC 8252 7.3: any loopback port
    _, code = authorize(c, reg["client_id"], ch, "https://chatgpt.com/cb"); assert code
def test_legacy_client_with_bad_redirect_blocked_at_authorize(srv):
    c, oauth, _ = srv
    for uri in ["javascript:alert(document.cookie)", "http://evil.example.com/cb", "myapp://cb"]:
        cid = "legacy-bad-" + secrets.token_hex(4)
        oauth.S.put("client", cid, {"client_id": cid, "redirect_uris": [uri], "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": "paas"})
        _, ch = pkce(); r, code = authorize(c, cid, ch, uri); assert code is None and r.status_code == 400 and "javascript" not in r.headers.get("location", "")
def test_pkce_s256_required(srv):
    c, _, _ = srv; reg = _reg(c, [REDIRECT]).json(); _, ch = pkce()
    base = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s", "code_challenge": ch}
    for extra in ({"code_challenge_method": "plain"}, {"code_challenge": None}):
        r = c.get("/authorize", params={k: v for k, v in (base | extra).items() if v is not None}, follow_redirects=False)
        assert r.status_code == 400 or "error=invalid_request" in r.headers.get("location", "")
    v, ch = pkce(); _, code = authorize(c, reg["client_id"], ch)
    assert c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": reg["client_id"]}).status_code == 400   # no verifier
def _consent(c, client_id, redirect):
    _, ch = pkce()
    r = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": ch, "code_challenge_method": "S256", "state": "st"}, follow_redirects=False)
    return c.get(r.headers["location"].replace("https://mcp.aloof.test", ""))
def test_consent_unverified_warning_and_escaping(srv):
    c, _, _ = srv
    reg = _reg(c, ["https://evil.example.com:8443/cb"], client_name='Claude <script>alert(1)</script>\u202e"').json()
    r = _consent(c, reg["client_id"], "https://evil.example.com:8443/cb"); page = r.text
    assert "Unverified app" in page and "https://evil.example.com:8443" in page and "<script>alert" not in page and "&lt;script&gt;" in page and "\u202e" not in page
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "Approve" in page and r.status_code == 200   # never auto-approved: a GET only renders the form
    reg = _reg(c, ["https://claude.ai/api/mcp/auth_callback"], client_name="Claude").json()
    page = _consent(c, reg["client_id"], "https://claude.ai/api/mcp/auth_callback").text
    assert "Verified app" in page and "Unverified" not in page and "https://claude.ai" in page
    reg = _reg(c, ["https://claude.ai.evil.example.com/cb"]).json()
    assert "Unverified app" in _consent(c, reg["client_id"], "https://claude.ai.evil.example.com/cb").text
def test_no_skip_consent(srv):
    """Approving needs a POST with valid credentials; nothing else issues a code."""
    c, _, _ = srv; reg = _reg(c, [REDIRECT]).json(); _, ch = pkce()
    r = c.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "code_challenge": ch, "code_challenge_method": "S256", "state": "st"}, follow_redirects=False)
    lp = r.headers["location"]; assert "/oauth/login" in lp and "code=" not in lp
    req = parse_qs(urlparse(lp).query)["req"][0]
    for data in ({"req": req, "action": "approve"}, {"req": req, "action": "approve", "api_key": "pk_bad"}, {"req": req}):
        r = c.post("/oauth/login", data=data, follow_redirects=False); assert r.status_code == 200 and "location" not in r.headers
def test_register_rate_limit_per_ip(srv, monkeypatch):
    c, oauth, _ = srv; monkeypatch.setattr(oauth, "REG_PER_IP", 3)
    codes = [_reg(c, [REDIRECT], ).status_code for _ in range(4)]; assert codes == [201, 201, 201, 429]
    # another IP behind the proxy is unaffected; spoofed XFF from a non-loopback peer is ignored
    from types import SimpleNamespace as NS
    mk = lambda peer, xff: NS(client=NS(host=peer), headers={"x-forwarded-for": xff} if xff else {})
    assert oauth.client_ip(mk("127.0.0.1", "1.2.3.4, 5.6.7.8")) == "5.6.7.8" and oauth.client_ip(mk("9.9.9.9", "1.2.3.4")) == "9.9.9.9"
    monkeypatch.setenv("PAAS_EDGE_IPS", "203.0.113.10,2001:db8:1::1")
    assert oauth.client_ip(mk("2001:db8:1::1:0", "5.6.7.8")) == "5.6.7.8" and oauth.client_ip(mk("203.0.113.10", "5.6.7.8")) == "5.6.7.8"
    assert oauth.client_ip(mk("2001:db8:2::1", "5.6.7.8")) == "2001:db8:2::1" and oauth.client_ip(mk("172.17.0.5", "5.6.7.8")) == "172.17.0.5"
    assert oauth.reg_rate_ok("5.6.7.8") is True
    for i in range(3): assert oauth.reg_rate_ok(f"2001:db8:9::{i+1}") is True
    assert oauth.reg_rate_ok("2001:db8:9::ffff") is False and oauth.reg_rate_ok("2001:db8:a::1") is True   # IPv6 limited per /64
def test_register_client_cap(srv, monkeypatch):
    c, oauth, _ = srv; oauth.sweep_unused_clients(force=True); monkeypatch.setattr(oauth, "MAX_CLIENTS", oauth.client_count())
    r = _reg(c, [REDIRECT]); assert r.status_code == 503
def test_unused_clients_expire_and_delete_client(srv):
    c, oauth, _ = srv; import time
    old = _reg(c, [REDIRECT]).json()["client_id"]; used = _reg(c, [REDIRECT]).json()["client_id"]; fresh = _reg(c, [REDIRECT]).json()["client_id"]
    for cid in (old, used):
        d = oauth.S.get("client", cid); d["client_id_issued_at"] = int(time.time()) - 2 * 86400; oauth.S.put("client", cid, d)
    v, ch = pkce(); _, code = authorize(c, used, ch)
    assert c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": used, "code_verifier": v}).status_code == 200
    _, _ = authorize(c, old, ch)   # leaves a code behind but no token
    assert oauth.sweep_unused_clients(force=True) >= 1
    assert oauth.S.get("client", old) is None and oauth.S.get("client", used) and oauth.S.get("client", fresh)
    assert not [k for k, v in oauth._rows("code") if oauth._owner(v) == old]
    n = oauth.delete_client(used); assert n["client"] == 1 and n["access"] == 1 and n["refresh"] == 1 and oauth.S.get("client", used) is None
def test_metadata_still_advertises_public_clients(srv):
    c, _, _ = srv; m = c.get("/.well-known/oauth-authorization-server").json()
    assert "none" in m["token_endpoint_auth_methods_supported"] and m["code_challenge_methods_supported"] == ["S256"]
CID = "https://client.example.com/oauth/client.json"
def _doc(**kw): return {"client_id": CID, "client_name": "Example Agent", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"} | kw
def test_cimd_flow(srv, monkeypatch):
    c, oauth, _ = srv; oauth._cimd_cache.clear()
    monkeypatch.setattr(oauth, "_resolve", lambda h: {"93.184.215.14"})
    calls = []
    monkeypatch.setattr(oauth, "_fetch", lambda url, u: (calls.append(url), (json.dumps(_doc()).encode(), "max-age=600"))[1])
    v, ch = pkce(); page, code = authorize(c, CID, ch)
    assert "Example Agent (client.example.com)" in page
    t = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": CID, "code_verifier": v}).json()
    assert t["access_token"].startswith("mcp_at_") and len(calls) == 1   # cached between /authorize and /token
    r, code = authorize(c, CID, ch, "https://evil.example.net/cb")   # redirect_uri not in the document
    assert code is None and r.status_code == 400
@pytest.mark.parametrize("url", ["http://client.example.com/c.json", "https://client.example.com", "https://client.example.com/",
    "https://u:p@client.example.com/c.json", "https://client.example.com/c.json#x", "https://client.example.com:8443/c.json",
    "https://127.0.0.1/c.json", "https://client.example.com/a/../c.json"])
def test_cimd_url_rules(srv, url):
    _, oauth, _ = srv
    with pytest.raises(oauth.CIMDError): oauth.check_cimd_url(url)
@pytest.mark.parametrize("ips", [{"127.0.0.1"}, {"10.0.0.5"}, {"169.254.169.254"}, {"93.184.215.14", "192.168.1.1"}, {"::1"}, {"::ffff:10.0.0.1"}, {"100.64.0.1"}])
def test_cimd_ssrf_private_ips(srv, monkeypatch, ips):
    _, oauth, _ = srv; oauth._cimd_cache.clear()
    monkeypatch.setattr(oauth, "_resolve", lambda h: ips)
    with pytest.raises(oauth.CIMDError, match="non-public"): oauth._fetch(CID, oauth.check_cimd_url(CID))
    assert oauth.resolve_cimd_client(CID) is None
@pytest.mark.parametrize("doc", [_doc(client_id="https://other.example.com/c.json"), _doc(client_secret="s"), _doc(token_endpoint_auth_method="client_secret_post"),
    _doc(redirect_uris=[]), _doc(redirect_uris=["javascript:alert(1)"]), _doc(redirect_uris=["http://evil.example.net/cb"]), _doc(grant_types=["client_credentials"]), ["x"], _doc(redirect_uris=["https://ok.example.com/cb#frag"])])
def test_cimd_doc_validation(srv, doc):
    _, oauth, _ = srv
    with pytest.raises(oauth.CIMDError): oauth.validate_cimd(CID, doc)
def test_cimd_size_and_cache(srv, monkeypatch):
    _, oauth, _ = srv; oauth._cimd_cache.clear(); n = []
    monkeypatch.setattr(oauth, "check_cimd_url", lambda u: None)
    def big(url, u): n.append(1); raise oauth.CIMDError("client metadata document too large")
    monkeypatch.setattr(oauth, "_fetch", big)
    assert oauth.resolve_cimd_client(CID, now=1000) is None and oauth.resolve_cimd_client(CID, now=1030) is None and len(n) == 1   # negative cache
    assert oauth.resolve_cimd_client(CID, now=1000 + oauth.CIMD_NEG_TTL + 1) is None and len(n) == 2
    assert oauth._ttl("max-age=999999") == oauth.CIMD_TTL_MAX and oauth._ttl("no-store") == oauth.CIMD_TTL_MIN and oauth._ttl("") == oauth.CIMD_TTL

def test_cimd_real_world_docs(srv):
    _, oauth, _ = srv
    vs = {"client_name": "Visual Studio Code", "grant_types": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:device_code"],
          "response_types": ["code"], "token_endpoint_auth_method": "none", "application_type": "native", "client_id": "https://vscode.dev/oauth/client-metadata.json",
          "redirect_uris": ["http://127.0.0.1:33418/", "https://vscode.dev/redirect"]}
    c = oauth.validate_cimd(vs["client_id"], vs); assert c.grant_types == ["authorization_code", "refresh_token"]
    cc = {"client_id": "https://claude.ai/oauth/claude-code-client-metadata", "client_name": "Claude Code", "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
          "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}
    c = oauth.validate_cimd(cc["client_id"], cc)
    from pydantic import AnyUrl
    assert str(c.validate_redirect_uri(AnyUrl("http://localhost:54321/callback"))) == "http://localhost:54321/callback"   # any loopback port
    for bad in ["http://localhost:54321/other", "http://evil.example.com/callback", "https://localhost/callback"]:
        with pytest.raises(Exception): c.validate_redirect_uri(AnyUrl(bad))

def test_mcp_healthz_endpoint(srv):
    """MCP /healthz endpoint must return 200 OK with JSON body."""
    c, _, _ = srv
    r = c.get("/healthz")
    assert r.status_code == 200, f"healthz returned {r.status_code}: {r.text}"
    assert r.json() == {"ok": True}


def test_pkce_mismatch_burns_code(srv):
    """A wrong code_verifier fails AND deletes the code, so the right verifier can't be used afterwards."""
    c, oauth, _ = srv
    v, ch = pkce()
    cid = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "t", "token_endpoint_auth_method": "none"}).json()["client_id"]
    _, code = authorize(c, cid, ch); assert code
    d = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": cid}
    r = c.post("/token", data=d | {"code_verifier": "x" * 50}); assert r.status_code == 400 and "invalid_grant" in r.text
    assert oauth.S.get("code", code) is None
    assert c.post("/token", data=d | {"code_verifier": v}).status_code == 400
def test_register_rate_limit_is_in_db(srv):
    c, oauth, _ = srv
    for i in range(10): assert oauth.reg_rate_ok("9.9.9.9")
    oauth._reg_hits.clear()   # a restart loses in-process state; the DB-backed count still holds
    assert oauth.reg_rate_ok("9.9.9.9") is False


def test_prompt_none_returns_login_required(srv):
    c, oauth, _ = srv
    cid = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "t", "token_endpoint_auth_method": "none"}).json()["client_id"]
    v, ch = pkce()
    r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": ch,
                                    "code_challenge_method": "S256", "state": "st", "prompt": "none"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT) and "error=login_required" in r.headers["location"] and "state=st" in r.headers["location"]
def test_raw_redirect_whitespace_rejected(srv):
    c, oauth, _ = srv
    for bad in ["https://ok.example.com/cb\r\n", " https://ok.example.com/cb", "https://ok.example.com/cb\t"]:
        r = c.post("/register", json={"redirect_uris": [bad], "client_name": "t", "token_endpoint_auth_method": "none"}); assert r.status_code == 400, bad
    cid = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "t", "token_endpoint_auth_method": "none"}).json()["client_id"]
    r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT + "\r\n", "code_challenge": "x" * 43,
                                    "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers
def test_unverified_warning_is_strong(srv):
    o = __import__("oauth"); assert "full access to your Aloof account" in o.PAGE and "Deny" in o.UNVERIFIED_BADGE and "may be fake" in o.UNVERIFIED_BADGE
