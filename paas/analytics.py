"""Server-side product analytics (PostHog EU). Fire-and-forget: events go to a bounded background queue and a single
daemon thread posts them with a short timeout, so analytics can never fail or slow a request. No-op unless POSTHOG_API_KEY is set.
distinct_id is always str(accounts.id), the same value the frontend passes to posthog.identify(). Test accounts (is_test) are skipped.
Never pass emails, secrets, tokens, API keys or message bodies: properties are filtered through an allow-list per event."""
import os, re, queue, threading, logging, httpx
log = logging.getLogger("paas.analytics")
TIMEOUT = 2.0
EVENTS = {   # event -> allowed property names (anything else is dropped)
    "signup_completed": {"method", "utm_source", "utm_medium", "utm_campaign", "referrer"},
    "first_deploy_succeeded": {"app_id", "region", "source_type"},
    "deploy_succeeded": {"app_id", "region", "source_type", "version"},
    "mcp_connected": {"client_name", "client_host"},
    "plan_upgraded": {"plan", "amount_usd", "interval"},
    "gpu_waitlist_joined": {"gpu"},
}
_SECRETish = re.compile(r"(sk|pk|rk|whsec|dt|mcp_at|mcp_rt|mcp_ac|ml|s)_[A-Za-z0-9_\-]{8,}|[^\s@]+@[^\s@]+\.[^\s@]+")
def api_key():
    k = os.environ.get("POSTHOG_API_KEY", "").strip()
    if k: return k
    try: from .site import posthog_key; return posthog_key() or ""   # POSTHOG_KEY / data/posthog_key (frontend config)
    except Exception: return ""
def host(): return (os.environ.get("POSTHOG_HOST") or "https://eu.i.posthog.com").rstrip("/")
def enabled(): return bool(api_key())
def clean(v, n=200):
    """Short, printable, no emails or key-looking strings."""
    if v is None: return None
    if isinstance(v, bool) or isinstance(v, (int, float)): return v
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(v)).strip()[:n]
    s = _SECRETish.sub("[redacted]", s)
    return s or None
def clean_referrer(v):
    """Keep scheme://host/path only (drops query strings, which can carry tokens or emails)."""
    v = clean(v, 500)
    if not v: return None
    m = re.match(r"^(https?://[^/?#\s]+)(/[^?#\s]*)?", v)
    return clean((m.group(1) + (m.group(2) or "")) if m else None, 300)
def attribution(src):
    """Optional first-touch UTM fields from the frontend, sanitized and length-limited."""
    nested = (src.get("attribution") if isinstance(src, dict) else getattr(src, "attribution", None)) or {}
    g0 = (lambda k: getattr(src, k, None)) if not isinstance(src, dict) else src.get
    g = lambda k: g0(k) or (nested.get(k) if isinstance(nested, dict) else None)
    out = {k: clean(g(k), 100) for k in ("utm_source", "utm_medium", "utm_campaign")}
    out["referrer"] = clean_referrer(g("referrer")); return {k: v for k, v in out.items() if v}
_q = queue.Queue(maxsize=1000); _t = None; _lock = threading.Lock()
_client = None   # tests swap in httpx.Client(transport=httpx.MockTransport(...))
def _worker():
    global _client
    while True:
        body = _q.get()
        try:
            if _client is None: _client = httpx.Client(timeout=TIMEOUT)
            _client.post(f"{host()}/i/v0/e/", json=body)
        except Exception as e: log.debug("posthog send failed: %s", type(e).__name__)
        finally: _q.task_done()
def _ensure():
    global _t
    with _lock:
        if not _t or not _t.is_alive(): _t = threading.Thread(target=_worker, name="posthog", daemon=True); _t.start()
def _is_test(account_id):
    try:
        from .db import q
        r = q("select is_test from accounts where id=?", (int(account_id),), one=True)
        return (not r) or bool(r.get("is_test"))
    except Exception: return True   # unknown -> don't send
def capture(account_id, event, props=None):
    """Never raises. Returns True if queued."""
    try:
        if not enabled() or event not in EVENTS or account_id is None or _is_test(account_id): return False
        p = {k: clean(v) for k, v in (props or {}).items() if k in EVENTS[event]}
        p = {k: v for k, v in p.items() if v is not None} | {"$lib": "aloof-api", "$process_person_profile": True}
        _ensure(); _q.put_nowait({"api_key": api_key(), "event": event, "distinct_id": str(int(account_id)), "properties": p}); return True
    except Exception as e: log.debug("analytics capture skipped: %s", type(e).__name__); return False
def once(account_id, event):
    """True the first time (account, event) is seen; used for first_deploy_succeeded."""
    try:
        from .db import q
        return bool(q("insert into analytics_once(account_id,event) values(?,?) on conflict do nothing returning account_id", (int(account_id), event)))
    except Exception: return False
def account_for_key(key):
    try:
        from .engine import account_by_key; a = account_by_key(key); return a and a["id"]
    except Exception: return None
def flush(timeout=5):
    """Tests only: wait for the queue to drain."""
    import time; end = time.time() + timeout
    while _q.unfinished_tasks and time.time() < end: time.sleep(0.01)
