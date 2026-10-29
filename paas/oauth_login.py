"""Sign up / log in with Google, GitHub and X (OAuth 2.0 authorization-code flow, server-side, PKCE S256), next to the
email magic link. A provider is enabled only when both of its env vars are set:
  GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET, GITHUB_OAUTH_CLIENT_ID / GITHUB_OAUTH_CLIENT_SECRET, X_OAUTH_CLIENT_ID / X_OAUTH_CLIENT_SECRET.
Safety:
  - state (single use, 10 min) is also bound to the browser by a short-lived HttpOnly cookie (login-CSRF); Google gets a nonce
    checked inside the signature-verified id_token (iss, aud, exp, nonce, email_verified).
  - redirect_uri is never taken from the request: it is <PAAS_OAUTH_BASE_URL or PAAS_SITE_URL>/auth/<provider>/callback.
  - accounts are linked by a *verified* provider email only, and only to an Aloof account whose email is itself verified
    (otherwise someone could pre-register a victim's address with a password and inherit the victim's Google login).
  - X usually returns no email: the identity is parked and the user confirms an email by magic link before anything is created/linked.
"""
import os, json, time, secrets, hashlib, base64, urllib.parse, httpx
from .db import q, kv_put, kv_get, audit
from . import engine
E = os.environ.get
STATE_TTL = 600; PENDING_TTL = 1800
STATE_COOKIE = "aloof_oauth_state"
PROVIDERS = {
    "google": {"label": "Google", "env": "GOOGLE_OAUTH", "pkce": True,
               "authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
               "scope": "openid email profile"},
    "github": {"label": "GitHub", "env": "GITHUB_OAUTH", "pkce": True,
               "authorize": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token",
               "scope": "read:user user:email"},
    "x":      {"label": "X", "env": "X_OAUTH", "pkce": True,
               "authorize": "https://x.com/i/oauth2/authorize", "token": "https://api.x.com/2/oauth2/token",
               "scope": "users.read tweet.read users.email"},
}
GOOGLE_ISS = ("https://accounts.google.com", "accounts.google.com"); GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"
class AuthError(Exception):
    def __init__(self, code, detail=""): super().__init__(detail or code); self.code = code
def creds(p):
    c = PROVIDERS[p]["env"]; return E(c + "_CLIENT_ID"), E(c + "_CLIENT_SECRET")
def enabled(p):
    """Both credentials set, and not switched off with AUTH_<PROVIDER>_ENABLED=0 (keeps the keys, hides the button)."""
    i, s = creds(p); return bool(i and s) and (E(f"AUTH_{p.upper()}_ENABLED") or "1").strip() != "0"
def providers(): return [{"id": p, "enabled": enabled(p), "label": v["label"]} for p, v in PROVIDERS.items()]
def base_url(): return (E("PAAS_OAUTH_BASE_URL") or E("PAAS_SITE_URL") or E("PAAS_PUBLIC_API_URL") or "http://localhost:8000").rstrip("/")
def redirect_uri(p): return f"{base_url()}/auth/{p}/callback"
# ---- HTTP (module functions so tests can mock providers) ----
def _post(url, **kw):
    with httpx.Client(timeout=20) as c: return c.post(url, **kw)
def _get(url, **kw):
    with httpx.Client(timeout=20) as c: return c.get(url, **kw)
# ---- start ----
def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def start(p, nxt, link_account=None):
    """Returns (authorize_url, state). nxt must already be a safe relative path."""
    if p not in PROVIDERS or not enabled(p): raise AuthError("provider_disabled")
    cid, _ = creds(p); st = secrets.token_urlsafe(32); ver = secrets.token_urlsafe(48); nonce = secrets.token_urlsafe(24)
    kv_put("oauth_login", hashlib.sha256(st.encode()).hexdigest(),
           json.dumps({"p": p, "ver": ver, "nonce": nonce, "next": nxt, "link": link_account, "t": time.time()}), ttl=STATE_TTL)
    prm = {"response_type": "code", "client_id": cid, "redirect_uri": redirect_uri(p), "scope": PROVIDERS[p]["scope"], "state": st,
           "code_challenge": _b64(hashlib.sha256(ver.encode()).digest()), "code_challenge_method": "S256"}
    if p == "google": prm |= {"nonce": nonce, "prompt": "select_account"}
    if p == "github": prm["allow_signup"] = "true"
    return PROVIDERS[p]["authorize"] + "?" + urllib.parse.urlencode(prm), st
def take_state(p, st, cookie_st):
    """Single use; must match the provider and the browser's state cookie."""
    if not st or not cookie_st or not secrets.compare_digest(st, cookie_st): raise AuthError("state_invalid")
    h = hashlib.sha256(st.encode()).hexdigest()
    r = q("delete from kv where kind='oauth_login' and k=? returning v, exp", (h,))
    if not r or (r[0]["exp"] and r[0]["exp"] < time.time()): raise AuthError("state_invalid")
    s = json.loads(r[0]["v"])
    if s["p"] != p: raise AuthError("state_invalid")
    return s
def peek_next(st):
    try:
        v = kv_get("oauth_login", hashlib.sha256((st or "").encode()).hexdigest()); return json.loads(v)["next"] if v else None
    except Exception: return None
# ---- token exchange + profile ----
def _exchange(p, code, ver):
    cid, sec = creds(p); data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(p), "code_verifier": ver}
    if p == "x": r = _post(PROVIDERS[p]["token"], data=data | {"client_id": cid}, auth=(cid, sec), headers={"Accept": "application/json"})
    else: r = _post(PROVIDERS[p]["token"], data=data | {"client_id": cid, "client_secret": sec}, headers={"Accept": "application/json"})
    try: j = r.json()
    except Exception: j = {}
    if r.status_code >= 300 or not j.get("access_token") or j.get("error"): raise AuthError("provider_error", f"{p} token exchange failed ({r.status_code})")
    return j
_jwks_cache = {"t": 0, "keys": {}}
def _google_keys():
    if time.time() - _jwks_cache["t"] > 3600 or not _jwks_cache["keys"]:
        import jwt
        ks = _get(GOOGLE_JWKS).json()["keys"]
        _jwks_cache.update(t=time.time(), keys={k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k)) for k in ks})
    return _jwks_cache["keys"]
def verify_google_id_token(tok, nonce):
    import jwt
    try:
        kid = jwt.get_unverified_header(tok).get("kid"); key = _google_keys().get(kid)
        if key is None: _jwks_cache["t"] = 0; key = _google_keys().get(kid)   # key rotation
        if key is None: raise AuthError("provider_error", "unknown Google signing key")
        c = jwt.decode(tok, key, algorithms=["RS256"], audience=creds("google")[0], options={"require": ["exp", "iat", "iss", "aud", "sub"]}, leeway=60)
    except jwt.PyJWTError as e: raise AuthError("provider_error", f"id_token rejected: {type(e).__name__}")
    if c.get("iss") not in GOOGLE_ISS: raise AuthError("provider_error", "id_token issuer")
    if not nonce or not secrets.compare_digest(str(c.get("nonce") or ""), nonce): raise AuthError("state_invalid", "id_token nonce")
    return c
def profile(p, tok, nonce):
    """-> {sub, email (lowercased or None), email_verified, name}"""
    at = tok["access_token"]
    if p == "google":
        c = verify_google_id_token(tok.get("id_token") or "", nonce)
        return {"sub": str(c["sub"]), "email": (c.get("email") or "").lower() or None, "email_verified": c.get("email_verified") is True, "name": c.get("name")}
    if p == "github":
        h = {"Authorization": f"Bearer {at}", "Accept": "application/vnd.github+json"}
        u = _get("https://api.github.com/user", headers=h)
        if u.status_code >= 300: raise AuthError("provider_error", "GitHub /user failed")
        u = u.json(); em = _get("https://api.github.com/user/emails", headers=h)
        rows = em.json() if em.status_code < 300 and isinstance(em.json(), list) else []
        pv = next((e for e in rows if e.get("primary") and e.get("verified")), None)
        anyp = next((e for e in rows if e.get("primary")), None)
        return {"sub": str(u["id"]), "email": ((pv or anyp or {}).get("email") or "").lower() or None, "email_verified": bool(pv), "name": u.get("name") or u.get("login")}
    if p == "x":
        r = _get("https://api.x.com/2/users/me", params={"user.fields": "confirmed_email,name,username"}, headers={"Authorization": f"Bearer {at}"})
        if r.status_code >= 300: raise AuthError("provider_error", "X /2/users/me failed")
        d = r.json().get("data") or {}
        em = (d.get("confirmed_email") or "").lower() or None   # only present with users.email scope + app setting; X confirmed it
        return {"sub": str(d["id"]), "email": em, "email_verified": bool(em), "name": d.get("name") or d.get("username")}
    raise AuthError("provider_disabled")
# ---- identities ----
def identity(p, sub): return q("select * from provider_identities where provider=? and subject=?", (p, sub), one=True)
def link(account_id, p, prof):
    acct = q("select email_verified from accounts where id=?", (account_id,), one=True)
    if not acct or not acct.get("email_verified"): raise AuthError("state_invalid", "verify the account email before connecting a social login")
    ex = identity(p, prof["sub"])
    if ex and ex["account_id"] != account_id: raise AuthError("already_linked")
    if q("select 1 from provider_identities where account_id=? and provider=? and subject!=?", (account_id, p, prof["sub"]), one=True):
        raise AuthError("already_linked", "this account is connected to a different account at that provider; disconnect it first")
    if ex: q("update provider_identities set last_login=?, email=? where id=?", (time.time(), prof.get("email"), ex["id"]))
    else: q("insert into provider_identities(account_id,provider,subject,email,email_verified,name,created,last_login) values(?,?,?,?,?,?,?,?)",
            (account_id, p, prof["sub"], prof.get("email"), int(bool(prof.get("email_verified"))), (prof.get("name") or "")[:200], time.time(), time.time()))
    audit(account_id, None, "auth.identity_link", p)
def list_identities(account_id):
    return [{"provider": r["provider"], "email": r["email"], "name": r["name"], "connected": r["created"], "last_login": r["last_login"]}
            for r in q("select * from provider_identities where account_id=? order by created", (account_id,))]
def unlink(account_id, p):
    n = len(q("delete from provider_identities where account_id=? and provider=? returning id", (account_id, p)))
    if n: audit(account_id, None, "auth.identity_unlink", p)
    return n
def resolve(p, prof, link_account=None):
    """-> (account, created, pending_id). Raises AuthError(code)."""
    if link_account:   # signed in: connect this provider to the current account
        link(link_account, p, prof); return q("select * from accounts where id=?", (link_account,), one=True), False, None
    ex = identity(p, prof["sub"])
    if ex:
        q("update provider_identities set last_login=? where id=?", (time.time(), ex["id"]))
        return q("select * from accounts where id=?", (ex["account_id"],), one=True), False, None
    em = prof.get("email")
    if not em:
        if p != "x": raise AuthError("email_unverified")
        pid = secrets.token_urlsafe(24); kv_put("oauth_pending", pid, json.dumps({"p": p, "prof": prof}), ttl=PENDING_TTL); return None, False, pid
    acc = q("select * from accounts where email=?", (em,), one=True)
    if not prof.get("email_verified"): raise AuthError("email_linked_other" if acc else "email_unverified")
    if acc:
        if not acc.get("email_verified"): raise AuthError("email_linked_other")   # unverified Aloof account: never inherit it
        link(acc["id"], p, prof); return acc, False, None
    acc = engine.create_account(em, None, verified=True); link(acc["id"], p, prof)
    return q("select * from accounts where id=?", (acc["id"],), one=True), True, None
def pending(pid):
    v = kv_get("oauth_pending", pid or ""); return json.loads(v) if v else None
def finish_pending(pid, account_id):
    """After the magic link proved the email: attach the parked X identity to that account."""
    from .db import kv_del
    d = pending(pid)
    if not d: return False
    kv_del("oauth_pending", pid); link(account_id, d["p"], d["prof"]); return True
