"""Vultr provisioner + autoscaler against a mocked Vultr API (no network, no spend)."""
import json, time, base64, httpx, pytest
from paas.db import q, kv_put
from paas.drivers import providers as P, remote, wg
from paas import autoscaler as A, regions as R

class FakeVultr:
    PLANS = {"vc2-1c-1gb": 0.007, "vc2-4c-8gb": 0.055, "vc2-1c-0.5gb-v6": 0.003}
    def __init__(self): self.inst = {}; self.creates = []; self.n = 0
    def handler(self, req):
        assert req.headers["authorization"] == "Bearer vk_test"
        p, m = req.url.path.removeprefix("/v2"), req.method
        if p == "/plans": return httpx.Response(200, json={"plans": [{"id": k, "hourly_cost": v, "monthly_cost": round(v * 730, 2)} for k, v in self.PLANS.items()]})
        if p == "/instances" and m == "POST":
            b = json.loads(req.content); self.creates.append(b); self.n += 1; iid = f"i-{self.n}"
            self.inst[iid] = {"id": iid, "label": b["label"], "tags": b["tags"], "status": "pending", "power_status": "stopped", "main_ip": "0.0.0.0", "date_created": "2026-10-14T00:00:00+00:00"}
            return httpx.Response(202, json={"instance": self.inst[iid]})
        if p == "/instances" and m == "GET":
            tag = req.url.params.get("tag"); return httpx.Response(200, json={"instances": [i for i in self.inst.values() if tag in i["tags"]], "meta": {"links": {"next": ""}}})
        if p.startswith("/instances/"):
            iid = p.split("/")[2]
            if iid not in self.inst: return httpx.Response(404, json={"error": "not found"})
            if m == "DELETE": del self.inst[iid]; return httpx.Response(204)
            return httpx.Response(200, json={"instance": self.inst[iid]})
        return httpx.Response(404)

@pytest.fixture
def fv(monkeypatch):
    f = FakeVultr(); prov = P.PROVISIONERS["vultr"]; prov._prices.clear()
    monkeypatch.setattr(prov, "http", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    monkeypatch.setenv("VULTR_API_KEY", "vk_test"); monkeypatch.setenv("PAAS_AUTOSCALE", "1")
    monkeypatch.setenv("PAAS_REGION_PROVIDER_US_EAST", "vultr"); monkeypatch.setenv("PAAS_REGION_PROVIDER_SG", "vultr")
    monkeypatch.setenv("PAAS_PROVISION_REGIONS", "nbg1,us-east,us-west,sg"); monkeypatch.setenv("PAAS_AUTOSCALE_UP_COOLDOWN", "0")
    monkeypatch.setenv("PAAS_EDGE_IPS", "203.0.113.10")
    q("update nodes set status='destroyed'"); q("delete from autoscale_events"); q("delete from kv where kind like 'as_%'")
    return f

def test_driver_create_body_status_destroy(fv):
    prov = P.PROVISIONERS["vultr"]
    ref = prov.create("n-abc", "shared-1x-512mb", "#!/bin/bash\necho hi", False, region="us-east")
    b = fv.creates[0]
    assert b["region"] == "ewr" and b["os_id"] == 2284 and b["plan"] == "vc2-4c-8gb" and b["label"] == "aloof-n-abc" and "aloof" in b["tags"]
    assert base64.b64decode(b["user_data"]).decode().startswith("#!/bin/bash")
    assert prov.create("n-abc", "shared-1x-512mb", "x", False, region="us-east") == ref and len(fv.creates) == 1   # idempotent retry
    assert prov.status(ref)["state"] == "provisioning"
    fv.inst[ref].update(status="active", power_status="running", main_ip="198.51.100.7"); s = prov.status(ref); assert s == s | {"state": "running", "public_ip": "198.51.100.7"}
    prov.destroy(ref); assert prov.status(ref)["state"] == "gone"; prov.destroy(ref)   # 404 on delete is fine
    assert prov.location("us-west") == "sjc" and prov.location("sg") == "sgp" and prov.hourly_usd("vc2-4c-8gb") == 0.055

def test_bootstrap_has_join_token_and_wireguard(fv):
    from paas.drivers import bootstrap
    s = bootstrap.render("n-x", "vultr", "shared-1x-512mb", "jt_SECRET")
    assert "JOIN_TOKEN=jt_SECRET" in s and "wg-quick@wg0" in s and "aloof-agent register" in s

def test_region_mapping_config(monkeypatch, fv):
    assert R.provider_for("us-east") == "vultr" and R.provider_for("us-west") == "ovh"
    st = {r["id"]: r for r in R.status()}; assert st["us-east"]["provider"] == "Vultr (ewr)" and st["us-west"]["provider_id"] == "ovh"
    drv = remote.RemoteDriver("vultr")
    assert drv.available(region="us-east")[0] and not drv.available(region="us-west")[0] and not drv.available(region="nbg1")[0]

def test_off_by_default(monkeypatch, fv):
    monkeypatch.delenv("PAAS_AUTOSCALE")
    with pytest.raises(P.ProvisionError, match="off"): A.scale_up("vultr", "shared-1x-512mb", "us-east")
    assert not fv.creates and q("select action from autoscale_events", one=True)["action"] == "refused"

def test_guardrails_per_region_global_and_spend(monkeypatch, fv):
    monkeypatch.setenv("PAAS_VULTR_PLAN_CPU_NODE", "vc2-1c-1gb")
    a = A.scale_up("vultr", "shared-1x-512mb", "us-east", key="k1")
    assert A.scale_up("vultr", "shared-1x-512mb", "us-east", key="k1") == a and len(fv.creates) == 1   # same key: reuse
    assert A.scale_up("vultr", "shared-1x-512mb", "us-east", key="k2") == a and len(fv.creates) == 1   # still provisioning: reuse
    q("update nodes set status='ready' where id=?", (a,))
    A.scale_up("vultr", "shared-1x-512mb", "us-east")
    q("update nodes set status='ready' where provider='vultr'")
    with pytest.raises(P.ProvisionError, match="max 2"): A.scale_up("vultr", "shared-1x-512mb", "us-east")
    monkeypatch.setenv("PAAS_AUTOSCALE_MAX_NODES", "2")
    with pytest.raises(P.ProvisionError, match="global max"): A.scale_up("vultr", "shared-1x-512mb", "sg")
    monkeypatch.setenv("PAAS_AUTOSCALE_MAX_NODES", "4"); monkeypatch.setenv("PAAS_VULTR_PLAN_CPU_NODE", "vc2-4c-8gb")
    with pytest.raises(P.ProvisionError, match="spend cap"): A.scale_up("vultr", "shared-1x-512mb", "sg")   # 2x$5.11 + $40.15 > $50
    assert len(fv.creates) == 2 and A.monthly_spend() == pytest.approx(2 * 0.007 * 730, abs=0.01)

def test_cooldown(monkeypatch, fv):
    monkeypatch.setenv("PAAS_AUTOSCALE_UP_COOLDOWN", "300"); monkeypatch.setenv("PAAS_VULTR_PLAN_CPU_NODE", "vc2-1c-1gb")
    a = A.scale_up("vultr", "shared-1x-512mb", "us-east"); q("update nodes set status='ready' where id=?", (a,))
    with pytest.raises(P.ProvisionError, match="cooldown"): A.scale_up("vultr", "shared-1x-512mb", "us-east")

def test_scale_down_drains_then_destroys_never_control_plane(monkeypatch, fv):
    monkeypatch.setenv("PAAS_VULTR_PLAN_CPU_NODE", "vc2-1c-1gb"); monkeypatch.setenv("PAAS_AUTOSCALE_DOWN_COOLDOWN", "0")
    a = A.scale_up("vultr", "shared-1x-512mb", "us-east"); q("update nodes set status='ready' where id=?", (a,))
    cp = A.scale_up("vultr", "shared-1x-512mb", "sg"); q("update nodes set status='ready', public_ip='203.0.113.10' where id=?", (cp,))   # edge IP = control plane
    monkeypatch.setattr(wg, "remove_peer", lambda k: None)
    t = time.time(); A.scale_down(t); A.scale_down(t + 29 * 60)
    assert q("select status from nodes where id=?", (a,), one=True)["status"] == "ready"
    A.scale_down(t + 31 * 60); assert q("select status from nodes where id=?", (a,), one=True)["status"] == "draining"
    A.scale_down(t + 32 * 60); assert q("select status from nodes where id=?", (a,), one=True)["status"] == "destroyed"
    assert q("select status from nodes where id=?", (cp,), one=True)["status"] == "ready" and len(fv.inst) == 1
    acts = [r["action"] for r in q("select action from autoscale_events order by id")]; assert "drain" in acts and "scale_down" in acts

def test_reconcile_orphans_and_vanished(monkeypatch, fv):
    monkeypatch.setattr(wg, "remove_peer", lambda k: None)
    prov = P.PROVISIONERS["vultr"]; orphan = prov.create("n-ghost", "shared-1x-512mb", "x", False, region="us-east")
    monkeypatch.setenv("PAAS_VULTR_PLAN_CPU_NODE", "vc2-1c-1gb")
    a = A.scale_up("vultr", "shared-1x-512mb", "us-west" if False else "sg")
    ref = q("select provider_ref from nodes where id=?", (a,), one=True)["provider_ref"]; del fv.inst[ref]
    q("update nodes set created=? where id=?", (time.time() - 600, a))
    A.reconcile(now=time.time() + 10**9)
    assert orphan not in fv.inst and q("select status from nodes where id=?", (a,), one=True)["status"] == "destroyed"
    assert {"destroy_orphan", "mark_vanished"} <= {r["action"] for r in q("select action from autoscale_events")}

def test_admin_endpoint(monkeypatch, fv):
    from fastapi.testclient import TestClient
    from paas import api
    monkeypatch.setattr(api, "ADMIN", "adm")
    c = TestClient(api.app); assert c.get("/v1/admin/autoscaler").status_code in (401, 403)
    r = c.get("/v1/admin/autoscaler", headers={"Authorization": "Bearer adm"})
    assert r.status_code == 200 and r.json()["config"]["max_per_region"] == 2 and r.json()["config"]["monthly_cap_usd"] == 50
