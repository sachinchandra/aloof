"""Accounts: email+password (scrypt), magic links, HttpOnly session cookies; API keys (hashed) for CLI/MCP.
Mailer: SMTP when SMTP_HOST is set (STARTTLS), otherwise appends to data/outbox.log (local dev)."""
import os, time, secrets, smtplib, ssl, re, logging
from email.message import EmailMessage
from .db import q
from .crypto import sha256, hash_password, check_password
from . import engine
E = lambda k, d=None: os.environ.get(k, d)
SESSION_TTL = 14 * 86400; MAGIC_TTL = 15 * 60
COOKIE = "aloof_session"
OUTBOX = os.environ.get("PAAS_OUTBOX") or os.path.join(os.path.dirname(__file__), "..", "data", "outbox.log")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
log = logging.getLogger("paas.mail")
class MailError(RuntimeError): pass
def _production(): return (E("PAAS_PUBLIC_API_URL") or "").startswith("https://") and E("PAAS_DEV_OUTBOX") != "1"
def send_mail(to, subject, body, html=None, headers=None):
    """SMTP when SMTP_HOST is set. Without it: dev appends to the outbox; production refuses (the outbox would hold live
    sign-in links and API keys) and raises MailError. SMTP failures are logged (never the body) and raise MailError."""
    msg = EmailMessage(); msg["From"] = E("MAIL_FROM", "Aloof <no-reply@aloof.si>"); msg["To"] = to; msg["Subject"] = subject; msg.set_content(body, cte="8bit")
    if html: msg.add_alternative(html, subtype="html")
    for k, v in (headers or {}).items(): msg[k] = v
    if E("SMTP_HOST"):
        try:
            with smtplib.SMTP(E("SMTP_HOST"), int(E("SMTP_PORT", "587")), timeout=20) as s:
                if E("SMTP_STARTTLS", "1") == "1": s.starttls(context=ssl.create_default_context())
                if E("SMTP_USER"): s.login(E("SMTP_USER"), E("SMTP_PASSWORD", ""))
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            log.error("smtp send failed to=%s subject=%r host=%s: %s: %s", to, subject, E("SMTP_HOST"), type(e).__name__, e)
            raise MailError("email delivery failed") from e
        log.info("mail sent to=%s subject=%r", to, subject)
        return "smtp"
    if _production():
        log.error("SMTP_HOST not set in production; dropped mail to=%s subject=%r (set SMTP_* in golive.env)", to, subject)
        raise MailError("email is not configured")
    with open(OUTBOX, "a") as f: f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{msg.as_string()}\n")
    return "outbox"
def _check_email(email):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email): raise ValueError("invalid email")
    return email
def register(email, password):
    email = _check_email(email)
    if len(password or "") < 10: raise ValueError("password must be at least 10 characters")
    if q("select 1 from accounts where email=?", (email,), one=True): raise PermissionError("account exists; log in or use a magic link")
    a = engine.create_account(email, hash_password(password))
    try:
        from . import emailkit; subj, txt, htm = emailkit.welcome(a["api_key"], f"{E('PAAS_PUBLIC_API_URL','http://localhost:8000')}/dashboard")
        send_mail(email, subj, txt, html=htm)
    except MailError: pass   # already logged; the key is also returned in the register response
    return a
def login(email, password):
    a = q("select * from accounts where email=?", ((email or "").strip().lower(),), one=True)
    if not a or not a.get("password_hash") or not check_password(password or "", a["password_hash"]):
        check_password("x", "scrypt$16384$8$1$00$00"); raise PermissionError("invalid email or password")   # equalize timing
    return a
_magic_attr = {}   # token hash -> first-touch attribution for a magic-link signup (in-memory, best effort, bounded)
def start_magic(email, base_url, attribution=None):
    """Always reports success (no account enumeration). Creates the account on first verified login."""
    email = _check_email(email); tok = "ml_" + secrets.token_urlsafe(32)
    q("insert into magic_links(token_hash,email,expires,used) values(?,?,?,0)", (sha256(tok), email, time.time() + MAGIC_TTL))
    if attribution:
        if len(_magic_attr) > 5000: _magic_attr.clear()
        _magic_attr[sha256(tok)] = attribution
    from . import emailkit; subj, txt, htm = emailkit.magic_link(f"{base_url.rstrip('/')}/auth/magic?token={tok}", MAGIC_TTL // 60)
    send_mail(email, subj, txt, html=htm)
    return True
def finish_magic(tok):
    won = q("update magic_links set used=1 where token_hash=? and used=0 and expires>? returning email", (sha256(tok or ""), time.time()))
    if not won: raise PermissionError("link invalid, expired or already used")
    email = won[0]["email"]; a = q("select * from accounts where email=?", (email,), one=True)
    created = not a; attr = _magic_attr.pop(sha256(tok or ""), None)
    if not a: a = engine.create_account(email, None, verified=True)
    else: q("update accounts set email_verified=1 where id=?", (a["id"],))
    a = q("select * from accounts where email=?", (email,), one=True)
    if created: a["_created"] = True; a["_attribution"] = attr
    return a
def new_session(account_id):
    tok = "s_" + secrets.token_urlsafe(32)
    q("insert into sessions(token_hash,account_id,expires,created) values(?,?,?,?)", (sha256(tok), account_id, time.time() + SESSION_TTL, time.time()))
    return tok
def session_account(tok):
    if not tok: return None
    r = q("select a.* from sessions s join accounts a on a.id=s.account_id where s.token_hash=? and s.expires>?", (sha256(tok), time.time()), one=True)
    return r
def end_session(tok): q("delete from sessions where token_hash=?", (sha256(tok or ""),))
def set_password(account_id, password):
    if len(password or "") < 10: raise ValueError("password must be at least 10 characters")
    q("update accounts set password_hash=? where id=?", (hash_password(password), account_id))

def safe_next(n, default="/dashboard"):
    """Post-login redirect target: only same-site paths that start with exactly one '/' and contain no backslash,
    control characters or whitespace (browsers strip TAB/CR/LF, turning '/\t/evil.com' into '//evil.com')."""
    import re as _re, urllib.parse as _u
    if not isinstance(n, str) or not n or len(n) > 2000: return default
    if _re.search(r"[\x00-\x20\x7f\\]", n) or not n.startswith("/") or n.startswith("//"): return default
    if _u.urlsplit(n).netloc or _u.urlsplit(n).scheme: return default
    if not n.isascii(): return default   # no Unicode slashes/lookalikes (e.g. U+2215, fullwidth solidus)
    path = _u.unquote(n.split("?", 1)[0].split("#", 1)[0])
    if not path.isascii() or "//" in path or "/./" in path or "/../" in path or path.endswith(("/.", "/..")) or path.startswith("/@") or "\\" in path:
        return default
    return n
