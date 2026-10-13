"""Aloof pricing v3 (2026-10-13): sold in USD worldwide. Phase 1 = CPU only on Hetzner Cloud in the EU (Germany/Finland);
phase 2 (PAAS_GPU_ENABLED=1, planned at 10 paying customers) adds GPUs from Hyperstack VMs and Runpod pods.
Structure mirrors Fly.io (shared vs performance CPU, per-second billing, volumes per GB-month, egress per GB) with Railway-style
workspace tiers (the fee comes back as included usage). Prices are USD excl. sales tax/VAT. A "month" is 720 hours.

Margin floor: every plan and rate must keep >= MIN_MARGIN gross margin AFTER payment fees, against the worst-case cost of every
pool it can be scheduled on. INR costs are converted at a pessimistic FX (COST_USD_INR, default ₹83/$: fewer rupees per dollar
means higher USD costs). Payment fees use the worst case: the smallest charge (a $5 Hobby subscription) where the fixed
$0.30 is 6%, plus card %, the international-card surcharge and Stripe Tax."""
import os
E = lambda k, d: os.environ.get(k, d)
CURRENCY = "USD"
COST_USD_INR = float(E("PAAS_COST_USD_INR", "83"))     # worst-case FX for costs
USD_INR = float(E("PAAS_USD_INR", "88"))               # only for legacy INR conversions (Razorpay, v3->v4 migration)
CPU_POOLS = [p for p in E("PAAS_CPU_POOLS", "hetzner,hetzner-cpx,e2e").split(",") if p]   # tried in order; floor refuses under-margin pools
GPU_ENABLED = E("PAAS_GPU_ENABLED", "0") == "1"
GPU_UNLOCK_PAID_CUSTOMERS = int(E("PAAS_GPU_UNLOCK_PAID_CUSTOMERS", "10"))
def gpu_status():
    return {"enabled": GPU_ENABLED, "status": "available" if GPU_ENABLED else "coming_soon",
            "waitlist": "POST /v1/waitlist {\"email\": \"...\", \"gpu\": \"gpu-h100\"}",
            "note": "" if GPU_ENABLED else "GPUs (L4, RTX 4090, L40, A100 80GB, H100) launch in phase 2. Join the waitlist to be notified."}
HOURS_MONTH = 720
# ---------------- payment fees (Stripe) ----------------
FEES = {"pct": float(E("PAAS_FEE_PCT", "0.029")), "intl_pct": float(E("PAAS_FEE_INTL_PCT", "0.015")),
        "fixed_usd": float(E("PAAS_FEE_FIXED_USD", "0.30")), "tax_pct": float(E("PAAS_FEE_TAX_PCT", "0.005")),   # Stripe Tax 0.5%
        "min_topup_usd": float(E("PAAS_MIN_TOPUP_USD", "10"))}
def _cpu(kind, vcpu, mem_mb, month_usd, fly_usd=None, render_usd=None, railway_usd=None, legacy=False):
    reserve = vcpu / 8 if kind == "shared" else vcpu      # shared vCPU: 1/8 core baseline, bursts to a full core (Fly gives 1/16)
    return {"kind": "cpu", "cpu_class": kind, "vcpu": vcpu, "cpu_reserve": reserve, "memory_mb": mem_mb, "gpu": None,
            "price_usd_month": month_usd, "price_usd_hr": round(month_usd / HOURS_MONTH, 6), "providers": CPU_POOLS,
            "compare_usd_month": {"fly": fly_usd, "render": render_usd, "railway": railway_usd}, "legacy": legacy}
def _gpu(name, vcpu, mem_mb, usd_hr, providers, fly_usd_hr=None, note=""):
    return {"kind": "gpu", "cpu_class": "dedicated", "vcpu": vcpu, "cpu_reserve": vcpu, "memory_mb": mem_mb, "gpu": name,
            "price_usd_hr": usd_hr, "price_usd_month": None, "providers": providers,
            "compare_usd_hr": {"fly_last_list": fly_usd_hr}, "note": note, "legacy": False}
# Railway (always-on, full use): $20/vCPU-mo + $10/GB-mo. Render has no 256MB/1GB/2GB shared sizes.
PLANS = {
  "shared-1x-256mb": _cpu("shared", 1, 256,  1.99,  fly_usd=2.19, railway_usd=22.5),
  "shared-1x-512mb": _cpu("shared", 1, 512,  3.49,  fly_usd=3.69, render_usd=7, railway_usd=25),
  "shared-1x-1gb":   _cpu("shared", 1, 1024, 5.99,  fly_usd=6.70, railway_usd=30),
  "shared-2x-2gb":   _cpu("shared", 2, 2048, 11.99, fly_usd=13.39, railway_usd=60),
  "shared-4x-8gb":   _cpu("shared", 4, 8192, 44,    railway_usd=160),
  "perf-1x-2gb":     _cpu("performance", 1, 2048,  24,  fly_usd=33,  render_usd=25,  railway_usd=40),
  "perf-2x-4gb":     _cpu("performance", 2, 4096,  59,  fly_usd=66,  render_usd=85,  railway_usd=80),
  "perf-4x-8gb":     _cpu("performance", 4, 8192,  119, fly_usd=132, render_usd=175, railway_usd=160),
  "perf-8x-16gb":    _cpu("performance", 8, 16384, 239, fly_usd=264, render_usd=300, railway_usd=320),
  "gpu-t4-spot": _gpu("NVIDIA T4 16GB (spot, interruptible)", 4, 15360, 0.55, ["aws-ap-south-1-spot", "self-hosted"]),
  "gpu-rtx4090": _gpu("NVIDIA RTX 4090 24GB (community hosts)", 8, 30720, 0.79, ["runpod", "self-hosted"]),
  "gpu-l4":      _gpu("NVIDIA L4 24GB", 8, 30720, 1.10, ["runpod", "e2e", "jarvislabs", "self-hosted"], fly_usd_hr=1.50, note="vs Fly A10 24GB"),
  "gpu-l40":     _gpu("NVIDIA L40 48GB", 8, 61440, 1.89, ["hyperstack", "self-hosted"]),
  "gpu-l40s":    _gpu("NVIDIA L40S 48GB", 8, 61440, 2.19, ["e2e", "self-hosted"], fly_usd_hr=1.25, note="above Fly's last $1.25 (retired) to hold the margin floor"),
  "gpu-a100":    _gpu("NVIDIA A100 80GB", 16, 92160, 3.99, ["hyperstack", "jarvislabs", "krutrim", "e2e", "self-hosted"], fly_usd_hr=3.50, note="vs Fly a100-80gb"),
  "gpu-h100":    _gpu("NVIDIA H100 80GB", 24, 194560, 5.49, ["hyperstack", "krutrim", "e2e", "jarvislabs", "self-hosted"], note="Fly never offered H100"),
}
# v1 plan ids keep working for existing apps (hidden from the pricing page)
PLANS["cpu-nano"]   = _cpu("shared", 0.25, 256, 1.99, legacy=True)
PLANS["cpu-small"]  = _cpu("shared", 1, 1024, 5.99, legacy=True)
PLANS["cpu-medium"] = _cpu("shared", 2, 4096, 21.99, legacy=True)     # shared-2x-2gb + 2 GB extra RAM
PLANS["cpu-large"]  = _cpu("shared", 4, 8192, 44, legacy=True)
for p in PLANS.values(): p["provider"] = (p["providers"] or ["local-docker"])[0]
# Usage-based extras (Fly: volume $0.15/GB-mo, egress $0.02 NA/EU, $0.12 India; Render disk $0.25, egress $0.15; Railway $0.15, $0.05)
RATES = {"volume_usd_gb_month": 0.22, "egress_usd_gb": 0.06, "extra_ram_usd_gb_month": 5.0}
# Workspace tiers: the fee comes back as included usage (Railway model). Egress counts against included usage (no separate
# free allowance) so the tier itself can never fall under the floor.
TIERS = {
  "free":  {"fee_usd": 0,  "included_usage_usd": 1,  "seats": 1,    "max_apps": 3,   "autoscale": True, "note": "apps sleep after 5 min idle; hard-capped at the included $1",
            "plans": ["shared-1x-256mb", "shared-1x-512mb"], "max_instances": 1, "idle_seconds": 300, "max_volume_gb": 1, "hard_cap": True,
            "compare": "Railway Free ($0 + $1 usage), Render Hobby ($0), Fly: no free tier"},
  "hobby": {"fee_usd": 5,  "included_usage_usd": 5,  "seats": 1,    "max_apps": 50,  "autoscale": True, "note": "",
            "compare": "Railway Hobby $5 incl. $5 usage"},
  "pro":   {"fee_usd": 20, "included_usage_usd": 20, "seats": None, "max_apps": None, "autoscale": True, "note": "unlimited seats, per workspace",
            "compare": "Railway Pro $20 incl. $20; Render Pro $25 + compute"},
}
for t in TIERS.values(): t["egress_gb"] = 0
# Regions, listed honestly. Phase 1: compute and the control plane/edge run on Hetzner in the EU. E2E (India) is only a CPU
# overflow pool and is listed only when its keys are set.
REGIONS = {"eu-de": "Nuremberg, Germany (Hetzner nbg1)"}   # live compute only; off-site backups in Helsinki, FI. See /regions
if E("E2E_API_KEY", ""): REGIONS["in-del"] = "Delhi NCR, India (E2E overflow)"
REGION_NOTE = ("All compute runs in Nuremberg, Germany (Hetzner), next to the edge; encrypted off-site backups are stored in Helsinki, Finland. Typical RTT: ~10-40 ms within "
               "Europe, ~90-110 ms from US East, ~150-200 ms from India/US West." + (" When EU capacity is short, CPU apps may overflow to E2E Networks in Delhi, India." if "in-del" in REGIONS else ""))
# ---------------- margin floor ----------------
MIN_MARGIN = float(E("PAAS_MIN_MARGIN", "0.30"))
COST_BUFFER = 1.5   # unverified costs inflated 1.5x (E2E CPU repriced 1 Aug 2026; storage/egress not confirmed)
def fee_rate(charge_usd=None):
    """Payment fees as a fraction of revenue for a charge; default = worst case (smallest charge we take)."""
    c = charge_usd or min(FEES["min_topup_usd"], min(t["fee_usd"] for t in TIERS.values() if t["fee_usd"]))
    return FEES["pct"] + FEES["intl_pct"] + FEES["tax_pct"] + FEES["fixed_usd"] / c
# CPU hosts per pool for 4 vCPU (inr_hr in INR, or usd_hr for USD-billed providers). Worst case = on-demand list prices.
HETZNER_BUFFER = 1.10   # EUR/USD swings, billing rounding; list prices from docs.hetzner.com (15 Jun 2026 adjustment), +$0.60 IPv4
CPU_HOSTS = {
  "hetzner":             {"usd_hr": (18.49 + 0.60) * HETZNER_BUFFER / 720, "vcpu": 8, "mem_gb": 16, "src": "Hetzner CX43 8 vCPU/16GB $18.49/mo + IPv4, x1.1"},
  "hetzner-cpx":         {"usd_hr": (81.99 + 0.60) * HETZNER_BUFFER / 720, "vcpu": 8, "mem_gb": 16, "src": "Hetzner CPX42 8 vCPU/16GB $81.99/mo + IPv4, x1.1 (fallback)"},
  "e2e":                 {"inr_hr": 2.25 * COST_BUFFER, "vcpu": 4, "mem_gb": 12, "src": "E2E C2 4vCPU/12GB ₹2.25/hr x1.5 buffer"},
  "krutrim":             {"inr_hr": 13.0, "vcpu": 4, "mem_gb": 16, "src": "Krutrim 4vCPU/16GB ₹13/hr"},
  "aws-ap-south-1-spot": {"usd_hr": 0.1504 * 1.1, "vcpu": 4, "mem_gb": 16, "src": "t3a.xlarge on-demand $0.1504/hr +10%"},
  "self-hosted":         {"inr_hr": 2.25 * COST_BUFFER, "vcpu": 4, "mem_gb": 12, "src": "own hardware, priced like E2E until a real cost is set"},
  "local-docker":        {"inr_hr": 2.25 * COST_BUFFER, "vcpu": 4, "mem_gb": 12, "src": "prototype box, priced like E2E"},
}
# GPU cost per pool per hour: float = INR, ("usd", x) = USD. Missing = unknown, so the pool is refused for that plan.
# Override: PAAS_COST_<POOL>_<PLAN>=USD/hr, e.g. PAAS_COST_JARVISLABS_GPU_L4=0.65
# Hyperstack/Runpod: on-demand list prices (Oct 2026) x1.1 for IPs/disk extras. Hyperstack includes vCPU/RAM/local disk.
_B = 1.10
GPU_COSTS = {
  "gpu-t4-spot": {"aws-ap-south-1-spot": ("usd", 0.30)},     # spot bid capped at MaxPrice $0.30 (typical ~$0.21)
  "gpu-rtx4090": {"runpod": ("usd", 0.34 * _B)},              # Runpod community cloud $0.34
  "gpu-l4":      {"runpod": ("usd", 0.59 * _B), "e2e": 49.0},  # Runpod secure cloud $0.59
  "gpu-l40":     {"hyperstack": ("usd", 1.00 * _B)},
  "gpu-l40s":    {"e2e": 102.0},
  "gpu-a100":    {"hyperstack": ("usd", 1.35 * _B), "jarvislabs": 131.0, "krutrim": 189.0, "e2e": 189.0},   # Hyperstack A100 PCIe 80GB
  "gpu-h100":    {"hyperstack": ("usd", 2.50 * _B), "krutrim": 213.0, "e2e": 255.55},                     # Hyperstack H100 PCIe
}
def _usd(v): return v[1] if isinstance(v, tuple) else v / COST_USD_INR
RATE_COST = {"volume_usd_gb_month": 7.0 * COST_BUFFER / COST_USD_INR, "egress_usd_gb": 1.0 * 2.5 / COST_USD_INR,
             "extra_ram_usd_gb_month": 1620 * COST_BUFFER / 10.8 / COST_USD_INR}
def _env_cost(pool, plan_id):
    v = os.environ.get(f"PAAS_COST_{pool.upper().replace('-', '_')}_{plan_id.upper().replace('-', '_')}")
    return float(v) if v else None
def pool_cost_hr(plan_id, pool):
    """Worst-case USD/hr for one instance of plan on pool, or None if the cost is unknown."""
    p = PLANS[plan_id]; ov = _env_cost(pool, plan_id)
    if ov is not None: return ov
    if p["kind"] == "gpu":
        c = GPU_COSTS.get(plan_id, {}).get(pool); return _usd(c) if c is not None else None
    h = CPU_HOSTS.get(pool)
    if not h: return None
    host_usd = h["usd_hr"] if "usd_hr" in h else h["inr_hr"] / COST_USD_INR
    cpu_share = (p["cpu_reserve"] if p["cpu_class"] == "performance" else p["vcpu"] / 8) / h["vcpu"]
    ram_share = (p["memory_mb"] / 1024) / (h["mem_gb"] * 0.9)
    return max(cpu_share, ram_share) * host_usd
def pool_cost_month(plan_id, pool):
    c = pool_cost_hr(plan_id, pool); return None if c is None else c * HOURS_MONTH
def price_month(p): return p["price_usd_month"] or p["price_usd_hr"] * HOURS_MONTH
def net_margin(price, cost):
    """Gross margin after payment fees: (price*(1-fees) - cost) / price."""
    return (price * (1 - fee_rate()) - cost) / price
def pool_margin(plan_id, pool):
    c = pool_cost_month(plan_id, pool); return None if c is None else net_margin(price_month(PLANS[plan_id]), c)
def pool_allowed(plan_id, pool):
    m = pool_margin(plan_id, pool); return m is not None and m >= MIN_MARGIN
def candidate_pools(plan_id):
    p = PLANS[plan_id]
    if p["kind"] == "cpu": return list(CPU_HOSTS)
    return list(dict.fromkeys(list(p["providers"]) + ["self-hosted"]))
def allowed_pools(plan_id): return [x for x in candidate_pools(plan_id) if pool_allowed(plan_id, x)]
def rate_margin(k): return net_margin(RATES[k], RATE_COST[k])
def margin_report():
    out = []
    for k in PLANS:
        for pool in sorted(set(candidate_pools(k)) | set(CPU_HOSTS if PLANS[k]["kind"] == "cpu" else GPU_COSTS.get(k, {}))):
            c = pool_cost_month(k, pool); m = pool_margin(k, pool)
            out.append((k, pool, c, m, m is not None and m >= MIN_MARGIN))
    return out
def worst_case_margin(plan_id):
    ms = [pool_margin(plan_id, x) for x in allowed_pools(plan_id)]
    return min(ms) if ms else None
def check_floor():
    """Every plan needs >=1 schedulable pool at >= MIN_MARGIN after fees; every rate must clear it too."""
    bad = [f"{k}: no pool at >= {MIN_MARGIN:.0%}" for k in PLANS if not allowed_pools(k)]
    bad += [f"{k}: {rate_margin(k):.0%}" for k in RATE_COST if rate_margin(k) < MIN_MARGIN]
    return bad
def margin(p, host=None):
    k = next(k for k, v in PLANS.items() if v is p); m = worst_case_margin(k); return round(m, 3) if m is not None else -1.0
def cents(usd): return usd * 100.0
# ---------------- managed Postgres (runs on local-docker next to the edge, private network to attached apps) ----------------
# Price = the compute plan + the data disk at the volume rate (+ a management fee when needed for margin); backups (daily pg_dump, 7 kept) are metered at the volume rate.
# Margin is checked against the worst-case cost of the compute pool Postgres runs on (local-docker) plus disk cost for data and
# a full set of backups (BACKUP_FACTOR x disk), so it holds even if customers never pay for backup storage.
PG_POOL = "local-docker"; PG_BACKUP_FACTOR = 1.0
PG_PLANS = {
  "pg-dev":      {"compute": "shared-1x-256mb", "disk_gb": 1,   "note": "development / hobby"},
  "pg-basic":    {"compute": "shared-1x-1gb",   "disk_gb": 10,  "note": "small production"},
  "pg-standard": {"compute": "shared-2x-2gb",   "disk_gb": 40,  "note": "production"},
  "pg-perf":     {"compute": "perf-1x-2gb",     "disk_gb": 100, "note": "dedicated vCPU"},
}
import math as _m
for _k, _p in PG_PLANS.items():   # compute + disk, plus a management fee where needed to keep a 35% target margin (floor 30%)
    _base = PLANS[_p["compute"]]["price_usd_month"] + _p["disk_gb"] * RATES["volume_usd_gb_month"]
    _cost = pool_cost_month(_p["compute"], PG_POOL) + _p["disk_gb"] * (1 + PG_BACKUP_FACTOR) * RATE_COST["volume_usd_gb_month"]
    _need = _cost / (1 - fee_rate() - max(MIN_MARGIN, 0.35))
    _p["price_usd_month"] = round(max(_base, _m.ceil(_need) - 0.01), 2)
    _p["price_usd_hr"] = round(_p["price_usd_month"] / HOURS_MONTH, 6)
    _p["vcpu"] = PLANS[_p["compute"]]["vcpu"]; _p["memory_mb"] = PLANS[_p["compute"]]["memory_mb"]
RATES["backup_usd_gb_month"] = RATES["volume_usd_gb_month"]
def pg_cost_month(k):
    p = PG_PLANS[k]; c = pool_cost_month(p["compute"], PG_POOL)
    return None if c is None else c + p["disk_gb"] * (1 + PG_BACKUP_FACTOR) * RATE_COST["volume_usd_gb_month"]
def pg_margin(k):
    c = pg_cost_month(k); return None if c is None else net_margin(PG_PLANS[k]["price_usd_month"], c)
for _k in PG_PLANS: PG_PLANS[_k]["margin"] = round(pg_margin(_k) or 0, 3)
# Log retention per workspace tier (days of searchable history on the platform; drains forward everything as it happens)
for _t, _d in (("free", 1), ("hobby", 7), ("pro", 30)): TIERS[_t]["log_retention_days"] = _d
