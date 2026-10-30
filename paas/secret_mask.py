"""Admin "view as user" (impersonated sessions) never sees customer secrets: env values, API keys and tokens, database
passwords and connection strings come back masked, and nothing in the read-only view can reveal them. Applied on the
server to every /v1 JSON or text response of an impersonated session, on top of the per-endpoint checks in api.py."""
import json, re
MASK = "••••••••"
# key names whose values are secrets wherever they appear in a JSON body
_SECRET_KEY = re.compile(r"(?:^|_)(?:password|passwd|pass|secret|token|api_key|apikey|key_value|private_key|database_url|dsn|connection_string|conn_str|new_api_key|access_token|refresh_token)$", re.I)
_ENV_KEY = re.compile(r"^(?:env|environment|env_values|secrets)$", re.I)
_URL_CREDS = re.compile(r"\b([a-z][a-z0-9+.\-]*://)([^:/@\s\"']+):([^@\s\"']+)@", re.I)
_TOKENS = re.compile(r"\b(pk|ml|s|dt|tok|ghp|gho|github_pat|sk|rk|whsec|xox[abp])_[A-Za-z0-9_\-]{12,}")
_ASSIGN = re.compile(r"\b([A-Z][A-Z0-9_]*(?:PASSWORD|PASS|SECRET|TOKEN|API_KEY|KEY|DSN|DATABASE_URL|_URL))=(\S+)")
def mask_text(s: str) -> str:
    s = _URL_CREDS.sub(lambda m: f"{m.group(1)}{m.group(2)}:{MASK}@", s)
    s = _TOKENS.sub(lambda m: f"{m.group(1)}_{MASK}", s)
    return _ASSIGN.sub(lambda m: f"{m.group(1)}={MASK}", s)
def mask(v, key=""):
    if isinstance(v, dict):
        if _ENV_KEY.match(key): return {k: MASK for k in v}
        return {k: mask(x, k) for k, x in v.items()}
    if isinstance(v, list): return [mask(x, key) for x in v]
    if isinstance(v, str):
        if key and _SECRET_KEY.search(key) and v: return mask_text(v) if "://" in v else MASK
        return mask_text(v)
    return v
def mask_body(body: bytes, content_type: str) -> bytes:
    ct = (content_type or "").lower()
    if "json" in ct:
        try: return json.dumps(mask(json.loads(body)), ensure_ascii=False).encode()
        except ValueError: pass
    return mask_text(body.decode("utf-8", "replace")).encode()
