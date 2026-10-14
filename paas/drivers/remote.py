"""Remote node driver + scheduler. Instances run on nodes (VMs/GPU boxes) via the node agent over WireGuard.
Placement: GPU plans -> a dedicated node per instance; CPU plans -> bin-packed (first fit on the fullest node with room,
2x vCPU overcommit, 90% memory). If nothing fits and the pool's provisioner has credentials, a node is provisioned
and we wait for its agent to register. Volumes pin an app to the node that holds the data."""
import os, time, secrets, threading, logging, httpx, subprocess
from .base import Driver, Instance
from .providers import PROVISIONERS, ProvisionError
from . import bootstrap, wg
from ..plans import PLANS
log = logging.getLogger("paas.remote")
CPU_OVERCOMMIT = float(os.environ.get("PAAS_CPU_OVERCOMMIT", "2.0"))
READY_TIMEOUT = int(os.environ.get("PAAS_NODE_READY_TIMEOUT", "900"))
_pool_locks = {}
def _db():
    from .. import db, crypto; return db, crypto
def agent(node, method, path, **kw):
    db, crypto = _db()
    tok = crypto.dec(node["agent_token_enc"])
    with httpx.Client(timeout=kw.pop("timeout", 120)) as c:
        r = c.request(method, node["agent_url"].rstrip("/") + path, headers={"Authorization": f"Bearer {tok}"}, **kw)
    if r.status_code >= 400: raise RuntimeError(f"agent {node['id']} {method} {path}: {r.status_code} {r.text[:200]}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
def create_node_record(pool, plan_id, gpu=False, region=None):
    """Create a pending node + single-use join token (used by provisioners and the admin 'add node' flow).
    region: canonical region id the node sits in (default: the control plane's region)."""
    db, crypto = _db(); from .. import regions as R; region = R.canonical(region or R.home())
    node_id = "n-" + secrets.token_hex(4); join = "jt_" + secrets.token_urlsafe(24)
    used = [r["wg_ip"] for r in db.q("select wg_ip from nodes where wg_ip is not null and status!='destroyed'")]
    ip = wg.allocate_ip(used)
    db.q("insert into nodes(id,provider,status,wg_ip,plan,created,gpus,region) values(?,?,?,?,?,?,?,?)", (node_id, pool, "provisioning", ip, plan_id, time.time(), 1 if gpu else 0, region))
    db.kv_put("join", crypto.sha256(join), node_id, ttl=3600 * 24)
    return node_id, join
def register_node(body):
    """Called by the agent at first boot. Validates and burns the join token, returns agent token + WG config."""
    db, crypto = _db()
    node_id = db.kv_get("join", crypto.sha256(body.get("join_token", "")))
    if not node_id: raise PermissionError("invalid or used join token")
    db.kv_del("join", crypto.sha256(body["join_token"]))
    n = db.q("select * from nodes where id=?", (node_id,), one=True)
    tok = "at_" + secrets.token_urlsafe(32)
    agent_url = body.get("agent_url") if (body.get("agent_url") and os.environ.get("PAAS_ALLOW_DIRECT_AGENT") == "1") else f"http://{n['wg_ip']}:{int(body.get('agent_port', 7070))}"
    db.q("update nodes set status='ready', wg_pubkey=?, agent_url=?, agent_token_enc=?, vcpu=?, mem_mb=?, gpus=?, gpu_model=?, last_seen=?, public_ip=coalesce(public_ip, ?) where id=?",
         (body.get("wg_pubkey"), agent_url, crypto.enc(tok), body.get("vcpu"), body.get("mem_mb"), body.get("gpus") or 0, body.get("gpu_model"), time.time(), body.get("public_ip"), node_id))
    peer = wg.add_peer(body.get("wg_pubkey"), n["wg_ip"])
    db.event(f"node:{node_id}", f"registered vcpu={body.get('vcpu')} mem={body.get('mem_mb')}MB gpus={body.get('gpus')} wg_peer={'added' if peer else 'not-applied'}")
    return {"node_id": node_id, "agent_token": tok, "region": n.get("region"),
            "wg": {"address": f"{n['wg_ip']}/{wg.NET.prefixlen}", "hub_pubkey": wg.hub_pubkey() or "HUB_PUBKEY_NOT_CONFIGURED",
                   "hub_endpoint": wg.hub_endpoint(), "allowed_ips": str(wg.NET)}}
def provision_regions():
    """Regions where the scheduler may auto-provision (create paid servers). Default: only the control plane's region, so code
    never spends money in a region nobody has opened. Set PAAS_PROVISION_REGIONS=nbg1,fsn1 to allow more."""
    from .. import regions as R
    raw = os.environ.get("PAAS_PROVISION_REGIONS")
    return [R.canonical(x) for x in raw.split(",") if x.strip()] if raw else [R.home()]
def provision(pool, plan_id, region=None):
    db, _ = _db(); prov = PROVISIONERS[pool]; gpu = PLANS[plan_id]["kind"] == "gpu"
    from .. import regions as R; region = R.canonical(region or R.home())
    if region not in provision_regions(): raise ProvisionError(f"{pool}: auto-provisioning is not enabled in region {region}")
    if prov.regions and region not in prov.regions and "any" not in prov.regions: raise ProvisionError(f"{pool}: no capacity in region {region}")
    ok, why = prov.available()
    if not ok: raise ProvisionError(f"{pool}: {why}")
    node_id, join = create_node_record(pool, plan_id, gpu, region)
    script = bootstrap.render(node_id, pool, plan_id, join, gpu)
    try:
        import inspect
        ref = prov.create(node_id, plan_id, script, gpu, **({"region": region} if "region" in inspect.signature(prov.create).parameters else {}))
    except Exception as e:
        db.q("update nodes set status='failed', error=? where id=?", (str(e)[:500], node_id)); raise
    db.q("update nodes set provider_ref=? where id=?", (ref, node_id)); db.event(f"node:{node_id}", f"provisioning on {pool} ({prov.node_size(plan_id)}) ref={ref}")
    return node_id
def wait_ready(node_id, timeout=READY_TIMEOUT):
    db, _ = _db(); end = time.time() + timeout; last_poll = 0
    while time.time() < end:
        n = db.q("select * from nodes where id=?", (node_id,), one=True)
        if n["status"] == "ready":   # registered; the agent service starts right after -> wait until it answers
            try: agent(n, "GET", "/v1/health", timeout=5); return n
            except Exception: time.sleep(1); continue
        if n["status"] in ("failed", "destroyed"): raise ProvisionError(f"node {node_id} {n['status']}: {n.get('error')}")
        if n["provider_ref"] and time.time() - last_poll > 15:
            last_poll = time.time()
            try:
                st = PROVISIONERS[n["provider"]].status(n["provider_ref"])
                if st.get("public_ip"): db.q("update nodes set public_ip=? where id=?", (st["public_ip"], node_id))
                if st["state"] in ("failed", "gone"):
                    db.q("update nodes set status='failed', error=? where id=?", (f"provider reports {st}", node_id)); continue
            except Exception as e: log.warning("status poll %s: %s", node_id, e)
        time.sleep(1)
    db.q("update nodes set status='failed', error='agent did not register in time' where id=?", (node_id,))
    destroy_node(node_id, "bootstrap timeout"); raise ProvisionError(f"node {node_id} not ready after {timeout}s")
def destroy_node(node_id, why):
    db, _ = _db(); n = db.q("select * from nodes where id=?", (node_id,), one=True)
    if not n or n["status"] == "destroyed": return
    try:
        if n["provider_ref"]: PROVISIONERS[n["provider"]].destroy(n["provider_ref"])
    finally:
        wg.remove_peer(n.get("wg_pubkey")); db.q("update nodes set status='destroyed' where id=?", (node_id,)); db.event(f"node:{node_id}", f"destroyed ({why})")
def node_load(node_id):
    db, _ = _db()
    rows = db.q("select a.spec from instances i join apps a on a.name=i.app where i.node_id=? and i.status in ('running','starting')", (node_id,))
    from ..crypto import load_spec
    plans = [PLANS[load_spec(r["spec"])["plan"]] for r in rows]
    return len(plans), sum(p.get("cpu_reserve", p["vcpu"]) for p in plans), sum(p["memory_mb"] for p in plans)
def place(pool, plan_id, app, spec):
    """Pick a node for one instance: same pool, same region as the app (never another region), volume-pinned node if any."""
    db, _ = _db(); plan = PLANS[plan_id]; gpu = plan["kind"] == "gpu"
    from .. import regions as R; region = R.canonical(spec.get("region"))
    vols = [v["name"] for v in spec.get("volumes") or []]
    pinned = {r["node_id"] for r in db.q("select node_id from volumes where node_id is not null and name in (%s)" % ",".join("?" * len(vols)), tuple(vols))} if vols else set()
    if len(pinned) > 1: raise ProvisionError("app's volumes live on different nodes")
    with _pool_locks.setdefault(pool, threading.Lock()):
        best = None
        for n in db.q("select * from nodes where provider=? and status='ready' order by created", (pool,)):
            if R.canonical(n.get("region")) != region: continue
            if pinned and n["id"] not in pinned: continue
            cnt, cpu, mem = node_load(n["id"])
            if gpu:
                if (n["gpus"] or 0) < 1 or cnt > 0: continue
            else:
                if (n["gpus"] or 0) > 0 and not os.environ.get("PAAS_ALLOW_CPU_ON_GPU"): continue
                if cpu + plan.get("cpu_reserve", plan["vcpu"]) > (n["vcpu"] or 1) * CPU_OVERCOMMIT or mem + plan["memory_mb"] > (n["mem_mb"] or 1024) * 0.9: continue
            if best is None or cpu > best[1]: best = (n, cpu)
        if best: return best[0]
        if pinned: raise ProvisionError(f"volume node {pinned.pop()} has no capacity or is offline")
        node_id = provision(pool, plan_id, region)
    return wait_ready(node_id)
def _nets(spec):
    """Private networks on the node: <app>.internal resolves between one account's apps placed on the same node."""
    al = spec.get("net_aliases") or {}
    return [{"name": n, "aliases": al.get(n) or ([spec["net_alias"]] if spec.get("net_alias") else [])} for n in spec.get("networks") or []]
class RemoteDriver(Driver):
    """Driver for one node pool (provider). App.provider == pool name."""
    def __init__(self, pool):
        self.name = pool; self.prov = PROVISIONERS[pool]; self.supports_gpu = self.prov.supports_gpu; self.regions = self.prov.regions
    def available(self, region=None):
        db, _ = _db(); from .. import regions as R
        if region is None:
            if db.q("select 1 from nodes where provider=? and status='ready'", (self.name,), one=True): return True, "ready nodes in pool"
            return self.prov.available()
        region = R.canonical(region)
        if any(R.canonical(n.get("region")) == region for n in db.q("select region from nodes where provider=? and status='ready'", (self.name,))):
            return True, f"ready nodes in {region}"
        if region not in provision_regions(): return False, f"no ready node in {region} and auto-provisioning is off there"
        if self.prov.regions and region not in self.prov.regions and "any" not in self.prov.regions: return False, f"pool has no {region} location"
        return self.prov.available()
    def build(self, app, source, log_):
        """Build/pull on the control plane, then push to the platform registry that nodes pull from over WireGuard."""
        from .local_docker import LocalDockerDriver
        reg = os.environ.get("PAAS_REGISTRY", "127.0.0.1:5000")
        img = LocalDockerDriver().build(app, source, log_)
        # Always mirror into the platform registry: nodes then never depend on Docker Hub (rate limits) or a public
        # route to the image, and a deploy is pinned to exactly the bytes we built/pulled.
        tag = f"{reg}/{app}:{int(time.time())}"
        for cmd in (["docker", "tag", img, tag], ["docker", "push", tag]):
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode: raise RuntimeError(f"registry push failed: {p.stderr[-300:]}")
        log_(f"pushed {tag}")
        node_reg = os.environ.get("PAAS_REGISTRY_NODE")   # registry as nodes see it (e.g. 10.88.0.1:5000 over WireGuard)
        return tag.replace(reg, node_reg, 1) if node_reg else tag
    def start_instance(self, app, image, spec, index):
        db, _ = _db(); plan = PLANS[spec["plan"]]; node = place(self.name, spec["plan"], app, spec)
        body = {"name": f"aloof-{app}-{index}", "image": image, "env": spec.get("env", {}), "port": int(spec.get("port", 8080)),
                "cpus": plan["vcpu"], "mem_mb": plan["memory_mb"], "gpu": 1 if plan["kind"] == "gpu" else 0,
                "volumes": spec.get("volumes") or [], "command": spec.get("command"), "labels": {"aloof.app": app}, "networks": _nets(spec)}
        r = agent(node, "POST", "/v1/containers", json=body, timeout=900)
        for v in spec.get("volumes") or []: db.q("update volumes set node_id=? where name=? and node_id is null", (node["id"], v["name"]))
        return Instance(id=f"{node['id']}.{r['id']}", host=r.get("host") or node["wg_ip"], port=int(r["host_port"]))
    def run_once(self, app, image, spec, command, timeout=900):
        plan = PLANS[spec["plan"]]; node = place(self.name, spec["plan"], app, spec)
        r = agent(node, "POST", "/v1/run", timeout=timeout + 120, json={"image": image, "env": spec.get("env", {}), "command": command, "port": int(spec.get("port", 8080)),
                  "cpus": plan["vcpu"], "mem_mb": plan["memory_mb"], "volumes": spec.get("volumes") or [], "networks": _nets(spec), "timeout": timeout})
        return int(r["exit_code"]), r.get("output", "")
    def _split(self, iid):
        db, _ = _db(); nid, cid = iid.split(".", 1); return db.q("select * from nodes where id=?", (nid,), one=True), cid
    def stop_instance(self, iid):
        n, cid = self._split(iid)
        if n and n["status"] == "ready":
            try: agent(n, "DELETE", f"/v1/containers/{cid}")
            except Exception as e: log.warning("stop %s: %s", iid, e)
    def logs(self, iid, tail=200):
        n, cid = self._split(iid)
        try: return agent(n, "GET", f"/v1/containers/{cid}/logs", params={"tail": tail})
        except Exception as e: return f"(logs unavailable: {e})\n"
    def inspect(self, iid):
        n, cid = self._split(iid)
        if not n or n["status"] != "ready": return "gone"
        try: return agent(n, "GET", f"/v1/containers/{cid}", timeout=10)["status"]
        except Exception: return "unreachable"
    def endpoint(self, iid):
        n, cid = self._split(iid)
        try:
            r = agent(n, "GET", f"/v1/containers/{cid}", timeout=10)
            return (r.get("host") or n["wg_ip"], int(r["host_port"])) if r.get("host_port") else None
        except Exception: return None
    def cpu_percent(self, iid):
        n, cid = self._split(iid)
        try:
            db, _ = _db(); from ..crypto import load_spec
            a = db.q("select a.spec from instances i join apps a on a.name=i.app where i.id=?", (iid,), one=True)
            vcpu = PLANS[load_spec(a["spec"])["plan"]]["vcpu"] if a else 1
            return agent(n, "GET", f"/v1/containers/{cid}/stats", timeout=15)["cpu_percent"] / vcpu   # docker % is per core
        except Exception: return 0.0
    def create_volume(self, name, size_gb): pass   # created lazily on the node that first mounts it
    def delete_volume(self, name):
        db, _ = _db(); v = db.q("select node_id from volumes where name=?", (name,), one=True)
        n = v and v["node_id"] and db.q("select * from nodes where id=?", (v["node_id"],), one=True)
        if n and n["status"] == "ready": agent(n, "DELETE", "/v1/volumes", params={"name": name})
def node_tick():
    """Heartbeat ready nodes; GC idle auto-provisioned nodes (GPU 10 min, CPU 30 min by default)."""
    db, _ = _db(); now = time.time()
    for n in db.q("select * from nodes where status in ('ready','lost')"):
        try:
            h = agent(n, "GET", "/v1/health", timeout=8)
            db.q("update nodes set last_seen=?, status='ready' where id=?", (now, n["id"]))
        except Exception:
            if now - (n["last_seen"] or now) > 300 and n["status"] == "ready":
                db.q("update nodes set status='lost' where id=?", (n["id"],)); db.event(f"node:{n['id']}", "lost (no heartbeat 5 min)")
            continue
        if n["provider"] == "self-hosted": continue
        cnt, _, _ = node_load(n["id"])
        if cnt: db.kv_del("idle", n["id"]); continue
        since = float(db.kv_get("idle", n["id"]) or now)
        if not db.kv_get("idle", n["id"]): db.kv_put("idle", n["id"], str(now))
        ttl = int(os.environ.get("PAAS_GPU_NODE_IDLE_TTL" if (n["gpus"] or 0) > 0 else "PAAS_CPU_NODE_IDLE_TTL", "600" if (n["gpus"] or 0) > 0 else "1800"))
        if now - since >= ttl: destroy_node(n["id"], f"idle {int(now - since)}s"); db.kv_del("idle", n["id"])


def _edge_nodes():
    from .. import regions as RG
    db, _ = _db(); q = db.q; out = []
    for n in q("select id,provider,status,wg_ip,wg_pubkey,public_ip,region,last_seen from nodes where status!='destroyed' and wg_ip is not null"):
        n = dict(n); n["region"] = n.get("region") or RG.DEFAULT
        out.append(n)
    return out


def edge_wireguard_config():
    """Render (never apply) a wg-quick hub config, peers grouped by region. No tunnels are created."""
    nodes = _edge_nodes()
    lines = ["# Aloof edge WireGuard hub config -- GENERATED, NOT APPLIED", "[Interface]",
             f"Address = {wg.NET[1]}/{wg.NET.prefixlen}", "ListenPort = 51820", "PrivateKey = <HUB_PRIVATE_KEY>", ""]
    for reg in sorted({n["region"] for n in nodes}):
        lines.append(f"# region {reg}")
        for n in [x for x in nodes if x["region"] == reg]:
            lines += ["[Peer]", f"# node {n['id']} ({n['status']})", f"PublicKey = {n.get('wg_pubkey') or '<PENDING_REGISTRATION>'}",
                      f"AllowedIPs = {n['wg_ip']}/32"] + ([f"Endpoint = {n['public_ip']}:51820"] if n.get("public_ip") else []) + ["PersistentKeepalive = 25", ""]
    return "\n".join(lines) + "\n"


def edge_routes():
    from .. import regions as RG
    q = _db()[0].q; nodes = _edge_nodes(); routes = {}
    for a in q("select name,region,provider from apps"):
        a = dict(a); r = a.get("region") or RG.DEFAULT
        routes[a["name"]] = {"region": r, "provider": a.get("provider"),
                             "upstreams": [n["wg_ip"] for n in nodes if n["region"] == r and n["status"] == "ready"]}
    return {"generated_only": True, "regions": sorted({n["region"] for n in nodes}), "routes": routes}
