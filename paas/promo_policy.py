"""Finance prep (Nov 3, draft for sachin's review). Three policies, each behind an env flag and OFF by default:

PAAS_PROMO_ONE_PER_PERSON = off | flag | block
    One promo per person. Every discounted subscription checkout is recorded (account, promo, normalized email, card fingerprint).
    A later redemption by the same account, normalized email or card is a repeat.
    flag:  record + audit 'billing.promo_repeat' for admin review (no customer impact).
    block: also stop offering promotion codes at checkout to an account already known to have redeemed one (before payment, so no
           refunds). Repeats that only show up after payment (new account, same card) are flagged, never auto-cancelled.
PAAS_PAST_DUE_ACTION = none | limit | free
    invoice.payment_failed / subscription status past_due:
    limit: pause running apps (like the Free cap) but keep the plan; free: drop to Free but keep the subscription id so Stripe's
    successful retry (invoice.paid / status active) restores the plan automatically. Both restore on payment.
PAAS_STARTER_CREDIT_PROMO = keep | clawback
    clawback: when a promo makes the first Hobby month free, remove whatever is left of the $5 starting credit (only if the account
    never topped up, so no paid money is touched). Ledger kind 'starter_clawback'.
"""
import os, time, logging, re
from .db import q
log = logging.getLogger("paas.promo")
E_ = lambda k, d: (os.environ.get(k) or d).strip().lower()
def one_per_person(): return E_("PAAS_PROMO_ONE_PER_PERSON", "off")
def past_due_action(): return E_("PAAS_PAST_DUE_ACTION", "none")
def starter_mode(): return E_("PAAS_STARTER_CREDIT_PROMO", "keep")
STARTER_CENTS = 500
DOT_INSENSITIVE = {"gmail.com", "googlemail.com"}   # only providers that really ignore dots; +tags are stripped everywhere
def normalize_email(e):
    e = (e or "").strip().lower(); local, _, dom = e.rpartition("@")
    if not local: return e
    local = local.split("+", 1)[0]
    if dom in DOT_INSENSITIVE: local = local.replace(".", ""); dom = "gmail.com"
    return f"{local}@{dom}"
def _email(acct):
    r = q("select email from accounts where id=?", (acct,), one=True); return (r or {}).get("email") or ""
def prior_redemptions(acct, email_norm=None, fingerprint=None):
    """Earlier redemptions by this account, the same normalized email, or the same card (other accounts included)."""
    email_norm = email_norm or normalize_email(_email(acct))
    rows = q("select * from promo_redemptions where account_id=? or email_norm=?" + (" or card_fingerprint=?" if fingerprint else ""),
             (acct, email_norm, *([fingerprint] if fingerprint else [])))
    return [dict(r) for r in rows]
def offer_promo_codes(acct, tier):
    """Checkout: promotion codes only on Hobby, and (block mode) never to someone already known to have redeemed one."""
    if tier != "hobby": return False
    if one_per_person() == "block" and prior_redemptions(acct): return False
    return True
def discount_of(session):
    """(promo_or_coupon_id, amount_discount_cents) from a Checkout Session object; (None, 0) when no discount applied."""
    amt = int(((session.get("total_details") or {}).get("amount_discount")) or 0)
    ds = session.get("discounts") or []
    pid = None
    if ds: pid = ds[0].get("promotion_code") or ds[0].get("coupon")
    return (pid if isinstance(pid, str) else (pid or {}).get("id")), amt
def record_redemption(acct, session, fingerprint=None):
    """Called on a paid, discounted subscription checkout. Returns 'ok', 'repeat' or None (no discount / policy off)."""
    promo, amt = discount_of(session)
    if not promo and not amt: return None
    em = normalize_email(_email(acct)); prior = [r for r in prior_redemptions(acct, em, fingerprint) if r.get("session_id") != session.get("id")]
    verdict = "repeat" if prior else "ok"
    if one_per_person() == "off": verdict = "ok" if not prior else "repeat_unflagged"
    q("insert into promo_redemptions(account_id,promo,email_norm,card_fingerprint,session_id,subscription,verdict,created) values(?,?,?,?,?,?,?,?)",
      (acct, promo, em, fingerprint, session.get("id"), session.get("subscription"), verdict, time.time()))
    if verdict == "repeat":
        from .db import audit
        why = sorted({"account" if r["account_id"] == acct else ("card" if fingerprint and r.get("card_fingerprint") == fingerprint else "email") for r in prior})
        audit(acct, None, "billing.promo_repeat", f"account={acct} promo={promo} matches={','.join(why)} prior_accounts={sorted({r['account_id'] for r in prior})}")
        log.warning("promo repeat: account %s promo %s matches %s", acct, promo, why)
    return verdict
def clawback_starter(acct, session):
    """PAAS_STARTER_CREDIT_PROMO=clawback: remove what's left of the $5 starting credit when a promo made the first month free."""
    if starter_mode() != "clawback": return 0
    promo, amt = discount_of(session)
    if not promo and not amt: return 0
    if q("select 1 from ledger where account_id=? and kind in ('topup','starter_clawback')", (acct,), one=True): return 0
    a = q("select credit_cents from accounts where id=?", (acct,), one=True)
    take = round(min(STARTER_CENTS, max(0.0, float((a or {}).get("credit_cents") or 0))), 4)
    if take <= 0: return 0
    from . import billing
    billing.credit_rows(acct, -take, "starter_clawback", f"starting credit replaced by promo {promo or ''}".strip())
    return take
def mark_past_due(acct, why):
    """invoice.payment_failed / status past_due. Returns the action taken."""
    act = past_due_action()
    q("update accounts set past_due_since=coalesce(past_due_since, ?) where id=?", (time.time(), acct))
    from .db import audit; audit(acct, None, "billing.past_due", f"account={acct} action={act} ({why})")
    if act == "limit":
        from . import billing; billing.cap_account(acct)
    elif act == "free":
        r = q("select stripe_subscription from accounts where id=?", (acct,), one=True)
        from .stripe_billing import set_tier
        set_tier(acct, "free", (r or {}).get("stripe_subscription"), why=f"past due ({why})")   # sub id kept: payment restores the plan
    return act
def clear_past_due(acct, tier=None):
    """Payment succeeded again. Returns True if the account was past due."""
    r = q("select past_due_since, stripe_subscription, tier from accounts where id=?", (acct,), one=True)
    if not r or not r.get("past_due_since"): return False
    q("update accounts set past_due_since=null where id=?", (acct,))
    from . import billing
    if tier and tier != r["tier"]:
        from .stripe_billing import set_tier; set_tier(acct, tier, r.get("stripe_subscription"), why="payment recovered")
    else: billing.uncap_account(acct, "payment received")
    from .db import audit; audit(acct, None, "billing.past_due_cleared", f"account={acct}")
    return True
