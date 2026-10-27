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
def check(secret, c, t=None, window=1):
    c = "".join(ch for ch in str(c or "") if ch.isdigit()); t = t if t is not None else time.time()
    return len(c) == 6 and any(hmac.compare_digest(code(secret, t + 30 * i), c) for i in range(-window, window + 1))
def uri(secret, email, issuer="Aloof"): return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
def status(acct_id):
    a = q("select totp_enabled, totp_secret_enc, totp_recovery from accounts where id=?", (acct_id,), one=True) or {}
    return {"enrolled": bool(a.get("totp_enabled")), "pending": bool(a.get("totp_secret_enc")) and not a.get("totp_enabled"),
            "recovery_left": len(json.loads(a.get("totp_recovery") or "[]"))}
def start(acct_id):
    """Begin (or restart) enrollment. Refuses while 2FA is on, so nobody can swap the secret on an enrolled owner."""
    if status(acct_id)["enrolled"]: raise PermissionError("2FA is already on")
    s = new_secret(); q("update accounts set totp_secret_enc=? where id=?", (enc(s), acct_id)); return s
def confirm(acct_id, c):
    a = q("select totp_secret_enc, totp_enabled from accounts where id=?", (acct_id,), one=True) or {}
    if a.get("totp_enabled") or not a.get("totp_secret_enc"): raise PermissionError("start enrollment first")
    if not check(dec(a["totp_secret_enc"]), c): raise ValueError("that code didn't match; check the phone's clock and try the newest code")
    codes = [secrets.token_hex(5) for _ in range(8)]
    q("update accounts set totp_enabled=1, totp_recovery=? where id=?", (json.dumps([sha256(x) for x in codes]), acct_id)); return codes
def verify(acct_id, c):
    """A current TOTP code, or an unused recovery code (spent on use)."""
    a = q("select totp_secret_enc, totp_enabled, totp_recovery from accounts where id=?", (acct_id,), one=True) or {}
    if not a.get("totp_enabled"): return False
    if check(dec(a["totp_secret_enc"]), c): return True
    rec = json.loads(a.get("totp_recovery") or "[]"); h = sha256(str(c or "").strip().lower().replace("-", ""))
    if h in rec: rec.remove(h); q("update accounts set totp_recovery=? where id=?", (json.dumps(rec), acct_id)); return True
    return False
