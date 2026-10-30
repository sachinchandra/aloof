"""Owner-only account deletion. Destroys every app through engine.destroy_app (containers, networks, domains, template
Postgres and volumes, with_volumes=True), revokes API keys, sessions, deploy tokens, OAuth/connected-app tokens and linked
sign-in identities, cancels a Stripe subscription, then deletes the account row and every dependent row. Money records
(ledger, invoices, payments, stripe_sessions, admin_refunds) and the audit log are kept; invoices lose the billing name and GSTIN.
Accounts 1 and 2 and the owner account can never be deleted; accounts with payments need allow_payments=True."""
import time
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from .db import q, audit
from . import www
R = APIRouter()
PROTECTED_IDS = {1, 2}
# account-keyed rows removed outright (no money in them)
ACCOUNT_TABLES = ["api_keys", "sessions", "deploy_tokens", "provider_identities", "mfa_recovery", "mfa_failures", "lifecycle_mail",
                  "analytics_once", "usage_month", "registry_creds", "git_creds", "github_installs", "github_links", "drains", "uploads",
                  "domains", "volumes", "pg_dbs", "archives", "offsite"]
APP_TABLES = ["instances", "releases", "events", "cron_runs", "deploy_tokens", "domains", "drains", "git_creds", "github_links", "registry_creds", "uploads"]
KEPT_TABLES = ["ledger", "invoices", "payments", "stripe_sessions", "admin_refunds", "audit"]
def _has_table(t):
    try: q(f"select 1 from {t} where 1=0"); return True
    except Exception: return False
def blockers(account_id):
    """What makes deletion risky: live apps, Postgres, volumes, domains, payments. Read-only."""
    n = lambda sql: int((q(sql, (account_id,), one=True) or {}).get("n") or 0)
    b = {"live_apps": n("select count(*) n from apps where account_id=? and status!='destroyed'"),
         "postgres": n("select count(*) n from pg_dbs where account_id=?"), "volumes": n("select count(*) n from volumes where account_id=?"),
         "domains": n("select count(*) n from domains where account_id=?"),
         "payments": n("select count(*) n from stripe_sessions where account_id=? and status='paid' and coalesce(amount_cents,0)>0")
                     + n("select count(*) n from payments where account_id=? and status='paid'")}
    return {k: v for k, v in b.items() if v}
MAX_ID = 2**63 - 1
def _id(account_id):
    """D6/D7: an int in the database's bigint range, else 404 (never a 500 from an out-of-range parameter)."""
    try: i = int(account_id)
    except (TypeError, ValueError): raise HTTPException(404, "no such account")
    if not 0 < i <= MAX_ID: raise HTTPException(404, "no such account")
    return i
def _email_hash(e):
    # D8: the audit row is kept (who deleted what, when) but holds sha256(lowercased email), not the address
    import hashlib; return hashlib.sha256((e or "").strip().lower().encode()).hexdigest()
def _begin(account_id):
    """D1 phase 1, one committed transaction: lock the row, mark suspended+deleting, revoke every credential.
    From here on deploys, volumes, Postgres, top-ups and sign-ins all refuse this account, so nothing new can appear."""
    from . import db, auth, oauth_apps
    import secrets
    with db.tx():
        a = q("select * from accounts where id=?" + auth._lock(), (account_id,), one=True)
        if not a: raise HTTPException(404, "no such account")
        hs = sorted(oauth_apps._key_hashes(account_id))
        q("update accounts set suspended=1, deleting=1, api_key_hash=? where id=?", ("revoked:" + secrets.token_hex(16), account_id))
        for t in ("sessions", "api_keys", "deploy_tokens"):
            if _has_table(t): q(f"delete from {t} where account_id=?", (account_id,))
        q("delete from sessions where impersonated_by=?", (account_id,))
        for h in hs: q("delete from kv where acct=?", (h,))   # OAuth/MCP tokens bound to the account's keys
    from . import ws_registry; ws_registry.close_account(account_id, "account deleted")   # N2: open consoles/proxies
    return a, hs
def _destroy_infra(account_id, out):
    from . import engine, parity
    seen = set()
    for _ in range(5):   # re-scan: an app created by a deploy already in flight when phase 1 committed
        apps = [r["name"] for r in q("select name from apps where account_id=? and status!='destroyed'", (account_id,))]
        apps = [x for x in apps if x not in seen]
        if not apps: break
        for name in apps:
            seen.add(name)
            try: engine.destroy_app(name, with_volumes=True); out["apps"].append(name)
            except Exception as e: out["errors"].append(f"app {name}: {e}")
    for r in q("select name from apps where account_id=? and status='destroyed'", (account_id,)):
        if r["name"] not in seen: engine._sweep_containers(r["name"]); out["apps"].append(r["name"])
    for p in q("select name from pg_dbs where account_id=?", (account_id,)):
        try: parity.pg_destroy(p["name"])
        except Exception as e: out["errors"].append(f"postgres {p['name']}: {e}")
    for v in q("select name from volumes where account_id=?", (account_id,)):
        try: q("update volumes set app=null, mount_path=null where name=?", (v["name"],)); engine.delete_volume(v["name"])
        except Exception as e: out["errors"].append(f"volume {v['name']}: {e}")
    try: out["swept_containers"] = engine.sweep_account_containers(account_id)   # anything labelled for the account
    except Exception as e: out["errors"].append(f"container sweep: {e}")
def delete_account(account_id, actor=None, reason="", allow_payments=False, ignore_cleanup_errors=False, require_empty=False, stripe_client=None):
    """Resumable: any failure before the rows go leaves the account suspended+deleting (no credentials, nothing
    external cancelled yet if infra cleanup failed); re-run the delete to finish. Unsuspend clears deleting."""
    account_id = _id(account_id)
    a = q("select * from accounts where id=?", (account_id,), one=True)
    if not a: raise HTTPException(404, "no such account")
    if account_id in PROTECTED_IDS or a.get("is_admin") or (a.get("email") or "").lower() == (www.ADMIN_EMAIL or "").lower():
        raise HTTPException(400, "this account is protected and can't be deleted")
    b = blockers(account_id)   # every check that can refuse runs before any side effect
    if require_empty and b: raise HTTPException(409, f"account still has live resources: {b}")
    if b.get("payments") and not allow_payments: raise HTTPException(409, "this account has payments; deleting it needs allow_payments")
    eh = _email_hash(a.get("email"))
    a, hs = _begin(account_id)
    out = {"account_id": account_id, "email": a["email"], "apps": [], "errors": [], "revoked": {}, "deleted_rows": {}}
    def _fail(stage):
        audit(actor, None, "admin.delete_account_failed", f"account={account_id} email_sha256={eh} stage={stage} errors={out['errors']}"[:2000])
        raise HTTPException(502, f"{stage} failed; account left suspended and marked deleting, re-run delete to finish: {out['errors'][:3]}")
    _destroy_infra(account_id, out)
    if out["errors"] and not ignore_cleanup_errors: _fail("infrastructure cleanup")   # Stripe/OAuth untouched so far
    n0 = len(out["errors"])
    try:
        from . import oauth_apps
        for c in oauth_apps.list_apps(account_id):
            cid = c.get("client_id")
            if cid: out["revoked"][f"oauth:{cid}"] = oauth_apps.revoke(account_id, cid)
    except Exception as e: out["errors"].append(f"oauth: {e}")
    if a.get("stripe_subscription"):
        try:
            from . import stripe_billing as sb
            sb._call("DELETE", f"/subscriptions/{a['stripe_subscription']}", client=stripe_client); out["stripe_subscription_cancelled"] = a["stripe_subscription"]
        except Exception as e: out["errors"].append(f"stripe subscription {a['stripe_subscription']}: {e}")
    if len(out["errors"]) > n0 and not ignore_cleanup_errors: _fail("subscription/OAuth revoke")
    from . import db
    with db.tx():
        apps = [r["name"] for r in q("select name from apps where account_id=?", (account_id,))]
        for name in apps:
            for t in APP_TABLES:
                if _has_table(t): q(f"delete from {t} where app=?", (name,))
        for t in ACCOUNT_TABLES:
            if not _has_table(t): continue
            c = int((q(f"select count(*) n from {t} where account_id=?", (account_id,), one=True) or {}).get("n") or 0)
            if c: q(f"delete from {t} where account_id=?", (account_id,)); out["deleted_rows"][t] = c
        for h in hs: q("delete from kv where acct=?", (h,))
        q("delete from kv where kind in ('is_test_pin','key_reveal') and k=?", (str(account_id),))
        if a.get("email"):
            q("delete from magic_links where lower(email)=?", (a["email"].lower(),))
            if _has_table("waitlist"):
                c = int((q("select count(*) n from waitlist where lower(email)=?", (a["email"].lower(),), one=True) or {}).get("n") or 0)
                if c: q("delete from waitlist where lower(email)=?", (a["email"].lower(),)); out["deleted_rows"]["waitlist"] = c
        q("update invoices set billing_name=null, gstin=null where account_id=?", (account_id,))
        q("delete from apps where account_id=?", (account_id,)); out["deleted_rows"]["apps"] = len(apps)
        q("delete from accounts where id=?", (account_id,))
        audit(actor, None, "admin.delete_account", f"account={account_id} email_sha256={eh} reason={reason} apps={len(apps)} rows={out['deleted_rows']} "
              f"allow_payments={allow_payments} ignore_cleanup_errors={ignore_cleanup_errors}"[:2000])
    if not out["errors"]: out.pop("errors")
    out["ok"] = True; return out
def leftovers(account_id):
    """Rows still pointing at a deleted account (money tables and audit excluded, they are kept on purpose)."""
    out = {}
    for t in ACCOUNT_TABLES + ["apps", "accounts"]:
        if not _has_table(t): continue
        col = "id" if t == "accounts" else "account_id"
        n = int((q(f"select count(*) n from {t} where {col}=?", (account_id,), one=True) or {}).get("n") or 0)
        if n: out[t] = n
    return out
class Delete(BaseModel): confirm: str; reason: str = ""; allow_payments: bool = False; ignore_cleanup_errors: bool = False   # UI never sends the second
@R.get("/v1/admin/accounts/{account_id}/delete-check")
def delete_check(account_id: int, _=Depends(www.require_admin)):
    account_id = _id(account_id)
    a = q("select id from accounts where id=?", (account_id,), one=True)
    if not a: raise HTTPException(404, "no such account")
    return {"protected": account_id in PROTECTED_IDS, "blockers": blockers(account_id)}
@R.post("/v1/admin/accounts/{account_id}/delete")
def delete(account_id: int, b: Delete, ctx=Depends(www.require_owner_2fa)):
    from .admin_ops import _acct, _typed, _reason, _who
    account_id = _id(account_id); a = _acct(account_id); _typed(a, b.confirm); why = _reason(b.reason)
    return delete_account(account_id, actor=_who(ctx), reason=why, allow_payments=b.allow_payments, ignore_cleanup_errors=b.ignore_cleanup_errors)
