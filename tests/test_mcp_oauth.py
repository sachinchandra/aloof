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
def test_confidential_dcr_still_works(srv):
    c, _, _ = srv
    reg = c.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "client_name": "Claude",
                                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).json()
    assert reg["client_secret"] and reg["token_endpoint_auth_method"] == "client_secret_post"
    v, ch = pkce(); _, code = authorize(c, reg["client_id"], ch, "https://claude.ai/api/mcp/auth_callback")
    d = {"grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "client_id": reg["client_id"], "code_verifier": v}
    assert c.post("/token", data=d).status_code == 401   # secret required
    assert c.post("/token", data=d | {"client_secret": reg["client_secret"]}).json()["access_token"]
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
