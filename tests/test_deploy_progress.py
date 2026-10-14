"""Deploy progress is derived only from real backend state: apps.status, the release row and platform events."""
import os, time
os.environ["PAAS_STATUS_PROBES"] = "0"
from fastapi.testclient import TestClient
from paas import api, crypto, engine
from paas.db import q, event
def _user():
    c = TestClient(api.app, base_url="http://api.aloof.test"); e = f"dp{time.time_ns()}@acme.dev"
    c.post("/v1/auth/register", json={"email": e, "password": "correct horse 1"}); c.headers["X-Aloof-CSRF"] = "1"
    return c, q("select id from accounts where email=?", (e,), one=True)["id"]
def _app(aid, name, status, src):
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", (name, aid, crypto.dump_spec({"plan": "shared-1x-256mb", "source": src}), status, "local-docker", time.time(), time.time()))
def _rel(name, v, status, image=None):
    q("insert into releases(app,version,image,spec,status,reason,created) values(?,?,?,?,?,?,?)", (name, v, image, crypto.dump_spec({}), status, "deploy", time.time()))
def test_phases_follow_real_state():
    c, aid = _user(); n = f"dp{time.time_ns() % 10**8}"
    _app(aid, n, "queued", {"git": "https://example.com/r.git"}); event(n, "deploy queued on local-docker plan=x instances=1")
    j = c.get(f"/v1/apps/{n}/deploy").json(); assert j["phase"] == "queued" and j["steps"][0]["state"] == "active" and j["steps"][1]["label"] == "Build"
    assert [s["id"] for s in j["steps"]] == ["queued", "building", "starting", "live"]   # local driver: no push step
    _rel(n, 1, "deploying"); event(n, "release v1: deploy"); q("update apps set status='building' where name=?", (n,)); event(n, "docker build")
    j = c.get(f"/v1/apps/{n}/deploy").json(); assert j["phase"] == "building" and [s["state"] for s in j["steps"]] == ["done", "active", "pending", "pending"]
    ev = c.get(f"/v1/apps/{n}/events?after={j['events'][0]['id']}").json(); assert [e["msg"] for e in ev][-1] == "docker build"
    assert c.get(f"/v1/apps/{n}/events?after={j['last_event_id']}").json() == []
    q("update apps set status='starting' where name=?", (n,)); assert c.get(f"/v1/apps/{n}/deploy").json()["phase"] == "starting"
    q("update releases set status='active' where app=? and version=1", (n,)); q("update apps set status='running' where name=?", (n,)); event(n, "deploy complete (v1 live)")
    j = c.get(f"/v1/apps/{n}/deploy").json(); assert j["phase"] == "live" and all(s["state"] == "done" for s in j["steps"]) and j["url"].startswith("https://")
def test_failed_build_has_excerpt_and_retry_guard(monkeypatch):
    c, aid = _user(); n = f"df{time.time_ns() % 10**8}"
    _app(aid, n, "queued", {"image": "nginx:alpine"}); event(n, "deploy queued on local-docker plan=x instances=1"); _rel(n, 1, "deploying"); event(n, "release v1: deploy")
    event(n, "pull access denied for nginx:nope"); q("update releases set status='failed' where app=?", (n,)); q("update apps set status='failed', error='pull failed' where name=?", (n,)); event(n, "ERROR: pull failed")
    j = c.get(f"/v1/apps/{n}/deploy").json()
    assert j["phase"] == "failed" and j["steps"][1] == {"id": "building", "label": "Pull image", "state": "failed"} and "ERROR: pull failed" in j["excerpt"] and j["error"] == "pull failed"
    called = {}
    monkeypatch.setattr(engine, "deploy", lambda acc, spec: called.update(spec=spec) or {"name": n, "status": "queued"})
    r = c.post(f"/v1/apps/{n}/retry"); assert r.status_code == 200 and called["spec"] == {"name": n, "source": {"image": "nginx:alpine"}}
    q("update apps set status='building' where name=?", (n,)); assert c.post(f"/v1/apps/{n}/retry").status_code == 409
    other, _ = _user(); assert other.get(f"/v1/apps/{n}/deploy").status_code == 404 and other.post(f"/v1/apps/{n}/retry").status_code == 404
def test_failed_with_previous_still_serving():
    c, aid = _user(); n = f"dk{time.time_ns() % 10**8}"
    _app(aid, n, "running", {"image": "nginx:alpine"}); _rel(n, 1, "active", "img1"); time.sleep(0.01)
    event(n, "deploy queued on local-docker plan=x instances=1"); _rel(n, 2, "deploying", "img2"); event(n, "release v2: deploy")
    q("update releases set status='failed' where app=? and version=2", (n,)); q("update apps set error='v2 failed: 1 new instance(s) failed readiness; still serving v1' where name=?", (n,))
    j = c.get(f"/v1/apps/{n}/deploy").json(); assert j["phase"] == "failed" and j["still_serving_previous"] and j["version"] == 2 and j["steps"][2]["state"] == "failed"
