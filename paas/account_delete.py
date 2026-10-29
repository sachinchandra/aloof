"""Owner-only account deletion. Destroys every app through engine.destroy_app (containers, networks, domains, template
Postgres and volumes, with_volumes=True), revokes API keys, sessions, deploy tokens, OAuth/connected-app tokens and linked
sign-in identities, cancels a Stripe subscription, then deletes the account row and every dependent row. Money records
(ledger, invoices, payments, stripe_sessions, admin_refunds) and the audit log are kept; invoices lose the billing name and GSTIN.
Accounts 1 and 2 and the owner account can never be deleted; accounts with payments need force=True."""
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
def delete_account(account_id, actor=None, reason="", force=False, require_empty=False, stripe_client=None):
    a = q("select * from accounts where id=?", (account_id,), one=True)
    if not a: raise HTTPException(404, "no such account")
    if account_id in PROTECTED_IDS or a.get("is_admin") or (a.get("email") or "").lower() == (www.ADMIN_EMAIL or "").lower():
        raise HTTPException(400, "this account is protected and can't be deleted")
    b = blockers(account_id)
    if require_empty and b: raise HTTPException(409, f"account still has live resources: {b}")
    if b.get("payments") and not force: raise HTTPException(409, "this account has payments; deleting it needs force")
    out = {"account_id": account_id, "email": a["email"], "apps": [], "errors": [], "revoked": {}, "deleted_rows": {}}
    from . import engine, oauth_apps
    apps = [r["name"] for r in q("select name, status from apps where account_id=?", (account_id,))]
    for name in apps:
        st = (q("select status from apps where name=?", (name,), one=True) or {}).get("status")
        try:
            if st != "destroyed": engine.destroy_app(name, with_volumes=True)
            else: engine._sweep_containers(name)
            out["apps"].append(name)
        except Exception as e: out["errors"].append(f"app {name}: {e}")
    from . import parity
    for p in q("select name from pg_dbs where account_id=?", (account_id,)):
        try: parity.pg_destroy(p["name"])
        except Exception as e: out["errors"].append(f"postgres {p['name']}: {e}")
    for v in q("select name from volumes where account_id=?", (account_id,)):
        try: q("update volumes set app=null, mount_path=null where name=?", (v["name"],)); engine.delete_volume(v["name"])
        except Exception as e: out["errors"].append(f"volume {v['name']}: {e}")
    try:   # OAuth/MCP connected apps: tokens carry the account's key hashes, so revoke before the keys go
        for c in oauth_apps.list_apps(account_id):
            cid = c.get("client_id")
            if cid: out["revoked"][f"oauth:{cid}"] = oauth_apps.revoke(account_id, cid)
    except Exception as e: out["errors"].append(f"oauth: {e}")
    if a.get("stripe_subscription"):
        try:
            from . import stripe_billing as sb
            sb._call("DELETE", f"/subscriptions/{a['stripe_subscription']}", client=stripe_client); out["stripe_subscription_cancelled"] = a["stripe_subscription"]
        except Exception as e: out["errors"].append(f"stripe subscription {a['stripe_subscription']}: {e}")
    if out["errors"] and not force:
        audit(actor, None, "admin.delete_account_failed", f"account={account_id} email={a['email']} errors={out['errors']}"[:2000])
        raise HTTPException(502, f"cleanup failed, account kept: {out['errors'][:3]}")
    hs = sorted(oauth_apps._key_hashes(account_id))   # any OAuth codes/tokens bound to this account's keys
    for name in apps:
        for t in APP_TABLES:
            if _has_table(t): q(f"delete from {t} where app=?", (name,))
    for t in ACCOUNT_TABLES:
        if not _has_table(t): continue
        c = int((q(f"select count(*) n from {t} where account_id=?", (account_id,), one=True) or {}).get("n") or 0)
        if c: q(f"delete from {t} where account_id=?", (account_id,)); out["deleted_rows"][t] = c
    q("delete from sessions where impersonated_by=?", (account_id,))
    for h in hs: q("delete from kv where acct=?", (h,))
    q("delete from kv where kind='is_test_pin' and k=?", (str(account_id),))
    if a.get("email"): q("delete from magic_links where lower(email)=?", (a["email"].lower(),))
    q("update invoices set billing_name=null, gstin=null where account_id=?", (account_id,))
    q("delete from apps where account_id=?", (account_id,)); out["deleted_rows"]["apps"] = len(apps)
    q("delete from accounts where id=?", (account_id,))
    audit(actor, None, "admin.delete_account", f"account={account_id} email={a['email']} reason={reason} apps={len(apps)} rows={out['deleted_rows']} force={force}"[:2000])
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
class Delete(BaseModel): confirm: str; reason: str = ""; force: bool = False
@R.get("/v1/admin/accounts/{account_id}/delete-check")
def delete_check(account_id: int, _=Depends(www.require_admin)):
    a = q("select id from accounts where id=?", (account_id,), one=True)
    if not a: raise HTTPException(404, "no such account")
    return {"protected": account_id in PROTECTED_IDS, "blockers": blockers(account_id)}
@R.post("/v1/admin/accounts/{account_id}/delete")
def delete(account_id: int, b: Delete, ctx=Depends(www.require_owner_2fa)):
    from .admin_ops import _acct, _typed, _reason, _who
    a = _acct(account_id); _typed(a, b.confirm); why = _reason(b.reason)
    return delete_account(account_id, actor=_who(ctx), reason=why, force=b.force)
