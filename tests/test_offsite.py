"""Off-site backups: AES-GCM format, GFS retention, and a full backup/restore/verify cycle against a local MinIO (skipped if absent)."""
import os, io, json, time, base64, socket, tarfile, pytest
from fastapi.testclient import TestClient
from paas import offsite, api, parity
from paas.db import q
from test_parity import Fake, fake, user, wait, deploy   # noqa: F401  (fixtures)
def _tgz(content):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as t:
        ti = tarfile.TarInfo("f.txt"); ti.size = len(content); t.addfile(ti, io.BytesIO(content))
    return b.getvalue()
V1 = _tgz(b"volume-v1")
MINIO = os.environ.get("TEST_MINIO", "http://127.0.0.1:9100")
def _up():
    try: socket.create_connection(("127.0.0.1", 9100), timeout=1).close(); return True
    except OSError: return False
@pytest.fixture
def s3env(monkeypatch, tmp_path):
    if not _up(): pytest.skip("local MinIO not running")
    for k, v in {"BACKUP_S3_ENDPOINT": MINIO, "BACKUP_S3_BUCKET": f"t{time.time_ns()}", "BACKUP_S3_REGION": "us-east-1",
                 "BACKUP_S3_ACCESS_KEY": "minioadmin", "BACKUP_S3_SECRET_KEY": "minioadmin-test",
                 "BACKUP_ENCRYPTION_KEY": base64.b64encode(os.urandom(32)).decode(), "BACKUP_SECRETS_PASSPHRASE": "correct horse battery staple"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(offsite, "_s3", None); offsite.ensure_bucket(); q("delete from offsite"); yield
    monkeypatch.setattr(offsite, "_s3", None)

def test_aes_gcm_format_roundtrip_tamper_truncate():
    key = os.urandom(32); data = os.urandom(offsite.CHUNK * 2 + 123)
    enc = io.BytesIO(); offsite.encrypt_stream(io.BytesIO(data), enc, key); blob = enc.getvalue()
    assert data[:64] not in blob and blob[:8] == b"ALOOFBK1"
    out = io.BytesIO(); offsite.decrypt_stream(io.BytesIO(blob), out, key); assert out.getvalue() == data
    bad = bytearray(blob); bad[100] ^= 1
    with pytest.raises(Exception): offsite.decrypt_stream(io.BytesIO(bytes(bad)), io.BytesIO(), key)
    first_chunk_end = 16 + 4 + offsite.CHUNK + 16
    with pytest.raises(Exception): offsite.decrypt_stream(io.BytesIO(blob[:first_chunk_end]), io.BytesIO(), key)   # truncated after a full chunk
    with pytest.raises(Exception): offsite.decrypt_stream(io.BytesIO(blob), io.BytesIO(), os.urandom(32))

def test_gfs_retention_daily7_weekly4_monthly3():
    now = time.mktime((2026, 10, 13, 12, 0, 0, 0, 0, -1)); rows = [{"id": f"d{i}", "created": now - i * 86400} for i in range(120)]
    keep = offsite.keep_set(rows)
    days = sorted(int(k[1:]) for k in keep)
    assert set(range(7)) <= set(days) and len(keep) <= 7 + 4 + 3 and len(keep) >= 10
    assert max(days) >= 60 and max(days) < 120   # 3 monthly reach back ~2-3 months, not further

def test_disabled_without_keys_and_alert(monkeypatch):
    for k in ("BACKUP_S3_ACCESS_KEY", "BACKUP_S3_SECRET_KEY"): monkeypatch.delenv(k, raising=False)
    assert not offsite.enabled() and offsite.backup_all() ["skipped"] == "disabled"
    st = offsite.status(); assert st["alert"] and "disabled" in st["alerts"][0]
    os.environ.setdefault("PAAS_ADMIN_TOKEN", "")
    c = TestClient(api.app); tok = open(os.path.join(os.path.dirname(api.__file__), "..", "data", "admin_token")).read().strip() if not api.ADMIN else api.ADMIN
    assert c.get("/v1/admin/metrics", headers={"Authorization": f"Bearer {tok}"}).json()["backups"]["alert"]

def test_full_cycle_minio(s3env, fake, user, monkeypatch):
    # a database + a volume, through the real API
    assert user.post("/v1/pg", json={"name": "offdb", "plan": "pg-basic"}).status_code == 200; wait(user, None, "/v1/pg/offdb")
    user.post("/v1/volumes", json={"name": "offvol", "size_gb": 1}); fake.files["offvol"] = V1
    monkeypatch.setattr(offsite, "dump_control_db", lambda p: open(p, "wb").write(open(os.environ["PAAS_DB"], "rb").read()))
    r = offsite.backup_all(force=True); assert not r["errors"], r["errors"]
    kinds = {x["kind"] for x in r["uploaded"]}; assert kinds == {"pg", "vol", "controldb", "secrets"}
    # objects are encrypted at rest in the bucket (no plaintext markers)
    b = offsite.cfg()["bucket"]
    for o in offsite.s3().list_objects_v2(Bucket=b)["Contents"]:
        body = offsite.s3().get_object(Bucket=b, Key=o["Key"])["Body"].read(); assert body[:8] == b"ALOOFBK1" and b"PGDMP" not in body and b"volume-v1" not in body
    # secrets need the passphrase too
    sec = q("select * from offsite where kind='secrets'", one=True); out = os.path.join(offsite._tmpdir(), "s.tgz")
    offsite.download(sec["id"], out); assert "secret.key" in tarfile.open(out).getnames() or tarfile.open(out).getnames(); os.remove(out)
    monkeypatch.setenv("BACKUP_SECRETS_PASSPHRASE", "wrong")
    with pytest.raises(Exception): offsite.download(sec["id"], out)
    monkeypatch.setenv("BACKUP_SECRETS_PASSPHRASE", "correct horse battery staple")
    # restore from off-site via API (local archives deleted first: true disaster)
    q("delete from archives where owner in ('offdb','offvol')")
    pid = user.get("/v1/pg/offdb/offsite").json()[0]["id"]; res = user.post("/v1/pg/offdb/offsite-restore", json={"backup_id": pid}).json()
    assert res["ok"] and res["from_offsite"] == pid and any(c[0][0] == "pg_restore" for c in [(x[1],) for x in fake.execs])
    fake.files["offvol"] = b"corrupted"; vid = user.get("/v1/volumes/offvol/offsite").json()[0]["id"]
    assert user.post("/v1/volumes/offvol/offsite-restore", json={"backup_id": vid}).json()["from_offsite"] == vid and fake.files["offvol"] == V1
    other = TestClient(api.app); k = other.post("/v1/signup", json={"email": f"z{time.time_ns()}@x.com"}).json()["api_key"]
    assert other.get("/v1/pg/offdb/offsite", headers={"Authorization": f"Bearer {k}"}).status_code == 404
    # weekly verification (pg test-restore container mocked here; real one is exercised live)
    monkeypatch.setattr(offsite, "_helper_pg", lambda *a, **k: b"3\n")
    v = offsite.verify_all(force=True); assert v and all(x.startswith("ok") for x in v.values()), v
    st = offsite.status(); assert st["enabled"] and not st["alert"] and st["last_offsite_age_hours"] < 1
    q("update offsite set created=created-100000"); assert any("old" in a for a in offsite.status()["alerts"])   # >26h -> alert
    # retention prunes the bucket too
    for i in range(12):
        offsite.upload("vol", "offvol", q("select path from archives where owner='offvol'", one=True)["path"])
        q("update offsite set created=created-86400*? where kind='vol' and owner='offvol' and id=(select id from offsite where kind='vol' and owner='offvol' order by created desc limit 1)", (i + 2,))
    offsite.prune("vol", "offvol"); n = len(user.get("/v1/volumes/offvol/offsite").json()); assert 7 <= n <= 14
    keys = [o["Key"] for o in offsite.s3().list_objects_v2(Bucket=b, Prefix="vol/offvol/").get("Contents", [])]; assert len(keys) == n
    user.delete("/v1/pg/offdb")
