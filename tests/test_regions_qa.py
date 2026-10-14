"""Regression tests for QA findings on feature/multi-region (H1, M1-M3, L1-L6, WG apply switch)."""
import base64, os, time, pytest
from fastapi.testclient import TestClient
from paas import api, engine, parity, regions as RG
from paas.db import q, kv_get
from paas.drivers import remote, wg
from paas.crypto import sha256

ADM = {"Authorization": f"Bearer {api.ADMIN}"}
PUB = base64.b64encode(bytes(range(32))).decode()
def C(): return TestClient(api.app, base_url="http://api.aloof.test")
def _client(email):
    c = C(); k = c.post("/v1/signup", json={"email": email}).json()["api_key"]; c.headers["Authorization"] = f"Bearer {k}"; return c
def _join(region="nbg1"):
    r = C().post("/v1/admin/nodes", json={"pool": "self-hosted", "plan": "cpu-large", "region": region}, headers=ADM); assert r.status_code == 200, r.text
    return r.json()
@pytest.fixture(autouse=True)
def _cleanup():
    yield
    q("delete from nodes where provider='self-hosted'"); q("delete from instances where id like 'qa-%'"); q("delete from apps where name like 'qa-%'")

# ---- H1: WireGuard config injection ----
@pytest.mark.parametrize("bad", [PUB + "\nAllowedIPs = 0.0.0.0/0", "ATTACKER=", base64.b64encode(b"x" * 31).decode(), PUB[:-1] + " ", "", None, 5])
def test_register_rejects_bad_pubkey(bad):
    j = _join(); r = C().post("/v1/nodes/register", json={"join_token": j["join_token"], "wg_pubkey": bad})
    assert r.status_code == 400 and kv_get("join", sha256(j["join_token"])) == j["node_id"]   # token not burned

@pytest.mark.parametrize("ip", ["169.254.169.254", "10.0.0.5", "127.0.0.1", "192.168.1.1", "::1", "fe80::1", "0.0.0.0", "224.0.0.1",
                                "1.2.3.4\nEndpoint = 169.254.169.254", "100.64.0.1", "fd00::1", "not-an-ip"])
def test_register_rejects_non_global_ip(ip):
    j = _join(); r = C().post("/v1/nodes/register", json={"join_token": j["join_token"], "wg_pubkey": PUB, "public_ip": ip})
    assert r.status_code == 400, ip

def test_register_ok_and_render_safe(monkeypatch):
    monkeypatch.setenv("PAAS_WG_APPLY", "0")
    j = _join("nbg1"); r = C().post("/v1/nodes/register", json={"join_token": j["join_token"], "wg_pubkey": PUB, "public_ip": "8.8.8.8", "vcpu": 4, "mem_mb": 8192})
    assert r.status_code == 200, r.text
    # tamper with stored rows the way an old/buggy writer could: renderer must still not emit injected lines
    q("update nodes set wg_pubkey=?, public_ip=? where id=?", ("X=\n[Peer]\nPublicKey = ATTACKER=\nAllowedIPs = 0.0.0.0/0", "169.254.169.254", j["node_id"]))
    cfg = remote.edge_wireguard_config()
    assert "ATTACKER" not in cfg and "0.0.0.0/0" not in cfg and "169.254.169.254" not in cfg and "<PENDING_REGISTRATION>" in cfg
    q("update nodes set wg_ip='0.0.0.0' where id=?", (j["node_id"],))   # AllowedIPs only from an address inside the WG net
    assert j["node_id"] not in remote.edge_wireguard_config()
    for line in remote.edge_wireguard_config().splitlines():
        if line.startswith("AllowedIPs"): assert line.endswith("/32")

# ---- L1: validate before burning the token ----
@pytest.mark.parametrize("extra", [{"agent_port": "abc"}, {"agent_port": 70000}, {"vcpu": "lots"}, {"mem_mb": -1}, {"gpus": True}, {"gpu_model": "a\nb"}])
def test_register_payload_validated_first(extra):
    j = _join(); r = C().post("/v1/nodes/register", json={"join_token": j["join_token"], "wg_pubkey": PUB, **extra})
    assert r.status_code == 400 and kv_get("join", sha256(j["join_token"])) == j["node_id"]
    assert C().post("/v1/nodes/register", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400

# ---- L2 / L3 / L4 ----
def test_bootstrap_token_bound_to_node():
    a, b = _join(), _join()
    assert C().get(f"/v1/nodes/bootstrap.sh?node={a['node_id']}&token={a['join_token']}").status_code == 200
    assert C().get(f"/v1/nodes/bootstrap.sh?node={b['node_id']}&token={a['join_token']}").status_code == 403

def test_admin_requires_bearer_prefix():
    assert C().get("/v1/admin/nodes", headers={"Authorization": api.ADMIN}).status_code == 401
    assert C().get("/v1/admin/nodes", headers=ADM).status_code == 200

def test_add_node_validates_plan():
    for bad in ("../../etc", "x" * 5000, "nope"):
        assert C().post("/v1/admin/nodes", json={"pool": "self-hosted", "plan": bad}, headers=ADM).status_code == 400

# ---- L5 ----
def test_plans_regions_only_deployable(monkeypatch):
    from paas import plans
    monkeypatch.setitem(plans.REGIONS, "in-del", "Delhi")
    regs = C().get("/v1/plans").json()["regions"]; assert "in-del" not in regs
    for r in regs: RG.require_live(r)

# ---- M1: region plan rules on storage ----
def test_free_storage_eu_only(monkeypatch):
    nid = "qa-node-use"; q("insert into nodes(id,provider,status,wg_ip,created,gpus,region,last_seen) values(?,?,?,?,?,?,?,?)",
                           (nid, "self-hosted", "ready", "10.88.9.9", time.time(), 0, "us-east", time.time()))
    c = _client("qa-free-vol@example.in")
    r = c.post("/v1/volumes", json={"name": "qa-vol-us", "region": "us-east"}); assert r.status_code == 400 and "EU-only" in r.text
    r = c.post("/v1/pg", json={"name": "qa-pg-us", "region": "us-east"}); assert r.status_code == 400 and "EU-only" in r.text
    assert not q("select 1 from volumes where name in ('qa-vol-us','pgdata-qa-pg-us')", one=True)

# ---- M2: edge routes ----
def test_edge_routes_correct():
    now = time.time()
    rows = [("qa-n1", "ready", "10.88.9.1", now), ("qa-n2", "ready", "10.88.9.2", now), ("qa-n3", "ready", "10.88.9.3", now - 3600)]
    for nid, st, ip, seen in rows:
        q("insert into nodes(id,provider,status,wg_ip,wg_pubkey,created,gpus,region,last_seen) values(?,?,?,?,?,?,?,?,?)", (nid, "self-hosted", st, ip, base64.b64encode(bytes([int(ip.split(".")[-1])] * 32)).decode(), now, 0, "nbg1", seen))
    for name, prov, status in (("qa-remote", "hetzner", "running"), ("qa-local", "local-docker", "running"), ("qa-gone", "hetzner", "destroyed"), ("pg-qa-db", "hetzner", "running")):
        q("insert into apps(name,account_id,spec,status,provider,created,region) values(?,?,?,?,?,?,?)", (name, 1, "{}", status, prov, now, "nbg1"))
    for iid, app, node in (("qa-i1", "qa-remote", "qa-n1"), ("qa-i3", "qa-remote", "qa-n3"), ("qa-i4", "pg-qa-db", "qa-n2"), ("qa-i5", "qa-local", "qa-n2")):
        q("insert into instances(id,app,provider,status,node_id) values(?,?,?,?,?)", (iid, app, "x", "running", node))
    rt = remote.edge_routes()["routes"]
    assert "qa-gone" not in rt
    assert rt["qa-remote"]["upstreams"] == ["10.88.9.1"]          # own node only; stale qa-n3 skipped; qa-n2 not included
    assert rt["qa-local"]["upstreams"] == [] and rt["pg-qa-db"]["upstreams"] == []
    q("delete from apps where name='pg-qa-db'")

# ---- M3: fly.toml region codes ----
def test_fly_regions_dont_400():
    from paas import appconfig
    spec, _, w = appconfig.to_spec({"app": "qa-fly", "primary_region": "iad"}); assert "region" not in spec and any("iad" in x for x in w)
    spec, _, w = appconfig.to_spec({"app": "qa-fly", "primary_region": "eu-de", "regions": ["ams"]}); assert spec["region"] == "nbg1"

# ---- WG apply switch ----
def test_wg_apply_switch(monkeypatch):
    calls = []; monkeypatch.setattr(wg, "_run", lambda *a: calls.append(a) or type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.delenv("PAAS_WG_APPLY", raising=False); monkeypatch.delenv("PAAS_ENV_FILE", raising=False)
    assert not wg.apply_enabled() and wg.add_peer(PUB, "10.88.0.9") is False and calls == []
    monkeypatch.setenv("PAAS_ENV_FILE", "/x/golive.env"); assert wg.apply_enabled()
    monkeypatch.setenv("PAAS_WG_APPLY", "0"); assert not wg.apply_enabled()
    monkeypatch.setenv("PAAS_WG_APPLY", "1"); assert wg.add_peer(PUB, "10.88.0.9") is True and calls
    assert wg.add_peer("bad\nkey", "10.88.0.9") is False

# ---- N1: one WG key per live node ----
def test_duplicate_pubkey_409(monkeypatch):
    monkeypatch.setenv("PAAS_WG_APPLY", "0")
    a, b = _join(), _join()
    assert C().post("/v1/nodes/register", json={"join_token": a["join_token"], "wg_pubkey": PUB}).status_code == 200
    r = C().post("/v1/nodes/register", json={"join_token": b["join_token"], "wg_pubkey": PUB})
    assert r.status_code == 409 and kv_get("join", sha256(b["join_token"])) == b["node_id"]   # token kept
    with pytest.raises(Exception):   # partial unique index backs it up at the DB level
        q("update nodes set wg_pubkey=?, status='ready' where id=?", (PUB, b["node_id"]))
    q("update nodes set status='destroyed' where id=?", (a["node_id"],))   # a destroyed node frees its key
    assert C().post("/v1/nodes/register", json={"join_token": b["join_token"], "wg_pubkey": PUB}).status_code == 200

# ---- N2: direct agent URLs ----
PUB2 = base64.b64encode(bytes(range(1, 33))).decode()
@pytest.mark.parametrize("url", ["http://169.254.169.254/", "http://127.0.0.1:7070", "http://10.0.0.1", "ftp://8.8.8.8/", "http://localhost:7070",
                                 "http://user:pw@8.8.8.8/", "http://8.8.8.8:99999/", "http://[::1]/", "http://8.8.8.8/\n", 5])
def test_direct_agent_url_rejected(monkeypatch, url):
    monkeypatch.setenv("PAAS_ALLOW_DIRECT_AGENT", "1")
    j = _join(); r = C().post("/v1/nodes/register", json={"join_token": j["join_token"], "wg_pubkey": PUB2, "agent_url": url})
    assert r.status_code == 400, url

def test_direct_agent_url_ok_and_ignored_without_flag(monkeypatch):
    monkeypatch.setenv("PAAS_WG_APPLY", "0")
    assert remote.check_agent_url("https://8.8.8.8:7070/") == "https://8.8.8.8:7070/"
    j = _join(); r = C().post("/v1/nodes/register", json={"join_token": j["join_token"], "wg_pubkey": PUB2, "agent_url": "http://169.254.169.254/"})
    assert r.status_code == 200 and "169.254" not in q("select agent_url from nodes where id=?", (j["node_id"],), one=True)["agent_url"]

def test_direct_agent_refused_in_prod(monkeypatch):
    monkeypatch.setenv("PAAS_ALLOW_DIRECT_AGENT", "1"); monkeypatch.delenv("PAAS_ENV_FILE", raising=False); remote.direct_agent_guard()
    monkeypatch.setenv("PAAS_ENV_FILE", "/x/golive.env")
    with pytest.raises(RuntimeError): remote.direct_agent_guard()
    monkeypatch.setenv("PAAS_ALLOW_DIRECT_AGENT_PROD", "1"); remote.direct_agent_guard()

# ---- N3 ----
def test_delete_unknown_node_404():
    assert C().delete("/v1/admin/nodes/n-doesnotexist", headers=ADM).status_code == 404
    j = _join(); assert C().delete(f"/v1/admin/nodes/{j['node_id']}", headers=ADM).status_code == 200
