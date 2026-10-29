"""Lifecycle (onboarding) emails. OFF unless LIFECYCLE_EMAILS_ENABLED=1. Copy: Sales draft onboarding_v1, PENDING SACHIN'S APPROVAL.
Kinds: welcome_onboarding (at signup, every method; replaces the password-only welcome when enabled; password signups get the API key as a
separate block), no_deploy_24h (24-72h after signup, no app yet), day4_tip (day 4-6). tick() runs every 10 min from controller.
Idempotency: a (account_id, kind) row is inserted before sending (primary key; ON CONFLICT DO NOTHING); only the inserter sends.
Never: test accounts (is_test), opted-out accounts (email_optout), suspended accounts, >1 lifecycle email per account per 24h."""
import os, time, hmac, hashlib, logging, urllib.parse as _u
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from .db import q
from . import emailkit
log = logging.getLogger("paas.lifecycle")
R = APIRouter()
KINDS = ("welcome_onboarding", "no_deploy_24h", "day4_tip")
DAY = 86400.0
def enabled(): return os.environ.get("LIFECYCLE_EMAILS_ENABLED", "0").strip() == "1"
def _base():
    from . import site; return site.SITE.rstrip("/")
def _secret():
    from . import crypto
    k = os.environ.get("PAAS_SECRET_KEY") or (open(crypto._KF).read().strip() if os.path.exists(crypto._KF) else "")
    return hashlib.sha256(b"aloof-lifecycle-unsub:" + k.encode()).digest()
def token(account_id): return hmac.new(_secret(), f"acct:{account_id}".encode(), hashlib.sha256).hexdigest()[:32]
def unsub_url(account_id): return f"{_base()}/email/unsubscribe?a={account_id}&t={token(account_id)}"
def utm(url, content):
    sep = "&" if "?" in url else "?"
    return url + sep + _u.urlencode({"utm_source": "email", "utm_medium": "onboarding", "utm_campaign": "onboarding_v1", "utm_content": content})
def first_name(a):
    n = (a.get("billing_name") or "").strip().split()
    return n[0][:40] if n else "there"

# ---------- copy (Sales draft onboarding_v1, pending Sachin's approval) ----------
def render(kind, a, api_key=None):
    """-> (subject, text, html). Everything user-supplied is escaped by emailkit."""
    P, S, B, C = emailkit.para, emailkit.small, emailkit.button, emailkit.code
    hi = f"Hi {first_name(a)},"; un = unsub_url(a["id"]); e = emailkit.e
    reason = "You're getting this because you signed up for Aloof."
    if kind == "welcome_onboarding":
        d = utm(f"{_base()}/dashboard", "e1"); subj = "Welcome to Aloof"
        bullets = ["Free is $0, includes $1/mo of usage, and pauses your apps instead of billing you.",
                   "Hobby is $5/mo and includes $5 of usage. Everything bills per second.",
                   "First-time customers get their first month of Hobby free with code FIRST1 (then $5/mo, cancel anytime).",
                   "Apps run in Nuremberg, Germany."]
        key_t = ["", "Your API key (shown once, copy it somewhere safe):", api_key, ""] if api_key else []
        key_h = [S("Your API key. We only show it once, so copy it somewhere safe."), C(api_key)] if api_key else []
        t = [hi, "", "Thanks for signing up. I'm Sachin, and I built Aloof to make hosting backends simple and cheap.", "", "A few things worth knowing:"] + \
            [f"- {b}" for b in bullets] + ["", f"Your dashboard: {d}"] + key_t + ["", "If you get stuck, just reply to this email. It comes straight to me.", "", "Sachin", "Founder, Aloof"]
        h = [P(e(hi)), P("Thanks for signing up. I'm Sachin, and I built Aloof to make hosting backends simple and cheap."), P("A few things worth knowing:"),
             "<ul style='margin:0 0 16px;padding-left:20px;font-size:16px;line-height:1.6'>" + "".join(f"<li>{e(b)}</li>" for b in bullets) + "</ul>",
             B("Open your dashboard", d)] + key_h + [P("If you get stuck, just reply to this email. It comes straight to me."), P("Sachin<br>Founder, Aloof")]
    elif kind == "no_deploy_24h":
        d = utm(f"{_base()}/dashboard", "e2"); subj = "Your first deploy on Aloof"
        steps = [f"Open your dashboard: {d}", "Create an app and point it at your repo or Docker image.",
                 "Deploy. Traffic only switches over once health checks pass, and rolling back is one command."]
        t = [hi, "", "I noticed you haven't deployed anything yet, so here's the shortest path:", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)] + \
            ["", "A small API on 256 MB costs $1.99/mo, billed per second.", "", "If something is blocking you, reply and tell me what you're trying to run. I'll help you get it live.", "", "Sachin"]
        h = [P(e(hi)), P("I noticed you haven't deployed anything yet, so here's the shortest path:"),
             "<ol style='margin:0 0 16px;padding-left:20px;font-size:16px;line-height:1.6'><li>Open your dashboard.</li><li>Create an app and point it at your repo or Docker image.</li><li>Deploy. Traffic only switches over once health checks pass, and rolling back is one command.</li></ol>",
             B("Open your dashboard", d), P("A small API on 256 MB costs $1.99/mo, billed per second."),
             P("If something is blocking you, reply and tell me what you're trying to run. I'll help you get it live."), P("Sachin")]
    elif kind == "day4_tip":
        d = utm(f"{_base()}/dashboard", "e3"); m = utm(f"{_base()}/docs/mcp", "e3"); subj = "Two things most people miss on Aloof"
        t = [hi, "", "Two features worth trying:", "", "Deploy from your AI editor. Aloof has an MCP server, so you can deploy and manage apps straight from Claude or Cursor using a scoped token you can revoke any time.", f"How: {m}", "",
             "Managed Postgres from $2.99/mo. Add a database next to your app instead of wiring up another provider.", f"Your dashboard: {d}", "",
             "Reminder: FIRST1 gets first-time customers their first month of Hobby free (then $5/mo, cancel anytime).", "", "Questions? Just reply.", "", "Sachin"]
        h = [P(e(hi)), P("Two features worth trying:"),
             P(f"<b>Deploy from your AI editor.</b> Aloof has an MCP server, so you can deploy and manage apps straight from Claude or Cursor using a scoped token you can revoke any time. <a href='{e(m, True)}' class=lnk>How it works</a>"),
             P("<b>Managed Postgres from $2.99/mo.</b> Add a database next to your app instead of wiring up another provider."), B("Open your dashboard", d),
             P("Reminder: FIRST1 gets first-time customers their first month of Hobby free (then $5/mo, cancel anytime)."), P("Questions? Just reply."), P("Sachin")]
    else: raise ValueError(kind)
    return subj, "\n".join(t) + f"\n\n--\n{reason} Unsubscribe: {un}\n{emailkit.LEGAL}\n", emailkit.render(subj, h, reason, subj, unsub=un)

# ---------- sending ----------
def _eligible(a):
    return a and a.get("email") and not a.get("is_test") and not a.get("email_optout") and not a.get("suspended")
def _claim(account_id, kind, now):
    """Insert-before-send. False if this kind was already claimed, or any lifecycle mail went to the account in the last 24h."""
    if q("select 1 from lifecycle_mail where account_id=? and sent_at>? and status in ('pending','sent')", (account_id, now - DAY), one=True): return False
    return bool(q("insert into lifecycle_mail(account_id,kind,sent_at,status) values(?,?,?,'pending') on conflict do nothing returning account_id", (account_id, kind, now)))
def send(account_id, kind, api_key=None, now=None):
    """Send one lifecycle email if enabled, eligible and not sent before. Never raises. Returns 'sent'|'skipped'|'failed'."""
    if not enabled() or kind not in KINDS: return "skipped"
    now = now or time.time()
    try:
        a = q("select * from accounts where id=?", (account_id,), one=True)
        if not _eligible(a) or not _claim(account_id, kind, now): return "skipped"
        subj, txt, htm = render(kind, a, api_key)
        from .auth import send_mail
        un = unsub_url(account_id)
        send_mail(a["email"], subj, txt, html=htm, headers={"List-Unsubscribe": f"<{un}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        q("update lifecycle_mail set status='sent', sent_at=? where account_id=? and kind=?", (time.time(), account_id, kind))
        log.info("lifecycle email sent account=%s kind=%s", account_id, kind)
        try:
            from . import analytics; analytics.capture(account_id, "lifecycle_email_sent", {"kind": kind, "campaign": "onboarding_v1"})
        except Exception: pass
        return "sent"
    except Exception as e:
        log.warning("lifecycle email failed account=%s kind=%s: %s", account_id, kind, type(e).__name__)
        try: q("update lifecycle_mail set status='failed' where account_id=? and kind=?", (account_id, kind))
        except Exception: pass
        return "failed"
def welcome(account_id, api_key=None):
    """Signup hook for every method. Flag on: onboarding welcome (Email 1, API key block for password signups). Flag off: the old
    password-only key email (only when api_key is given). Exactly one welcome either way."""
    if enabled(): return send(account_id, "welcome_onboarding", api_key)
    return "legacy"
# (kind, min age, max age, extra SQL condition)
RULES = [("welcome_onboarding", 0, 2 * DAY, ""), ("no_deploy_24h", DAY, 3 * DAY, " and not exists (select 1 from apps p where p.account_id=a.id)"),
         ("day4_tip", 4 * DAY, 6 * DAY, "")]
def tick(now=None):
    if not enabled(): return 0
    now = now or time.time(); n = 0
    for kind, lo, hi, extra in RULES:
        rows = q("select a.id from accounts a where a.created between ? and ? and coalesce(a.is_test,0)=0 and coalesce(a.email_optout,0)=0"
                 " and coalesce(a.suspended,0)=0 and not exists (select 1 from lifecycle_mail m where m.account_id=a.id and m.kind=?)" + extra + " order by a.id limit 200",
                 (now - hi, now - lo, kind))
        for r in rows: n += send(r["id"], kind, now=now) == "sent"
    return n

# ---------- unsubscribe ----------
def set_optout(account_id, on=True):
    q("update accounts set email_optout=?, email_optout_at=? where id=?", (int(on), time.time() if on else None, account_id))
def _ok(a, t):
    try: a = int(a)
    except (TypeError, ValueError): return None
    return a if hmac.compare_digest(token(a), t or "") and q("select 1 from accounts where id=?", (a,), one=True) else None
def _page(h1, inner, status=200):   # token page: never cached, no analytics
    from . import site
    return HTMLResponse(site.page(f"{h1} · Aloof", f"<section class=s><div class=wrap style='max-width:560px;padding:64px 0'><h1>{h1}</h1>{inner}</div></section>", analytics=False), status, headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
@R.get("/email/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
def unsub_page(request: Request, a: str = "", t: str = ""):
    from .netaddr import limit; limit("unsub", request)
    if not _ok(a, t): return _page("Link not valid", "<p class=muted>This unsubscribe link isn't valid. You can turn onboarding emails off in Dashboard → Settings.</p>", 400)
    import html
    return _page("Unsubscribe from onboarding emails", f"""<p>Stop onboarding and tips emails to this account. Sign-in links, receipts and balance alerts still arrive.</p>
<form method=post action="/email/unsubscribe"><input type=hidden name=a value="{html.escape(a)}"><input type=hidden name=t value="{html.escape(t)}"><button class="btn pri">Unsubscribe</button></form>""")
@R.post("/email/unsubscribe", include_in_schema=False)
async def unsub_do(request: Request):
    """One-click (RFC 8058): mail clients POST List-Unsubscribe=One-Click to the List-Unsubscribe URL (a, t in the query)."""
    from .netaddr import limit; limit("unsub", request)
    f = await request.form(); qp = request.query_params
    aid = _ok(f.get("a") or qp.get("a"), f.get("t") or qp.get("t"))
    if aid: set_optout(aid, True); log.info("lifecycle unsubscribe account=%s", aid)
    if f.get("List-Unsubscribe") == "One-Click": return PlainTextResponse("ok" if aid else "invalid", 200 if aid else 400, headers={"Cache-Control": "no-store"})
    return _page("You're unsubscribed", "<p>You won't get onboarding or tips emails from us. You can turn them back on in Dashboard → Settings.</p>") if aid else \
        _page("Link not valid", "<p class=muted>This unsubscribe link isn't valid.</p>", 400)
