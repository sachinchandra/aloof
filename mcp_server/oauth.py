"""OAuth 2.1 authorization server for the MCP endpoint (MCP auth spec 2025-06-18):
- RFC 9728 protected-resource metadata, RFC 8414 AS metadata, RFC 7591 dynamic client registration,
  authorization code + PKCE (S256, mandatory for every client, enforced by the SDK), refresh-token rotation, revocation.
- Public clients: token_endpoint_auth_method "none" (no secret; PKCE is the proof), via DCR or a Client ID Metadata Document.
- Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document, MCP auth 2025-11-25): an https client_id URL is
  fetched with SSRF guards (https:443 only, every resolved IP public, connection pinned to the checked IP, no redirects,
  5 s / 5 KB limits, bounded cache) and its redirect_uris are enforced.
- The user "logs in" on /oauth/login by pasting their platform API key (or signing up); the issued
  access token maps to that account's API key server-side. Raw pk_ API keys are also accepted as bearer tokens."""
import os, time, json, secrets, sqlite3, threading, html, httpx, socket, ipaddress, re, logging
from urllib.parse import urlsplit
from pydantic import AnyUrl
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
    OAuthAuthorizationServerProvider, construct_redirect_uri, TokenError)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken, InvalidRedirectUriError
from mcp.server.auth.provider import RegistrationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
API = os.environ.get("ALOOF_API_URL", os.environ.get("PAAS_API_URL", "http://localhost:8000"))
ACCESS_TTL, REFRESH_TTL, CODE_TTL = 3600, 30*86400, 300
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from paas import db as _db, crypto as _crypto
class Store:
    """OAuth state in the platform DB (Postgres by default). Account API keys bound to tokens are Fernet-encrypted;
    tokens/codes are looked up by SHA-256 hash so a DB leak doesn't expose bearer tokens."""
    SECRET_FIELDS = ("api_key",)
    def _k(self, kind, k): return _crypto.sha256(k) if kind in ("access", "refresh", "code", "pkcache") else k
    def put(self, kind, k, v, ttl=None):
        v = dict(v) if isinstance(v, dict) else v
        if isinstance(v, dict):
            for f in self.SECRET_FIELDS:
                if v.get(f): v[f] = "enc:" + _crypto.enc(v[f])
        _db.kv_put("oauth:" + kind, self._k(kind, k), json.dumps(v), ttl)
    def get(self, kind, k):
        raw = _db.kv_get("oauth:" + kind, self._k(kind, k))
        if raw is None: return None
        v = json.loads(raw)
        if isinstance(v, dict):
            for f in self.SECRET_FIELDS:
                if isinstance(v.get(f), str) and v[f].startswith("enc:"): v[f] = _crypto.dec(v[f][4:])
        return v
    def rm(self, kind, k): _db.kv_del("oauth:" + kind, self._k(kind, k))
S = Store()
def check_api_key(key):
    try: r = httpx.get(f"{API}/v1/auth/me", headers={"Authorization": f"Bearer {key}"}, timeout=10); return r.status_code == 200
    except Exception: return False
def password_login(email, password):
    """Validate email+password against the control plane and return a fresh API key for the token binding."""
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{API}/v1/auth/login", json={"email": email, "password": password})
        if r.status_code != 200: return None
        sess = r.cookies.get("aloof_session")   # Secure cookie: pass explicitly on this internal (plain-http) hop
        if not sess: return None
        k = c.post(f"{API}/v1/auth/mcp-key", headers={"X-Aloof-CSRF": "1", "Cookie": f"aloof_session={sess}"})
        c.post(f"{API}/v1/auth/logout", headers={"X-Aloof-CSRF": "1", "Cookie": f"aloof_session={sess}"})   # don't leave the session lying around
        return k.json().get("api_key") if k.status_code == 200 else None
log = logging.getLogger("mcp.oauth")
# ---------- Client ID Metadata Documents ----------
CIMD_MAX_BYTES, CIMD_TIMEOUT, CIMD_TTL, CIMD_TTL_MIN, CIMD_TTL_MAX, CIMD_NEG_TTL, CIMD_CACHE_MAX = 5120, 5.0, 300, 60, 86400, 60, 1000
_cimd_cache = {}   # url -> (expires, OAuthClientInformationFull | None)
class CIMDError(ValueError): pass
def is_cimd_client_id(cid): return isinstance(cid, str) and cid.startswith("https://")
def _public(ip):
    a = ipaddress.ip_address(ip)
    if getattr(a, "ipv4_mapped", None): a = a.ipv4_mapped
    return a.is_global and not (a.is_multicast or a.is_reserved or a.is_loopback or a.is_link_local or a.is_private)
def _resolve(host): return {ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
def check_cimd_url(url):
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname: raise CIMDError("client_id must be an https URL")
    if u.username or u.password or u.fragment or "#" in url: raise CIMDError("client_id URL must not contain credentials or a fragment")
    if u.port not in (None, 443): raise CIMDError("client_id URL must use the default https port")
    if not u.path or u.path == "/" or any(seg in (".", "..") for seg in u.path.split("/")): raise CIMDError("client_id URL needs a path without dot segments")
    if len(url) > 2048: raise CIMDError("client_id URL too long")
    try: ipaddress.ip_address(u.hostname); is_ip = True
    except ValueError: is_ip = False
    if is_ip: raise CIMDError("client_id URL must use a DNS name, not an IP")
    return u
# ---------- redirect_uri policy (shared by DCR, CIMD and /authorize) ----------
def _csv(name, default): return tuple(x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip())
# Native-app custom schemes we accept (MCP clients that use a private-use scheme callback). Everything else must be https or loopback http.
REDIRECT_SCHEMES = _csv("PAAS_MCP_REDIRECT_SCHEMES", "cursor,vscode,vscode-insiders,windsurf,claude")
_NEVER_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "filesystem", "ftp", "ws", "wss"}
# Redirect hosts operated by known MCP clients: consent shows these as verified (exact host or a subdomain of it).
VERIFIED_HOSTS = _csv("PAAS_MCP_VERIFIED_HOSTS", "claude.ai,claude.com,chatgpt.com,chat.openai.com,openai.com,cursor.com,cursor.sh,"
                      "smithery.ai,glama.ai,vscode.dev,insiders.vscode.dev,github.com,windsurf.com,codeium.com,mcpjam.com")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
def redirect_problem(r):
    """Why a redirect_uri is not acceptable, or None. https://<host>, http://<loopback>[:port], or an allowlisted native scheme.
    No fragments, wildcards, userinfo, control characters or dangerous schemes."""
    r = str(r or "")
    if not r or len(r) > 2048: return "redirect_uri missing or too long"
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in r): return "redirect_uri contains whitespace or control characters"
    if "#" in r: return "redirect_uri must not contain a fragment"
    if "*" in r or "%2a" in r.lower(): return "redirect_uri must not contain wildcards"
    try: u = urlsplit(r); u.port
    except ValueError: return "redirect_uri is not a valid URL"
    sch = (u.scheme or "").lower()
    if sch in _NEVER_SCHEMES: return f"redirect_uri scheme '{sch}' is not allowed"
    if "@" in u.netloc or u.username or u.password: return "redirect_uri must not contain credentials"
    if sch == "https": return None if u.hostname else "https redirect_uri needs a host"
    if sch == "http": return None if u.hostname in LOOPBACK_HOSTS else "http redirect_uri is only allowed for loopback (127.0.0.1, [::1], localhost)"
    if sch in REDIRECT_SCHEMES: return None
    return f"redirect_uri scheme '{sch}' is not allowed (use https, loopback http, or a supported native app scheme)"
def _ok_redirect(r): return redirect_problem(r) is None
def is_verified_redirect(r):
    u = urlsplit(str(r or "")); h = (u.hostname or "").lower()
    return u.scheme == "https" and bool(h) and any(h == v or h.endswith("." + v) for v in VERIFIED_HOSTS)
def _ttl(cache_control):
    m = re.search(r"max-age=(\d+)", cache_control or "")
    if re.search(r"no-store|no-cache", cache_control or ""): return CIMD_TTL_MIN
    return max(CIMD_TTL_MIN, min(CIMD_TTL_MAX, int(m.group(1)))) if m else CIMD_TTL
def _fetch(url, u):
    ips = _resolve(u.hostname)
    if not ips or not all(_public(ip) for ip in ips): raise CIMDError("client_id host resolves to a non-public address")
    ip = sorted(ips)[0]; hostport = f"[{ip}]" if ":" in ip else ip
    target = f"https://{hostport}{u.path}" + (f"?{u.query}" if u.query else "")
    with httpx.Client(timeout=CIMD_TIMEOUT, follow_redirects=False, verify=True) as c:   # pinned to the checked IP; TLS still verifies the hostname
        req = c.build_request("GET", target, headers={"Host": u.hostname, "Accept": "application/json", "User-Agent": "aloof-mcp-oauth/1"},
                              extensions={"sni_hostname": u.hostname})
        r = c.send(req, stream=True)
        try:
            if r.status_code != 200: raise CIMDError(f"client metadata fetch returned HTTP {r.status_code}")
            if int(r.headers.get("content-length") or 0) > CIMD_MAX_BYTES: raise CIMDError("client metadata document too large")
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > CIMD_MAX_BYTES: raise CIMDError("client metadata document too large")
            return body, r.headers.get("cache-control", "")
        finally: r.close()
def validate_cimd(url, doc):
    if not isinstance(doc, dict): raise CIMDError("client metadata must be a JSON object")
    if doc.get("client_id") != url: raise CIMDError("client_id in the metadata document must equal its URL")
    if "client_secret" in doc or "client_secret_expires_at" in doc: raise CIMDError("client metadata must not contain a client_secret")
    if doc.get("token_endpoint_auth_method", "none") != "none": raise CIMDError("only token_endpoint_auth_method 'none' is supported for metadata-document clients")
    ru = doc.get("redirect_uris")
    if not isinstance(ru, list) or not ru or not all(isinstance(r, str) for r in ru) or len(ru) > 20: raise CIMDError("redirect_uris must be a non-empty list of strings")
    bad = [r for r in ru if not _ok_redirect(r)]
    if bad: raise CIMDError(f"redirect_uri not allowed: {bad[0][:200]}")
    if "authorization_code" not in (doc.get("grant_types") or ["authorization_code"]): raise CIMDError("grant_types must include authorization_code")
    if "code" not in (doc.get("response_types") or ["code"]): raise CIMDError("response_types must include code")   # extra grants (e.g. device_code) are ignored, never granted
    host = urlsplit(url).hostname; name = str(doc.get("client_name") or host)[:100]
    return LoopbackClient.model_validate({"client_id": url, "client_name": f"{name} ({host})" if host not in name else name,
        "redirect_uris": ru, "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "paas", **({"client_uri": doc["client_uri"]} if isinstance(doc.get("client_uri"), str) and doc["client_uri"].startswith("https://") else {})})
def _loopback(u): return u.scheme == "http" and u.hostname in LOOPBACK_HOSTS
class LoopbackClient(OAuthClientInformationFull):
    """RFC 8252 s7.3: for http loopback redirect URIs the port is chosen at runtime, so it is ignored when matching."""
    def validate_redirect_uri(self, redirect_uri):
        r = self._match(redirect_uri); why = redirect_problem(r)
        if why: raise InvalidRedirectUriError(why)
        return r
    def _match(self, redirect_uri):
        if redirect_uri is not None and self.redirect_uris:
            r = urlsplit(str(redirect_uri))
            if _loopback(r):
                for reg in self.redirect_uris:
                    g = urlsplit(str(reg))
                    if _loopback(g) and g.hostname == r.hostname and (g.path or "/") == (r.path or "/") and g.query == r.query: return redirect_uri
        return super().validate_redirect_uri(redirect_uri)
def resolve_cimd_client(url, now=None):
    now = now or time.time(); hit = _cimd_cache.get(url)
    if hit and hit[0] > now: return hit[1]
    client, ttl = None, CIMD_NEG_TTL
    try:
        u = check_cimd_url(url); body, cc = _fetch(url, u)
        try: doc = json.loads(body)
        except ValueError: raise CIMDError("client metadata is not valid JSON")
        client, ttl = validate_cimd(url, doc), _ttl(cc)
    except CIMDError as e: log.warning("CIMD rejected %s: %s", url[:200], e)
    except Exception as e: log.warning("CIMD fetch failed %s: %s: %s", url[:200], type(e).__name__, e)
    if len(_cimd_cache) >= CIMD_CACHE_MAX: _cimd_cache.pop(next(iter(_cimd_cache)))
    _cimd_cache[url] = (now + ttl, client); return client
def patch_metadata():
    """Advertise public clients and CIMD in /.well-known/oauth-authorization-server (the SDK's builder lists secret methods only)."""
    from mcp.server.auth import routes
    if getattr(routes.build_metadata, "_aloof", False): return
    orig = routes.build_metadata
    def build(*a, **k):
        m = orig(*a, **k)
        m.token_endpoint_auth_methods_supported = ["client_secret_post", "client_secret_basic", "none"]
        if m.revocation_endpoint: m.revocation_endpoint_auth_methods_supported = ["client_secret_post", "client_secret_basic", "none"]
        m.client_id_metadata_document_supported = True
        return m
    build._aloof = True; routes.build_metadata = build
    patch_registration()
def _track_connected(client, api_key, code):
    """mcp_connected analytics (OAuth grant completed). Sends only the client's display name and redirect host. Never raises."""
    try:
        from paas import analytics
        if not analytics.enabled(): return
        r = _db.q("select a.id from accounts a where a.api_key_hash=? union select k.account_id from api_keys k where k.key_hash=?",
                  (_crypto.sha256(api_key),) * 2, one=True)
        if not r: return
        host = urlsplit(str(code.redirect_uri or "")).hostname or (urlsplit(client.client_id).hostname if is_cimd_client_id(client.client_id) else None)
        analytics.capture(r["id"], "mcp_connected", {"client_name": getattr(client, "client_name", None), "client_host": host})
    except Exception: pass
# ---------- Dynamic Client Registration hardening ----------
# DCR clients are unverified public clients: no client_secret, token_endpoint_auth_method "none", PKCE S256 (enforced by the SDK at
# /authorize and /token). Clients registered before this change that hold a secret keep working as confidential clients.
REG_PER_IP, REG_WINDOW = int(os.environ.get("PAAS_MCP_REG_PER_IP", "10")), 3600
MAX_CLIENTS = int(os.environ.get("PAAS_MCP_MAX_CLIENTS", "10000"))
UNUSED_CLIENT_TTL = int(os.environ.get("PAAS_MCP_UNUSED_CLIENT_TTL", "86400"))
SWEEP_EVERY = 600
_reg_hits, _reg_lock, _sweep_at = {}, threading.Lock(), [0.0]
def _clean_name(n):
    n = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", str(n or "")).strip()
    return n[:80] or None
def client_ip(request):
    """Caddy appends the real peer to X-Forwarded-For (and drops client-supplied values from untrusted peers); only trust it from loopback."""
    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for", "")
    if peer in ("127.0.0.1", "::1", "localhost") and xff: return xff.split(",")[-1].strip()
    return peer or "unknown"
def reg_rate_ok(ip, now=None):
    now = now or time.time()
    with _reg_lock:
        hits = [t for t in _reg_hits.get(ip, []) if t > now - REG_WINDOW]
        if len(hits) >= REG_PER_IP: _reg_hits[ip] = hits; return False
        hits.append(now); _reg_hits[ip] = hits
        if len(_reg_hits) > 50000: _reg_hits.clear()
        return True
def client_count(): return _db.q("select count(*) c from kv where kind='oauth:client'", one=True)["c"]
def _rows(kind):
    for r in _db.q("select k, v, exp from kv where kind=?", ("oauth:" + kind,)):
        if r["exp"] and r["exp"] < time.time(): continue
        try: yield r["k"], json.loads(r["v"])
        except ValueError: continue
def _owner(v): return v.get("client_id") or (v.get("code") or {}).get("client_id") if isinstance(v, dict) else None
def delete_client(client_id):
    """Remove a DCR client and every access/refresh token, auth code and pending request it holds. Returns counts."""
    n = {}
    for kind in ("access", "refresh", "code", "pending"):
        ks = [k for k, v in _rows(kind) if _owner(v) == client_id]
        for k in ks: _db.kv_del("oauth:" + kind, k)
        n[kind] = len(ks)
    n["client"] = 1 if _db.kv_get("oauth:client", client_id) is not None else 0
    _db.kv_del("oauth:client", client_id); return n
def sweep_unused_clients(now=None, force=False):
    """Delete DCR clients older than UNUSED_CLIENT_TTL that never got a token (and hold none). Throttled."""
    now = now or time.time()
    if not force and now - _sweep_at[0] < SWEEP_EVERY: return 0
    _sweep_at[0] = now
    live = {_owner(v) for kind in ("access", "refresh") for _, v in _rows(kind)}
    gone = 0
    for cid, d in list(_rows("client")):
        if d.get("aloof_used") or cid in live: continue
        if (d.get("client_id_issued_at") or 0) < now - UNUSED_CLIENT_TTL: delete_client(cid); gone += 1
    if gone: log.info("swept %d unused OAuth clients", gone)
    return gone
def _mark_used(client_id):
    if is_cimd_client_id(client_id): return
    try:
        d = S.get("client", client_id)
        if d is not None and not d.get("aloof_used"): d["aloof_used"] = True; S.put("client", client_id, d)
    except Exception: pass
def patch_registration():
    """Wrap the SDK's /register handler with a per-IP rate limit and a global client cap."""
    from mcp.server.auth.handlers.register import RegistrationHandler
    from starlette.responses import JSONResponse
    if getattr(RegistrationHandler.handle, "_aloof", False): return
    orig = RegistrationHandler.handle
    async def handle(self, request):
        import anyio
        if not reg_rate_ok(client_ip(request)):
            return JSONResponse({"error": "invalid_client_metadata", "error_description": "too many client registrations from this address; try again later"},
                                status_code=429, headers={"Retry-After": "3600"})
        if await anyio.to_thread.run_sync(client_count) >= MAX_CLIENTS:
            await anyio.to_thread.run_sync(lambda: sweep_unused_clients(force=True))
            if await anyio.to_thread.run_sync(client_count) >= MAX_CLIENTS:
                return JSONResponse({"error": "invalid_client_metadata", "error_description": "client registration is temporarily unavailable"}, status_code=503)
        else: await anyio.to_thread.run_sync(sweep_unused_clients)
        return await orig(self, request)
    handle._aloof = True; RegistrationHandler.handle = handle
class PaasOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self, issuer): self.issuer = issuer.rstrip("/")
    async def get_client(self, client_id):
        if is_cimd_client_id(client_id):
            import anyio; return await anyio.to_thread.run_sync(resolve_cimd_client, client_id)
        d = S.get("client", client_id)
        return LoopbackClient.model_validate({k: v for k, v in d.items() if not k.startswith("aloof_")}) if d else None
    async def register_client(self, client_info):
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or len(uris) > 10: raise RegistrationError("invalid_redirect_uri", "register between 1 and 10 redirect_uris")
        for u in uris:
            why = redirect_problem(u)
            if why: raise RegistrationError("invalid_redirect_uri", why)
        # Unverified public client: never issue a secret, whatever was requested (RFC 7591 lets the server override metadata).
        client_info.client_secret = None; client_info.client_secret_expires_at = None; client_info.token_endpoint_auth_method = "none"
        client_info.client_name = _clean_name(client_info.client_name)
        client_info.scope = "paas"
        d = client_info.model_dump(mode="json") | {"aloof_dcr": True, "aloof_used": False}
        S.put("client", client_info.client_id, d)
    async def authorize(self, client, params: AuthorizationParams) -> str:
        rid = secrets.token_urlsafe(16)
        S.put("pending", rid, {"client_id": client.client_id, "client_name": client.client_name, "params": params.model_dump(mode="json")}, ttl=600)
        return f"{self.issuer}/oauth/login?req={rid}"
    async def load_authorization_code(self, client, code):
        d = S.get("code", code)
        return AuthorizationCode.model_validate(d["code"]) if d and d["code"]["client_id"] == client.client_id else None
    def _issue(self, client_id, scopes, api_key, subject, resource):
        at, rt = "mcp_at_" + secrets.token_urlsafe(32), "mcp_rt_" + secrets.token_urlsafe(32)
        _mark_used(client_id)
        meta = {"client_id": client_id, "scopes": scopes, "api_key": api_key, "subject": subject, "resource": resource}
        S.put("access", at, meta | {"expires_at": int(time.time()) + ACCESS_TTL}, ttl=ACCESS_TTL)
        S.put("refresh", rt, meta | {"expires_at": int(time.time()) + REFRESH_TTL}, ttl=REFRESH_TTL)
        return OAuthToken(access_token=at, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=rt, scope=" ".join(scopes))
    async def exchange_authorization_code(self, client, code: AuthorizationCode):
        d = S.get("code", code.code); S.rm("code", code.code)   # single use
        if not d: raise TokenError("invalid_grant", "code expired or used")
        tok = self._issue(client.client_id, code.scopes, d["api_key"], d["subject"], code.resource)
        _track_connected(client, d["api_key"], code)
        return tok
    async def load_refresh_token(self, client, refresh_token):
        d = S.get("refresh", refresh_token)
        if not d or d["client_id"] != client.client_id: return None
        return RefreshToken(token=refresh_token, client_id=d["client_id"], scopes=d["scopes"], expires_at=d["expires_at"], resource=d.get("resource"), subject=d.get("subject"))
    async def exchange_refresh_token(self, client, refresh_token, scopes):
        d = S.get("refresh", refresh_token.token); S.rm("refresh", refresh_token.token)   # rotation (OAuth 2.1)
        return self._issue(client.client_id, scopes or d["scopes"], d["api_key"], d["subject"], d.get("resource"))
    async def load_access_token(self, token):
        if token.startswith("pk_"):   # plain platform API key still works as a bearer token
            ok = S.get("pkcache", token) or (check_api_key(token) and (S.put("pkcache", token, True, ttl=300) or True))
            return AccessToken(token=token, client_id="api-key", scopes=["paas"], expires_at=None, subject="api-key") if ok else None
        d = S.get("access", token)
        return AccessToken(token=token, client_id=d["client_id"], scopes=d["scopes"], expires_at=d["expires_at"], resource=d.get("resource"), subject=d.get("subject")) if d else None
    async def revoke_token(self, token):
        t = token.token; S.rm("access", t); S.rm("refresh", t)
def api_key_for(token: str):
    if token.startswith("pk_"): return token
    d = S.get("access", token); return d and d["api_key"]
PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Authorize {client}</title></head>
<body style="font-family:system-ui;max-width:480px;margin:60px auto;padding:0 16px">
<h2>Authorize {client}</h2>{badge}
<p><b>{client}</b> wants to deploy and manage apps on your account (scope: {scopes}).</p>
<p>After you approve, you will be sent to: <code style="word-break:break-all">{target}</code></p>
{err}<form method="post"><input type="hidden" name="req" value="{req}">
<label>Email<br><input name="email" size="44" autocomplete="email"></label><br>
<label>Password<br><input name="password" type="password" size="44"></label><br><br>
<label>or paste an API key<br><input name="api_key" size="44" placeholder="pk_..."></label><br><br>
<button name="action" value="approve">Approve</button> <button name="action" value="deny">Deny</button></form></body></html>"""
VERIFIED_BADGE = '<p style="color:#0a6b2d">&#10003; Verified app: redirects to a known MCP client domain.</p>'
UNVERIFIED_BADGE = ('<div style="border:2px solid #b42318;background:#fef3f2;padding:10px 12px;border-radius:6px"><b style="color:#b42318">&#9888; Unverified app</b><br>'
    'Aloof has not verified this app or its name. Only approve if you just started this connection yourself and you trust <b>{host}</b>. '
    'Approving gives it full access to your Aloof account.</div>')
CONSENT_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                   "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
def redirect_target(redirect_uri):
    """The exact origin the user will be sent to: scheme://host[:port] (native schemes show scheme://host)."""
    u = urlsplit(str(redirect_uri or "")); host = u.hostname or ""
    if ":" in host: host = f"[{host}]"
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")
async def login_page(request: Request):
    form = await request.form() if request.method == "POST" else {}
    rid = form.get("req") or request.query_params.get("req", ""); p = S.get("pending", rid)
    if not p: return HTMLResponse("authorization request expired; start again from your MCP client", 400)
    params = AuthorizationParams.model_validate(p["params"])
    target = redirect_target(params.redirect_uri); verified = is_verified_redirect(params.redirect_uri)
    badge = VERIFIED_BADGE if verified else UNVERIFIED_BADGE.format(host=html.escape(target))
    name = _clean_name(p.get("client_name")) or p["client_id"]
    render = lambda err="": HTMLResponse(PAGE.format(client=html.escape(name), badge=badge, target=html.escape(target), scopes=html.escape(" ".join(params.scopes or ["paas"])),
        req=html.escape(rid), err=f'<p style="color:red">{html.escape(err)}</p>' if err else ""), headers=CONSENT_HEADERS)
    if request.method == "GET": return render()
    if form.get("action") == "deny":
        S.rm("pending", rid); return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state), 302)
    key = (form.get("api_key") or "").strip(); subject = "api-key"
    if not key and form.get("email") and form.get("password"):
        key = password_login(form["email"].strip(), form["password"]); subject = form["email"].strip().lower()
        if not key: return render("invalid email or password")
    if not key: return render("enter your email and password, or an API key")
    if not check_api_key(key): return render("invalid API key")
    code = "mcp_ac_" + secrets.token_urlsafe(32)
    ac = AuthorizationCode(code=code, scopes=params.scopes or ["paas"], expires_at=time.time()+CODE_TTL, client_id=p["client_id"],
        code_challenge=params.code_challenge, redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
        resource=params.resource, subject=subject)
    S.put("code", code, {"code": ac.model_dump(mode="json"), "api_key": key, "subject": subject}, ttl=CODE_TTL); S.rm("pending", rid)
    return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302)
