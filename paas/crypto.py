"""Secrets at rest: Fernet (AES-128-CBC + HMAC-SHA256). Key from PAAS_SECRET_KEY or data/secret.key (0600, auto-created).
Supports key rotation: PAAS_SECRET_KEY may hold several comma-separated keys; the first encrypts, all decrypt."""
import os, hashlib, hmac, secrets, json
from cryptography.fernet import Fernet, MultiFernet
_KF = os.path.join(os.path.dirname(__file__), "..", "data", "secret.key")
def _keys():
    k = os.environ.get("PAAS_SECRET_KEY")
    if not k:
        if not os.path.exists(_KF):
            os.makedirs(os.path.dirname(_KF), exist_ok=True); fd = os.open(_KF, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, Fernet.generate_key()); os.close(fd)
        k = open(_KF).read().strip()
    return [Fernet(x.strip().encode()) for x in k.split(",")]
F = MultiFernet(_keys())
def enc(s: str) -> str: return F.encrypt(s.encode()).decode()
def dec(s: str) -> str: return F.decrypt(s.encode()).decode()
def enc_json(o) -> str: return enc(json.dumps(o))
def dec_json(s): return json.loads(dec(s))
def sha256(s: str) -> str: return hashlib.sha256(s.encode()).hexdigest()
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16); h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"
def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except Exception: return False
# App spec: env values are encrypted as one blob; the rest stays queryable JSON.
def dump_spec(spec: dict) -> str:
    s = dict(spec); env = s.pop("env", {}) or {}
    s["env_enc"] = enc_json(env); return json.dumps(s)
def load_spec(raw: str) -> dict:
    s = json.loads(raw)
    if "env_enc" in s: s["env"] = dec_json(s.pop("env_enc"))
    s.setdefault("env", {}); return s
