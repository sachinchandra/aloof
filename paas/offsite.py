"""Off-site backups to any S3-compatible store (default: Hetzner Object Storage).
Everything is encrypted client-side with AES-256-GCM before upload (chunked; the last chunk is authenticated as last, so a
truncated object fails to decrypt). Data key: BACKUP_ENCRYPTION_KEY (base64, 32 bytes). Control-plane secrets (secret.key,
golive.env, admin token) are additionally encrypted with a key derived (scrypt) from BACKUP_SECRETS_PASSPHRASE, so a leaked
data key alone doesn't expose them.
Retention per (kind, owner): newest per day for 7 days, per ISO week for 4 weeks, per month for 3 months (GFS); others deleted.
Jobs (hourly loop): daily upload of every Postgres DB, every volume (snapshot), the control-plane DB and secrets; weekly
verification test-restores the newest object of each kind. Disabled (no-op) until endpoint, bucket, keys and data key are set."""
import os, io, json, time, base64, struct, hashlib, secrets, tempfile, logging, subprocess, tarfile, shutil
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .db import q, event, PG as PGDB, DB_URL
log = logging.getLogger("paas.offsite")
ROOT = os.path.join(os.path.dirname(__file__), "..")
E = lambda k, d="": os.environ.get(k, d)
def cfg():
    return {"endpoint": E("BACKUP_S3_ENDPOINT", "https://fsn1.your-objectstorage.com"), "bucket": E("BACKUP_S3_BUCKET", "aloof-backups"),
            "region": E("BACKUP_S3_REGION", "fsn1"), "access": E("BACKUP_S3_ACCESS_KEY"), "secret": E("BACKUP_S3_SECRET_KEY"),
            "key": E("BACKUP_ENCRYPTION_KEY"), "pass": E("BACKUP_SECRETS_PASSPHRASE"), "prefix": E("BACKUP_S3_PREFIX", "").strip("/")}
def missing():
    c = cfg(); return [n for n, k in (("BACKUP_S3_ACCESS_KEY", "access"), ("BACKUP_S3_SECRET_KEY", "secret"), ("BACKUP_ENCRYPTION_KEY", "key"), ("BACKUP_SECRETS_PASSPHRASE", "pass")) if not c[k]]
def enabled(): return E("BACKUP_OFFSITE_ENABLED", "1") == "1" and not missing()
def _schema():
    q("create table if not exists offsite(id TEXT primary key, kind TEXT, owner TEXT, account_id bigint, s3_key TEXT, size_bytes bigint, sha256 TEXT, created REAL, verified REAL, verify_result TEXT)")
_schema()
# ---------------- crypto ----------------
MAGIC = b"ALOOFBK1"; CHUNK = 1 << 20
def data_key():
    k = base64.b64decode(cfg()["key"]); assert len(k) == 32, "BACKUP_ENCRYPTION_KEY must be base64 of 32 bytes"; return k
def passphrase_key(salt):
    return hashlib.scrypt(cfg()["pass"].encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=2**26)
def encrypt_stream(src, dst, key):
    a = AESGCM(key); pre = os.urandom(8); dst.write(MAGIC + pre); i = 0; buf = src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK); last = not nxt
        ct = a.encrypt(pre + struct.pack(">I", i), buf, b"last" if last else b"more"); dst.write(struct.pack(">I", len(ct)) + ct)
        if last: return
        buf = nxt; i += 1
def decrypt_stream(src, dst, key):
    a = AESGCM(key); h = src.read(16)
    if h[:8] != MAGIC: raise ValueError("not an Aloof backup object")
    pre = h[8:]; i = 0
    while True:
        n = src.read(4)
        if len(n) < 4: raise ValueError("truncated backup (no final chunk)")
        ct = src.read(struct.unpack(">I", n)[0]); peek = src.read(1)
        last = not peek
        dst.write(a.decrypt(pre + struct.pack(">I", i), ct, b"last" if last else b"more"))
        if last: return
        src.seek(-1, 1); i += 1
def _enc_file(path, key):
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".enc", dir=_tmpdir())
    with open(path, "rb") as f: encrypt_stream(f, out, key)
    out.close(); return out.name
def _tmpdir():
    d = os.path.join(ROOT, "data", "tmp-offsite"); os.makedirs(d, mode=0o700, exist_ok=True); return d
# ---------------- S3 ----------------
_s3 = None
def s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        c = cfg(); _s3 = boto3.client("s3", endpoint_url=c["endpoint"], region_name=c["region"], aws_access_key_id=c["access"], aws_secret_access_key=c["secret"],
                                      config=Config(s3={"addressing_style": "path"}, signature_version="s3v4", retries={"max_attempts": 5}))
    return _s3
def ensure_bucket():
    b = cfg()["bucket"]
    try: s3().head_bucket(Bucket=b)
    except Exception: s3().create_bucket(Bucket=b)
def _key(kind, owner, oid): p = cfg()["prefix"]; return (p + "/" if p else "") + f"{kind}/{owner}/{time.strftime('%Y/%m', time.gmtime())}/{oid}.enc"
def upload(kind, owner, path, account_id=None, secret=False):
    """Encrypt + upload a local file. kind: pg | vol | controldb | secrets."""
    oid = f"{kind}_{time.strftime('%Y%m%d%H%M%S', time.gmtime())}_{secrets.token_hex(3)}"
    if secret:   # secrets: passphrase layer inside, data key outside
        salt = os.urandom(16); inner = _enc_file(path, passphrase_key(salt)); tmp = inner + ".s"
        with open(tmp, "wb") as f, open(inner, "rb") as g: f.write(b"PASS" + salt); shutil.copyfileobj(g, f)
        os.remove(inner); enc = _enc_file(tmp, data_key()); os.remove(tmp)
    else: enc = _enc_file(path, data_key())
    h = hashlib.sha256(open(path, "rb").read()).hexdigest(); size = os.path.getsize(enc); key = _key(kind, owner, oid)
    try: s3().upload_file(enc, cfg()["bucket"], key, ExtraArgs={"Metadata": {"aloof-sha256": h, "aloof-kind": kind}})
    finally: os.remove(enc)
    q("insert into offsite(id,kind,owner,account_id,s3_key,size_bytes,sha256,created) values(?,?,?,?,?,?,?,?)", (oid, kind, owner, account_id, key, size, h, time.time()))
    prune(kind, owner); return view(q("select * from offsite where id=?", (oid,), one=True))
def download(oid, dest):
    r = q("select * from offsite where id=?", (oid,), one=True)
    if not r: raise ValueError("off-site backup not found")
    enc = os.path.join(_tmpdir(), oid + ".dl")
    try:
        s3().download_file(cfg()["bucket"], r["s3_key"], enc)
        with open(enc, "rb") as f, open(dest, "wb") as o: decrypt_stream(f, o, data_key())
    finally:
        if os.path.exists(enc): os.remove(enc)
    if r["kind"] == "secrets":
        raw = open(dest, "rb").read(); assert raw[:4] == b"PASS"; salt = raw[4:20]
        with open(dest, "wb") as o: decrypt_stream(io.BytesIO(raw[20:]), o, passphrase_key(salt))
    if hashlib.sha256(open(dest, "rb").read()).hexdigest() != r["sha256"]: raise ValueError("checksum mismatch after decrypt")
    os.chmod(dest, 0o600); return r
def view(r): return {k: r[k] for k in ("id", "kind", "owner", "size_bytes", "created", "verified", "verify_result")}
def list_(kind, owner): return [view(r) for r in q("select * from offsite where kind=? and owner=? order by created desc", (kind, owner))]
# ---------------- retention (GFS) ----------------
KEEP = {"daily": int(E("BACKUP_KEEP_DAILY", "7")), "weekly": int(E("BACKUP_KEEP_WEEKLY", "4")), "monthly": int(E("BACKUP_KEEP_MONTHLY", "3"))}
def keep_set(rows, now=None):
    """rows: [{id, created}] -> ids to keep: newest per day (7), per ISO week (4), per month (3)."""
    keep = set(); rows = sorted(rows, key=lambda r: -r["created"])
    for fmt, n, pick_oldest in (("%Y-%m-%d", KEEP["daily"], False), ("%G-W%V", KEEP["weekly"], True), ("%Y-%m", KEEP["monthly"], True)):
        buckets = {}
        for r in rows: buckets.setdefault(time.strftime(fmt, time.gmtime(r["created"])), []).append(r)
        for b in sorted(buckets, reverse=True)[:n]:   # newest n buckets; daily keeps the newest in a day, weekly/monthly the first
            keep.add((buckets[b][-1] if pick_oldest else buckets[b][0])["id"])
    return keep
def prune(kind, owner):
    rows = q("select * from offsite where kind=? and owner=?", (kind, owner)); keep = keep_set(rows)
    for r in rows:
        if r["id"] in keep: continue
        try: s3().delete_object(Bucket=cfg()["bucket"], Key=r["s3_key"])
        except Exception as e: log.warning("delete %s: %s", r["s3_key"], e); continue
        q("delete from offsite where id=?", (r["id"],))
def lifecycle_rule():
    """Server-side safety net (objects older than ~100 days expire even if the pruner stops running)."""
    try:
        s3().put_bucket_lifecycle_configuration(Bucket=cfg()["bucket"], LifecycleConfiguration={"Rules": [
            {"ID": "aloof-max-age", "Status": "Enabled", "Filter": {"Prefix": cfg()["prefix"]}, "Expiration": {"Days": 100},
             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2}}]})
        return True
    except Exception as e: log.warning("lifecycle rule not applied: %s", e); return False
# ---------------- sources ----------------
def _helper_pg(cmd, env, mounts=None, network="host"):
    """Run a postgres:16 client container (no pg tools needed on the host)."""
    from .drivers import get_drivers
    d = get_drivers()["local-docker"]; img = E("PAAS_PG_IMAGE", "postgres:16-alpine")
    try: d.c.images.get(img)
    except Exception: d.c.images.pull(img)
    return d.c.containers.run(img, ["sh", "-c", cmd], environment=env, network_mode=network, remove=True, volumes=mounts or {}, stdout=True, stderr=True)
def dump_control_db(path):
    if PGDB:
        d = os.path.dirname(os.path.realpath(path)); f = os.path.basename(path)
        _helper_pg(f'pg_dump -Fc -f /arc/{f} "$DBURL" && chown {os.getuid()}:{os.getgid()} /arc/{f} && chmod 600 /arc/{f}', {"DBURL": DB_URL}, {d: {"bind": "/arc", "mode": "rw"}})
    else:
        import sqlite3
        from . import db as _db
        src = _db._c; dst = sqlite3.connect(path); src.backup(dst); dst.close()
def bundle_secrets(path):
    files = {"secret.key": os.path.join(ROOT, "data", "secret.key"), "admin_token": os.path.join(ROOT, "data", "admin_token"),
             "golive.env": os.path.join(ROOT, "deploy", "golive.env"), "wg-hub.key": os.path.join(ROOT, "data", "wg-hub.key"),
             "database_url": os.path.join(ROOT, "data", "database_url")}
    with tarfile.open(path, "w:gz") as t:
        skipped = []
        for n, p in files.items():
            if not os.path.exists(p): continue
            if os.access(p, os.R_OK): t.add(p, arcname=n)
            else: skipped.append(n)   # e.g. root-owned wg-hub.key: back it up out of band
        if skipped:
            b = ("not readable by the control plane, back up separately: " + ", ".join(skipped) + "\n").encode()
            ti = tarfile.TarInfo("SKIPPED.txt"); ti.size = len(b); t.addfile(ti, io.BytesIO(b)); log.warning("secrets bundle skipped %s", skipped)
        if E("PAAS_SECRET_KEY"):
            b = E("PAAS_SECRET_KEY").encode(); ti = tarfile.TarInfo("PAAS_SECRET_KEY.env"); ti.size = len(b); t.addfile(ti, io.BytesIO(b))
def _due(kind, owner, every=86400):
    t = (q("select max(created) t from offsite where kind=? and owner=?", (kind, owner), one=True) or {}).get("t") or 0
    return time.time() - t > every - 600
import threading as _th
_run_lock = _th.Lock()
def backup_all(force=False):
    with _run_lock: return _backup_all(force)
def _backup_all(force=False):
    """Daily: every Postgres DB (fresh pg_dump), every local volume (fresh snapshot), control-plane DB, secrets."""
    if not enabled(): return {"skipped": "disabled", "missing": missing()}
    from . import parity
    done, errs = [], []
    def run(kind, owner, fn):
        if not (force or _due(kind, owner)): return
        try: done.append(fn())
        except Exception as e: errs.append(f"{kind}/{owner}: {type(e).__name__}: {e}"); log.exception("offsite %s/%s", kind, owner)
    for db in q("select * from pg_dbs"):
        def f(db=db):
            a = parity.pg_backup(db["name"], "off-site"); r = q("select * from archives where id=?", (a["id"],), one=True)
            return upload("pg", db["name"], r["path"], db["account_id"])
        run("pg", db["name"], f)
    for v in q("select * from volumes where node_id is null"):
        if v["name"].startswith("pgdata-"): continue   # Postgres is covered by consistent pg_dump
        def f(v=v):
            a = parity.snapshot_create(v["name"], "off-site"); r = q("select * from archives where id=?", (a["id"],), one=True)
            return upload("vol", v["name"], r["path"], v["account_id"])
        run("vol", v["name"], f)
    def ctl():
        p = os.path.join(_tmpdir(), "control.dump")
        try: dump_control_db(p); return upload("controldb", "control-plane", p)
        finally:
            if os.path.exists(p): os.remove(p)
    run("controldb", "control-plane", ctl)
    def sec():
        p = os.path.join(_tmpdir(), "secrets.tgz")
        try: bundle_secrets(p); return upload("secrets", "control-plane", p, secret=True)
        finally:
            if os.path.exists(p): os.remove(p)
    run("secrets", "control-plane", sec)
    from .db import kv_put
    kv_put("offsite", "last_run", json.dumps({"ts": time.time(), "uploaded": len(done), "errors": errs}))
    return {"uploaded": done, "errors": errs}
# ---------------- restore ----------------
def restore_pg(name, oid):
    from . import parity
    r = q("select * from offsite where id=? and kind='pg' and owner=?", (oid, name), one=True)
    if not r: raise ValueError("off-site backup not found for this database")
    aid = "pgb_offsite_" + oid.split("_", 1)[1]; path = parity._arc_path("pg", name, aid, "dump"); download(oid, path)
    db = q("select * from pg_dbs where name=?", (name,), one=True)
    q("insert into archives(id,kind,owner,account_id,path,size_bytes,created,note) values(?,?,?,?,?,?,?,?) on conflict(id) do nothing",
      (aid, "pg", name, db["account_id"], path, os.path.getsize(path), time.time(), f"from off-site {oid}"))
    return parity.pg_restore(name, aid) | {"from_offsite": oid}
def restore_vol(vol, oid):
    from . import parity
    r = q("select * from offsite where id=? and kind='vol' and owner=?", (oid, vol), one=True)
    if not r: raise ValueError("off-site backup not found for this volume")
    sid = "snap_offsite_" + oid.split("_", 1)[1]; path = parity._arc_path("snap", vol, sid, "tar.gz"); download(oid, path)
    v = q("select * from volumes where name=?", (vol,), one=True)
    q("insert into archives(id,kind,owner,account_id,path,size_bytes,created,note) values(?,?,?,?,?,?,?,?) on conflict(id) do nothing",
      (sid, "snap", vol, v["account_id"], path, os.path.getsize(path), time.time(), f"from off-site {oid}"))
    return parity.snapshot_restore(vol, sid) | {"from_offsite": oid}
# ---------------- verification (weekly test-restore) ----------------
def verify(oid):
    r = q("select * from offsite where id=?", (oid,), one=True); tmp = os.path.join(_tmpdir(), oid + ".verify")
    try:
        download(oid, tmp)   # decrypt + checksum
        if r["kind"] in ("pg", "controldb") and (r["kind"] == "pg" or PGDB):
            d = os.path.dirname(os.path.realpath(tmp)); f = os.path.basename(tmp)
            cmd = (f"cp /arc/{f} /tmp/x.dump && chmod 644 /tmp/x.dump && mkdir -p /tmp/v && chown postgres /tmp/v && su postgres -c 'initdb -D /tmp/v -A trust >/dev/null && pg_ctl -D /tmp/v -o \"-c listen_addresses=\" -w start >/dev/null' "
                   f"&& su postgres -c 'createdb vt && (pg_restore --no-owner --no-acl -d vt /tmp/x.dump 2>/tmp/err || true) && psql -At -d vt -c \"select count(*) from pg_tables where schemaname = current_schema()\"'")
            out = _helper_pg(cmd, {}, {d: {"bind": "/arc", "mode": "ro"}}, network="none").decode().strip().splitlines()
            n = int(out[-1]) if out and out[-1].isdigit() else -1
            res = (f"ok: restored into scratch Postgres, {n} tables" if n > 0 or (r["kind"] == "pg" and n == 0)
                   else f"FAILED: restore produced {n} tables")
        elif r["kind"] == "controldb":
            import sqlite3
            c = sqlite3.connect(tmp); n = c.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]; ok = c.execute("pragma integrity_check").fetchone()[0]; c.close()
            res = f"ok: sqlite {ok}, {n} tables"
        else:
            with tarfile.open(tmp) as t: n = len(t.getnames())
            res = f"ok: archive opens, {n} entries"
    except Exception as e: res = f"FAILED: {type(e).__name__}: {e}"
    finally:
        if os.path.exists(tmp): os.remove(tmp)
    q("update offsite set verified=?, verify_result=? where id=?", (time.time(), res[:500], oid)); return res
def verify_all(force=False):
    with _run_lock: return _verify_all(force)
def _verify_all(force=False):
    if not enabled(): return {"skipped": "disabled"}
    out = {}
    for k in q("select distinct kind, owner from offsite"):
        r = q("select * from offsite where kind=? and owner=? order by created desc", (k["kind"], k["owner"]), one=True)
        last = (q("select max(verified) t from offsite where kind=? and owner=?", (k["kind"], k["owner"]), one=True) or {}).get("t") or 0
        if force or time.time() - last > 7 * 86400: out[r["id"]] = verify(r["id"])
    return out
# ---------------- status for admin metrics ----------------
STALE_HOURS = float(E("BACKUP_ALERT_HOURS", "26"))
def status():
    last = (q("select max(created) t from offsite", one=True) or {}).get("t")
    age = (time.time() - last) / 3600 if last else None
    bad = q("select id, verify_result from offsite where verify_result like ? order by verified desc", ("FAILED%",))
    st = {"enabled": enabled(), "missing_config": missing(), "endpoint": cfg()["endpoint"], "bucket": cfg()["bucket"],
          "last_offsite_backup": last, "last_offsite_age_hours": round(age, 2) if age is not None else None,
          "objects": (q("select count(*) n from offsite", one=True) or {}).get("n"),
          "last_verify": (q("select max(verified) t from offsite", one=True) or {}).get("t"), "verify_failures": bad[:5]}
    alerts = []
    if not st["enabled"]: alerts.append("off-site backups disabled: set " + ", ".join(st["missing_config"]))
    elif age is None or age > STALE_HOURS: alerts.append(f"last off-site backup is {'missing' if age is None else f'{age:.1f}h old'} (> {STALE_HOURS:g}h)")
    if bad: alerts.append(f"{len(bad)} off-site verification failure(s)")
    st["alerts"] = alerts; st["alert"] = bool(alerts); return st
def tick():
    if not enabled(): return
    try: ensure_bucket()
    except Exception: log.exception("bucket")
    backup_all(); verify_all()
def start():
    import threading
    def loop():
        if enabled():
            try: ensure_bucket(); lifecycle_rule()
            except Exception: log.exception("offsite init")
        while True:
            try: tick()
            except Exception: log.exception("offsite tick")
            time.sleep(int(E("BACKUP_INTERVAL", "3600")))
    threading.Thread(target=loop, daemon=True).start()
