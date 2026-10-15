import time, pytest
from fastapi.testclient import TestClient
from paas import api, engine, capacity
from paas.db import q
from paas.crypto import dump_spec
from paas.drivers.local_docker import LocalDockerDriver

def _client(email):
    c = TestClient(api.app, base_url="http://api.aloof.test")
    k = c.post("/v1/signup", json={"email": email}).json()["api_key"]; c.headers["Authorization"] = f"Bearer {k}"; return c

@pytest.fixture
def running(monkeypatch):
    """Insert fake running local-docker instances of a given plan; cleaned up afterwards."""
    made = []
    def mk(plan, n=1, provider="local-docker", status="running"):
        app = f"capt-{len(made)}-{int(time.time()*1000)%10**7}"
        q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)",
          (app, 1, dump_spec({"plan": plan, "name": app}), "running", provider, time.time(), time.time()))
        for i in range(n): q("insert into instances(id,app,provider,host,port,status,started) values(?,?,?,?,?,?,?)", (f"{app}-{i}", app, provider, "127.0.0.1", 1000 + i, status, time.time()))
        made.append(app)
    yield mk
    for a in made: q("delete from instances where app=?", (a,)); q("delete from apps where name=?", (a,))

def test_budget_default_and_env(monkeypatch):
    monkeypatch.delenv("PAAS_LOCAL_MEM_BUDGET_MB", raising=False); assert capacity.budget_mb() == 6144
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", "2048"); assert capacity.budget_mb() == 2048
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", "junk"); assert capacity.budget_mb() == 6144

def test_admit_sums_running_local_instances_only(monkeypatch, running):
    base = capacity.used_mb()
    running("shared-1x-1gb", 2); running("shared-1x-1gb", 3, provider="hetzner"); running("shared-1x-1gb", 1, status="stopped")
    assert capacity.used_mb() == base + 2048        # remote and non-running instances don't count
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", str(base + 2048 + 1024)); capacity.admit("shared-1x-1gb")      # exactly fits
    with pytest.raises(capacity.PlatformAtCapacity, match="platform at capacity, try again shortly"): capacity.admit("shared-1x-1gb", 2)

def test_zero_disables_and_zero_count_ok(monkeypatch, running):
    running("shared-1x-1gb", 5)
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", "0"); capacity.admit("shared-1x-1gb", 50)
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", "1"); capacity.admit("shared-1x-1gb", 0)

def test_driver_refuses_before_touching_docker(monkeypatch, running):
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", "1")
    drv = object.__new__(LocalDockerDriver)       # no docker client: the check must fire first
    with pytest.raises(capacity.PlatformAtCapacity): drv.start_instance("x", "img", {"plan": "shared-1x-256mb", "port": 80}, "1")

def test_deploy_api_503_with_retry_after_then_ok_when_room(monkeypatch, running):
    monkeypatch.setattr(engine, "_run_deploy", lambda *a, **k: None)
    monkeypatch.setattr(engine, "pick_provider", lambda spec: "local-docker")
    c = _client(f"cap-{int(time.time()*1000)}@example.com")
    body = {"name": f"capapp-{int(time.time())%100000}", "source": {"image": "nginx:alpine"}, "port": 80, "plan": "shared-1x-256mb"}
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", str(capacity.used_mb() + 128))
    r = c.post("/v1/apps", json=body)
    assert r.status_code == 503 and r.headers["Retry-After"] == str(capacity.RETRY_AFTER_S)
    assert "platform at capacity, try again shortly" in r.json()["detail"] and r.json()["retry_after"] == capacity.RETRY_AFTER_S
    assert q("select 1 from apps where name=?", (body["name"],), one=True) is None      # nothing queued
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", str(capacity.used_mb() + 256))
    assert c.post("/v1/apps", json=body).status_code == 200                              # exactly fits
    q("delete from apps where name=?", (body["name"],))

def test_remote_pool_deploy_not_limited(monkeypatch):
    monkeypatch.setattr(engine, "_run_deploy", lambda *a, **k: None)
    monkeypatch.setattr(engine, "pick_provider", lambda spec: "hetzner")
    monkeypatch.setenv("PAAS_LOCAL_MEM_BUDGET_MB", "1")
    c = _client(f"cap2-{int(time.time()*1000)}@example.com")
    n = f"caprem-{int(time.time())%100000}"
    assert c.post("/v1/apps", json={"name": n, "source": {"image": "nginx:alpine"}, "port": 80, "plan": "shared-1x-256mb"}).status_code == 200
    q("delete from apps where name=?", (n,))
