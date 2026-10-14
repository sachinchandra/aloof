"""diagnose_app: read-only report, rule-based causes, redaction."""
import time, json
from paas import diagnose, engine, api
from paas.db import q, event
from tests.test_parity import fake, user, wait, deploy   # noqa: F401

def _snapshot(name):
    return (q("select count(*) n from events where app=?", (name,), one=True)["n"], json.dumps(q("select id,status from instances where app=? order by id", (name,))),
            json.dumps(q("select * from releases where app=? order by version", (name,))), q("select status, spec from apps where name=?", (name,), one=True))

def test_redaction():
    r = diagnose.redactor({"DB_PASS": "hunter2-very-secret", "SHORT": "ab"})
    s = r("connect failed pw=hunter2-very-secret key sk_live_abcdefghijklmnop token=abc123xyz postgres://u:p4ss@db:5432/x Bearer eyJabcdefghijkl.eyJabcdefghijkl.sigsigsig")
    assert "hunter2" not in s and "sk_live_abc" not in s and "abc123xyz" not in s and "p4ss" not in s and "eyJabcdefghijkl.eyJ" not in s
    assert "connect failed" in s

def test_healthy_app_read_only_and_redacted(fake, user, monkeypatch):
    deploy(user, "dg-ok", "good:v1", env={"API_SECRET": "s3cr3t-value-xyz"})
    iid = q("select id from instances where app='dg-ok' and status='running'", one=True)["id"]
    monkeypatch.setattr(fake, "logs", lambda i, tail=200, **k: "Listening on port 8080\nERROR boom using s3cr3t-value-xyz\n", raising=False)
    before = _snapshot("dg-ok")
    r = user.get("/v1/apps/dg-ok/diagnose"); assert r.status_code == 200, r.text; d = r.json()
    assert _snapshot("dg-ok") == before and d["read_only"] is True
    assert "s3cr3t-value-xyz" not in r.text and any("ERROR boom" in e["line"] for e in d["error_log_lines"])
    assert d["instances"][0]["id"] == iid and d["deploys"] and not [c for c in d["likely_causes"] if "Port mismatch" in c["cause"]]
    assert user.post("/v1/apps/dg-ok/diagnose").status_code == 405

def test_rules(fake, user, monkeypatch):
    deploy(user, "dg-bad", "good:v1")
    monkeypatch.setattr(fake, "logs", lambda i, tail=200, **k: "Server listening on :3000\nFATAL out of memory\n", raising=False)
    monkeypatch.setattr(fake, "exit_info", lambda i: {"exit_code": 137, "oom_killed": True}, raising=False)
    for k in range(3): event("dg-bad", f"health: replacing ct{k} (container exited)")
    d = user.get("/v1/apps/dg-bad/diagnose").json(); causes = " | ".join(c["cause"] for c in d["likely_causes"])
    assert "Out of memory" in causes and "Crash loop" in causes and "Port mismatch: app listens on 3000" in causes and "Bad release" in causes
    assert all(c["fix"] for c in d["likely_causes"])
    q("update apps set status='suspended' where name='dg-bad'")
    assert "Suspended" in user.get("/v1/apps/dg-bad/diagnose").json()["likely_causes"][0]["cause"]

def test_scaled_to_zero_and_ownership(fake, user):
    deploy(user, "dg-idle", "good:v1", autoscale={"min": 0, "max": 1})
    q("update instances set status='stopped' where app='dg-idle'"); q("update apps set status='idle' where name='dg-idle'")
    d = user.get("/v1/apps/dg-idle/diagnose").json(); assert any("Scaled to zero" in c["cause"] for c in d["likely_causes"])
    from fastapi.testclient import TestClient
    o = TestClient(api.app, base_url="http://api.aloof.test"); e = f"o{time.time_ns()}@example.com"
    o.headers["Authorization"] = "Bearer " + o.post("/v1/signup", json={"email": e}).json()["api_key"]
    assert o.get("/v1/apps/dg-idle/diagnose").status_code == 404
