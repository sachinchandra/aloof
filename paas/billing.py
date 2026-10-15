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
    if cents > 0: q("update accounts set lowbal_sent='' where id=?", (acct,))   # a top-up re-arms the low-balance alerts
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
    from .plans import TIERS, RATES, HOURS_MONTH, PG_PLANS
    now = now or time.time(); mk = month_key(now); charges = {}   # acct -> {kind: (qty, cost)}
    add = lambda a, k, qty, cost: charges.setdefault(a, {}).__setitem__(k, tuple(x + y for x, y in zip(charges.get(a, {}).get(k, (0, 0)), (qty, cost))))
    for i in q("select i.id, i.started, a.account_id, a.spec from instances i join apps a on a.name=i.app where i.status='running'"):
        m = q("select last_billed from meter where instance_id=?", (i["id"],), one=True); last = m["last_billed"] if m else i["started"]
        hrs = max(0, now - last) / 3600
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (i["id"], now))
        sp = load_spec(i["spec"]); rate = PLANS[sp["plan"]]["price_usd_hr"]
        if sp.get("kind") == "postgres" and sp.get("pg_plan") in PG_PLANS:   # pg plan price minus its disk (metered as a volume)
            pp = PG_PLANS[sp["pg_plan"]]; rate = (pp["price_usd_month"] - pp["disk_gb"] * RATES["volume_usd_gb_month"]) / HOURS_MONTH
        add(i["account_id"], "postgres" if sp.get("kind") == "postgres" else "compute", hrs, hrs * rate * 100)
    for v in q("select name, account_id, size_gb, created from volumes"):
        key = "vol:" + v["name"]; m = q("select last_billed from meter where instance_id=?", (key,), one=True); last = m["last_billed"] if m else v["created"]
        gbm = v["size_gb"] * max(0, now - last) / (HOURS_MONTH * 3600)
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (key, now))
        add(v["account_id"], "volume", gbm, gbm * RATES["volume_usd_gb_month"] * 100)
    for r in q("select id, account_id, size_bytes, created from archives"):   # Postgres backups + volume snapshots, per GB-month
        key = "arc:" + r["id"]; m = q("select last_billed from meter where instance_id=?", (key,), one=True); last = m["last_billed"] if m else r["created"]
        gbm = (r["size_bytes"] or 0) / 1e9 * max(0, now - last) / (HOURS_MONTH * 3600)
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (key, now))
        add(r["account_id"], "backup", gbm, gbm * RATES["backup_usd_gb_month"] * 100)
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
        before = sum(_month(acct, mk, k)[1] for k in ("compute", "volume", "egress", "postgres", "backup"))
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
    try: check_low_balance(now)
    except Exception as e: log.warning("low-balance check: %s", e)
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

# ---- low-balance runway + alerts ----
BURN_WINDOW = 6 * 3600
LOWBAL_LEVELS = (("72h", 72), ("24h", 24))
def burn_cents_hr(acct, now=None):
    """Average wallet burn (metered usage debits) over the last 6 hours, in cents/hour. Plan fees and top-ups don't count."""
    now = now or time.time(); since = now - BURN_WINDOW
    created = (q("select created from accounts where id=?", (acct,), one=True) or {}).get("created") or since
    r = q("select coalesce(sum(-amount_cents),0) as s from ledger where account_id=? and kind='usage' and ts>?", (acct, since), one=True)
    hrs = max(1.0, (now - max(since, created)) / 3600.0)
    return max(0.0, float(r["s"] or 0)) / hrs
def runway(acct, now=None):
    """Estimated hours of prepaid credit left at the current burn rate. level: ok | warn (<~3 days) | critical (<24h) | suspended | free."""
    from .plans import TIERS
    a = q("select credit_cents, tier, lowbal_alerts, lowbal_threshold_usd from accounts where id=?", (acct,), one=True) or {}
    tier = TIERS.get(a.get("tier") or "free", TIERS["free"]); bal = float(a.get("credit_cents") or 0)
    susp = (q("select count(*) as n from apps where account_id=? and status='suspended'", (acct,), one=True) or {}).get("n", 0)
    burn = burn_cents_hr(acct, now); hours = (bal / burn) if burn > 0 and bal > 0 else (0.0 if bal <= 0 and burn > 0 else None)
    if tier.get("hard_cap"): level = "free"
    elif susp or (bal <= 0 and burn > 0): level = "suspended"
    elif hours is not None and hours < 24: level = "critical"
    elif hours is not None and hours < 72: level = "warn"
    else: level = "ok"
    th = a.get("lowbal_threshold_usd")
    return {"credit_usd": usd(bal), "burn_usd_hr": round(burn / 100, 4), "burn_usd_day": round(burn * 24 / 100, 2),
            "hours_left": None if hours is None else round(hours, 1), "level": level, "suspended_apps": susp,
            "alerts": bool(a.get("lowbal_alerts", 1) if a.get("lowbal_alerts") is not None else 1), "threshold_usd": th,
            "below_threshold": bool(th and bal / 100 <= th and level != "free")}
def _fmt_hours(h):
    return f"about {round(h)} hours" if h < 48 else f"about {round(h / 24)} days"
def _lowbal_mail(email, kind, rw):
    """-> (subject, text, html)"""
    from . import emailkit
    base = (E("PAAS_PUBLIC_API_URL") or "http://localhost:8000").rstrip("/")
    when = _fmt_hours(rw["hours_left"]) if rw.get("hours_left") is not None else "less than you might expect"
    return emailkit.low_balance(kind, f"${rw['credit_usd']:.2f}", f"${rw['burn_usd_day']:.2f}/day", when, f"{base}/dashboard#billing", rw.get("threshold_usd"))
def check_low_balance(now=None):
    """Called from meter_tick. Emails each threshold once (72h, 24h, custom $, suspended); credit() re-arms after a top-up."""
    from .auth import send_mail
    sent_n = 0
    for a in q("select id, email, lowbal_sent, lowbal_alerts, is_test from accounts where (tier is not null and tier != ?) or credit_cents <= 0", ("free",)):
        if a.get("is_test") and not E("PAAS_LOWBAL_TEST_ACCOUNTS"): continue   # reserved test domains never get mail
        try:
            rw = runway(a["id"], now)
            if rw["level"] == "free": continue
            due = []
            if rw["level"] == "suspended": due.append("suspended")
            else:
                h = rw["hours_left"]
                for k, lim in LOWBAL_LEVELS:
                    if h is not None and h < lim: due.append(k)
                if rw["below_threshold"]: due.append("custom")
            sent = set(filter(None, (a.get("lowbal_sent") or "").split(",")))
            new = [k for k in due if k not in sent]
            if not new: continue
            pick = "suspended" if "suspended" in new else ("24h" if "24h" in new else new[0])
            if a.get("lowbal_alerts", 1) not in (0, False) and a.get("email"):
                subj, body, htm = _lowbal_mail(a["email"], pick, rw)
                try: send_mail(a["email"], subj, body, html=htm); sent_n += 1
                except Exception as e: log.warning("low-balance mail failed account=%s: %s", a["id"], type(e).__name__); continue
            q("update accounts set lowbal_sent=? where id=?", (",".join(sorted(sent | set(new))), a["id"]))
        except Exception as e: log.warning("low-balance check failed account=%s: %s", a.get("id"), e)
    return sent_n
