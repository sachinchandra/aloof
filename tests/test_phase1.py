"""Phase 1 (CPU on Hetzner) + phase 2 GPU drivers behind PAAS_GPU_ENABLED. All provider HTTP is mocked."""
import os, sys, json, time, pytest, httpx, respx
from fastapi.testclient import TestClient
from paas import api, engine, plans
from paas.db import q
from paas.drivers import providers as P
from paas.drivers.runpod import RunpodDriver, RunpodError
SCRIPT = "#!/bin/bash\necho join\n"
HZ = "https://api.hetzner.cloud/v1"

@respx.mock
def test_hetzner_create_firewall_location_fallback_status_destroy(monkeypatch):
    monkeypatch.setenv("HCLOUD_TOKEN", "hz"); monkeypatch.setenv("PAAS_SSH_ALLOW_CIDR", "198.51.100.7/32")
    monkeypatch.setenv("HCLOUD_LOCATIONS", "fsn1,nbg1")   # default is nbg1 only (the one live region); fallback still works when configured
    p = P.PROVISIONERS["hetzner"]; assert p.available()[0] and p.node_size("shared-1x-1gb") == "cx43"
    respx.get(f"{HZ}/firewalls").mock(return_value=httpx.Response(200, json={"firewalls": []}))
    fw = respx.post(f"{HZ}/firewalls").mock(return_value=httpx.Response(201, json={"firewall": {"id": 7}}))
    cr = respx.post(f"{HZ}/servers").mock(side_effect=[
        httpx.Response(412, json={"error": {"code": "resource_unavailable", "message": "server type unavailable"}}),
        httpx.Response(201, json={"server": {"id": 4242, "status": "initializing"}})])
    assert p.create("n1", "shared-1x-1gb", SCRIPT, False) == "4242"
    rules = json.loads(fw.calls[0].request.content)["rules"]
    assert {r["protocol"] for r in rules} == {"icmp", "tcp"} and [r for r in rules if r["protocol"] == "tcp"][0]["source_ips"] == ["198.51.100.7/32"]
    b1, b2 = (json.loads(c.request.content) for c in cr.calls)
    assert (b1["location"], b2["location"]) == ("fsn1", "nbg1") and b2["server_type"] == "cx43" and b2["user_data"] == SCRIPT
    assert b2["firewalls"] == [{"firewall": 7}] and b2["labels"]["aloof-node"] == "n1" and cr.calls[0].request.headers["authorization"] == "Bearer hz"
    respx.get(f"{HZ}/servers/4242").mock(side_effect=[httpx.Response(200, json={"server": {"status": "starting", "public_net": {}}}),
        httpx.Response(200, json={"server": {"status": "running", "public_net": {"ipv4": {"ip": "95.216.1.2"}}}}), httpx.Response(404)])
    assert p.status("4242")["state"] == "provisioning" and p.status("4242")["public_ip"] == "95.216.1.2" and p.status("4242")["state"] == "gone"
    d = respx.delete(f"{HZ}/servers/4242").mock(return_value=httpx.Response(200, json={})); p.destroy("4242"); assert d.called
    with pytest.raises(P.ProvisionError): p.create("n2", "gpu-a100", SCRIPT, True)

@respx.mock
def test_hetzner_all_locations_full_and_cpx(monkeypatch):
    monkeypatch.setenv("HCLOUD_TOKEN", "hz")
    respx.get(f"{HZ}/firewalls").mock(return_value=httpx.Response(200, json={"firewalls": [{"id": 9}]}))
    respx.post(f"{HZ}/servers").mock(return_value=httpx.Response(412, json={"error": {"code": "resource_unavailable"}}))
    with pytest.raises(P.ProvisionError, match="not available"): P.PROVISIONERS["hetzner"].create("n1", "shared-1x-1gb", SCRIPT, False)
    assert P.PROVISIONERS["hetzner-cpx"].node_size("perf-1x-2gb") == "cpx42"

def test_margin_floor_hetzner_cx43_and_cpx_refusal():
    cpu = [k for k, v in plans.PLANS.items() if v["kind"] == "cpu" and not v["legacy"]]
    for k in cpu:
        assert plans.pool_allowed(k, "hetzner") and plans.pool_margin(k, "hetzner") >= 0.60, k   # CX43 worst case
        if plans.PLANS[k]["cpu_class"] == "shared": assert not plans.pool_allowed(k, "hetzner-cpx"), k   # CPX fallback refused
    assert plans.CPU_POOLS[:2] == ["hetzner", "hetzner-cpx"] and plans.PLANS["shared-1x-1gb"]["provider"] == "hetzner"
    assert plans.check_floor() == []

def test_scheduler_picks_hetzner_then_skips_cpx_for_shared(monkeypatch):
    class D:
        supports_gpu = False
        def __init__(self, ok): self.ok = ok
        def available(self): return self.ok, "ok" if self.ok else "missing credentials"
    monkeypatch.setattr(engine, "get_drivers", lambda: {"hetzner": D(True), "hetzner-cpx": D(True), "e2e": D(True), "local-docker": D(True)})
    assert engine.pick_provider({"plan": "shared-1x-1gb"}) == "hetzner"
    monkeypatch.setattr(engine, "get_drivers", lambda: {"hetzner": D(False), "hetzner-cpx": D(True), "e2e": D(False), "local-docker": D(True)})
    assert engine.pick_provider({"plan": "shared-1x-1gb"}) == "local-docker"     # cpx refused (under floor), not used
    assert engine.pick_provider({"plan": "perf-2x-4gb"}) == "hetzner-cpx"

def test_gpu_hidden_when_disabled_and_waitlist():
    assert not plans.GPU_ENABLED
    c = TestClient(api.app)
    p = c.get("/v1/plans").json()
    assert not [x for x in p["plans"] if x["kind"] == "gpu"] and p["gpu"]["status"] == "coming_soon" and p["gpu"]["coming_soon"]
    with pytest.raises(ValueError, match="coming soon"): engine.validate({"name": "llm1", "plan": "gpu-l4", "source": {"image": "x"}})
    r = c.post("/v1/waitlist", json={"email": "gpu@example.com", "gpu": "gpu-h100"}); assert r.status_code == 200
    assert c.post("/v1/waitlist", json={"email": "gpu@example.com", "gpu": "shared-1x-1gb"}).status_code == 400
    txt = c.get("/llms.txt").text
    assert "COMING SOON" in txt and "$5.49" not in txt and "/v1/waitlist" in txt and "India" not in txt and "Hetzner" in txt
    html = c.get("/pricing").text; assert "coming soon" in html and "id=wl" in html and "$5.49" not in html
    h = c.get("/").text; assert "compute in the eu" not in h.lower() and "Idle apps cost nothing, busy ones scale up." in h and "India" not in h
    for prov in ("hyperstack",): assert P.PROVISIONERS[prov].available()[0] is False
    assert RunpodDriver().available()[0] is False

def test_admin_metrics_paid_customers(monkeypatch):
    monkeypatch.setattr(api, "ADMIN", "adm"); c = TestClient(api.app); h = {"Authorization": "Bearer adm"}
    assert c.get("/v1/admin/metrics").status_code == 401
    base = c.get("/v1/admin/metrics", headers=h).json()["paid_customers"]
    a = engine.create_account("payer1@payer.dev"); b = engine.create_account("payer2@payer.dev")
    q("insert into stripe_sessions(id,account_id,kind,status,created,updated) values(?,?,?,?,?,?)", ("cs_m1", a["id"], "topup", "paid", 0, 0))
    q("insert into stripe_sessions(id,account_id,kind,status,created,updated) values(?,?,?,?,?,?)", ("cs_m2", a["id"], "topup", "paid", 0, 0))
    q("update accounts set stripe_subscription='sub_metrics_test' where id=?", (b["id"],))
    m = c.get("/v1/admin/metrics", headers=h).json()
    assert m["paid_customers"] == base + 2 and m["gpu_unlock_at_paid_customers"] == 10 and not m["gpu_enabled"] and "GPU unlocks at 10" in m["note"]

@respx.mock
def test_hyperstack_vm(monkeypatch):
    monkeypatch.setattr(plans, "GPU_ENABLED", True)
    for k, v in dict(HYPERSTACK_API_KEY="hk", HYPERSTACK_ENVIRONMENT="CANADA-1").items(): monkeypatch.setenv(k, v)
    p = P.PROVISIONERS["hyperstack"]; assert p.available()[0]
    b = "https://infrahub-api.nexgencloud.com/v1/core/virtual-machines"
    cr = respx.post(b).mock(return_value=httpx.Response(200, json={"status": True, "instances": [{"id": 551, "status": "CREATING"}]}))
    assert p.create("n1", "gpu-a100", SCRIPT, True) == "551"
    rq = cr.calls[0].request; body = json.loads(rq.content)
    assert rq.headers["api_key"] == "hk" and body["flavor_name"] == "n3-A100x1" and body["user_data"] == SCRIPT and body["environment_name"] == "CANADA-1"
    respx.get(f"{b}/551").mock(side_effect=[httpx.Response(200, json={"instance": {"status": "BUILD"}}), httpx.Response(200, json={"instance": {"status": "ACTIVE", "floating_ip": "38.80.1.2"}})])
    assert p.status("551")["state"] == "provisioning"; assert p.status("551") == {"state": "running", "public_ip": "38.80.1.2", "raw_status": "ACTIVE"}
    d = respx.delete(f"{b}/551").mock(return_value=httpx.Response(200, json={})); p.destroy("551"); assert d.called
    with pytest.raises(P.ProvisionError): p.create("n2", "shared-1x-1gb", SCRIPT, False)

@respx.mock
def test_runpod_pod_driver(monkeypatch):
    monkeypatch.setattr(plans, "GPU_ENABLED", True); monkeypatch.setenv("RUNPOD_API_KEY", "rk"); monkeypatch.setenv("PAAS_RUNPOD_POLL_S", "0")
    d = RunpodDriver(); assert d.available()[0]
    b = "https://rest.runpod.io/v1/pods"
    cr = respx.post(b).mock(return_value=httpx.Response(201, json={"id": "pod1", "desiredStatus": "RUNNING", "portMappings": {}}))
    respx.get(f"{b}/pod1").mock(side_effect=[httpx.Response(200, json={"id": "pod1", "desiredStatus": "RUNNING", "publicIp": None}),
        httpx.Response(200, json={"id": "pod1", "desiredStatus": "RUNNING", "publicIp": "69.30.0.5", "portMappings": {"8000": 40123}})] + [
        httpx.Response(200, json={"id": "pod1", "desiredStatus": "RUNNING", "publicIp": "69.30.0.5", "portMappings": {"8000": 40123}})] * 3)
    inst = d.start_instance("llm", "vllm/vllm-openai:latest", {"plan": "gpu-rtx4090", "port": 8000, "env": {"A": "1"}}, "1")
    assert (inst.id, inst.host, inst.port) == ("pod1:8000", "69.30.0.5", 40123)
    body = json.loads(cr.calls[0].request.content)
    assert cr.calls[0].request.headers["authorization"] == "Bearer rk" and body["gpuTypeIds"] == ["NVIDIA GeForce RTX 4090"] and body["cloudType"] == "COMMUNITY"
    assert body["ports"] == ["8000/tcp"] and body["env"] == {"A": "1", "PORT": "8000"} and body["imageName"] == "vllm/vllm-openai:latest"
    assert d.inspect("pod1:8000") == "running" and d.endpoint("pod1:8000") == ("69.30.0.5", 40123)
    dl = respx.delete(f"{b}/pod1").mock(return_value=httpx.Response(204)); d.stop_instance("pod1:8000"); assert dl.called
    assert json.loads(RunpodDriver().pod_body("x", "img", {"plan": "gpu-l4"}, "1") and json.dumps(RunpodDriver().pod_body("x", "img", {"plan": "gpu-l4"}, "1")))["cloudType"] == "SECURE"
    with pytest.raises(RunpodError): d.pod_body("x", "img", {"plan": "gpu-h100"}, "1")
    with pytest.raises(RunpodError, match="PAAS_PUBLIC_REGISTRY"): d.build("x", {"git": "https://github.com/a/b"}, print)

def test_gpu_margins_on_phase2_pools():
    for k, pool in (("gpu-a100", "hyperstack"), ("gpu-h100", "hyperstack"), ("gpu-l40", "hyperstack"), ("gpu-l4", "runpod"), ("gpu-rtx4090", "runpod")):
        assert plans.pool_allowed(k, pool), (k, pool, plans.pool_margin(k, pool))

@respx.mock
def test_hetzner_golive_script(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy")); import hetzner_golive as G
    env = tmp_path / "golive.env"; env.write_text("HCLOUD_TOKEN=tok\nPAAS_BASE_DOMAIN=aloof.si\nPAAS_EDGE_IPS=<edge server public IPv4>\nPAAS_WG_ENDPOINT=<edge public IPv4>:51820\nSTRIPE_SECRET_KEY=sk_live_x\n")
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAC3Nz test@laptop\n")
    respx.get(f"{G.API}/ssh_keys").mock(return_value=httpx.Response(200, json={"ssh_keys": []}))
    sk = respx.post(f"{G.API}/ssh_keys").mock(return_value=httpx.Response(201, json={"ssh_key": {"id": 11}}))
    respx.get(f"{G.API}/firewalls").mock(return_value=httpx.Response(200, json={"firewalls": []}))
    fw = respx.post(f"{G.API}/firewalls").mock(return_value=httpx.Response(201, json={"firewall": {"id": 22}}))
    respx.get(f"{G.API}/servers").mock(return_value=httpx.Response(200, json={"servers": []}))
    cr = respx.post(f"{G.API}/servers").mock(return_value=httpx.Response(201, json={"server": {"id": 33, "status": "initializing"}}))
    respx.get(f"{G.API}/servers/33").mock(return_value=httpx.Response(200, json={"server": {"id": 33, "status": "running",
        "public_net": {"ipv4": {"ip": "49.12.0.9"}, "ipv6": {"ip": "2a01:4f8:1::/64"}}, "datacenter": {"location": {"name": "fsn1"}}}}))
    calls = []
    class R:
        def __init__(self, rc=0): self.returncode, self.stdout, self.stderr = rc, b"", b""
    def run(cmd, **kw): calls.append((cmd, kw.get("input"))); return R()
    out = G.main(["--env", str(env), "--pubkey", str(pub)], client=httpx.Client(), run=run, sleep=0)
    assert out == {"ip": "49.12.0.9", "ipv6": "2a01:4f8:1::1", "created": True}
    body = json.loads(cr.calls[0].request.content)
    assert body["server_type"] == "cx33" and body["location"] == "fsn1" and body["firewalls"] == [{"firewall": 22}] and body["ssh_keys"] == [11]
    assert "setup-box.sh" in body["user_data"] and "sk_live" not in body["user_data"]          # secrets never in user_data
    ports = {(r["protocol"], r.get("port")) for r in json.loads(fw.calls[0].request.content)["rules"]}
    assert {("tcp", "22"), ("tcp", "80"), ("tcp", "443"), ("udp", "51820")} <= ports
    sent = [i for c, i in calls if i][0].decode(); assert "PAAS_EDGE_IPS=49.12.0.9" in sent and "PAAS_WG_ENDPOINT=49.12.0.9:51820" in sent
    assert any("golive.sh" in c[-1] for c, _ in calls)
    assert ("A", "*.aloof.si", "49.12.0.9") in G.dns_records({"PAAS_BASE_DOMAIN": "aloof.si"}, "49.12.0.9")
