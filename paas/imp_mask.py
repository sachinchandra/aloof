"""Admin 'view as user' (impersonation) never shows a customer's secrets (owner decision Oct 30).
Every JSON response served to an impersonation session is masked: values under secret-like keys (env, secrets, passwords,
tokens, API keys, connection strings, private keys, digests), credentials embedded in URLs, and strings that look like keys.
Reveal/copy paths are refused with 403. The dashboard shows 'Secrets hidden while viewing as user'."""
import re, json
HIDDEN = "hidden while viewing as user"
HEADER = "X-Aloof-Secrets-Hidden"
_KEY = re.compile(r"(^|_)(env|environment|secrets?|password|passwd|pass|pw|token|tokens|api_?key|new_api_key|key|keys|secret_key|"
                  r"private_key|database_url|db_url|dsn|connection(_string|_url)?|conn|url_with_password|auth|authorization|"
                  r"credentials?|digest|command|cmd|args|entrypoint|release_command|processes|process|procfile|health_?check|health_?cmd|cron|crons|schedule_command|webhook_secret|signing_secret|totp|otp|recovery|cookie|session)($|_)", re.I)
_SAFE_KEYS = {"key_prefix", "api_key_prefix", "token_id", "has_password", "tokens_count", "access_tokens", "refresh_tokens", "keys_count"}
_VAL = [re.compile(p) for p in (r"\b(?:pk|ak|sk|rk|dt|ghp|gho|ghs|github_pat|xox[abpr]|whsec|mcp)_[A-Za-z0-9_\-]{8,}",
                                r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
                                r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")]
_URL_CRED = re.compile(r"([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^\s\"'<>]+)@", re.I)   # greedy: up to the last @ (passwords may hold / or @)
# reveal/copy endpoints and parameters: refused outright in the read-only view
_BLOCK_PATHS = [re.compile(p) for p in (r"/reveal\b", r"/copy\b", r"/credentials?/[^/]+/(value|secret)", r"/export(\.|/|$)")]
_BLOCK_QS = {"reveal", "show", "show_secrets", "plaintext", "decrypt"}
def blocked(path, query):
    if any(p.search(path) for p in _BLOCK_PATHS): return True
    return any(k.lower() in _BLOCK_QS and (v or "").lower() not in ("", "0", "false", "no") for k, v in query)
# M1: secrets written as free text (key=value, --password X, Authorization: ...) in commands, logs and messages
_SECRETISH = r"[A-Za-z0-9_.\-]*(?:pass(?:word|wd|phrase)?|pwd?|secret|token|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key|auth|credential|dsn|database[_\-]?url|db[_\-]?url|cookie|session)[A-Za-z0-9_.\-]*"
_TXT = [(re.compile(r"(?i)\b((?:proxy-)?authorization|x-api-key|cookie|set-cookie)(\s*[:=]\s*)[^\r\n]+"), lambda m: f"{m[1]}{m[2]}{HIDDEN}"),
        (re.compile(r"(?i)(\s|^|[\"'\[,])(--?" + _SECRETISH + r")(=|\s+)(\"[^\"]*\"|'[^']*'|[^\s\"',\]]+)"), lambda m: f"{m[1]}{m[2]}{m[3]}{HIDDEN}"),
        (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/\-=]{6,}"), lambda m: f"{m[1]}{m[2]}{HIDDEN}"),
        (re.compile(r"(\s|^|[\"'\[,])(-p)(=|\s+)(?!\d+\b)(?!hidden\b)([^\s\"',\]]+)"), lambda m: f"{m[1]}{m[2]}{m[3] or ''}{HIDDEN}"),
        (re.compile(r"(?i)\b(" + _SECRETISH + r")([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)"), lambda m: f"{m[1]}{m[2]}{HIDDEN}"),
        (re.compile(r"(?im)^(\s*-?\s*(?:command|cmd|args|entrypoint|release_command)\s*:\s*)\S.*$"), lambda m: f"{m[1]}{HIDDEN}")]
_DUP = re.compile(re.escape(HIDDEN) + r"(?: while viewing as user)+")
def _s(v):
    for p, f in _TXT: v = p.sub(f, v)
    v = _DUP.sub(HIDDEN, v)
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

# M1: free-text output (logs, events, release reasons, deploy logs, diagnose) is hidden entirely in this view
APP_ROUTES = re.compile(r"^/v1/(apps(/.*)?|status)$")
TEXT_ROUTES = re.compile(r"^/v1/apps/[^/]+/(logs(/history)?|events|releases|deploy|diagnose)/?$")
_TEXT_KEYS = re.compile(r"(?i)^(reason|log|logs|log_tail|last_log|tail|lines?|message|msg|messages|output|stdout|stderr|error|errors|detail|details|"
                        r"text|hint|hints|summary|diagnosis|evidence|cause|build_log|deploy_log|excerpt|events|steps|checks|notes?)$")
def hide_text(o):
    if isinstance(o, dict):
        return {k: (HIDDEN if isinstance(k, str) and _TEXT_KEYS.match(k) and v not in (None, "", [], {}) else hide_text(v)) for k, v in o.items()}
    if isinstance(o, list): return [hide_text(x) for x in o]
    return o
# L2: deny by default. Only these routes and content types are served to an impersonation session.
_ALLOW_GET_V1 = re.compile(r"^/v1/(apps(/[^/]+(/(certs|cron|domains|drains|ips|metrics|previews|secrets|status|self-heal|"
    r"logs|logs/history|events|releases|deploy|diagnose))?)?|auth/(me|keys|sessions|connected-apps|identities|providers)|"
    r"billing(/daily|/runway|/invoices/[^/]+)?|config|domains|plans|providers|regions|status|usage|templates(/schema|/[^/]+)?|"
    r"volumes(/[^/]+/(snapshots|offsite))?|pg(/plans|/[^/]+(/backups|/offsite)?)?|tokens|registry|git/credentials|github/status|audit|"
    r"waitlist/byo-server)/?$")
_ALLOW_GET_PAGES = re.compile(r"^/(|about|blog(/[^/]+)?|changelog|compare/[^/]+|customers|dashboard(/.*)?|docs(/[^/]+)?|examples|"
    r"guides(/[^/]+)?|login|pricing|privacy|regions|security|status|templates(/[^/]+)?|terms|static/.+|button\.svg|robots\.txt|"
    r"sitemap\.xml|rss\.xml|favicon\.ico|healthz)$")
ALLOW_POST = {"/v1/auth/logout", "/v1/admin/impersonate/exit"}
_CT_V1 = ("application/json", "text/plain")
_CT_PAGES = ("text/html", "text/css", "text/javascript", "application/javascript", "image/", "application/json", "text/plain", "font/")
def allowed_route(method, path):
    if method in ("GET", "HEAD"): return bool(_ALLOW_GET_V1.match(path) or _ALLOW_GET_PAGES.match(path))
    return method == "POST" and path in ALLOW_POST
def allowed_type(path, ct):
    ct = (ct or "").lower()
    return any(ct.startswith(t) for t in (_CT_V1 if path.startswith("/v1/") else _CT_PAGES))
