#!/usr/bin/env python3
"""Provision the Aloof control plane + edge on one Hetzner Cloud CX33 and bring it live. Idempotent: re-running reuses the
SSH key, firewall and server it finds by name. Run from a laptop or this box:

    cp deploy/golive.env.example deploy/golive.env   # fill in (HCLOUD_TOKEN, Stripe, SMTP, ACME_EMAIL...)
    python3 deploy/hetzner_golive.py                 # creates the server, installs everything, switches Caddy to real TLS

Steps: upload your SSH public key -> firewall aloof-edge (22/80/443 tcp, 51820 udp, icmp) -> server aloof-edge (cx33, Ubuntu 24.04,
HCLOUD_LOCATION, default fsn1, falling back to nbg1/hel1) whose cloud-init clones the repo and runs deploy/setup-box.sh
(Docker, Postgres, registry, WireGuard hub, supervisord, Caddy) -> copy golive.env over SSH with the server's IP filled in
-> run deploy/golive.sh -> print the DNS records to create. Secrets go over SSH only, never into Hetzner user_data."""
import os, sys, time, json, subprocess, shlex, argparse
import httpx
HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("HCLOUD_API_BASE", "https://api.hetzner.cloud/v1")
REPO = os.environ.get("ALOOF_REPO", "https://github.com/sachinchandra/aloof.git")
NAME = "aloof-edge"
def load_env(path):
    env = {}
    for line in open(path):
        line = line.split(" #")[0].strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    return env
def cloud_init(pubkey, repo=REPO):
    return f"""#cloud-config
users:
  - name: box
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys: [{json.dumps(pubkey)}]
package_update: true
packages: [git, curl, docker.io, supervisor, wireguard-tools, iproute2, iptables, sqlite3, python3-venv]
write_files:
  - path: /etc/sysctl.d/90-aloof.conf
    content: "net.ipv4.ip_forward=1\\n"
runcmd:
  - [sysctl, --system]
  - [systemctl, disable, --now, docker.socket, docker.service, supervisor.service]
  - [mkdir, -p, /workspace]
  - [git, clone, --depth, "1", {json.dumps(repo)}, /workspace/paas]
  - [chown, -R, "box:box", /workspace]
  - [su, -, box, -c, "cd /workspace/paas && mkdir -p data && bash deploy/setup-box.sh > /workspace/paas/data/setup-box.log 2>&1 && sudo touch /var/lib/aloof-ready"]
"""
class Hetzner:
    def __init__(self, token, client=None):
        self.c = client or httpx.Client(timeout=60); self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    def req(self, method, path, **kw):
        r = self.c.request(method, API + path, headers=self.h, **kw)
        if r.status_code >= 400: raise RuntimeError(f"hetzner {method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}
    def ssh_key(self, pubkey):
        for k in self.req("GET", "/ssh_keys")["ssh_keys"]:
            if k["public_key"].split()[:2] == pubkey.split()[:2]: return k["id"]
        return self.req("POST", "/ssh_keys", json={"name": "aloof-ops", "public_key": pubkey, "labels": {"aloof": "1"}})["ssh_key"]["id"]
    def firewall(self, ssh_cidrs):
        d = self.req("GET", "/firewalls", params={"name": NAME})
        if d["firewalls"]: return d["firewalls"][0]["id"]
        any_ = ["0.0.0.0/0", "::/0"]
        rules = [{"direction": "in", "protocol": "tcp", "port": "22", "source_ips": ssh_cidrs},
                 {"direction": "in", "protocol": "tcp", "port": "80", "source_ips": any_},
                 {"direction": "in", "protocol": "tcp", "port": "443", "source_ips": any_},
                 {"direction": "in", "protocol": "udp", "port": "51820", "source_ips": any_},
                 {"direction": "in", "protocol": "icmp", "source_ips": any_}]
        return self.req("POST", "/firewalls", json={"name": NAME, "rules": rules, "labels": {"aloof": "edge"}})["firewall"]["id"]
    def server(self, server_type, locations, user_data, ssh_key_id, fw_id):
        d = self.req("GET", "/servers", params={"name": NAME})
        if d["servers"]: return d["servers"][0], False
        last = None
        for loc in locations:
            body = {"name": NAME, "server_type": server_type, "image": "ubuntu-24.04", "location": loc, "ssh_keys": [ssh_key_id],
                    "firewalls": [{"firewall": fw_id}], "user_data": user_data, "labels": {"aloof": "edge"},
                    "public_net": {"enable_ipv4": True, "enable_ipv6": True}}
            r = self.c.post(API + "/servers", headers=self.h, json=body)
            if r.status_code < 400: return r.json()["server"], True
            last = f"{loc}: {r.status_code} {r.text[:200]}"; print(f"  {server_type} not available in {loc}, trying next ({r.status_code})")
        raise RuntimeError(f"could not create {server_type} in {locations}: {last}")
    def wait_running(self, sid, timeout=600, sleep=5):
        end = time.time() + timeout
        while time.time() < end:
            s = self.req("GET", f"/servers/{sid}")["server"]
            if s["status"] == "running": return s
            time.sleep(sleep)
        raise RuntimeError("server did not reach running")
def fill_env(text, ip):
    return (text.replace("<edge server public IPv4>", ip).replace("<edge public IPv4>", ip)
            .replace("__EDGE_IP_DASHED__", ip.replace(".", "-")).replace("__EDGE_IP__", ip))
def dns_records(env, ip, ipv6=None):
    d = env.get("PAAS_BASE_DOMAIN", "aloof.si"); recs = []
    for host in (d, f"www.{d}", f"api.{d}", f"mcp.{d}", f"*.{d}"):   # PAAS_API_HOST may point at an alt domain (sslip.io) pre-DNS
        recs.append(("A", host, ip))
        if ipv6: recs.append(("AAAA", host, ipv6))
    return recs
def main(argv=None, client=None, run=subprocess.run, sleep=10):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", default=os.path.join(HERE, "golive.env")); ap.add_argument("--pubkey", default=os.path.expanduser("~/.ssh/id_ed25519.pub"))
    ap.add_argument("--type", default=None); ap.add_argument("--no-ssh", action="store_true", help="only create the server")
    a = ap.parse_args(argv)
    env = load_env(a.env); tok = os.environ.get("HCLOUD_TOKEN") or env.get("HCLOUD_TOKEN")
    if not tok or "<" in tok: sys.exit("HCLOUD_TOKEN missing: Hetzner Console -> project -> Security -> API tokens (Read & Write)")
    pubkey = open(a.pubkey).read().strip()
    h = Hetzner(tok, client)
    stype = a.type or env.get("HCLOUD_EDGE_TYPE") or "cx33"
    locs = (env.get("HCLOUD_LOCATION") or "fsn1").split(",") + [l for l in ("fsn1", "nbg1", "hel1") if l not in (env.get("HCLOUD_LOCATION") or "fsn1")]
    ssh_cidrs = (env.get("PAAS_SSH_ALLOW_CIDR") or "0.0.0.0/0,::/0").split(",")
    print("1/5 ssh key + firewall"); kid = h.ssh_key(pubkey); fw = h.firewall(ssh_cidrs)
    print(f"2/5 server {NAME} ({stype})"); s, new = h.server(stype, locs, cloud_init(pubkey), kid, fw)
    s = h.wait_running(s["id"], sleep=sleep if client is None else 0)
    ip = s["public_net"]["ipv4"]["ip"]; ip6 = ((s["public_net"].get("ipv6") or {}).get("ip") or "").replace("::/64", "::1") or None
    print(f"   {'created' if new else 'found'} {NAME} {ip} in {(s.get('location') or s.get('datacenter', {}).get('location') or {}).get('name', '?')}")
    if not a.no_ssh:
        ssh = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", f"box@{ip}"]
        print("3/5 waiting for cloud-init + setup-box.sh (5-10 min)")
        for _ in range(120):
            if run(ssh + ["test -f /var/lib/aloof-ready"], capture_output=True).returncode == 0: break
            time.sleep(sleep)
        else: sys.exit("setup did not finish; ssh in and check /workspace/paas/data/setup-box.log")
        print("4/5 copying golive.env (0600) and running deploy/golive.sh")
        body = fill_env(open(a.env).read(), ip)
        p = run(ssh + ["umask 077 && cat > /workspace/paas/deploy/golive.env"], input=body.encode(), capture_output=True)
        if p.returncode: sys.exit(f"copy failed: {p.stderr[-300:]}")
        p = run(ssh + ["cd /workspace/paas && git checkout -q -- deploy/supervisord.conf && git pull -q --ff-only && bash deploy/golive.sh"], capture_output=True)
        if p.returncode: sys.exit(f"golive.sh failed: {(p.stdout + p.stderr)[-500:]}")
    print("5/5 create these DNS records at your registrar (TTL 300):")
    filled = {k: fill_env(v, ip) for k, v in env.items()}
    for t, host, val in dns_records(filled, ip, ip6): print(f"   {t:5} {host:28} {val}")
    print(f"Then open https://{env.get('PAAS_BASE_DOMAIN', 'aloof.si')}/ (certificates are issued on first request).")
    return {"ip": ip, "ipv6": ip6, "created": new}
if __name__ == "__main__": main()
