#!/usr/bin/env python3
"""Aloof node agent. Runs on every remote node (E2E / Krutrim / Jarvislabs / AWS / self-hosted).
Stdlib only + docker CLI. Listens on the node's WireGuard address so only the control plane can reach it.
Lifecycle: `agent.py register` (one-time, uses join token, writes /etc/aloof/agent.json + wg0.conf) then `agent.py serve`.
API (Authorization: Bearer <agent_token>):
  GET  /v1/health                      -> capacity, gpus, containers
  POST /v1/containers                  {name,image,env,port,cpus,mem_mb,gpu,volumes,command,labels,registry_auth} -> {id, host_port}
  GET  /v1/containers/<id>             -> {status, host_port}
  DELETE /v1/containers/<id>
  GET  /v1/containers/<id>/logs?tail=N
  GET  /v1/containers/<id>/stats       -> {cpu_percent}
  DELETE /v1/accounts/<id>/containers  -> {removed}   (label paas.account=<id> or network aloof-acct-<id>; volumes -v)
"""
import json, os, sys, subprocess, socket, hmac, time, urllib.request, urllib.parse, re, threading, platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CONF = os.environ.get("ALOOF_AGENT_CONF", "/etc/aloof/agent.json")
VERSION = "0.5"
def sh(*args, check=True, timeout=600):
    p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check and p.returncode: raise RuntimeError((p.stderr or p.stdout).strip()[-800:])
    return p.stdout.strip()
def load(): return json.load(open(CONF))
def capacity():
    vcpu = os.cpu_count() or 1
    try: mem = int([l for l in open("/proc/meminfo") if l.startswith("MemTotal")][0].split()[1]) // 1024
    except Exception: mem = 1024
    gpus, model = 0, None
    try:
        out = sh("nvidia-smi", "--query-gpu=name", "--format=csv,noheader", check=True, timeout=20).splitlines()
        gpus, model = len(out), (out[0].strip() if out else None)
    except Exception: pass
    return {"vcpu": vcpu, "mem_mb": mem, "gpus": gpus, "gpu_model": model}
NAME_OK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
def bind_ip(cfg): return cfg.get("publish_ip") or cfg.get("wg_ip") or "127.0.0.1"
def run_container(cfg, b):
    name = b["name"]; assert NAME_OK.match(name), "bad name"
    port = int(b.get("port", 8080)); image = b["image"]
    if b.get("registry_auth"):
        ra = b["registry_auth"]; subprocess.run(["docker", "login", ra["server"], "-u", ra["user"], "--password-stdin"], input=ra["password"], text=True, capture_output=True)
    sh("docker", "pull", image, check=False)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    args = ["docker", "create", "--name", name, "--restart", "unless-stopped", "--label", "aloof.managed=1",
            "-p", f"{bind_ip(cfg)}::{port}", "-e", f"PORT={port}"] + _net_args(b)
    for k, v in (b.get("labels") or {}).items(): args += ["--label", f"{k}={v}"]
    if b.get("cpus"): args += ["--cpus", str(b["cpus"])]
    if b.get("mem_mb"): args += ["--memory", f"{int(b['mem_mb'])}m"]
    if b.get("gpu"): args += ["--gpus", str(b["gpu"]) if str(b["gpu"]) != "1" else "all"]
    for v in b.get("volumes") or []:
        assert NAME_OK.match(v["name"]) and v["path"].startswith("/"), "bad volume"
        args += ["-v", f"aloof-vol-{v['name']}:{v['path']}"]
    env_file = None
    if b.get("env"):   # pass env via a 0600 temp file so values never appear in `ps`
        import tempfile; fd, env_file = tempfile.mkstemp(prefix="aloof-env-"); os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            for k, v in b["env"].items(): f.write(f"{k}={str(v).replace(chr(10), ' ')}\n")
        args += ["--env-file", env_file]
    args += [image] + list(b.get("command") or [])
    try: cid = sh(*args)[:12]
    finally:
        if env_file: os.unlink(env_file)
    _connect_rest(cid, b); sh("docker", "start", cid)
    return {"id": cid, "host_port": host_port(cid, port), "host": bind_ip(cfg)}
def _nets(b):
    out = []
    for n in b.get("networks") or []:
        n = n if isinstance(n, dict) else {"name": n}
        assert NAME_OK.match(n["name"]) and all(NAME_OK.match(a) for a in n.get("aliases") or []), "bad network"
        if subprocess.run(["docker", "network", "inspect", n["name"]], capture_output=True).returncode:
            subprocess.run(["docker", "network", "create", "--label", "aloof.net=1", n["name"]], capture_output=True)
        out.append(n)
    return out
def _net_args(b):
    ns = _nets(b); b["_nets"] = ns
    if not ns: return []
    return ["--network", ns[0]["name"]] + sum((["--network-alias", a] for a in ns[0].get("aliases") or []), [])
def _connect_rest(cid, b):
    for n in (b.get("_nets") or [])[1:]:
        sh("docker", "network", "connect", *sum((["--alias", a] for a in n.get("aliases") or []), []), n["name"], cid, check=False)
def run_once(cfg, b):
    """One-off machine (release_command / cron): runs to completion, returns exit code + output, removed afterwards."""
    image = b["image"]; name = f"aloof-run-{int(time.time()*1000)%10**10}"
    sh("docker", "pull", image, check=False)
    args = ["docker", "create", "--name", name, "--label", "aloof.oneoff=1", "-e", f"PORT={int(b.get('port', 8080))}"] + _net_args(b)
    for k, v in (b.get("labels") or {}).items(): args += ["--label", f"{k}={v}"]
    if b.get("cpus"): args += ["--cpus", str(b["cpus"])]
    if b.get("mem_mb"): args += ["--memory", f"{int(b['mem_mb'])}m"]
    for v in b.get("volumes") or []:
        assert NAME_OK.match(v["name"]) and v["path"].startswith("/"), "bad volume"
        args += ["-v", f"aloof-vol-{v['name']}:{v['path']}"]
    import tempfile; fd, env_file = tempfile.mkstemp(prefix="aloof-env-"); os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, v in (b.get("env") or {}).items(): f.write(f"{k}={str(v).replace(chr(10), ' ')}\n")
    cmd = b["command"]; cmd = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
    try:
        cid = sh(*(args + ["--env-file", env_file, image] + cmd))[:12]
    finally: os.unlink(env_file)
    try:
        _connect_rest(cid, b); sh("docker", "start", cid)
        try: code = int(sh("docker", "wait", cid, timeout=int(b.get("timeout", 900))) or 1)
        except subprocess.TimeoutExpired: subprocess.run(["docker", "kill", cid], capture_output=True); code = 124
        p = subprocess.run(["docker", "logs", cid], capture_output=True, text=True)
        return {"exit_code": code, "output": (p.stdout + p.stderr)[-50000:]}
    finally: subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
def host_port(cid, port=None):
    out = sh("docker", "port", cid, check=False)
    for line in out.splitlines():
        if port is None or line.startswith(f"{port}/tcp"): return int(line.rsplit(":", 1)[1])
    return None
def sweep_account(account_id):
    """Same targets as the control plane's account_sweep_targets(): label paas.account=<id>, network aloof-acct-<id>."""
    i = int(account_id); net = f"aloof-acct-{i}"; ids = set()
    for f in (f"label=paas.account={i}", f"network={net}"):
        ids |= set(sh("docker", "ps", "-aq", "--filter", f, check=False).split())
    keep = set()
    for c in ids:   # a container labelled for another account is never touched, even if attached to this network
        lab = sh("docker", "inspect", "-f", '{{index .Config.Labels "paas.account"}}', c, check=False).strip()
        if lab and lab != str(i): keep.add(c)
    gone = sorted(ids - keep)
    for c in gone: subprocess.run(["docker", "rm", "-f", "-v", c], capture_output=True)
    subprocess.run(["docker", "network", "rm", net], capture_output=True)
    return {"removed": len(gone)}
def cid_ok(cid): return re.match(r"^[a-f0-9]{12,64}$", cid) is not None
class H(BaseHTTPRequestHandler):
    server_version = f"aloof-agent/{VERSION}"
    def _send(self, code, obj):
        b = (json.dumps(obj) if not isinstance(obj, str) else obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json" if not isinstance(obj, str) else "text/plain")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _auth(self):
        tok = self.server.cfg["agent_token"]; got = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if not hmac.compare_digest(got.encode(), tok.encode()): self._send(401, {"error": "unauthorized"}); return False
        return True
    def _route(self, method):
        if not self._auth(): return
        u = urllib.parse.urlparse(self.path); parts = [p for p in u.path.split("/") if p]; qs = urllib.parse.parse_qs(u.query)
        try:
            if parts == ["v1", "health"]:
                cs = sh("docker", "ps", "-q", "--filter", "label=aloof.managed=1", check=False).split()
                return self._send(200, {"ok": True, "version": VERSION, "node_id": self.server.cfg.get("node_id"), **capacity(), "containers": len(cs)})
            if parts == ["v1", "run"] and method == "POST":
                n = int(self.headers.get("Content-Length") or 0); b = json.loads(self.rfile.read(n) or b"{}")
                return self._send(200, run_once(self.server.cfg, b))
            if parts == ["v1", "containers"] and method == "POST":
                n = int(self.headers.get("Content-Length") or 0); b = json.loads(self.rfile.read(n) or b"{}")
                return self._send(200, run_container(self.server.cfg, b))
            if len(parts) >= 3 and parts[:2] == ["v1", "containers"]:
                cid = parts[2]
                if not cid_ok(cid): return self._send(400, {"error": "bad id"})
                if len(parts) == 3 and method == "GET":
                    st = sh("docker", "inspect", "-f", "{{.State.Status}}", cid, check=False) or "gone"
                    return self._send(200, {"status": st, "host_port": host_port(cid) if st == "running" else None, "host": bind_ip(self.server.cfg)})
                if len(parts) == 3 and method == "DELETE":
                    subprocess.run(["docker", "rm", "-f", "-v", cid], capture_output=True); return self._send(200, {"ok": True})
                if parts[3:] == ["logs"]:
                    tail = str(int(qs.get("tail", ["200"])[0]))
                    p = subprocess.run(["docker", "logs", "--timestamps", "--tail", tail, cid], capture_output=True, text=True)
                    return self._send(200, p.stdout + p.stderr)
                if parts[3:] == ["stats"]:
                    out = sh("docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", cid, check=False).strip().rstrip("%")
                    return self._send(200, {"cpu_percent": float(out or 0)})
            if len(parts) == 4 and parts[:2] == ["v1", "accounts"] and parts[3] == "containers" and method == "DELETE":
                if not parts[2].isdigit(): return self._send(400, {"error": "bad id"})
                return self._send(200, sweep_account(parts[2]))
            if parts == ["v1", "volumes"] and method == "DELETE":
                name = qs.get("name", [""])[0]
                if NAME_OK.match(name): subprocess.run(["docker", "volume", "rm", "-f", f"aloof-vol-{name}"], capture_output=True)
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as e: self._send(500, {"error": str(e)})
    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_DELETE(self): self._route("DELETE")
    def log_message(self, f, *a): sys.stderr.write("agent: " + (f % a) + "\n")
def register(api, join_token, no_wg=False, agent_port=7070, agent_host=None):
    """One-time join. Generates a WireGuard key, registers, writes wg0.conf and agent.json."""
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    priv = pub = None
    if not no_wg:
        priv = sh("wg", "genkey"); pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True).stdout.strip()
    body = {"join_token": join_token, "wg_pubkey": pub, "agent_port": agent_port, "hostname": socket.gethostname(),
            "os": platform.platform(), "agent_version": VERSION, **capacity()}
    if no_wg: body["agent_url"] = f"http://{agent_host or '127.0.0.1'}:{agent_port}"
    req = urllib.request.Request(api.rstrip("/") + "/v1/nodes/register", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    cfg = {"node_id": r["node_id"], "agent_token": r["agent_token"], "api": api, "port": agent_port}
    if not no_wg:
        w = r["wg"]; cfg["wg_ip"] = w["address"].split("/")[0]
        conf = (f"[Interface]\nPrivateKey = {priv}\nAddress = {w['address']}\n\n[Peer]\nPublicKey = {w['hub_pubkey']}\n"
                f"Endpoint = {w['hub_endpoint']}\nAllowedIPs = {w['allowed_ips']}\nPersistentKeepalive = 25\n")
        fd = os.open(os.environ.get("ALOOF_WG_CONF", "/etc/wireguard/wg0.conf"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.write(fd, conf.encode()); os.close(fd)
    else: cfg["listen"] = agent_host or "127.0.0.1"; cfg["publish_ip"] = agent_host or "127.0.0.1"
    fd = os.open(CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.write(fd, json.dumps(cfg).encode()); os.close(fd)
    print(json.dumps({"node_id": cfg["node_id"], "wg_ip": cfg.get("wg_ip")}))
def serve():
    cfg = load(); host = cfg.get("listen") or cfg.get("wg_ip") or "127.0.0.1"
    srv = ThreadingHTTPServer((host, int(cfg.get("port", 7070))), H); srv.cfg = cfg
    print(f"aloof agent {VERSION} node={cfg['node_id']} listening on {host}:{cfg.get('port', 7070)}", flush=True); srv.serve_forever()
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); s = p.add_subparsers(dest="cmd", required=True)
    r = s.add_parser("register"); r.add_argument("--api", required=True); r.add_argument("--join-token", required=True)
    r.add_argument("--no-wg", action="store_true"); r.add_argument("--port", type=int, default=7070); r.add_argument("--host")
    s.add_parser("serve"); o = p.parse_args()
    register(o.api, o.join_token, o.no_wg, o.port, o.host) if o.cmd == "register" else serve()
