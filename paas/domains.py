"""Custom domain verification.
1) Ownership: TXT  _aloof-challenge.<domain>  =  "aloof-verify=<token>"   (token issued when the domain is added)
2) Routing:   CNAME <domain> -> <app>.<base>   or   A/AAAA -> one of PAAS_EDGE_IPS
Only domains passing both get certificates (Caddy on-demand TLS ask) and traffic."""
import os, secrets, dns.resolver, dns.exception
E = lambda k, d=None: os.environ.get(k, d)
def resolver():
    r = dns.resolver.Resolver(configure=not E("PAAS_DNS_SERVER"))
    if E("PAAS_DNS_SERVER"):   # e.g. "127.0.0.1:5353" (tests) or "1.1.1.1"
        host, _, port = E("PAAS_DNS_SERVER").partition(":"); r.nameservers = [host]; r.port = int(port or 53)
    r.lifetime = 5; return r
def new_token(): return secrets.token_hex(16)
def txt_name(domain): return f"_aloof-challenge.{domain}"
def instructions(domain, app, base, token, edge_ips):
    return {"ownership": {"type": "TXT", "name": txt_name(domain), "value": f"aloof-verify={token}"},
            "routing": [{"type": "CNAME", "name": domain, "value": f"{app}.{base}"}] + [{"type": "A", "name": domain, "value": ip} for ip in edge_ips if ":" not in ip]}
def check(domain, app, base, token, edge_ips):
    r = resolver(); out = {"owned": False, "routed": False, "detail": []}
    try:
        vals = {b"".join(x.strings).decode() for x in r.resolve(txt_name(domain), "TXT")}
        out["owned"] = f"aloof-verify={token}" in vals
        if not out["owned"]: out["detail"].append(f"TXT {txt_name(domain)} found {sorted(vals)} but not aloof-verify=<token>")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer): out["detail"].append(f"TXT {txt_name(domain)} not found")
    except dns.exception.DNSException as e: out["detail"].append(f"TXT lookup failed: {e.__class__.__name__}")
    try:
        cn = [str(x.target).rstrip(".") for x in r.resolve(domain, "CNAME")]
        out["routed"] = f"{app}.{base}" in cn
        if not out["routed"]: out["detail"].append(f"CNAME points to {cn}, expected {app}.{base}")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException):
        ips = set()
        for t in ("A", "AAAA"):
            try: ips |= {x.address for x in r.resolve(domain, t)}
            except dns.exception.DNSException: pass
        out["routed"] = bool(ips & set(edge_ips))
        if not out["routed"]: out["detail"].append(f"{domain} resolves to {sorted(ips) or 'nothing'}; expected CNAME {app}.{base} or A {sorted(edge_ips)}")
    out["verified"] = out["owned"] and out["routed"]; return out
