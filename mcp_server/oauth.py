"""OAuth 2.1 authorization server for the MCP endpoint (MCP auth spec 2025-06-18):
- RFC 9728 protected-resource metadata, RFC 8414 AS metadata, RFC 7591 dynamic client registration,
  authorization code + PKCE (S256, enforced by the SDK), refresh-token rotation, revocation.
- The user "logs in" on /oauth/login by pasting their platform API key (or signing up); the issued
  access token maps to that account's API key server-side. Raw pk_ API keys are also accepted as bearer tokens."""
import os, time, json, secrets, sqlite3, threading, html, httpx
from pydantic import AnyUrl
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
    OAuthAuthorizationServerProvider, construct_redirect_uri, TokenError)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
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
class PaasOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self, issuer): self.issuer = issuer.rstrip("/")
    async def get_client(self, client_id):
        d = S.get("client", client_id); return OAuthClientInformationFull.model_validate(d) if d else None
    async def register_client(self, client_info): S.put("client", client_info.client_id, client_info.model_dump(mode="json"))
    async def authorize(self, client, params: AuthorizationParams) -> str:
        rid = secrets.token_urlsafe(16)
        S.put("pending", rid, {"client_id": client.client_id, "client_name": client.client_name, "params": params.model_dump(mode="json")}, ttl=600)
        return f"{self.issuer}/oauth/login?req={rid}"
    async def load_authorization_code(self, client, code):
        d = S.get("code", code)
        return AuthorizationCode.model_validate(d["code"]) if d and d["code"]["client_id"] == client.client_id else None
    def _issue(self, client_id, scopes, api_key, subject, resource):
        at, rt = "mcp_at_" + secrets.token_urlsafe(32), "mcp_rt_" + secrets.token_urlsafe(32)
        meta = {"client_id": client_id, "scopes": scopes, "api_key": api_key, "subject": subject, "resource": resource}
        S.put("access", at, meta | {"expires_at": int(time.time()) + ACCESS_TTL}, ttl=ACCESS_TTL)
        S.put("refresh", rt, meta | {"expires_at": int(time.time()) + REFRESH_TTL}, ttl=REFRESH_TTL)
        return OAuthToken(access_token=at, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=rt, scope=" ".join(scopes))
    async def exchange_authorization_code(self, client, code: AuthorizationCode):
        d = S.get("code", code.code); S.rm("code", code.code)   # single use
        if not d: raise TokenError("invalid_grant", "code expired or used")
        return self._issue(client.client_id, code.scopes, d["api_key"], d["subject"], code.resource)
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
PAGE = """<!doctype html><html><body style="font-family:system-ui;max-width:460px;margin:60px auto">
<h2>Authorize {client}</h2><p><b>{client}</b> wants to deploy and manage apps on your account (scope: {scopes}).</p>
{err}<form method="post"><input type="hidden" name="req" value="{req}">
<label>Email<br><input name="email" size="44" autocomplete="email"></label><br>
<label>Password<br><input name="password" type="password" size="44"></label><br><br>
<label>or paste an API key<br><input name="api_key" size="44" placeholder="pk_..."></label><br><br>
<button name="action" value="approve">Approve</button> <button name="action" value="deny">Deny</button></form></body></html>"""
async def login_page(request: Request):
    form = await request.form() if request.method == "POST" else {}
    rid = form.get("req") or request.query_params.get("req", ""); p = S.get("pending", rid)
    if not p: return HTMLResponse("authorization request expired; start again from your MCP client", 400)
    params = AuthorizationParams.model_validate(p["params"])
    render = lambda err="": HTMLResponse(PAGE.format(client=html.escape(p.get("client_name") or p["client_id"]), scopes=html.escape(" ".join(params.scopes or ["paas"])), req=rid, err=f'<p style="color:red">{html.escape(err)}</p>' if err else ""))
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
