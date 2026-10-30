"""N2: open console/proxy websockets per account, so a suspend or delete closes them at once; a 30s re-check per socket
covers other worker processes and any path that changes the account without calling close_account."""
import asyncio, threading, logging
from .db import q
log = logging.getLogger("paas.ws")
RECHECK_S = 30
_lock = threading.Lock(); _open = {}   # account_id -> {token: (loop, ws, kill)}
def active(account_id):
    a = q("select suspended, deleting from accounts where id=?", (account_id,), one=True)
    return bool(a) and not a.get("suspended") and not a.get("deleting")
def register(account_id, ws, kill=None):
    tok = object(); loop = asyncio.get_running_loop()
    with _lock: _open.setdefault(account_id, {})[tok] = (loop, ws, kill)
    return tok
def unregister(account_id, tok):
    with _lock:
        d = _open.get(account_id) or {}; d.pop(tok, None)
        if not d: _open.pop(account_id, None)
async def _close(ws, kill, reason):
    try:
        if kill: kill()
    except Exception: pass
    try: await ws.close(code=4403, reason=reason)
    except Exception: pass
def close_account(account_id, reason="account suspended"):
    """Callable from sync code (admin routes run in a thread pool). Returns how many sockets were told to close."""
    with _lock: items = list((_open.get(account_id) or {}).values())
    for loop, ws, kill in items:
        try: asyncio.run_coroutine_threadsafe(_close(ws, kill, reason), loop)
        except Exception as e: log.warning("ws close for account %s: %s", account_id, e)
    return len(items)
def count(account_id):
    with _lock: return len(_open.get(account_id) or {})
async def watchdog(account_id, ws, kill=None, every=None):
    """Runs alongside a socket; closes it once the account is suspended, deleting or gone."""
    while True:
        await asyncio.sleep(every or RECHECK_S)
        try: ok = await asyncio.to_thread(active, account_id)
        except Exception: ok = True   # a DB hiccup shouldn't drop a live session
        if not ok: await _close(ws, kill, "account suspended or deleted"); return
