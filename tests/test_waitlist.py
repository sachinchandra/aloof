"""Waitlist end to end: storage, duplicates, emails (outbox in dev), honeypot, rate limit, unsubscribe, admin list/filter/CSV/delete, test flags."""
import os, re, time
os.environ["PAAS_STATUS_PROBES"] = "0"
from fastapi.testclient import TestClient
from paas import api, www, auth, waitlist as W
from paas.db import q, kv_put
c = TestClient(api.app, base_url="http://api.aloof.test")
def _out(): return open(auth.OUTBOX).read() if os.path.exists(auth.OUTBOX) else ""
def _reset(): W._hits.clear()
def _owner():
    o = TestClient(api.app, base_url="http://api.aloof.test")
    if not q("select 1 from accounts where lower(email)=?", (www.ADMIN_EMAIL,), one=True):
        o.post("/v1/auth/register", json={"email": www.ADMIN_EMAIL, "password": "correct horse 1"})
    else: o.post("/v1/auth/login", json={"email": www.ADMIN_EMAIL, "password": "correct horse 1"})
    www.mark_flags()
    o.headers.update({"X-Aloof-CSRF": "1"}); return o
def test_signup_duplicate_emails_and_unsubscribe(monkeypatch):
    _reset(); monkeypatch.setenv("PAAS_WAITLIST_NOTIFY", "ops@aloof.test,owner@aloof.test")
    e = f"buyer{time.time_ns()}@acme.dev"
    r = c.post("/v1/waitlist", json={"email": e.upper(), "gpu": "gpu-h100", "source": "/pricing"})
    assert r.status_code == 200 and r.json()["duplicate"] is False and "GPU waitlist" in r.json()["message"]
    row = q("select * from waitlist where email=?", (e,), one=True)
    assert row["gpu"] == "gpu-h100" and row["source"] == "/pricing" and row["consent_ts"] and row["is_test"] == 0
    assert row["mail_user"] == "sent:outbox" and row["mail_admin"].startswith("sent:") and "ops@aloof.test=outbox" in row["mail_admin"]
    out = _out(); assert "You're on the Aloof GPU waitlist" in out and "New gpu waitlist signup" in out and "List-Unsubscribe" in out
    first = row["created"]; time.sleep(0.01); n = len(_out())
    r = c.post("/v1/waitlist", json={"email": e, "gpu": "gpu-a100"})
    assert r.json() == {"ok": True, "message": "You're already on the list, we updated your choice.", "duplicate": True}
    row2 = q("select * from waitlist where email=?", (e,), one=True)
    assert row2["gpu"] == "gpu-a100" and row2["created"] == first and row2["updated"] > first and len(_out()) == n   # no second email
    assert q("select count(*) n from waitlist where email=?", (e,), one=True)["n"] == 1
    url = W.unsub_url(row["id"], e); path = url.split("aloof.si", 1)[1]
    assert e in c.get(path).text and c.get(path.replace("t=", "t=x")).text.count("not valid") >= 1
    t = re.search(r"t=([0-9a-f]+)", url).group(1)
    assert c.post("/waitlist/unsubscribe", data={"id": str(row["id"]), "t": "bad"}).status_code == 200 and q("select 1 from waitlist where id=?", (row["id"],), one=True)
    assert "removed" in c.post("/waitlist/unsubscribe", data={"id": str(row["id"]), "t": t}).text and not q("select 1 from waitlist where id=?", (row["id"],), one=True)
def test_other_kinds_and_validation():
    _reset(); e = f"r{time.time_ns()}@acme.dev"
    assert c.post("/v1/waitlist", json={"email": e, "kind": "region", "region": "ap-sin"}).json()["duplicate"] is False
    assert c.post("/v1/waitlist", json={"email": e, "kind": "region", "region": "ap-sin"}).json()["duplicate"] is True
    assert c.post("/v1/waitlist", json={"email": e, "kind": "region", "region": "mars"}).status_code == 400
    assert c.post("/v1/waitlist", json={"email": e, "kind": "nope"}).status_code == 400
    assert c.post("/v1/waitlist", json={"email": "bad", "gpu": "gpu-h100"}).status_code == 400
    assert c.post("/v1/waitlist", json={"email": e, "gpu": "shared-1x-1gb"}).status_code == 400
    assert c.post("/v1/waitlist", json={"email": e, "kind": "design_partner", "company": "Acme", "note": "x" * 5000}).status_code == 200
    assert len(q("select note from waitlist where kind='design_partner' and email=?", (e,), one=True)["note"]) == 1000
def test_honeypot_and_rate_limit():
    _reset(); e = f"bot{time.time_ns()}@acme.dev"
    r = c.post("/v1/waitlist", json={"email": e, "gpu": "gpu-l4", "website": "http://spam"})
    assert r.status_code == 200 and r.json()["ok"] and not q("select 1 from waitlist where email=?", (e,), one=True)
    _reset()
    codes = [c.post("/v1/waitlist", json={"email": f"rl{i}{time.time_ns()}@acme.dev"}).status_code for i in range(W.LIMIT + 1)]
    assert codes[:W.LIMIT] == [200] * W.LIMIT and codes[-1] == 429; _reset()
def test_test_entries_excluded_and_admin():
    _reset(); o = _owner()
    assert W.is_test_email("ux-test+gpu@aloof.si") and W.is_test_email("someone@aloof.si") and not W.is_test_email("a@acme.dev")
    before = W.counts()["gpu"]
    c.post("/v1/waitlist", json={"email": "ux-test+gpu@aloof.si", "gpu": "gpu-h100"}); real = f"real{time.time_ns()}@acme.dev"
    c.post("/v1/waitlist", json={"email": real, "gpu": "gpu-l4"})
    cn = W.counts(); assert cn["gpu"] == before + 1 and cn["test_entries"] >= 1
    assert c.get("/v1/admin/waitlist").status_code == 401
    j = o.get("/v1/admin/waitlist?kind=gpu&gpu=gpu-l4").json()
    assert all(x["gpu"] == "gpu-l4" and x["kind"] == "gpu" for x in j["entries"]) and any(x["email"] == real for x in j["entries"]) and "smtp_configured" in j["mail"]
    csv_ = o.get("/v1/admin/waitlist.csv?kind=gpu").text
    assert csv_.startswith("id,kind,email,gpu") and real in csv_ and "ux-test+gpu@aloof.si" not in csv_
    assert "ux-test+gpu@aloof.si" in o.get("/v1/admin/waitlist.csv?include_test=true").text
    rid = q("select id from waitlist where email=?", (real,), one=True)["id"]
    assert o.post(f"/v1/admin/waitlist/{rid}/test", json={"is_test": True}).status_code == 200 and W.counts()["gpu"] == before
    m = o.get("/v1/admin/dashboard").json(); assert m["waitlist"]["gpu"] == before and "mail" in m
    assert o.delete(f"/v1/admin/waitlist/{rid}").status_code == 200 and o.delete(f"/v1/admin/waitlist/{rid}").status_code == 404
    assert c.delete(f"/v1/admin/waitlist/{rid}").status_code == 401
def test_no_admin_recipients_is_recorded(monkeypatch):
    _reset(); monkeypatch.delenv("PAAS_WAITLIST_NOTIFY", raising=False); monkeypatch.setattr(www, "ADMIN_EMAIL", "")
    e = f"na{time.time_ns()}@acme.dev"; c.post("/v1/waitlist", json={"email": e})
    assert q("select mail_admin from waitlist where email=?", (e,), one=True)["mail_admin"].startswith("skipped")
def test_mail_failure_recorded(monkeypatch):
    _reset()
    def boom(*a, **k): raise auth.MailError("email is not configured")
    monkeypatch.setattr(auth, "send_mail", boom); monkeypatch.setenv("PAAS_WAITLIST_NOTIFY", "ops@aloof.test")
    e = f"mf{time.time_ns()}@acme.dev"; assert c.post("/v1/waitlist", json={"email": e}).status_code == 200
    r = q("select mail_user, mail_admin from waitlist where email=?", (e,), one=True)
    assert r["mail_user"].startswith("failed") and r["mail_admin"].startswith("failed") and W.mail_health()["last_error"]
def test_legacy_kv_migrates():
    e = f"old{time.time_ns()}@acme.dev"; kv_put("gpu_waitlist", e, '{"gpu": "gpu-l40", "note": "", "ts": 1700000000}')
    W.migrate_kv(); r = q("select * from waitlist where email=?", (e,), one=True)
    assert r["gpu"] == "gpu-l40" and r["created"] == 1700000000 and r["source"] == "legacy" and not q("select 1 from kv where kind='gpu_waitlist' and k=?", (e,), one=True)
