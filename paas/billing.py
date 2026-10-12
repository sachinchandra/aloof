"""USD wallet (US cents). Payments: Stripe (paas/stripe_billing.py, primary); Razorpay INR is optional (paas/razorpay_legacy.py).
Metering: running instances per second at the plan's USD rate, volumes per provisioned GB-month, egress per GB. The tier fee is charged
monthly (from the wallet, or by a Stripe subscription), and usage up to the tier's included amount isn't deducted. Apps are suspended
when the balance runs out (or, on Free, paused at the hard cap)."""
import os, json, time, logging
from .db import q, event
from .plans import PLANS
from .crypto import load_spec
log = logging.getLogger("paas.billing")
E = lambda k, d=None: os.environ.get(k, d)
def usd(cents): return round((cents or 0) / 100.0, 2)
def credit(acct, cents, kind, ref):
    """Add to the wallet + ledger, then resume suspended apps."""
    q("update accounts set credit_cents = credit_cents + ? where id=?", (cents, acct))
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (acct, time.time(), round(cents, 4), kind, ref))
    resume_suspended(acct)
EGRESS = {}   # filled by the edge proxy: app -> bytes since last tick
def month_key(ts=None): return time.strftime("%Y-%m", time.localtime(ts or time.time()))
def _bump(acct, month, kind, qty, cost):
    q("insert into usage_month(account_id,month,kind,qty,cost_cents) values(?,?,?,?,?) on conflict(account_id,month,kind) do update set "
      "qty=usage_month.qty+excluded.qty, cost_cents=usage_month.cost_cents+excluded.cost_cents", (acct, month, kind, qty, cost))
def _month(acct, month, kind):
    r = q("select qty, cost_cents from usage_month where account_id=? and month=? and kind=?", (acct, month, kind), one=True)
    return (r["qty"], r["cost_cents"] or 0.0) if r else (0.0, 0.0)
def _debit(acct, amount, kind, ref, now):
    if amount <= 0: return
    q("update accounts set credit_cents = credit_cents - ? where id=?", (amount, acct))
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (acct, now, -round(amount, 4), kind, ref))
def meter_tick(now=None):
    """Meter compute (per second), volumes (GB-month, provisioned size) and egress (per GB beyond the tier allowance).
    All amounts in US cents. Tier fee is charged at the start of each month unless a Stripe subscription pays it."""
    from .plans import TIERS, RATES, HOURS_MONTH
    now = now or time.time(); mk = month_key(now); charges = {}   # acct -> {kind: (qty, cost)}
    add = lambda a, k, qty, cost: charges.setdefault(a, {}).__setitem__(k, tuple(x + y for x, y in zip(charges.get(a, {}).get(k, (0, 0)), (qty, cost))))
    for i in q("select i.id, i.started, a.account_id, a.spec from instances i join apps a on a.name=i.app where i.status='running'"):
        m = q("select last_billed from meter where instance_id=?", (i["id"],), one=True); last = m["last_billed"] if m else i["started"]
        hrs = max(0, now - last) / 3600
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (i["id"], now))
        add(i["account_id"], "compute", hrs, hrs * PLANS[load_spec(i["spec"])["plan"]]["price_usd_hr"] * 100)
    for v in q("select name, account_id, size_gb, created from volumes"):
        key = "vol:" + v["name"]; m = q("select last_billed from meter where instance_id=?", (key,), one=True); last = m["last_billed"] if m else v["created"]
        gbm = v["size_gb"] * max(0, now - last) / (HOURS_MONTH * 3600)
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (key, now))
        add(v["account_id"], "volume", gbm, gbm * RATES["volume_usd_gb_month"] * 100)
    drained = {k: EGRESS.pop(k) for k in list(EGRESS)}
    for app, nbytes in drained.items():
        a = q("select account_id from apps where name=?", (app,), one=True)
        if not a: continue
        acct = a["account_id"]; tier = TIERS.get((q("select tier from accounts where id=?", (acct,), one=True) or {}).get("tier") or "free", TIERS["free"])
        gb = nbytes / 1e9; used_gb, _ = _month(acct, mk, "egress"); free_left = max(0, tier["egress_gb"] - used_gb)
        add(acct, "egress", gb, max(0, gb - free_left) * RATES["egress_usd_gb"] * 100)
    # monthly tier fee (once per account per month)
    for a in q("select id, tier, month_key, stripe_subscription from accounts where month_key is null or month_key != ?", (mk,)):
        tier = TIERS.get(a["tier"] or "free", TIERS["free"])
        q("update accounts set month_key=? where id=?", (mk, a["id"])); uncap_account(a["id"], "new month")
        if tier["fee_usd"] and not a.get("stripe_subscription"): _debit(a["id"], tier["fee_usd"] * 100, "plan", f"{a['tier']} plan {mk}", now)
    for acct, kinds in charges.items():
        tier = TIERS.get((q("select tier from accounts where id=?", (acct,), one=True) or {}).get("tier") or "free", TIERS["free"])
        before = sum(_month(acct, mk, k)[1] for k in ("compute", "volume", "egress"))
        for k, (qty, cost) in kinds.items(): _bump(acct, mk, k, qty, cost)
        total = sum(c for _, c in kinds.values()); after = before + total
        billable = max(0, after - tier["included_usage_usd"] * 100) - max(0, before - tier["included_usage_usd"] * 100)
        if tier.get("hard_cap"):     # free tier never draws on the wallet; at the cap its apps sleep until next month/upgrade
            if after >= tier["included_usage_usd"] * 100: cap_account(acct)
            continue
        if billable > 0:
            _debit(acct, billable, "usage", "metered " + ", ".join(f"{k} {c:.3f}c" for k, (_, c) in kinds.items() if c), now)
    if E("PAAS_SUSPEND_ON_ZERO", "1") == "1":
        from . import engine
        for a in q("select id from accounts where credit_cents <= 0"):
            for app in q("select name from apps where account_id=? and status in ('running','idle')", (a["id"],)):
                engine.scale(app["name"], 0, reason="suspended: balance exhausted")
                q("update apps set status='suspended' where name=?", (app["name"],)); event(app["name"], "suspended: prepaid balance exhausted; top up to resume")
def cap_account(acct):
    from . import engine
    for app in q("select name from apps where account_id=? and status in ('running','idle')", (acct,)):
        engine.scale(app["name"], 0, reason="free tier cap reached")
        q("update apps set status='capped' where name=?", (app["name"],))
        event(app["name"], "paused: free tier included usage used up for this month; upgrade or wait for next month")
def uncap_account(acct, why):
    for app in q("select name from apps where account_id=? and status='capped'", (acct,)):
        q("update apps set status='idle', updated=? where name=?", (time.time(), app["name"])); event(app["name"], f"unpaused ({why}); wakes on next request")
def month_summary(account_id, mk=None):
    from .plans import TIERS
    mk = mk or month_key(); a = q("select tier from accounts where id=?", (account_id,), one=True); t = TIERS.get((a or {}).get("tier") or "free", TIERS["free"])
    rows = {r["kind"]: {"qty": round(r["qty"], 4), "cost_cents": round(r["cost_cents"] or 0, 3), "cost_usd": usd(r["cost_cents"])}
            for r in q("select kind, qty, cost_cents from usage_month where account_id=? and month=?", (account_id, mk))}
    used = sum(r["cost_cents"] for r in rows.values()); inc = t["included_usage_usd"] * 100
    return {"month": mk, "currency": "USD", "tier": (a or {}).get("tier") or "free", "fee_usd": t["fee_usd"], "included_usage_usd": t["included_usage_usd"],
            "usage": rows, "usage_total_cents": round(used, 3), "usage_total_usd": usd(used), "billable_usd": usd(max(0, used - inc))}
def resume_suspended(account_id):
    from . import engine
    for app in q("select name from apps where account_id=? and status='suspended'", (account_id,)):
        q("update apps set status='running' where name=?", (app["name"],)); event(app["name"], "resumed after top-up")
        sp = load_spec(q("select spec from apps where name=?", (app["name"],), one=True)["spec"])
        engine.scale(app["name"], max(1, (sp.get("autoscale") or {}).get("min", 1)), reason="resume")
