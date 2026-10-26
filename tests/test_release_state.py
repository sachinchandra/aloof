"""requested_release vs serving_release: a failed deploy must never look like success while an older release keeps serving."""
import os, sys, time
os.environ["PAAS_STATUS_PROBES"] = "0"
from paas.db import q, event
from paas import engine
from test_deploy_progress import _user, _app, _rel
from test_parity import fake, user  # noqa: F401 (fixtures)

def test_mismatch_when_new_release_fails_and_old_serves():
    c, aid = _user(); n = f"rs{time.time_ns() % 10**8}"
    _app(aid, n, "running", {"git": "https://example.com/r.git"}); _rel(n, 4, "active", "img4")
    q("insert into instances(id,app,provider,host,port,status,started) values(?,?,?,?,?,?,?)", (f"i{n}", n, "local-docker", "127.0.0.1", 1, "running", time.time()))
    j = c.get(f"/v1/apps/{n}").json()
    assert j["requested_release"]["version"] == 4 and j["serving_release"]["version"] == 4 and not j["mismatch"] and j["warning"] is None and j["deploy_ok"]
    assert j["health"] == {"release": 4, "state": "healthy", "instances_running": 1, "note": "health checks reflect v4"}
    _rel(n, 5, "failed"); q("update releases set error='build failed: exit 1' where app=? and version=5", (n,))
    for path in (f"/v1/apps/{n}", f"/v1/apps/{n}/status", f"/v1/apps/{n}/diagnose", f"/v1/apps/{n}/deploy"):
        j = c.get(path).json()
        assert j["requested_release"]["version"] == 5 and j["requested_release"]["status"] == "failed" and j["requested_release"]["error"] == "build failed: exit 1", path
        assert j["serving_release"]["version"] == 4 and j["health"]["release"] == 4 and j["mismatch"] is True and j["deploy_ok"] is False, path
        assert j["warning"] == "v5 failed to build; v4 is still serving. Health checks reflect v4, not your deploy.", path

def test_failed_first_deploy_nothing_serving_and_deploying_state():
    c, aid = _user(); n = f"rt{time.time_ns() % 10**8}"
    _app(aid, n, "failed", {"image": "x"}); _rel(n, 1, "failed", "img")
    j = c.get(f"/v1/apps/{n}").json(); assert j["serving_release"] is None and j["mismatch"] and "nothing is serving" in j["warning"] and j["health"]["state"] == "none"
    _rel(n, 2, "deploying")
    assert "still deploying" in c.get(f"/v1/apps/{n}").json()["warning"]

def test_engine_records_release_error_on_failed_deploy(fake, user):
    from test_parity import deploy
    deploy(user, "rs-app", "good:v1"); a = deploy(user, "rs-app", "bad:v2")
    assert a["mismatch"] and a["requested_release"]["version"] == 2 and a["requested_release"]["error"] and a["serving_release"]["version"] == 1
    assert a["warning"].startswith("v2 failed") and "v1 is still serving" in a["warning"]

def _mcp():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server")); from mcp_server import server; return server

def test_mcp_deploy_wait_reports_failure_when_old_release_serves(monkeypatch):
    s = _mcp(); seq = [{"requested_release": {"version": 4, "status": "active"}}]
    states = iter([{"requested_release": {"version": 5, "status": "deploying"}},
                   {"requested_release": {"version": 5, "status": "failed", "error": "build failed"}, "serving_release": {"version": 4, "status": "active"},
                    "health": {"release": 4, "state": "healthy"}, "mismatch": True, "deploy_ok": False, "status": "running", "url": "https://x",
                    "warning": "v5 failed to build; v4 is still serving. Health checks reflect v4, not your deploy."}])
    def fake_r(method, path, key=None, **kw):
        if method == "POST": return {"name": "x", "status": "queued"}
        return seq.pop() if seq else next(states)
    monkeypatch.setattr(s, "_r", fake_r); monkeypatch.setattr("time.sleep", lambda x: None)
    r = s.deploy_app("x", image="bad:1", wait=True)
    assert r["ok"] is False and r["result"] == "failed" and r["mismatch"] and r["serving_release"]["version"] == 4 and "not your deploy" in r["warning"] and r["error"] == "build failed"

def test_cli_wait_flags_mismatch_as_failure():
    cli = open(os.path.join(os.path.dirname(__file__), "..", "cli", "aloof")).read()
    assert 'if r.get("mismatch") or r.get("deploy_failed"):' in cli and 'r["status"] = "failed"' in cli
