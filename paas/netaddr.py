"""Client address helpers shared by the API and the MCP server.
client_ip: X-Forwarded-For (last hop, which Caddy appends) only when the socket peer is a trusted proxy; otherwise the peer.
rate_key: the bucket a rate limit counts against: IPv4 as-is, IPv4-mapped IPv6 as its IPv4, other IPv6 as its /64."""
import os, ipaddress
def _csv(*names):
    return [x.strip() for n in names for x in os.environ.get(n, "").split(",") if x.strip()]
def trusted_proxy(peer, extra_env=("PAAS_TRUSTED_PROXIES", "PAAS_EDGE_IPS")):
    try: a = ipaddress.ip_address((peer or "").split("%", 1)[0])
    except ValueError: return False
    a = getattr(a, "ipv4_mapped", None) or a
    if a.is_loopback: return True
    for e in _csv(*extra_env):
        try:
            if a in ipaddress.ip_network(e if "/" in e else (f"{e}/64" if ":" in e else f"{e}/32"), strict=False): return True
        except ValueError: continue
    return False
def client_ip(request, extra_env=("PAAS_TRUSTED_PROXIES", "PAAS_EDGE_IPS")):
    """Socket peer, or the last X-Forwarded-For hop when the peer is our Caddy (loopback, or a listed proxy/edge address; a bare IPv6 edge entry covers its /64)."""
    peer = request.client.host if request.client else ""
    if trusted_proxy(peer, extra_env):
        xff = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
        if xff: return xff[-1]
    return peer or "unknown"
def rate_key(ip):
    try: a = ipaddress.ip_address((ip or "").split("%", 1)[0])
    except ValueError: return ip or "unknown"
    m = getattr(a, "ipv4_mapped", None)
    if m: return str(m)
    return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)
