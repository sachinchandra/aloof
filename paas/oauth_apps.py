"""Connected OAuth/MCP apps for an account: list them and revoke one (deletes its access + refresh tokens, codes and pending
requests). Reads the MCP server's token store (kv kinds oauth:*) directly; each token is bound to an account through the
encrypted API key it carries, so an account only ever sees and revokes its own connections."""
import json, time
from .db import q, kv_del
from . import crypto
def _key_hashes(account_id):
    hs = {r["api_key_hash"] for r in q("select api_key_hash from accounts where id=?", (account_id,)) if r["api_key_hash"]}
    return hs | {r["key_hash"] for r in q("select key_hash from api_keys where account_id=?", (account_id,))}
def _mine(v, hs):
    k = v.get("api_key") if isinstance(v, dict) else None
    if isinstance(k, str) and k.startswith("enc:"):
        try: k = crypto.dec(k[4:])
        except Exception: return False
    return bool(k) and crypto.sha256(k) in hs
def _tokens(account_id):
    hs, now = _key_hashes(account_id), time.time()
    for kind in ("access", "refresh"):
        for r in q("select k, v, exp from kv where kind=?", ("oauth:" + kind,)):
            if r["exp"] and r["exp"] < now: continue
            try: v = json.loads(r["v"])
            except ValueError: continue
            if _mine(v, hs): yield kind, r["k"], v
def _client(cid):
    raw = q("select v from kv where kind='oauth:client' and k=?", (cid,), one=True)
    try: return json.loads(raw["v"]) if raw else {}
    except ValueError: return {}
def list_apps(account_id):
    apps = {}
    for kind, _, v in _tokens(account_id):
        cid = v.get("client_id"); a = apps.setdefault(cid, {"client_id": cid, "access_tokens": 0, "refresh_tokens": 0, "expires_at": 0})
        a[kind + "_tokens"] += 1; a["expires_at"] = max(a["expires_at"], v.get("expires_at") or 0)
    out = []
    for cid, a in apps.items():
        c = _client(cid); ru = (c.get("redirect_uris") or [None])[0]
        from urllib.parse import urlsplit
        out.append(a | {"name": c.get("client_name") or (cid if str(cid).startswith("https://") else "Unnamed app"),
                        "redirect_host": urlsplit(ru).hostname if ru else None, "registered_at": c.get("client_id_issued_at")})
    return sorted(out, key=lambda x: -x["expires_at"])
def revoke(account_id, client_id):
    n = 0
    for kind, k, v in list(_tokens(account_id)):
        if v.get("client_id") == client_id: kv_del("oauth:" + kind, k); n += 1
    return n
