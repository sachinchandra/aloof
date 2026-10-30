"""Open console/proxy websockets per account and credential.
- R1: _ws_account registers the socket the moment auth passes (before any await), then re-checks, so a suspend/delete/revoke
  that lands between auth and the stream setup can't miss it.
- R2: each socket remembers how it authenticated (API key / deploy token hash, or session); revalidate(account_id) is
  called by every key/token/session revoke or rotate path and closes sockets whose credential no longer resolves.
- R3: the registry is per process. Production runs a single paas.main process, so close_account/revalidate reach every
  socket; with more workers only the per-socket watchdog (RECHECK_S) applies there. See docs/websocket-sessions.md.
- R4: the watchdog fails closed: a DB error is retried once, then the socket is closed."""
import asyncio, threading, logging, time, functools
from .db import q
log = logging.getLogger("paas.ws")
RECHECK_S = 30
_lock = threading.Lock(); _open = {}   # account_id -> {id(ws): Handle}
class Handle:
    def __init__(self, account_id, ws, cred, loop):
        self.account_id, self.ws, self.cred, self.loop, self.kill, self.task, self.closed = account_id, ws, cred, loop, None, None, False
def active(account_id):
    a = q("select suspended, deleting from accounts where id=?", (account_id,), one=True)
    return bool(a) and not a.get("suspended") and not a.get("deleting")
def cred_valid(account_id, cred):
    kind, h = cred or (None, None)
    if kind == "key":
        if q("select 1 from accounts where id=? and api_key_hash=?", (account_id, h), one=True): return True
        if q("select 1 from api_keys where account_id=? and key_hash=?", (account_id, h), one=True): return True
        t = q("select expires from deploy_tokens where account_id=? and token_hash=?", (account_id, h), one=True)
        return bool(t) and not (t.get("expires") and t["expires"] < time.time())
    if kind == "session":
        return bool(q("select 1 from sessions where token_hash=? and account_id=? and expires>?", (h, account_id, time.time()), one=True))
    return False
def valid(account_id, cred): return active(account_id) and cred_valid(account_id, cred)
def register(account_id, ws, cred=None):
    """Call synchronously from inside the socket's event loop, right after auth. Starts the watchdog too."""
    loop = asyncio.get_running_loop(); h = Handle(account_id, ws, cred, loop)
    with _lock: _open.setdefault(account_id, {})[id(ws)] = h
    try: ws.state.aloof_ws = h
    except Exception: pass
    h.task = loop.create_task(watchdog(h))
    return h
def handle(ws): return getattr(getattr(ws, "state", None), "aloof_ws", None)
def set_kill(ws, fn):
    h = handle(ws)
    if h:
        h.kill = fn
        if h.closed: _kill(h)
def release(ws):
    h = handle(ws)
    if not h: return
    with _lock:
        d = _open.get(h.account_id) or {}; d.pop(id(ws), None)
        if not d: _open.pop(h.account_id, None)
    if h.task: h.task.cancel()
def _kill(h):
    try:
        if h.kill: h.kill()
    except Exception: pass
async def _close(h, reason):
    h.closed = True; _kill(h)
    try: await h.ws.close(code=4403, reason=reason)
    except Exception: pass
def _schedule_close(h, reason):
    try:
        if h.loop.is_running(): asyncio.run_coroutine_threadsafe(_close(h, reason), h.loop)
    except Exception as e: log.warning("ws close for account %s: %s", h.account_id, e)
def _handles(account_id):
    with _lock: return list((_open.get(account_id) or {}).values())
def close_account(account_id, reason="account suspended"):
    """Callable from sync code (admin routes run in a thread pool). Returns how many sockets were told to close."""
    hs = _handles(account_id)
    for h in hs: _schedule_close(h, reason)
    return len(hs)
def revalidate(account_id, reason="credential revoked"):
    """R2: after any key/token/session revoke or rotate, close this account's sockets whose credential is gone."""
    n = 0
    for h in _handles(account_id):
        try: ok = valid(h.account_id, h.cred)
        except Exception: ok = False
        if not ok: _schedule_close(h, reason); n += 1
    return n
def count(account_id): return len(_handles(account_id))
def tracked(f):
    """Wraps a websocket endpoint so its registry entry is always released, whichever path returns."""
    @functools.wraps(f)
    async def w(*a, **k):
        try: return await f(*a, **k)
        finally:
            ws = k.get("ws") or (a[0] if a else None)
            if ws is not None: release(ws)
    return w
async def watchdog(h, every=None):
    while True:
        await asyncio.sleep(every or RECHECK_S)
        ok = None
        for attempt in range(2):   # R4: one retry, then fail closed
            try: ok = await asyncio.to_thread(valid, h.account_id, h.cred); break
            except Exception as e:
                log.warning("ws recheck for account %s failed (%s), attempt %d", h.account_id, e, attempt + 1)
                if attempt == 0: await asyncio.sleep(1)
        if not ok: await _close(h, "account suspended or deleted, credential revoked, or check failed"); return
