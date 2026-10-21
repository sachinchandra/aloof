import time, pytest
from fastapi.testclient import TestClient
from paas import api, engine, regions as RG, appconfig
from paas.db import q
from paas.drivers import remote

def _client(email):
    c = TestClient(api.app, base_url="http://api.aloof.test")
    k = c.post("/v1/signup", json={"email": email}).json()["api_key"]; c.headers["Authorization"] = f"Bearer {k}"; return c

@pytest.fixture
def node():
    made = []
    def mk(region, nid=None, ready=True, seen=0):
        nid = nid or f"n-{region}-{len(made)}-{int(time.time()*1000)%100000}"
        q("insert into nodes(id,provider,status,wg_ip,wg_pubkey,public_ip,plan,created,gpus,region,last_seen) values(?,?,?,?,?,?,?,?,?,?,?)",
          (nid, "hetzner", "ready" if ready else "provisioning", f"10.88.{len(made)+50}.2", f"PUB{nid}=", "203.0.113.9", "cx22", time.time(), 0, region, time.time() - seen))
        made.append(nid); return nid
    yield mk
    for n in made: q("delete from nodes where id=?", (n,))

def test_catalog_aliases():
    assert set(RG.CATALOG) == {"nbg1", "fsn1", "hel1", "us-east", "us-west", "sg"}
    for a in ("eu-de", "eu-central", "EU-DE", "", None): assert RG.canonical(a) == "nbg1"
    assert RG.canonical("ash") == "us-east" and RG.canonical("us-east") == "us-east" and RG.canonical("ap-sin") == "sg" and RG.PROVIDER["sg"] == "ovh"
    with pytest.raises(RG.RegionError): RG.canonical("mars-1")

def test_only_nbg1_live_and_api():
    st = {r["id"]: r for r in RG.status()}
    assert st["nbg1"]["live"] and all(not st[r]["live"] and st[r]["status"] == "coming_soon" for r in st if r != "nbg1")
    r = TestClient(api.app).get("/v1/regions").json()
    assert r["default"] == "nbg1" and r["live"] == ["nbg1"] and list(r["pricing"]) == ["nbg1"]
    assert list(TestClient(api.app).get("/v1/plans").json()["region_pricing"]) == ["nbg1"]
    with pytest.raises(RG.RegionError, match="not live|live"): RG.require_live("sg")

def test_health_derives_live(node):
    node("us-east", seen=10); assert "us-east" in RG.live_ids()
    node("us-west", seen=RG.NODE_STALE + 60); node("sg", ready=False)
    assert "us-west" not in RG.live_ids() and "sg" not in RG.live_ids()

def test_rejects_non_live_volume_pg():
    c = _client("rg1@example.in")
    r = c.post("/v1/volumes", json={"name": "vol-sin", "region": "sg"}); assert r.status_code == 400 and "sg" in r.text
    r = c.post("/v1/pg", json={"name": "dbash", "region": "us-east"}); assert r.status_code == 400
    r = c.post("/v1/volumes", json={"name": "vol-eu", "region": "eu-de"}); assert r.status_code == 200, r.text
    assert q("select region from volumes where name='vol-eu'", one=True)["region"] == "nbg1"
    assert any(v.get("region") == "nbg1" for v in c.get("/v1/volumes").json())

def test_set_region_rules(node):
    c = _client("rg2@example.in"); acct = q("select id from accounts where email='rg2@example.in'", one=True)["id"]
    s = {"name": "x1", "plan": "shared-1x-256mb"}; engine._set_region(s, None, acct); assert s["region"] == "nbg1"
    node("us-east", seen=5)
    with pytest.raises(RG.RegionError, match="EU-only"): engine._set_region({"name": "x2", "plan": "shared-1x-256mb", "region": "us-east"}, None, acct)
    q("update accounts set tier='hobby' where id=?", (acct,))
    s = {"name": "x3", "plan": "shared-1x-256mb", "region": "us-east"}; engine._set_region(s, None, acct); assert s["region"] == "us-east"
    with pytest.raises(RG.RegionError, match="moving"): engine._set_region({"name": "x4", "region": "us-east"}, {"status": "running", "region": "nbg1"}, acct)
    q("insert into volumes(name,account_id,size_gb,region,created) values('vco',?,1,'nbg1',?)", (acct, time.time()))
    with pytest.raises(RG.RegionError): engine._set_region({"name": "x5", "region": "us-east", "plan": "shared-1x-256mb", "volumes": [{"name": "vco"}]}, None, acct)
    q("delete from volumes where name='vco'")

def test_same_region_helper():
    RG.same_region("volume", "v", "eu-de", "a", "nbg1")
    with pytest.raises(RG.RegionError): RG.same_region("postgres", "d", "us-east", "a", "nbg1")

def test_pricing_multipliers():
    assert all(RG.multiplier(r, k) == 1.0 for r in RG.CATALOG for k in ("shared", "perf", "usage"))
    assert RG.multiplier("sg", "postgres:shared-1x-256mb") == 1.0
    p = RG.region_prices("us-east"); assert p["draft"] and p["provider"] == "ovh" and p["plans_usd_month"]["shared-1x-256mb"] == 1.99
    assert RG.region_prices("sg")["egress"]["cap_tb_month"] == 3 and RG.region_prices("nbg1")["egress"] is None
    e = RG.region_prices("nbg1"); assert not e["draft"] and e["provider"] == "hetzner"
    with pytest.raises(RG.RegionError): RG.check_allowed("sg", "free")
    RG.check_allowed("fsn1", "free"); RG.check_allowed("sg", "pro")

def test_appconfig_primary_region():
    r = appconfig.to_spec({"app": "tomlr", "primary_region": "eu-central"})
    spec = r[0] if isinstance(r, tuple) else r
    assert spec["region"] == "nbg1"

def test_edge_config(node):
    node("nbg1", nid="edge-n1"); node("us-east", nid="edge-a1")
    cfg = remote.edge_wireguard_config()
    assert "GENERATED, NOT APPLIED" in cfg and "# region us-east" in cfg and "<PENDING_REGISTRATION>" in cfg and "PrivateKey = <HUB_PRIVATE_KEY>" in cfg
    rt = remote.edge_routes(); assert rt["generated_only"] and "us-east" in rt["regions"]

def test_status_page_regions():
    d = TestClient(api.app).get("/v1/status").json(); regs = {r["id"]: r for r in d["regions"]}
    assert regs["nbg1"]["live"] and not regs["sg"]["live"]
    h = TestClient(api.app).get("/status").text; assert h.count("coming soon · not live") == 5

def test_migration_columns():
    for t in ("apps", "volumes", "pg_dbs", "nodes"): q(f"select region from {t} limit 1")
def test_dashboard_region_picker_markup():
    import os
    h = open(os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")).read()
    for s in ("id=d_region", "/v1/regions", "Coming soon", "region:rg", "id=v_region", "id=pg_region", "EU regions only", "d_err"): assert s in h, s
def test_host_registered_as_node_not_double_counted(monkeypatch):
    from paas.db import q
    monkeypatch.setenv("PAAS_EDGE_IPS", "203.0.113.9"); monkeypatch.setenv("PAAS_LOCAL_CAPACITY", "1")
    q("delete from nodes where id in ('selfnode','othernode')")
    q("insert into nodes(id,status,public_ip,wg_ip,region,last_seen) values('selfnode','ready','203.0.113.9','10.88.0.1','nbg1',?)", (time.time(),))
    base = next(r for r in RG.status() if r["id"] == "nbg1")["healthy_nodes"]
    q("insert into nodes(id,status,public_ip,wg_ip,region,last_seen) values('othernode','ready','198.51.100.7','10.88.0.5','nbg1',?)", (time.time(),))
    assert next(r for r in RG.status() if r["id"] == "nbg1")["healthy_nodes"] == base + 1
    q("delete from nodes where id in ('selfnode','othernode')")
