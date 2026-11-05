"""Plain welcome email at signup (every method: password, magic link, GitHub, Google, X). OFF unless PAAS_WELCOME_EMAIL=1.
Signup (in either app process) only queues a row in lifecycle_mail (kind='welcome', status='queued'); the send happens in tick(),
which runs from controller.start(), i.e. only in the paas process (PAAS_BACKGROUND=1), never in paas-b.
Template: paas/mail_templates/welcome.txt (Marketing edits it). Skipped: test/undeliverable/opted-out/suspended accounts, and
when LIFECYCLE_EMAILS_ENABLED=1 (its welcome_onboarding is the one welcome then)."""
import os, time, logging
from .db import q
log = logging.getLogger("paas.welcome")
KIND = "welcome"
TEMPLATE = os.path.join(os.path.dirname(__file__), "mail_templates", "welcome.txt")
def enabled(): return os.environ.get("PAAS_WELCOME_EMAIL", "0").strip() == "1"
def queue(account_id):
    """Called at account creation. Flag off: nothing happens (no row, so turning the flag on later doesn't mail old sign-ups)."""
    if not enabled(): return False
    try: return bool(q("insert into lifecycle_mail(account_id,kind,sent_at,status) values(?,?,?,'queued') on conflict do nothing returning account_id",
                       (account_id, KIND, time.time())))
    except Exception as e: log.warning("welcome queue failed account=%s: %s", account_id, type(e).__name__); return False
def render(account_id, path=TEMPLATE):
    from . import site, lifecycle
    raw = [l for l in open(path, encoding="utf-8").read().splitlines() if not l.startswith("#")]
    if not raw or not raw[0].startswith("Subject:"): raise ValueError("welcome template must start with 'Subject:'")
    base = site.SITE.rstrip("/")
    vals = {"dashboard_url": lifecycle.utm(f"{base}/dashboard", "welcome"), "docs_url": lifecycle.utm(f"{base}/docs", "welcome"),
            "unsubscribe_url": lifecycle.unsub_url(account_id)}
    body = "\n".join(raw[1:]).strip("\n") + "\n"
    for k, v in vals.items(): body = body.replace("{" + k + "}", v)
    return raw[0][len("Subject:"):].strip(), body
def _eligible(a):
    from . import engine
    return a and a.get("email") and not a.get("is_test") and not a.get("email_optout") and not a.get("suspended") and not engine.undeliverable_email(a["email"])
def tick(now=None):
    """Background (paas process only): send queued welcomes. Claim = queued->pending update by one process; never raises."""
    if not enabled(): return 0
    from . import lifecycle
    n = 0
    for r in q("select account_id from lifecycle_mail where kind=? and status='queued' order by sent_at limit 50", (KIND,)):
        aid = r["account_id"]
        if not q("update lifecycle_mail set status='pending' where account_id=? and kind=? and status='queued' returning account_id", (aid, KIND)): continue
        try:
            a = q("select * from accounts where id=?", (aid,), one=True)
            if lifecycle.enabled() or not _eligible(a):
                q("update lifecycle_mail set status='skipped' where account_id=? and kind=?", (aid, KIND)); continue
            subj, body = render(aid)
            from .auth import send_mail
            un = lifecycle.unsub_url(aid)
            send_mail(a["email"], subj, body, headers={"List-Unsubscribe": f"<{un}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
            q("update lifecycle_mail set status='sent', sent_at=? where account_id=? and kind=?", (time.time(), aid, KIND)); n += 1
            log.info("welcome email sent account=%s", aid)
        except Exception as e:
            log.warning("welcome email failed account=%s: %s", aid, type(e).__name__)
            try: q("update lifecycle_mail set status='failed' where account_id=? and kind=?", (aid, KIND))
            except Exception: pass
    return n
