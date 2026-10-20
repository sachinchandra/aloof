"""Regions. One catalog of where Aloof can run; whether a region is *live* is derived, never declared: a region is live only
when it has capacity right now, i.e. a healthy registered node in that region (or it is the control plane's own region and
local Docker capacity is enabled). Everything else is "coming soon" and is rejected for new placements with a clear error.

Provider-agnostic: each region names its provider (hetzner, ovh, netcup, ...). EU ids are Hetzner location codes
(nbg1/fsn1/hel1); other regions use neutral ids (us-east, us-west, sg) so the provider can change underneath. Legacy ids are accepted as aliases: eu-de and eu-central -> nbg1."""
import os, time
from .db import q
CATALOG = {   # id -> display data. Order is the display order.
    "nbg1": {"city": "Nuremberg", "country": "Germany", "cc": "DE", "area": "eu", "provider": "Hetzner Cloud (nbg1)"},
    "fsn1": {"city": "Falkenstein", "country": "Germany", "cc": "DE", "area": "eu", "provider": "Hetzner Cloud (fsn1)"},
    "hel1": {"city": "Helsinki", "country": "Finland", "cc": "FI", "area": "eu", "provider": "Hetzner Cloud (hel1)"},
    "us-east": {"city": "Vint Hill", "country": "Virginia, US", "cc": "US", "area": "us", "provider": "OVHcloud (planned)"},
    "us-west": {"city": "Hillsboro", "country": "Oregon, US", "cc": "US", "area": "us", "provider": "OVHcloud (planned)"},
    "sg":      {"city": "Singapore", "country": "Singapore", "cc": "SG", "area": "ap", "provider": "OVHcloud (planned)"},
}
ALIASES = {"eu-de": "nbg1", "eu-central": "nbg1", "local": "nbg1", "nuremberg": "nbg1",
           "eu-fsn": "fsn1", "eu-hel": "hel1", "us-ash": "us-east", "ash": "us-east", "vint-hill": "us-east",
           "us-hil": "us-west", "hil": "us-west", "ap-sin": "sg", "sin": "sg", "singapore": "sg"}
# Provider id per region (machine-readable; nodes carry their own pool/provider too). US/SG: OVHcloud VPS-4, not yet contracted.
PROVIDER = {"nbg1": "hetzner", "fsn1": "hetzner", "hel1": "hetzner", "us-east": "ovh", "us-west": "ovh", "sg": "ovh"}
# Optional per-region egress policy. None = no cap. SG VPS throttles after 3 TB/month per node.
EGRESS = {"sg": {"cap_tb_month": 3, "over_cap": "throttled", "overage_usd_gb": None}}
DEFAULT = "nbg1"
NODE_STALE = 300   # a node unseen for 5 min is not healthy (node_tick marks it 'lost' at the same threshold)
class RegionError(ValueError): pass
def home():
    """Region of the control plane / edge host (where local-docker capacity lives)."""
    return canonical(os.environ.get("PAAS_LOCAL_REGION") or DEFAULT)
def canonical(r):
    """Canonical id for a region id or alias; None/'' -> default. Unknown -> RegionError."""
    if r is None or str(r).strip() == "": return DEFAULT
    k = str(r).strip().lower(); k = ALIASES.get(k, k)
    if k not in CATALOG: raise RegionError(f"unknown region '{r}'; valid regions: {', '.join(CATALOG)} (aliases: eu-de, eu-central = nbg1)")
    return k
def name(r):
    c = CATALOG[canonical(r)]; return f"{c['city']}, {c['country']}"
def local_capacity(): return os.environ.get("PAAS_LOCAL_CAPACITY", "1") == "1"
def healthy_nodes(now=None):
    """{region: count of healthy nodes}. Healthy = status ready and seen within NODE_STALE seconds."""
    now = now or time.time(); out = {}
    for n in q("select region, last_seen from nodes where status='ready'"):
        if n["last_seen"] and now - n["last_seen"] <= NODE_STALE:
            r = ALIASES.get(n["region"] or DEFAULT, n["region"] or DEFAULT); out[r] = out.get(r, 0) + 1
    return out
def status(now=None):
    """Every catalog region with its derived status. live = has capacity now; coming_soon otherwise."""
    h = healthy_nodes(now); hm = home(); out = []
    for rid, c in CATALOG.items():
        nodes = h.get(rid, 0); local = rid == hm and local_capacity()
        live = bool(nodes or local)
        if local: nodes += 1   # the control-plane host itself is a healthy node when local Docker capacity is on
        out.append({"id": rid, "name": f"{c['city']}, {c['country']}", "city": c["city"], "country": c["country"], "country_code": c["cc"], "area": c["area"],
                    "provider": c["provider"], "provider_id": PROVIDER[rid], "egress": EGRESS.get(rid), "status": "live" if live else "coming_soon", "live": live, "healthy_nodes": nodes,
                    "control_plane": rid == hm, "aliases": sorted(a for a, t in ALIASES.items() if t == rid)})
    return out
def live_ids(now=None): return [r["id"] for r in status(now) if r["live"]]
def require_live(r, now=None):
    """Canonical id if the region can take new workloads; otherwise RegionError with the live list."""
    rid = canonical(r)
    if rid not in live_ids(now):
        raise RegionError(f"region {rid} ({name(rid)}) has no capacity yet (coming soon, no healthy node registered); "
                          f"live regions: {', '.join(live_ids(now)) or 'none'}")
    return rid
def same_region(kind, obj_name, obj_region, app, app_region):
    """Volumes and Postgres must live in their app's region."""
    a, b = canonical(obj_region), canonical(app_region)
    if a != b: raise RegionError(f"{kind} {obj_name} is in {a} but app {app} is in {b}; {kind}s must be in the same region as their app")


# ---- Per-region pricing (Finance: aloof-baseline.xlsx, sheet '5 Regions') --------------------------------------
# Multiplier per plan class applied to the EU list price. Postgres follows its compute class; "usage" scales the
# Hobby/Pro usage rates (volume, egress, extra RAM, backups). EU = 1.0.
# DRAFT non-EU values: PENDING sachin's sign-off. They are not shown anywhere until a region is live.
PRICING_DRAFT = True
REGION_PRICING = {
    "eu": {"shared": 1.0, "perf": 1.0, "gpu": 1.0, "usage": 1.0},
    "us": {"shared": 1.0, "perf": 1.0, "gpu": 1.0, "usage": 1.0},   # DRAFT 1.0 (OVH); earlier Hetzner draft was shared 2.5 / perf 1.5
    "ap": {"shared": 1.0, "perf": 1.0, "gpu": 1.0, "usage": 1.0},   # DRAFT 1.0 (OVH SG); earlier Hetzner draft was shared 2.7 / perf 1.35
}
# Plans each area accepts. Free is EU-only; non-EU starts at Hobby. Set "shared": False to also restrict shared CPU.
REGION_ALLOWED = {
    "eu": {"tiers": ["free", "hobby", "pro"], "shared": True},
    "us": {"tiers": ["hobby", "pro"], "shared": True},
    "ap": {"tiers": ["hobby", "pro"], "shared": True},
}


def _area(region): return CATALOG[canonical(region)]["area"]


def multiplier(region, plan_class):
    """plan_class: shared | perf | gpu | usage | postgres:<compute plan id>."""
    m = REGION_PRICING[_area(region)]
    if plan_class.startswith("postgres:"):
        from .plans import PLANS
        p = PLANS.get(plan_class.split(":", 1)[1]) or {}
        plan_class = p.get("cpu_class") or "shared"
    return m.get(plan_class, 1.0)


def plan_class(plan):
    return "gpu" if plan.get("kind") == "gpu" else ("perf" if plan.get("cpu_class") == "performance" or plan.get("cpu_class") == "perf" else "shared")


def check_allowed(region, tier, plan=None):
    rule = REGION_ALLOWED[_area(region)]; r = canonical(region)
    if (tier or "free") not in rule["tiers"]:
        raise RegionError(f"the {tier or 'free'} tier isn't available in {r}; it's EU-only (nbg1/fsn1/hel1). Upgrade to Hobby or Pro, or deploy in nbg1")
    if plan is not None and plan_class(plan) == "shared" and not rule["shared"]:
        raise RegionError(f"shared-CPU plans aren't available in {r}; choose a performance plan")


def region_prices(region):
    """Region-adjusted price list. Callers must only expose this for live regions."""
    from .plans import PLANS, RATES
    r = canonical(region); out = {}
    for k, p in PLANS.items():
        if p.get("legacy") or not p.get("price_usd_month"): continue
        out[k] = round(p["price_usd_month"] * multiplier(r, plan_class(p)), 2)
    um = multiplier(r, "usage")
    return {"region": r, "multipliers": REGION_PRICING[_area(r)], "draft": PRICING_DRAFT and _area(r) != "eu",
            "allowed": REGION_ALLOWED[_area(r)], "provider": PROVIDER[r], "egress": EGRESS.get(r), "plans_usd_month": out,
            "rates": {k: round(v * um, 4) for k, v in RATES.items()}}


def live_prices():
    return {r: region_prices(r) for r in live_ids()}
