"""aloof.toml (fly.toml-compatible subset) -> app spec. `aloof config validate` calls validate(); `aloof deploy` sends the parsed TOML.
Supported: app, primary_region (region id, e.g. nbg1; see GET /v1/regions), regions (first entry used; one region per app), [build] image|dockerfile|git/ref|build_args|subdir, [env],
[processes], [http_service] (+ checks, concurrency, auto_stop/min_machines_running), [[services]] (first internal_port), [checks],
[deploy] release_command, [[mounts]], [autoscale], [[vm]] size|cpus+memory, [scale] counts per process group, [[cron]]."""
import re
from .plans import PLANS
KNOWN = {"app", "primary_region", "regions", "kill_signal", "kill_timeout", "build", "env", "processes", "http_service", "services", "checks",
         "deploy", "mounts", "autoscale", "vm", "scale", "cron", "swap_size_mb", "console_command", "statics", "experimental"}
def _dur(v, default):
    if v is None: return default
    if isinstance(v, (int, float)): return int(v)
    m = re.fullmatch(r"\s*(\d+)\s*(ms|s|m|h)?\s*", str(v))
    if not m: raise ValueError(f"bad duration {v!r}")
    n, u = int(m.group(1)), m.group(2) or "s"
    return max(1, {"ms": n // 1000, "s": n, "m": n * 60, "h": n * 3600}[u])
def _plan(vm):
    if not vm: return None
    vm = vm[0] if isinstance(vm, list) else vm
    if vm.get("size"):
        size = vm["size"]; alias = {"shared-cpu-1x": "shared-1x-256mb", "shared-cpu-2x": "shared-2x-2gb", "performance-1x": "perf-1x-2gb",
                                    "performance-2x": "perf-2x-4gb", "performance-4x": "perf-4x-8gb", "performance-8x": "perf-8x-16gb"}
        size = alias.get(size, size)
        if size not in PLANS: raise ValueError(f"[[vm]] size {vm['size']!r} unknown; choose from {', '.join(PLANS)}")
        return size
    cpus = float(vm.get("cpus", 1)); mem = vm.get("memory_mb") or vm.get("memory") or 1024
    if isinstance(mem, str):
        m = re.fullmatch(r"(\d+)\s*(gb|mb)?", mem.lower().strip()); mem = int(m.group(1)) * (1024 if (m.group(2) == "gb") else 1)
    cands = sorted((p["memory_mb"], p["vcpu"], k) for k, p in PLANS.items() if p["kind"] == "cpu" and p["vcpu"] >= cpus and p["memory_mb"] >= int(mem) and not k.startswith("cpu-"))
    if not cands: raise ValueError(f"no plan with >= {cpus} vCPU and {mem} MB")
    return cands[0][2]
def to_spec(cfg, name=None):
    """Returns (spec, mounts, warnings). Raises ValueError on invalid config."""
    w = []
    for k in cfg:
        if k not in KNOWN: w.append(f"unknown key {k!r} ignored")
    app = name or cfg.get("app")
    if not app: raise ValueError("app = \"<name>\" missing")
    b = cfg.get("build") or {}; src = {}
    if b.get("image"): src["image"] = b["image"]
    elif b.get("git"): src = {"git": b["git"], **({"ref": b["ref"]} if b.get("ref") else {})}
    if b.get("dockerfile") and b["dockerfile"] != "Dockerfile": src["dockerfile"] = b["dockerfile"]
    if b.get("subdir"): src["subdir"] = b["subdir"]
    if b.get("build_args") or b.get("args"): src["build_args"] = {str(k): str(v) for k, v in (b.get("build_args") or b.get("args")).items()}
    if b.get("builder") or b.get("buildpacks"): w.append("[build] builder/buildpacks: Nixpacks or buildpacks are used automatically when no Dockerfile exists")
    env = {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
    spec = {"name": app, "source": src, "env": env}
    hs = cfg.get("http_service"); svcs = cfg.get("services") or []
    if hs: spec["port"] = int(hs.get("internal_port", 8080))
    elif svcs:
        spec["port"] = int(svcs[0].get("internal_port", 8080))
        if len(svcs) > 1: w.append("only the first [[services]] entry is routed (HTTP/HTTPS via the edge); extra TCP/UDP services are not exposed publicly")
        for p in svcs[0].get("ports") or []:
            if int(p.get("port", 443)) not in (80, 443): w.append(f"public port {p.get('port')} not supported; apps are served on 80/443")
    else: spec["port"] = int(env.get("PORT") or 8080)
    spec["env"].setdefault("PORT", str(spec["port"]))
    checks = []
    if hs: checks += hs.get("checks") or []
    for cname, c in (cfg.get("checks") or {}).items(): checks.append(c)
    if checks:
        c = checks[0]
        if c.get("type") == "tcp" or (not c.get("path") and c.get("type") != "http"): spec["health"] = {"path": None, "tcp": True, "interval": _dur(c.get("interval"), 10), "failures": 3}
        else: spec["health"] = {"path": c.get("path", "/"), "interval": _dur(c.get("interval"), 10), "failures": int(c.get("failures", 3)), "grace": _dur(c.get("grace_period"), 0)}
    proc = cfg.get("processes")
    if proc:
        if not isinstance(proc, dict): raise ValueError("[processes] must be a table: web = \"cmd\", worker = \"cmd\"")
        spec["processes"] = {str(k): v for k, v in proc.items()}
        if hs and hs.get("processes") and hs["processes"] != ["web"]: w.append("http_service.processes: only the 'web' group receives HTTP traffic")
    scale = cfg.get("scale") or {}
    if proc: spec["process_counts"] = {g: int(scale.get(g, 1)) for g in proc}
    if scale.get("count") is not None: spec["instances"] = int(scale["count"])
    elif proc and "web" in proc: spec["instances"] = int(scale.get("web", 1))
    a = cfg.get("autoscale")
    if hs and not a:
        stop = hs.get("auto_stop_machines")
        if stop in (True, "stop", "suspend"):
            mn = int(hs.get("min_machines_running", 0)); soft = ((hs.get("concurrency") or {}).get("soft_limit")) or 10
            a = {"min": mn, "max": max(mn, int(scale.get("max", 3))), "target_concurrency": int(soft), "idle_seconds": 60}
    if a: spec["autoscale"] = {k: a[k] for k in ("min", "max", "target_concurrency", "cpu_percent", "idle_seconds") if k in a}
    d = cfg.get("deploy") or {}
    if d.get("release_command"): spec["release_command"] = d["release_command"]
    if d.get("strategy") and d["strategy"] not in ("rolling", "immediate", "canary", "bluegreen"): raise ValueError("deploy.strategy: rolling|immediate|canary|bluegreen")
    if d.get("strategy") in ("canary", "bluegreen"): w.append(f"deploy.strategy={d['strategy']}: deploys are rolling with readiness gating and automatic keep-old-on-failure")
    p = _plan(cfg.get("vm"))
    if p: spec["plan"] = p
    mounts = []
    ms = cfg.get("mounts") or []
    if isinstance(ms, dict): ms = [ms]
    for m in ms:
        if not m.get("source") or not str(m.get("destination", "")).startswith("/"): raise ValueError("[[mounts]] needs source and an absolute destination")
        size = m.get("initial_size", "1gb"); size = int(re.match(r"(\d+)", str(size)).group(1)) if not isinstance(size, int) else size
        mounts.append({"name": m["source"], "path": m["destination"], "size_gb": max(1, size)})
    if len(mounts) > 1: w.append("several mounts: all volumes must live on the same node")
    crons = cfg.get("cron") or []
    if isinstance(crons, dict): crons = [dict(v, name=k) for k, v in crons.items()]
    if crons:
        from .cron import parse
        for i, c in enumerate(crons):
            if not c.get("schedule") or not c.get("command"): raise ValueError("[[cron]] needs schedule and command")
            parse(c["schedule"])
        spec["cron"] = [{"name": c.get("name") or f"job{i}", "schedule": c["schedule"], "command": c["command"]} for i, c in enumerate(crons)]
    regs = ([cfg["primary_region"]] if cfg.get("primary_region") else []) + [r for r in (cfg.get("regions") or []) if r != cfg.get("primary_region")]
    if regs:   # primary_region wins over regions[0]. Unknown ids (e.g. fly.toml codes like ams/iad) warn and fall back, never 400.
        from .regions import canonical, RegionError, home
        try: spec["region"] = canonical(regs[0])
        except RegionError: w.append(f"unknown region {regs[0]!r} (fly.io region codes aren't Aloof regions): ignored; the app runs in {home()}. See `aloof regions`")
    if len(regs) > 1: w.append(f"multi-region placement is not available yet: the app runs in one region ({spec.get('region') or 'the default'}, primary_region or the first listed)")
    from .engine import NAME_RE
    if not NAME_RE.match(app): raise ValueError("app name must be 3-32 chars, lowercase letters/digits/hyphens")
    return spec, mounts, w
def validate(cfg, name=None):
    try:
        spec, mounts, w = to_spec(cfg, name)
        if spec.get("plan") and spec["plan"] not in PLANS: raise ValueError("unknown plan")
        return {"ok": True, "spec": spec, "mounts": mounts, "warnings": w}
    except (ValueError, TypeError, KeyError, AttributeError) as e: return {"ok": False, "error": str(e), "warnings": []}
