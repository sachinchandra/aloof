"""TOTP (RFC 6238: SHA-1, 6 digits, 30 s) for the owner's admin 2FA, plus one-time recovery codes. No third-party dependency."""
import base64, hashlib, hmac, json, secrets, struct, time
from urllib.parse import quote
from .db import q
from .crypto import enc, dec, sha256
def new_secret(): return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
def code(secret, t=None, step=30):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8)); c = int((t if t is not None else time.time()) // step)
    h = hmac.new(key, struct.pack(">Q", c), hashlib.sha1).digest(); o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 1000000)
def match_step(secret, c, t=None, window=1):
    """The time step (counter) a 6-digit code belongs to within ±window steps, or None."""
    c = "".join(ch for ch in str(c or "") if ch.isdigit()); t = t if t is not None else time.time()
    if len(c) != 6: return None
    for i in range(-window, window + 1):
        if hmac.compare_digest(code(secret, t + 30 * i), c): return int((t + 30 * i) // 30)
    return None
def check(secret, c, t=None, window=1): return match_step(secret, c, t, window) is not None
def uri(secret, email, issuer="Aloof"): return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
LOCK_FAILS, LOCK_WINDOW, LOCK_FOR = 10, 15 * 60, 30 * 60
REC_N = 8
def _rec_hash(salt, c): return hashlib.sha256((salt + ":" + _norm(c)).encode()).hexdigest()
def _norm(c): return str(c or "").strip().lower().replace("-", "").replace(" ", "")
def new_recovery_codes(acct_id):
    """8 codes of 80 bits each (20 hex chars, shown as xxxxx-xxxxx-xxxxx-xxxxx), stored as per-code salted SHA-256."""
    q("delete from mfa_recovery where account_id=?", (acct_id,)); out = []
    for _ in range(REC_N):
        raw = secrets.token_hex(10); salt = secrets.token_hex(16)
        q("insert into mfa_recovery(account_id,salt,hash,used) values(?,?,?,NULL)", (acct_id, salt, _rec_hash(salt, raw)))
        out.append("-".join(raw[i:i + 5] for i in range(0, 20, 5)))
    q("update accounts set totp_recovery=NULL where id=?", (acct_id,)); return out
def _spend_recovery(acct_id, c):
    n = _norm(c)
    if len(n) != 20: return False
    for r in q("select id, salt, hash from mfa_recovery where account_id=? and used is null", (acct_id,)):
        if hmac.compare_digest(_rec_hash(r["salt"], n), r["hash"]):
            return bool(q("update mfa_recovery set used=? where id=? and used is null returning id", (time.time(), r["id"])))   # atomic: one winner
    return False
def locked_until(acct_id, now=None):
    """Lockout: LOCK_FAILS failures within LOCK_WINDOW lock 2FA for LOCK_FOR seconds after the last of them."""
    now = now or time.time()
    ts = [r["ts"] for r in q("select ts from mfa_failures where account_id=? and ts>? order by ts", (acct_id, now - LOCK_WINDOW - LOCK_FOR))]
    for i in range(len(ts) - LOCK_FAILS + 1):
        if ts[i + LOCK_FAILS - 1] - ts[i] <= LOCK_WINDOW:
            until = ts[i + LOCK_FAILS - 1] + LOCK_FOR
            if until > now: return until
    return 0
def record_failure(acct_id, now=None):
    """Returns True when this failure is the one that starts a lockout (so the caller alerts once)."""
    now = now or time.time(); was = locked_until(acct_id, now)
    q("insert into mfa_failures(account_id,ts) values(?,?)", (acct_id, now)); q("delete from mfa_failures where ts<?", (now - 86400,))
    return not was and bool(locked_until(acct_id, now))
def status(acct_id):
    a = q("select totp_enabled, totp_secret_enc from accounts where id=?", (acct_id,), one=True) or {}
    left = (q("select count(*) n from mfa_recovery where account_id=? and used is null", (acct_id,), one=True) or {}).get("n") or 0
    lu = locked_until(acct_id)
    return {"enrolled": bool(a.get("totp_enabled")), "pending": bool(a.get("totp_secret_enc")) and not a.get("totp_enabled"),
            "recovery_left": int(left), "locked_until": lu or None}
def start(acct_id):
    """Begin (or restart) enrollment. Refuses while 2FA is on, so nobody can swap the secret on an enrolled owner."""
    if status(acct_id)["enrolled"]: raise PermissionError("2FA is already on")
    s = new_secret(); q("update accounts set totp_secret_enc=?, totp_last_step=0 where id=?", (enc(s), acct_id)); return s
def _accept_step(acct_id, step):
    """Replay guard: a code is accepted once, and only if its step is newer than the last accepted one (atomic compare-and-set)."""
    return bool(q("update accounts set totp_last_step=? where id=? and coalesce(totp_last_step,0)<? returning id", (step, acct_id, step)))
def confirm(acct_id, c):
    a = q("select totp_secret_enc, totp_enabled from accounts where id=?", (acct_id,), one=True) or {}
    if a.get("totp_enabled") or not a.get("totp_secret_enc"): raise PermissionError("start enrollment first")
    st = match_step(dec(a["totp_secret_enc"]), c)
    if st is None or not _accept_step(acct_id, st): raise ValueError("that code didn't match; check the phone's clock and try the newest code")
    q("update accounts set totp_enabled=1 where id=?", (acct_id,)); return new_recovery_codes(acct_id)
def verify(acct_id, c):
    """A current, not-yet-used TOTP code, or an unused recovery code (spent atomically on use)."""
    a = q("select totp_secret_enc, totp_enabled from accounts where id=?", (acct_id,), one=True) or {}
    if not a.get("totp_enabled"): return False
    st = match_step(dec(a["totp_secret_enc"]), c)
    if st is not None: return _accept_step(acct_id, st)
    return _spend_recovery(acct_id, c)
