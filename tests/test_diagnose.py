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
    q("update apps set status='suspended' where name='dg-bad'"); diagnose._cache.clear()
    assert "Suspended" in user.get("/v1/apps/dg-bad/diagnose").json()["likely_causes"][0]["cause"]

def test_scaled_to_zero_and_ownership(fake, user):
    deploy(user, "dg-idle", "good:v1", autoscale={"min": 0, "max": 1})
    q("update instances set status='stopped' where app='dg-idle'"); q("update apps set status='idle' where name='dg-idle'")
    d = user.get("/v1/apps/dg-idle/diagnose").json(); assert any("Scaled to zero" in c["cause"] for c in d["likely_causes"])
    from fastapi.testclient import TestClient
    o = TestClient(api.app, base_url="http://api.aloof.test"); e = f"o{time.time_ns()}@example.com"
    o.headers["Authorization"] = "Bearer " + o.post("/v1/signup", json={"email": e}).json()["api_key"]
    assert o.get("/v1/apps/dg-idle/diagnose").status_code == 404

# ---- QA Oct 14 (M3): every listed leak format must be redacted ----
import base64 as _b64
QA_LEAKS = {
 "auth_hdr": ("ERROR request failed Authorization: Bearer abcdEFGH1234secretTOKEN", "abcdEFGH1234secretTOKEN"),
 "bearer_bare": ("error: upstream said Bearer abcdEFGH1234secretTOKEN invalid", "abcdEFGH1234secretTOKEN"),
 "aws": ("error boto: AKIAIOSFODNN7EXAMPLE wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY failed", "AKIAIOSFODNN7EXAMPLE|wJalrXUtnFEMI"),
 "pw_space": ("failed login: password is hunter2hunter2", "hunter2"),
 "pw_flag": ("fatal: mysql --password hunter2hunter2 refused", "hunter2"),
 "json_pw": ('error {"password": "hunter2hunter2"}', "hunter2"),
 "pw_spaces_val": ("error password = 'correct horse battery staple'", "horse|battery|staple"),
 "stripe_live": ("error sk_live_51Habcdefghijklmnop", "51Habcdefghijklmnop"),
 "openai": ("error key sk-proj-abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz"),
 "anthropic": ("error key sk-ant-api03-abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"),
 "mongo": ("error mongodb+srv://admin:Sup3rS3cret@cluster0.x.net/db", "Sup3rS3cret"),
 "redis_nouser": ("error redis://:Sup3rS3cret@cache:6379", "Sup3rS3cret"),
 "pg_query": ("error postgresql://db/x?user=u&password=Sup3rS3cret", "Sup3rS3cret"),
 "private_key": ("error -----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKCAQEA", "MIIEowIBAAKCAQEA"),
 "env_b64": ("error " + _b64.b64encode(b"envsecretvalue-123").decode(), _b64.b64encode(b"envsecretvalue-123").decode()),
 "env_url": ("error envsecret%2Dvalue", "envsecret%2Dvalue"),
 "slack": ("error xoxb-1234567890-abcdefghij", "1234567890-abcdefghij"),
 "pk_aloof": ("error pk_abcdefghijklmnop1234", "abcdefghijklmnop1234"),
 "set_cookie": ("error Cookie: session=abcdefSESSIONsecret123", "abcdefSESSIONsecret123"),
 "x_api_key": ("error x-api-key: abcdefAPIKEYsecret123", "abcdefAPIKEYsecret123"),
}
import re as _re, pytest as _pytest
@_pytest.mark.parametrize("k", sorted(QA_LEAKS))
def test_qa_redaction_matrix(k):
    r = diagnose.redactor({"S": "envsecretvalue-123", "U": "envsecret-value"})
    line, secret = QA_LEAKS[k]; out = r(line)
    assert not _re.search(secret, out), (k, out)
    assert "[redacted" in out

def test_redaction_keeps_ordinary_lines():
    r = diagnose.redactor({})
    for l in ["Listening on port 8080", "GET /api/users 500 12ms", "Traceback (most recent call last):", "ERROR connection refused to db:5432"]:
        assert r(l) == l

def test_qa_end_to_end_readonly_and_old_release_env(fake, user, monkeypatch):
    deploy(user, "qa-dg", "good:v1", env={"DATABASE_URL": "postgres://u:dbpw12345@h/d", "API_KEY": "zz-api-key-value", "OLD": "old-env-secret-1"})
    assert user.post("/v1/apps/qa-dg/env", json={"env": {"OLD": None}}).status_code == 200; time.sleep(0.5); wait(user, "qa-dg")
    monkeypatch.setattr(fake, "logs", lambda i, tail=200, **k: "\n".join(l for l, _ in QA_LEAKS.values()) + "\nerror using zz-api-key-value old-env-secret-1\n", raising=False)
    assert "OLD" not in user.get("/v1/apps/qa-dg").json()["env_keys"]
    before = _snapshot("qa-dg"); r = user.get("/v1/apps/qa-dg/diagnose"); assert r.status_code == 200
    assert _snapshot("qa-dg") == before
    for s in ["dbpw12345", "zz-api-key-value", "old-env-secret-1", "hunter2", "Sup3rS3cret", "abcdEFGH1234secretTOKEN"]: assert s not in r.text, s
    user.delete("/v1/apps/qa-dg"); time.sleep(0.3); assert user.get("/v1/apps/qa-dg/diagnose").status_code == 404
