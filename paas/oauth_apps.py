"""Connected OAuth/MCP apps for an account: list them and revoke one (deletes its access + refresh tokens, codes and pending
requests). Reads the MCP server's token store (kv kinds oauth:*) directly; each token is bound to an account through the
encrypted API key it carries, so an account only ever sees and revokes its own connections. Revoke also deletes that
account's unexchanged auth codes for the client. Pending (not yet approved) requests carry no account, so there is nothing
of this account's to delete there: they can only become a code after a fresh sign-in."""
import json, time
REFRESH_TTL = 30 * 86400   # must equal mcp_server/oauth.py REFRESH_TTL (tested)
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
def _owner(v): return v.get("client_id") or (v.get("code") or {}).get("client_id")
KINDS = ("access", "refresh", "code")
def _dec_key(v):
    k = v.get("api_key") if isinstance(v, dict) else None
    if isinstance(k, str) and k.startswith("enc:"):
        try: return crypto.dec(k[4:])
        except Exception: return None
    return k
def backfill(limit=5000):
    """One-off (idempotent): set kv.acct = sha256(bound API key) on OAuth token/code rows written before migration v13."""
    n = 0
    for r in q("select kind, k, v from kv where kind in ('oauth:access','oauth:refresh','oauth:code') and acct is null limit ?", (limit,)):
        try: key = _dec_key(json.loads(r["v"]))
        except ValueError: key = None
        q("update kv set acct=? where kind=? and k=?", (crypto.sha256(key) if key else "-", r["kind"], r["k"])); n += 1
    return n
def _tokens(account_id, client_id=None, kinds=("access", "refresh")):
    """This account's rows (optionally one client), selected in SQL by kv.acct (indexed) and, for one client, the client_id in the
    JSON text; only those rows are decrypted, and the decrypted key and client_id are still checked exactly."""
    backfill()
    hs, now = sorted(_key_hashes(account_id)), time.time()
    if not hs: return
    like = "%" + json.dumps(client_id)[1:-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if client_id else None
    for kind in kinds:
        sql = f"select k, v, exp from kv where kind=? and acct in ({','.join('?' * len(hs))})"; args = ["oauth:" + kind, *hs]
        if like: sql += " and v like ? escape '\\'"; args.append(like)
        for r in q(sql, tuple(args)):
            if r["exp"] and r["exp"] < now: continue
            try: v = json.loads(r["v"])
            except ValueError: continue
            if client_id is not None and _owner(v) != client_id: continue
            if _mine(v, set(hs)): yield kind, r["k"], v
def _client(cid):
    raw = q("select v from kv where kind='oauth:client' and k=?", (cid,), one=True)
    try: return json.loads(raw["v"]) if raw else {}
    except ValueError: return {}
def list_apps(account_id):
    apps = {}
    for kind, _, v in _tokens(account_id):
        cid = v.get("client_id"); a = apps.setdefault(cid, {"client_id": cid, "access_tokens": 0, "refresh_tokens": 0, "expires_at": 0, "last_token_at": None})
        a[kind + "_tokens"] += 1; a["expires_at"] = max(a["expires_at"], v.get("expires_at") or 0)
        if kind == "refresh" and v.get("expires_at"):   # a refresh token is issued on approval and on every renewal
            a["last_token_at"] = max(a["last_token_at"] or 0, int(v["expires_at"]) - REFRESH_TTL)
    out = []
    for cid, a in apps.items():
        c = _client(cid); ru = (c.get("redirect_uris") or [None])[0]
        from urllib.parse import urlsplit
        out.append(a | {"name": c.get("client_name") or (cid if str(cid).startswith("https://") else "Unnamed app"),
                        "redirect_host": urlsplit(ru).hostname if ru else None, "registered_at": c.get("client_id_issued_at")})
    return sorted(out, key=lambda x: -x["expires_at"])
def revoke(account_id, client_id):
    """Deletes this account's tokens and unexchanged codes for client_id. Returns counts per kind."""
    n = {"access": 0, "refresh": 0, "code": 0}
    for kind, k, v in list(_tokens(account_id, client_id, ("access", "refresh", "code"))):
        kv_del("oauth:" + kind, k); n[kind] += 1
    # deploy tokens the connection minted (create_deploy_token over MCP) go with it
    from .db import q
    rows = q("select id from deploy_tokens where account_id=? and oauth_client=?", (account_id, client_id))
    q("delete from deploy_tokens where account_id=? and oauth_client=?", (account_id, client_id)); n["deploy_tokens"] = len(rows)
    return n
