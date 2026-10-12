"""Aloof pricing v2 (2026-10-12). Structure mirrors Fly.io (shared vs performance CPU, per-second billing, volumes per
GB-month, egress per GB) with Railway/Render-style workspace tiers (fee includes the same amount of usage).
Prices in INR excl. 18% GST; USD at USD_INR. Cost basis (PLAN.md section 2): CPU is bin-packed on E2E C2 4 vCPU/12 GB at
₹2.25/hr (₹1,620 per 30 days); GPUs are routed to the cheapest provider. A "month" is 720 hours (30 days, same as Fly)."""
import os
USD_INR = float(os.environ.get("PAAS_USD_INR", "88"))
CPU_POOLS = [p for p in os.environ.get("PAAS_CPU_POOLS", "").split(",") if p]
HOURS_MONTH = 720
HOST = {"name": "E2E C2 4vCPU/12GB", "inr_hr": 2.25, "vcpu": 4, "mem_gb": 12, "overcommit_shared": 8, "ram_fill": 0.9}
# Krutrim 4vCPU/16GB ₹13/hr, used for below-cost checks if CPU ever lands there
ALT_HOST = {"name": "Krutrim 4vCPU/16GB", "inr_hr": 13.0, "vcpu": 4, "mem_gb": 16, "overcommit_shared": 8, "ram_fill": 0.9}
def _cpu(kind, vcpu, mem_mb, month_inr, fly_usd=None, render_usd=None, railway_usd=None, legacy=False):
    reserve = vcpu / 8 if kind == "shared" else vcpu      # shared vCPU: 1/8 core baseline, bursts to a full core (Fly gives 1/16)
    return {"kind": "cpu", "cpu_class": kind, "vcpu": vcpu, "cpu_reserve": reserve, "memory_mb": mem_mb, "gpu": None,
            "price_inr_month": month_inr, "price_inr_hr": round(month_inr / HOURS_MONTH, 4), "providers": CPU_POOLS,
            "compare_usd_month": {"fly": fly_usd, "render": render_usd, "railway": railway_usd}, "legacy": legacy}
def _gpu(name, vcpu, mem_mb, inr_hr, cost_inr_hr, providers, fly_usd_hr=None, note=""):
    return {"kind": "gpu", "cpu_class": "dedicated", "vcpu": vcpu, "cpu_reserve": vcpu, "memory_mb": mem_mb, "gpu": name,
            "price_inr_hr": inr_hr, "price_inr_month": None, "providers": providers, "cost_inr_hr": cost_inr_hr,
            "compare_usd_hr": {"fly_last_list": fly_usd_hr}, "note": note, "legacy": False}
# Railway (always-on, full use): $20/vCPU-mo + $10/GB-mo. Render has no 256MB/1GB/2GB shared sizes; its nearest is listed.
PLANS = {
  "shared-1x-256mb": _cpu("shared", 1, 256,  179,  fly_usd=2.19, railway_usd=22.5),
  "shared-1x-512mb": _cpu("shared", 1, 512,  299,  fly_usd=3.69, render_usd=7, railway_usd=25),
  "shared-1x-1gb":   _cpu("shared", 1, 1024, 549,  fly_usd=6.70, railway_usd=30),
  "shared-2x-2gb":   _cpu("shared", 2, 2048, 1099, fly_usd=13.39, railway_usd=60),
  "shared-4x-8gb":   _cpu("shared", 4, 8192, 4199, railway_usd=160),
  "perf-1x-2gb":     _cpu("performance", 1, 2048,  2199,  fly_usd=33,  render_usd=25,  railway_usd=40),
  "perf-2x-4gb":     _cpu("performance", 2, 4096,  5499,  fly_usd=66,  render_usd=85,  railway_usd=80),
  "perf-4x-8gb":     _cpu("performance", 4, 8192,  10999, fly_usd=132, render_usd=175, railway_usd=160),
  "perf-8x-16gb":    _cpu("performance", 8, 16384, 21999, fly_usd=264, render_usd=300, railway_usd=320),
  "gpu-t4-spot": _gpu("NVIDIA T4 16GB (spot, interruptible)", 4, 15360, 39, 25, ["aws-ap-south-1-spot", "self-hosted"]),
  "gpu-l4":      _gpu("NVIDIA L4 24GB", 8, 30720, 72, 49, ["e2e", "jarvislabs", "self-hosted"], fly_usd_hr=1.50, note="vs Fly A10 24GB"),
  "gpu-l40s":    _gpu("NVIDIA L40S 48GB", 8, 61440, 149, 102, ["e2e", "self-hosted"], fly_usd_hr=1.25, note="above Fly's old $1.25 (retired) to hold a 30% margin"),
  "gpu-a100":    _gpu("NVIDIA A100 80GB", 16, 92160, 279, 189, ["jarvislabs", "krutrim", "e2e", "self-hosted"], fly_usd_hr=3.50, note="vs Fly a100-80gb"),
  "gpu-h100":    _gpu("NVIDIA H100 80GB", 24, 194560, 369, 256, ["krutrim", "e2e", "jarvislabs", "self-hosted"], note="Fly never offered H100"),
}
# v1 plan ids keep working for existing apps (hidden from the pricing page, repriced on the v2 structure)
PLANS["cpu-nano"]   = _cpu("shared", 0.25, 256, 179, legacy=True)
PLANS["cpu-small"]  = _cpu("shared", 1, 1024, 549, legacy=True)
PLANS["cpu-medium"] = _cpu("shared", 2, 4096, 2099, legacy=True)     # shared-2x-2gb + 2 GB extra RAM
PLANS["cpu-large"]  = _cpu("shared", 4, 8192, 4199, legacy=True)
for p in PLANS.values(): p["provider"] = (p["providers"] or ["local-docker"])[0]
# Usage-based extras (Fly: volume $0.15/GB-mo, egress India $0.12/GB; Render disk $0.25, egress $0.15; Railway volume $0.15, egress $0.05)
RATES = {
  "volume_inr_gb_month": 16.0,     # $0.18
  "egress_inr_gb": 4.0,            # $0.045, below all three
  "extra_ram_inr_gb_month": 499,   # Fly $6/GB-mo = ₹528
}
# Workspace tiers: the fee includes the same amount of usage (Railway model); bandwidth allowance as Render
TIERS = {
  "free":  {"fee_inr": 0,    "included_usage_inr": 100,  "egress_gb": 5,   "seats": 1,    "max_apps": 3,   "autoscale": True, "note": "apps sleep after 5 min idle; hard-capped at the included ₹100",
            "plans": ["shared-1x-256mb", "shared-1x-512mb"], "max_instances": 1, "idle_seconds": 300, "max_volume_gb": 1, "hard_cap": True,
            "compare": "Railway Free ($0 + $1 usage), Render Hobby ($0, 5 GB egress), Fly: no free tier"},
  "hobby": {"fee_inr": 399,  "included_usage_inr": 399,  "egress_gb": 25,  "seats": 1,    "max_apps": 50,  "autoscale": True, "note": "",
            "compare": "Railway Hobby $5 incl. $5 usage (₹440)"},
  "pro":   {"fee_inr": 1699, "included_usage_inr": 1699, "egress_gb": 100, "seats": None, "max_apps": None, "autoscale": True, "note": "unlimited seats, per workspace",
            "compare": "Railway Pro $20 incl. $20 (₹1,760); Render Pro $25 + compute (₹2,200)"},
}
REGIONS = {"in-blr":"Bengaluru","in-mum":"Mumbai","in-del":"Delhi NCR","in-chn":"Chennai","local":"Prototype box"}
GST_RATE = 0.18
# ---------------- margin floor (no losses, keep a buffer) ----------------
MIN_MARGIN = float(os.environ.get("PAAS_MIN_MARGIN", "0.30"))
COST_BUFFER = 1.5   # unverified costs are inflated 1.5x (E2E CPU repriced 1 Aug 2026; storage/egress not confirmed)
# CPU hosts per pool, ₹/hr for 4 vCPU. Worst case uses on-demand prices (no committed/spot discounts).
CPU_HOSTS = {
  "e2e":                 {"inr_hr": 2.25 * COST_BUFFER, "vcpu": 4, "mem_gb": 12, "src": "E2E C2 4vCPU/12GB ₹2.25/hr x1.5 buffer (repriced Aug 2026, unverified)"},
  "krutrim":             {"inr_hr": 13.0, "vcpu": 4, "mem_gb": 16, "src": "Krutrim 4vCPU/16GB ₹13/hr"},
  "aws-ap-south-1-spot": {"inr_hr": 0.1504 * USD_INR * 1.1, "vcpu": 4, "mem_gb": 16, "src": "t3a.xlarge on-demand $0.1504/hr (spot can be reclaimed) +10% FX"},
  "self-hosted":         {"inr_hr": 2.25 * COST_BUFFER, "vcpu": 4, "mem_gb": 12, "src": "own hardware, priced like E2E until a real cost is set"},
  "local-docker":        {"inr_hr": 2.25 * COST_BUFFER, "vcpu": 4, "mem_gb": 12, "src": "prototype box, priced like E2E"},
}
# GPU ₹/hr per pool (on-demand, PLAN.md section 2). Missing = unknown cost, so the pool is refused for that plan.
# Override with env PAAS_COST_<POOL>_<PLAN>=₹/hr, e.g. PAAS_COST_JARVISLABS_GPU_L4=55
GPU_COSTS = {
  "gpu-t4-spot": {"aws-ap-south-1-spot": 25.0},                      # spot capped at MaxPrice = ₹25 (~$0.28); typical ₹18
  "gpu-l4":      {"e2e": 49.0},
  "gpu-l40s":    {"e2e": 102.0},
  "gpu-a100":    {"jarvislabs": 131.0, "krutrim": 189.0, "e2e": 189.0},
  "gpu-h100":    {"krutrim": 213.0, "e2e": 255.55},
}
RATE_COST = {"volume_inr_gb_month": 7.0 * COST_BUFFER, "egress_inr_gb": 1.0 * 2.5, "extra_ram_inr_gb_month": 1620 * COST_BUFFER / 10.8}
def _env_cost(pool, plan_id):
    v = os.environ.get(f"PAAS_COST_{pool.upper().replace('-', '_')}_{plan_id.upper().replace('-', '_')}")
    return float(v) if v else None
def pool_cost_month(plan_id, pool):
    """Worst-case ₹ per 30 days for one instance of plan on pool, or None if the cost is unknown."""
    p = PLANS[plan_id]; ov = _env_cost(pool, plan_id)
    if ov is not None: return ov * HOURS_MONTH
    if p["kind"] == "gpu":
        c = GPU_COSTS.get(plan_id, {}).get(pool); return c * HOURS_MONTH if c is not None else None
    h = CPU_HOSTS.get(pool)
    if not h: return None
    cpu_share = (p["cpu_reserve"] if p["cpu_class"] == "performance" else p["vcpu"] / 8) / h["vcpu"]
    ram_share = (p["memory_mb"] / 1024) / (h["mem_gb"] * 0.9)
    return max(cpu_share, ram_share) * h["inr_hr"] * HOURS_MONTH
def price_month(p): return p["price_inr_month"] or p["price_inr_hr"] * HOURS_MONTH
def pool_margin(plan_id, pool):
    c = pool_cost_month(plan_id, pool); return None if c is None else 1 - c / price_month(PLANS[plan_id])
def pool_allowed(plan_id, pool):
    m = pool_margin(plan_id, pool); return m is not None and m >= MIN_MARGIN
def candidate_pools(plan_id):
    p = PLANS[plan_id]
    if p["kind"] == "cpu": return list(CPU_HOSTS)      # any CPU pool may be enabled via PAAS_CPU_POOLS
    return list(dict.fromkeys(list(p["providers"]) + ["self-hosted"]))
def allowed_pools(plan_id): return [x for x in candidate_pools(plan_id) if pool_allowed(plan_id, x)]
def margin_report():
    """[(plan, pool, cost/mo, margin, allowed)] for every plan x candidate pool, plus extras."""
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
    """Every sellable plan must have >=1 schedulable pool at >= MIN_MARGIN, and every rate must clear it too."""
    bad = [f"{k}: no pool at >= {MIN_MARGIN:.0%}" for k, p in PLANS.items() if not allowed_pools(k)]
    bad += [f"{k}: {1 - RATE_COST[k] / RATES[k]:.0%}" for k in RATE_COST if 1 - RATE_COST[k] / RATES[k] < MIN_MARGIN]
    return bad
# kept for older callers
def unit_cost_month(p, host=None):
    k = next(k for k, v in PLANS.items() if v is p); return max((pool_cost_month(k, x) or 0) for x in allowed_pools(k) or [None] if x) if allowed_pools(k) else float("inf")
def margin(p, host=None):
    k = next(k for k, v in PLANS.items() if v is p); m = worst_case_margin(k); return round(m, 3) if m is not None else -1.0
