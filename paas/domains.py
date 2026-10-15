"""Custom domains.
1) Ownership: TXT  _aloof-challenge.<domain>  =  "aloof-verify=<token>"   (token issued when the domain is added)
2) Routing:   CNAME <domain> -> <app>.<base>   (apex: ALIAS/ANAME/CNAME-flattening)   or   A + AAAA -> PAAS_EDGE_IPS
Only domains passing both get certificates (Caddy on-demand TLS ask) and traffic. A background tick (tick()) re-checks every domain
with backoff: pending ones become verified on their own, verified ones whose DNS later breaks are flagged status='dns_error'
(still served, never removed), and unverified claims are released after CLAIM_TTL_DAYS days.
The only network access here is DNS lookups of the (validated) domain name; nothing ever connects to a user-supplied host."""
import os, re, time, secrets, ipaddress, threading, logging, dns.resolver, dns.exception
from concurrent.futures import ThreadPoolExecutor
log = logging.getLogger("paas.domains")
E = lambda k, d=None: os.environ.get(k, d)
CLAIM_TTL_DAYS = int(E("PAAS_DOMAIN_CLAIM_DAYS", "7"))            # unverified claims are released after this
CLAIM_GRACE_S = int(E("PAAS_DOMAIN_CLAIM_GRACE_S", "3600"))        # a pending claim can't be taken over by another account within this
MAX_VERIFIED_HOSTS = int(E("PAAS_MAX_CUSTOM_DOMAINS", "500"))      # platform-wide cap on certificate-triggering hosts (verified + paired www)
MAX_PENDING = int(E("PAAS_MAX_PENDING_DOMAINS", "5000"))           # platform-wide cap on unverified claims
VERIFIED_EVERY = int(E("PAAS_DOMAIN_RECHECK_S", "1800"))           # re-check a healthy verified domain every 30 min
PENDING_BACKOFF = [120, 300, 600, 1800, 3600, 10800, 21600]        # pending: 2m, 5m, 10m, 30m, 1h, 3h, then every 6h
ERROR_BACKOFF = [300, 900, 3600, 10800, 21600]                     # dns_error: 5m, 15m, 1h, 3h, then every 6h
DEFAULT_LIMITS = {"free": 1, "hobby": 3, "pro": 10}                # custom domains per account (every app counts)
BATCH = int(E("PAAS_DOMAIN_TICK_BATCH", "25"))
# rate limits (per account, in-memory sliding windows): (max events, window seconds)
RATE = {"add": (int(E("PAAS_DOMAIN_ADD_PER_HOUR", "20")), 3600), "verify": (int(E("PAAS_DOMAIN_VERIFY_PER_HOUR", "30")), 3600)}
VERIFY_MIN_GAP = int(E("PAAS_DOMAIN_VERIFY_GAP_S", "10"))          # same domain can't be manually verified more often than this
class LimitError(Exception):
    def __init__(self, status, msg, retry_after=None): super().__init__(msg); self.status = status; self.msg = msg; self.retry_after = retry_after
_hits = {}; _hl = threading.Lock()
def rate_check(kind, key, now=None):
    n, win = RATE[kind]; now = now or time.time()
    with _hl:
        h = [t for t in _hits.get((kind, key), []) if t > now - win]
        if len(h) >= n:
            _hits[(kind, key)] = h; raise LimitError(429, f"too many domain {kind} requests ({n} per {win // 3600} hour); try again in {int(h[0] + win - now) + 1}s", int(h[0] + win - now) + 1)
        h.append(now); _hits[(kind, key)] = h
_vlast = {}
def verify_gap(domain, now=None):
    now = now or time.time()
    with _hl:
        if len(_vlast) > 5000: _vlast.clear()
        t = _vlast.get(domain)
        if t and now - t < VERIFY_MIN_GAP: raise LimitError(429, f"{domain} was just checked; wait {int(VERIFY_MIN_GAP - (now - t)) + 1}s before verifying again (we also re-check automatically)", int(VERIFY_MIN_GAP - (now - t)) + 1)
        _vlast[domain] = now
def reset_limits():
    with _hl: _hits.clear(); _vlast.clear()
def limit_for(tier): return int(E(f"PAAS_DOMAIN_LIMIT_{(tier or 'free').upper()}", DEFAULT_LIMITS.get(tier or "free", DEFAULT_LIMITS["free"])))
# ---------------- names ----------------
_NAME = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
RESERVED_TLDS = {"local", "localhost", "internal", "lan", "home", "corp", "intranet", "test", "invalid", "example", "arpa", "onion", "alt", "private", "localdomain"}
def normalize(d): return (d or "").lower().strip().strip(".")
def valid_error(d, blocked=()):
    """None if d is an acceptable custom domain, else a human-readable reason."""
    if not _NAME.match(d) or "xn--" in d.split(".")[-1]: return "invalid domain name (use a hostname like api.example.com, without http:// or a path)"
    for b in blocked:
        if b and (d == b or d.endswith("." + b)): return f"{b} hosts are managed by the platform"
    if d.split(".")[-1] in RESERVED_TLDS: return f"'.{d.split('.')[-1]}' is a reserved/private suffix and can't get a public certificate"
    try: ipaddress.ip_address(d); return "IP addresses aren't allowed; use a domain name"
    except ValueError: pass
    return None
SLD = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "nom"}
def is_apex(d):
    """Heuristic registrable-domain test without a public-suffix list: example.com / example.co.uk are apex, api.example.com isn't."""
    p = d.split(".")
    if len(p) == 2: return True
    return len(p) == 3 and p[1] in SLD and len(p[2]) == 2
def partner(d):
    """The www/apex twin of a domain: www.example.com <-> example.com. None for other subdomains."""
    if d.startswith("www.") and is_apex(d[4:]): return d[4:]
    if is_apex(d): return "www." + d
    return None
def new_token(): return secrets.token_hex(16)
def txt_name(domain): return f"_aloof-challenge.{domain}"
# ---------------- records to show ----------------
def edge_split(edge_ips):
    return [x for x in edge_ips if ":" not in x], [x for x in edge_ips if ":" in x]
def instructions(domain, app, base, token, edge_ips):
    v4, v6 = edge_split(edge_ips); apex = is_apex(domain)
    routing = [{"type": "CNAME", "name": domain, "value": f"{app}.{base}", "use": "subdomains (apex only with ALIAS/ANAME/CNAME flattening)"}] \
        + [{"type": "A", "name": domain, "value": ip, "use": "apex domains, or instead of the CNAME"} for ip in v4] \
        + [{"type": "AAAA", "name": domain, "value": ip, "use": "IPv6, add alongside the A record"} for ip in v6]
    out = {"ownership": {"type": "TXT", "name": txt_name(domain), "value": f"aloof-verify={token}"}, "routing": routing,
           "a": v4, "aaaa": v6, "is_apex": apex,
           "recommended": ([{"type": "A", "name": domain, "value": ip} for ip in v4] + [{"type": "AAAA", "name": domain, "value": ip} for ip in v6]) if apex else [routing[0]]}
    out["note"] = (f"{domain} is a root (apex) domain: DNS doesn't allow a plain CNAME there. Add the A (and AAAA) records, or, if your DNS provider offers ALIAS/ANAME/CNAME flattening, "
                   f"point that at {app}.{base} instead. Don't add both styles.") if apex else \
                  f"Add the CNAME; or the A/AAAA records if you can't use a CNAME. If your DNS is on Cloudflare, set the records to DNS only (grey cloud)."
    return out
def pair_hint(domain, app, base, edge_ips):
    p = partner(domain)
    if not p: return None
    return {"domain": p, "redirects_to": domain, "status": 308, "note": f"Once {domain} is verified, https://{p} is redirected (308) to https://{domain}. Point {p} at us too (CNAME {app}.{base}, or the same A/AAAA records) so the redirect can be served; no extra verification needed.",
            "dns_records": instructions(p, app, base, "<n/a>", edge_ips)["routing"]}
# ---------------- DNS checks (lookups only) ----------------
def resolver():
    r = dns.resolver.Resolver(configure=not E("PAAS_DNS_SERVER"))
    if E("PAAS_DNS_SERVER"):   # e.g. "127.0.0.1:5353" (tests) or "1.1.1.1"
        host, _, port = E("PAAS_DNS_SERVER").partition(":"); r.nameservers = [host]; r.port = int(port or 53)
    r.lifetime = float(E("PAAS_DNS_TIMEOUT", "5")); return r
CF_NETS = [ipaddress.ip_network(n) for n in ("173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
           "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32")]
def _is_cf(ip):
    try: a = ipaddress.ip_address(ip)
    except ValueError: return False
    return any(a.version == n.version and a in n for n in CF_NETS)
def check(domain, app, base, token, edge_ips):
    """-> {owned, routed, verified, detail[], transient}. transient=True when a lookup failed for a reason other than 'no such record'
    (timeout, SERVFAIL): the caller must not treat that as evidence the DNS is wrong."""
    r = resolver(); out = {"owned": False, "routed": False, "detail": [], "transient": False}; v4, v6 = edge_split(edge_ips)
    try:
        vals = {b"".join(x.strings).decode() for x in r.resolve(txt_name(domain), "TXT")}
        out["owned"] = f"aloof-verify={token}" in vals
        if not out["owned"]: out["detail"].append(f"TXT {txt_name(domain)} found {sorted(vals)} but not aloof-verify=<token>")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer): out["detail"].append(f"TXT {txt_name(domain)} not found")
    except dns.exception.DNSException as e: out["transient"] = True; out["detail"].append(f"TXT lookup failed: {e.__class__.__name__}")
    target = f"{app}.{base}"
    try:
        cn = [str(x.target).rstrip(".").lower() for x in r.resolve(domain, "CNAME")]
        out["routed"] = target in cn
        if not out["routed"]: out["detail"].append(f"CNAME points to {cn}, expected {target}")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as e:
        if isinstance(e, dns.exception.Timeout): out["transient"] = True
        ips4 = set(); ips6 = set()
        for t, s in (("A", ips4), ("AAAA", ips6)):
            try: s |= {x.address for x in r.resolve(domain, t)}
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer): pass
            except dns.exception.DNSException: out["transient"] = True
        ok4 = bool(ips4 & set(v4)) if v4 else False; ok6 = bool(ips6 & set(v6))
        # all-edge: IPv4 must hit us; if the edge has an IPv6 address, any AAAA you publish must be ours too (else IPv6 visitors go elsewhere)
        bad6 = bool(v6) and bool(ips6) and not ok6; bad4 = bool(ips4) and not ok4
        out["routed"] = (ok4 or (not v4 and ok6)) and not bad6 and not (bad4 and not ok4)
        if not out["routed"]:
            allips = sorted(ips4 | ips6)
            out["detail"].append(f"{domain} resolves to {allips or 'nothing'}; expected CNAME {target} or A {sorted(v4)}" + (f" and AAAA {sorted(v6)}" if v6 else ""))
            if any(_is_cf(i) for i in allips): out["detail"].append("these look like Cloudflare proxy addresses: set the record to DNS only (grey cloud), then verify again")
            if bad6 and ok4: out["detail"].append(f"your AAAA record points elsewhere ({sorted(ips6)}): remove it or set it to {sorted(v6)}")
    out["verified"] = out["owned"] and out["routed"]; return out
# ---------------- state machine ----------------
def backoff(table, n): return table[min(max(n, 0), len(table) - 1)]
def cert_hosts(q):
    """Number of hosts that can trigger a certificate: every verified domain plus its www/apex twin."""
    rows = [r["domain"] for r in q("select domain from domains where verified=1")]; s = set(rows)
    for d in rows:
        p = partner(d)
        if p: s.add(p)
    return len(s)
def apply(q, d, r, now=None, manual=False):
    """Fold one check result into the row. Returns the new status."""
    now = now or time.time(); was = bool(d["verified"]); detail = "; ".join(r["detail"])[:500]
    f = int(d.get("fail_count") or 0); n = int(d.get("check_count") or 0) + 1
    if r["verified"]:
        if not was and cert_hosts(q) >= MAX_VERIFIED_HOSTS:
            q("update domains set owned=1, routed=1, status='pending', last_checked_at=?, next_check_at=?, check_count=?, last_detail=? where domain=?",
              (now, now + 3600, n, "DNS is correct, but the platform's custom-domain capacity is full right now; we'll retry automatically", d["domain"]))
            return "pending"
        q("update domains set owned=1, routed=1, verified=1, status='verified', fail_count=0, check_count=?, verified_at=coalesce(verified_at,?), last_checked_at=?, next_check_at=?, last_detail=null where domain=?",
          (n, now, now, now + VERIFIED_EVERY, d["domain"])); return "verified"
    if r.get("transient") and not manual:      # resolver trouble is not evidence the DNS broke
        q("update domains set last_checked_at=?, next_check_at=?, last_detail=? where domain=?", (now, now + 300, detail or "DNS lookup failed; retrying", d["domain"]))
        return d.get("status") or "pending"
    if was:
        f += 1; flagged = manual or f >= 2; st = "dns_error" if flagged else (d.get("status") or "verified")
        q("update domains set owned=?, routed=?, status=?, fail_count=?, check_count=?, last_checked_at=?, next_check_at=?, last_detail=? where domain=?",
          (int(r["owned"]), int(r["routed"]), st, f, n, now, now + (backoff(ERROR_BACKOFF, f - 2) if flagged else 300), detail, d["domain"])); return st
    q("update domains set owned=?, routed=?, status='pending', check_count=?, last_checked_at=?, next_check_at=?, last_detail=? where domain=?",
      (int(r["owned"]), int(r["routed"]), n, now, now + backoff(PENDING_BACKOFF, n - 1), detail, d["domain"])); return "pending"
def release_expired(q, now=None):
    now = now or time.time(); cutoff = now - CLAIM_TTL_DAYS * 86400
    old = q("select domain from domains where verified=0 and created<?", (cutoff,))
    for r in old: q("delete from domains where domain=? and verified=0", (r["domain"],))
    return [r["domain"] for r in old]
def tick(q=None, base=None, edge_ips=None, now=None):
    """Controller tick: release stale claims, then re-check up to BATCH due domains (in parallel; lookups only)."""
    if q is None: from .db import q
    if base is None or edge_ips is None:
        from . import api; base = base or api.BASE_DOMAIN; edge_ips = edge_ips if edge_ips is not None else api.EDGE_IPS
    now = now or time.time(); gone = release_expired(q, now)
    if gone: log.info("released %d unverified domain claims older than %dd", len(gone), CLAIM_TTL_DAYS)
    due = q("select * from domains where coalesce(next_check_at,0)<=? order by coalesce(next_check_at,0) limit ?", (now, BATCH))
    if not due: return 0
    def one(d):
        try: return d, check(d["domain"], d["app"], base, d["txt_token"], edge_ips)
        except Exception: log.exception("domain check %s", d["domain"]); return d, None
    with ThreadPoolExecutor(max_workers=8) as ex: res = list(ex.map(one, due))
    for d, r in res:
        if r is None: q("update domains set next_check_at=? where domain=?", (now + 300, d["domain"])); continue
        fresh = q("select * from domains where domain=?", (d["domain"],), one=True)
        if fresh and fresh["app"] == d["app"]: apply(q, fresh, r, now)
    return len(res)
