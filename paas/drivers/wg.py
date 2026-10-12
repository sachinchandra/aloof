"""WireGuard hub on the control plane (wg0, 10.88.0.1/16, UDP 51820). Nodes dial out to the hub (no inbound ports
needed on nodes); the edge proxy reaches node containers at <wg_ip>:<port> through the tunnel."""
import os, subprocess, ipaddress, base64, logging
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization
log = logging.getLogger("paas.wg")
IFACE = os.environ.get("PAAS_WG_IFACE", "wg0")
NET = ipaddress.ip_network(os.environ.get("PAAS_WG_NET", "10.88.0.0/16"))
HUB_IP = str(next(NET.hosts()))
def keypair():
    k = X25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()
def _run(*a):
    cmd = (["sudo", "-n"] if os.geteuid() != 0 else []) + list(a)
    return subprocess.run(cmd, capture_output=True, text=True)
def iface_up(): return _run("wg", "show", IFACE).returncode == 0
def hub_pubkey():
    if os.environ.get("PAAS_WG_HUB_PUBKEY"): return os.environ["PAAS_WG_HUB_PUBKEY"]
    p = _run("wg", "show", IFACE, "public-key"); return p.stdout.strip() if p.returncode == 0 else None
def hub_endpoint(): return os.environ.get("PAAS_WG_ENDPOINT", f"{os.environ.get('PAAS_EDGE_IPS','127.0.0.1').split(',')[0]}:51820")
def allocate_ip(used):
    used = set(used) | {HUB_IP}
    for h in NET.hosts():
        if str(h) not in used: return str(h)
    raise RuntimeError("WireGuard address space exhausted")
def add_peer(pubkey, ip):
    if not pubkey: return False
    if not iface_up(): log.warning("wg hub %s not up; peer %s recorded only", IFACE, ip); return False
    p = _run("wg", "set", IFACE, "peer", pubkey, "allowed-ips", f"{ip}/32")
    if p.returncode: log.error("wg set failed: %s", p.stderr); return False
    return True
def remove_peer(pubkey):
    if pubkey and iface_up(): _run("wg", "set", IFACE, "peer", pubkey, "remove")
def sync_peers(nodes):
    """Re-apply all live peers (after control-plane/hub restart)."""
    return sum(add_peer(n["wg_pubkey"], n["wg_ip"]) for n in nodes if n.get("wg_pubkey") and n.get("wg_ip"))
def render_hub_conf(private_key, listen_port=51820):
    return f"[Interface]\nAddress = {HUB_IP}/{NET.prefixlen}\nListenPort = {listen_port}\nPrivateKey = {private_key}\n"
