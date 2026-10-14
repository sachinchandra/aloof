"""Node autoscaler for the managed pools (default: vultr). Off unless PAAS_AUTOSCALE=1.

Scale up: the scheduler (drivers.remote.place) asks for a node when no ready node in the app's region has room for a pending
instance. Scale down: a managed node with no instances and no pinned volumes for PAAS_AUTOSCALE_IDLE_MIN minutes (default 30)
is drained (status 'draining', so nothing new is placed) and destroyed. reconcile() destroys orphaned tagged instances and
marks nodes whose instance vanished.

Guardrails (all enforced before any create): PAAS_AUTOSCALE=0 by default; max nodes per region (PAAS_AUTOSCALE_MAX_PER_REGION, 2);
global max (PAAS_AUTOSCALE_MAX_NODES, 4); monthly spend cap in USD from plan hourly prices x 730 (PAAS_AUTOSCALE_MONTHLY_CAP_USD, 50);
scale-up/down cooldowns per region; idempotency (a provisioning node in the region, or the same request key, is reused, never a
second create); never touches the control plane (local-docker/self-hosted pools, PAAS_CONTROL_PLANE_NODE_IDS, PAAS_EDGE_IPS).
Every decision (including refusals) is written to autoscale_events and shown at GET /v1/admin/autoscaler."""
import os, time, json, threading, logging
from .db import q, kv_get, kv_put, kv_del, event
log = logging.getLogger("paas.autoscaler")
HOURS_MONTH = 730
ACTIVE = ("provisioning", "ready", "lost", "draining")
_lock = threading.Lock()
E = lambda k, d: os.environ.get(k, d)
def cfg():
    return {"enabled": E("PAAS_AUTOSCALE", "0") == "1", "pools": [p for p in E("PAAS_AUTOSCALE_POOLS", "vultr").split(",") if p],
            "max_per_region": int(E("PAAS_AUTOSCALE_MAX_PER_REGION", "2")), "max_nodes": int(E("PAAS_AUTOSCALE_MAX_NODES", "4")),
            "monthly_cap_usd": float(E("PAAS_AUTOSCALE_MONTHLY_CAP_USD", "50")), "idle_min": float(E("PAAS_AUTOSCALE_IDLE_MIN", "30")),
            "up_cooldown_s": int(E("PAAS_AUTOSCALE_UP_COOLDOWN", "300")), "down_cooldown_s": int(E("PAAS_AUTOSCALE_DOWN_COOLDOWN", "600")),
            "orphan_grace_s": int(E("PAAS_AUTOSCALE_ORPHAN_GRACE", "600"))}
class Refused(RuntimeError): pass
def manages(pool): return pool in cfg()["pools"]
def record(action, pool=None, region=None, node_id=None, reason="", **detail):
    try: q("insert into autoscale_events(ts,action,pool,region,node_id,reason,detail) values(?,?,?,?,?,?,?)",
           (time.time(), action, pool, region, node_id, str(reason)[:500], json.dumps(detail, default=str)[:2000]))
    except Exception as e: log.warning("autoscale record failed: %s", e)
    log.info("autoscale %s pool=%s region=%s node=%s %s", action, pool, region, node_id, reason)
def _prov(pool):
    from .drivers.providers import PROVISIONERS; return PROVISIONERS[pool]
def protected(n):
    """Control-plane / edge nodes are never scaled down or reconciled away."""
    ids = {x for x in E("PAAS_CONTROL_PLANE_NODE_IDS", "").split(",") if x}
    edge = {x.strip() for x in E("PAAS_EDGE_IPS", "").split(",") if x.strip()}
    return n["provider"] in ("local-docker", "self-hosted") or n["id"] in ids or (n.get("public_ip") and n["public_ip"] in edge)
def node_hourly(n):
    v = kv_get("as_price", n["id"])
    if v: return float(v)
    try: return _prov(n["provider"]).hourly_usd(_prov(n["provider"]).node_size(n.get("plan") or "cpu-node"))
    except Exception: return None
def active_nodes(pools=None):
    pools = pools or cfg()["pools"]
    return [n for n in q("select * from nodes where status in (%s)" % ",".join("?" * len(ACTIVE)), ACTIVE) if n["provider"] in pools and not protected(n)]
def monthly_spend(nodes=None):
    tot = 0.0
    for n in nodes if nodes is not None else active_nodes():
        h = node_hourly(n)
        if h is None: return None   # unknown price: treat as over the cap
        tot += h * HOURS_MONTH
    return round(tot, 2)
def scale_up(pool, plan_id, region, reason="", key=None, now=None):
    """Return a node id for a new node in (pool, region), or raise ProvisionError. Idempotent under retries."""
    from .drivers.providers import ProvisionError
    from .drivers import remote
    from . import regions as R
    c = cfg(); now = now or time.time(); region = R.canonical(region)
    def refuse(why, **d): record("refused", pool, region, None, why, plan=plan_id, key=key, **d); raise ProvisionError(f"autoscaler: {why}")
    if not c["enabled"]: refuse("autoscaling is off (PAAS_AUTOSCALE=0)")
    if pool not in c["pools"]: refuse(f"pool {pool} is not autoscaled")
    with _lock:
        if key:
            prev = kv_get("as_key", key)
            if prev:
                n = q("select status from nodes where id=?", (prev,), one=True)
                if n and n["status"] in ACTIVE: record("reuse", pool, region, prev, "same request key", key=key); return prev
        pend = [n for n in active_nodes([pool]) if R.canonical(n.get("region")) == region and n["status"] == "provisioning"]
        if pend: record("reuse", pool, region, pend[0]["id"], "a node is already provisioning in this region", key=key); return pend[0]["id"]
        last = float(kv_get("as_up", f"{pool}:{region}") or 0)
        if now - last < c["up_cooldown_s"]: refuse(f"scale-up cooldown ({int(c['up_cooldown_s'] - (now - last))}s left)")
        nodes = active_nodes(); here = [n for n in nodes if n["provider"] == pool and R.canonical(n.get("region")) == region]
        if len(here) >= c["max_per_region"]: refuse(f"region {region} already has {len(here)} node(s) (max {c['max_per_region']})")
        if len(nodes) >= c["max_nodes"]: refuse(f"{len(nodes)} autoscaled node(s) running (global max {c['max_nodes']})")
        prov = _prov(pool); size = prov.node_size(plan_id)
        try: hourly = prov.hourly_usd(size)
        except Exception as e: refuse(f"can't price {size}: {e}")
        cur = monthly_spend(nodes)
        if cur is None: refuse("can't price an existing node; refusing to grow")
        if cur + hourly * HOURS_MONTH > c["monthly_cap_usd"] + 1e-9:
            refuse(f"monthly spend cap: ${cur:.2f} + ${hourly * HOURS_MONTH:.2f} ({size}) > ${c['monthly_cap_usd']:.2f}", size=size)
        kv_put("as_up", f"{pool}:{region}", str(now))   # before the create, so a crash/retry can't double-create inside the cooldown
        node_id = remote.provision(pool, plan_id, region)
        kv_put("as_price", node_id, str(hourly))
        if key: kv_put("as_key", key, node_id, ttl=3600)
        record("scale_up", pool, region, node_id, reason, size=size, hourly_usd=hourly, monthly_after=round(cur + hourly * HOURS_MONTH, 2), key=key)
        return node_id
def reconcile(now=None):
    """Destroy tagged instances with no live node record (after a grace period); mark nodes whose instance vanished."""
    from .drivers import wg
    now = now or time.time(); c = cfg()
    for pool in c["pools"]:
        prov = _prov(pool)
        if not prov.available()[0] or not hasattr(prov, "list_tagged"): continue
        try: insts = prov.list_tagged()
        except Exception as e: record("reconcile_error", pool, reason=str(e)); continue
        live = {n["id"]: n for n in q("select * from nodes where provider=? and status!='destroyed'", (pool,))}
        refs = {str(i["id"]) for i in insts}
        for i in insts:
            nid = (i.get("label") or "").removeprefix("aloof-")
            n = live.get(nid)
            if n and protected(n): continue
            if not n or (n.get("provider_ref") and n["provider_ref"] != str(i["id"])):
                created = _ts(i.get("date_created")) or now
                if now - created < c["orphan_grace_s"]: continue
                try: prov.destroy(str(i["id"])); record("destroy_orphan", pool, None, nid or None, f"tagged instance {i['id']} has no live node", label=i.get("label"))
                except Exception as e: record("reconcile_error", pool, None, nid or None, f"destroy orphan {i['id']}: {e}")
        for n in live.values():
            if protected(n) or not n.get("provider_ref") or n["provider_ref"] in refs: continue
            if now - (n.get("created") or now) < 300: continue
            wg.remove_peer(n.get("wg_pubkey")); q("update nodes set status='destroyed', error=? where id=?", ("instance vanished at provider", n["id"]))
            event(f"node:{n['id']}", "instance vanished at provider; marked destroyed"); record("mark_vanished", pool, n.get("region"), n["id"], "instance not found at provider")
def _ts(s):
    if not s: return None
    try:
        from datetime import datetime; return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception: return None
def _pinned(node_id): return bool(q("select 1 from volumes where node_id=?", (node_id,), one=True))
def scale_down(now=None):
    from .drivers import remote
    from . import regions as R
    c = cfg(); now = now or time.time()
    if not c["enabled"]: return
    for n in active_nodes():
        if n["status"] not in ("ready", "draining") or protected(n): continue
        cnt, _, _ = remote.node_load(n["id"])
        if cnt or _pinned(n["id"]):
            kv_del("as_idle", n["id"])
            if n["status"] == "draining": q("update nodes set status='ready' where id=? and status='draining'", (n["id"],)); record("drain_cancel", n["provider"], n.get("region"), n["id"], "work arrived while draining")
            continue
        since = kv_get("as_idle", n["id"])
        if not since: kv_put("as_idle", n["id"], str(now)); continue
        if now - float(since) < c["idle_min"] * 60: continue
        region = R.canonical(n.get("region")); lastd = float(kv_get("as_down", f"{n['provider']}:{region}") or 0)
        if now - lastd < c["down_cooldown_s"]: continue
        if n["status"] == "ready":
            q("update nodes set status='draining' where id=? and status='ready'", (n["id"],)); record("drain", n["provider"], region, n["id"], f"empty {int((now - float(since)) / 60)} min"); continue
        if remote.node_load(n["id"])[0]: continue
        kv_put("as_down", f"{n['provider']}:{region}", str(now))
        remote.destroy_node(n["id"], "autoscaler: idle"); kv_del("as_idle", n["id"]); record("scale_down", n["provider"], region, n["id"], "drained and empty")
def tick():
    try: reconcile()
    except Exception as e: record("tick_error", reason=f"reconcile: {e}")
    try: scale_down()
    except Exception as e: record("tick_error", reason=f"scale_down: {e}")
def view(limit=200):
    from . import regions as R
    c = cfg(); nodes = active_nodes(); per = {}
    for n in nodes: per.setdefault(R.canonical(n.get("region")), []).append({"id": n["id"], "provider": n["provider"], "status": n["status"], "hourly_usd": node_hourly(n)})
    return {"config": c, "nodes": per, "node_count": len(nodes), "monthly_spend_usd": monthly_spend(nodes),
            "region_providers": {r: R.provider_for(r) for r in R.CATALOG},
            "decisions": q("select * from autoscale_events order by id desc limit ?", (limit,))}
