"""Admin 'view as user' (impersonation) never shows a customer's secrets (owner decision Oct 30).
Every JSON response served to an impersonation session is masked: values under secret-like keys (env, secrets, passwords,
tokens, API keys, connection strings, private keys, digests), credentials embedded in URLs, and strings that look like keys.
Reveal/copy paths are refused with 403. The dashboard shows 'Secrets hidden while viewing as user'."""
import re, json
HIDDEN = "hidden while viewing as user"
HEADER = "X-Aloof-Secrets-Hidden"
_KEY = re.compile(r"(^|_)(env|environment|secrets?|password|passwd|pass|pw|token|tokens|api_?key|new_api_key|key|keys|secret_key|"
                  r"private_key|database_url|db_url|dsn|connection(_string|_url)?|conn|url_with_password|auth|authorization|"
                  r"credentials?|digest|webhook_secret|signing_secret|totp|otp|recovery|cookie|session)($|_)", re.I)
_SAFE_KEYS = {"key_prefix", "api_key_prefix", "token_id", "has_password", "tokens_count", "access_tokens", "refresh_tokens", "keys_count"}
_VAL = [re.compile(p) for p in (r"\b(?:pk|ak|sk|rk|dt|ghp|gho|ghs|github_pat|xox[abpr]|whsec|mcp)_[A-Za-z0-9_\-]{8,}",
                                r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
                                r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")]
_URL_CRED = re.compile(r"([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^@\s/]+)@", re.I)
# reveal/copy endpoints and parameters: refused outright in the read-only view
_BLOCK_PATHS = [re.compile(p) for p in (r"/reveal\b", r"/copy\b", r"/credentials?/[^/]+/(value|secret)", r"/export(\.|/|$)")]
_BLOCK_QS = {"reveal", "show", "show_secrets", "plaintext", "decrypt"}
def blocked(path, query):
    if any(p.search(path) for p in _BLOCK_PATHS): return True
    return any(k.lower() in _BLOCK_QS and (v or "").lower() not in ("", "0", "false", "no") for k, v in query)
def _s(v):
    v = _URL_CRED.sub(lambda m: f"{m[1]}{m[2]}:{HIDDEN}@", v)
    for p in _VAL: v = p.sub(HIDDEN, v)
    return v
def mask(o, under_secret=False):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            sk = isinstance(k, str) and k.lower() not in _SAFE_KEYS and bool(_KEY.search(k))
            if under_secret and not isinstance(v, (dict, list)): out[k] = HIDDEN if v not in (None, "") else v   # env: names stay, values go
            elif sk and isinstance(v, (dict, list)): out[k] = mask(v, True)
            elif sk and v not in (None, "", True, False) and not isinstance(v, (int, float)): out[k] = HIDDEN
            else: out[k] = mask(v, under_secret)
        return out
    if isinstance(o, list): return [mask(x, under_secret) for x in o]
    if isinstance(o, str): return HIDDEN if under_secret and o else _s(o)
    return o
def mask_bytes(body):
    try: return json.dumps(mask(json.loads(body))).encode()
    except Exception: return None
