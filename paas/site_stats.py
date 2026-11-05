"""First-party website visitors and clicks for the admin dashboard (privacy-conscious; complements PostHog).
Collected by a beacon in static/analytics.js (only on platform hosts, only when JS runs, so most bots never count):
  pageview  -> site_counts(day, 'pv', path) += 1 and a daily-unique visitor hash in site_visitors
  CTA click -> site_counts(day, 'cta', <data-cta>) += 1   (only elements marked data-cta: sign-up/pricing/deploy buttons)
Visitor hash = sha256(daily salt + IP bucket + User-Agent)[:16]. The salt is random per UTC day, kept in the DB only for that day
and deleted afterwards, so hashes can't be linked across days or turned back into an IP. No IPs, UAs, cookies or account ids stored.
Never counted: API traffic (/v1/*), the dashboard/admin/auth pages, bots by UA, logged-in test/admin accounts, non-platform hosts."""
import re, time, hashlib, secrets, datetime as _dt
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response
from .db import q
R = APIRouter()
PATH_RE = re.compile(r"^/[A-Za-z0-9/_\-.]{0,120}$")
CTA_RE = re.compile(r"^[a-z0-9_\-]{1,40}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,62}\.)+[a-z]{2,24}$")   # outbound link destination host only (no path/query)
EXCLUDE = ("/v1/", "/dashboard", "/admin", "/auth", "/login", "/register", "/email/", "/static/", "/console")
BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|curl|wget|python-|httpx|go-http|java/|monitor|uptime|testclient", re.I)
MAX_DAYS = 366
def _day(ts=None): return _dt.datetime.fromtimestamp(ts or time.time(), _dt.timezone.utc).strftime("%Y-%m-%d")
def _salt(day):
    r = q("select salt from site_salt where day=?", (day,), one=True)
    if r: return r["salt"]
    q("insert into site_salt(day, salt) values(?, ?) on conflict do nothing", (day, secrets.token_hex(16)))
    q("delete from site_salt where day<?", (day,))   # yesterday's salt is gone: old hashes can't be recomputed
    return q("select salt from site_salt where day=?", (day,), one=True)["salt"]
def _bump(day, kind, key):
    q("insert into site_counts(day, kind, key, n) values(?, ?, ?, 1) on conflict(day, kind, key) do update set n=site_counts.n+1", (day, kind, key))
def _skip_account(request):
    """Logged-in test or admin accounts aren't website visitors."""
    try:
        from . import auth
        tok = request.cookies.get(auth.COOKIE) or request.cookies.get(auth.LEGACY_COOKIE)
        a = auth.session_account(tok) if tok else None
        return bool(a and (a.get("is_test") or a.get("is_admin")))
    except Exception: return False
def record(request, kind, path, cta=None, now=None):
    """-> True if counted. Never raises for bad input; just ignores it."""
    from .site import analytics_hosts
    from .netaddr import client_ip, rate_key
    host = (request.headers.get("host") or "").split(":")[0].lower(); ua = request.headers.get("user-agent") or ""
    if host not in analytics_hosts() or not ua or BOT_RE.search(ua): return False
    if not isinstance(path, str) or not PATH_RE.match(path) or path.startswith(EXCLUDE) or path in ("/v1",): return False
    if kind == "cta" and not (isinstance(cta, str) and CTA_RE.match(cta)): return False
    if kind == "out":
        if not (isinstance(cta, str) and HOST_RE.match(cta.lower())) or cta.lower() in analytics_hosts(): return False
        cta = cta.lower()
    if kind not in ("pv", "cta", "out") or _skip_account(request): return False
    day = _day(now)
    if kind == "pv":
        vh = hashlib.sha256(f"{_salt(day)}|{rate_key(client_ip(request))}|{ua[:300]}".encode()).hexdigest()[:16]
        q("insert into site_visitors(day, vh) values(?, ?) on conflict do nothing", (day, vh))
        _bump(day, "pv", path)
    else: _bump(day, kind, cta)
    return True
@R.post("/v1/site/hit", include_in_schema=False)
async def hit(request: Request):
    from .netaddr import limit; limit("site_hit", request, n=300, window=600)
    try: b = await request.json()
    except Exception: b = None
    if isinstance(b, dict): record(request, b.get("k"), b.get("p"), b.get("c"))
    return Response(status_code=204)
def _range(frm, to):
    today = _dt.datetime.now(_dt.timezone.utc).date()
    try:
        t = _dt.date.fromisoformat(to) if to else today
        f = _dt.date.fromisoformat(frm) if frm else t - _dt.timedelta(days=29)
    except ValueError: raise HTTPException(400, "from/to must be YYYY-MM-DD (UTC)")
    if f > t: raise HTTPException(400, "from must be on or before to")
    if (t - f).days + 1 > MAX_DAYS: raise HTTPException(400, f"range is at most {MAX_DAYS} days")
    return f, t
def report(frm=None, to=None, top=20):
    f, t = _range(frm, to); fs, ts = f.isoformat(), t.isoformat()
    vis = {r["day"]: r["n"] for r in q("select day, count(*) n from site_visitors where day between ? and ? group by day", (fs, ts))}
    cnt = q("select day, kind, key, n from site_counts where day between ? and ?", (fs, ts))
    pv, ct, pages, ctas, oc, outs = {}, {}, {}, {}, {}, {}
    for r in cnt:
        if r["kind"] == "out": oc[r["day"]] = oc.get(r["day"], 0) + r["n"]; outs[r["key"]] = outs.get(r["key"], 0) + r["n"]
        if r["kind"] == "pv": pv[r["day"]] = pv.get(r["day"], 0) + r["n"]; pages[r["key"]] = pages.get(r["key"], 0) + r["n"]
        elif r["kind"] == "cta": ct[r["day"]] = ct.get(r["day"], 0) + r["n"]; ctas[r["key"]] = ctas.get(r["key"], 0) + r["n"]
    days = [(f + _dt.timedelta(days=i)).isoformat() for i in range((t - f).days + 1)]
    since = (q("select min(day) d from site_counts", one=True) or {}).get("d")
    # no_data: nothing collected yet, or the whole range is before collection started. Days before collecting_since are null
    # (not measured), days after it with no traffic are 0 (measured, zero).
    status = "ok" if since and since <= ts else "no_data"
    m = lambda d, src: (src.get(d, 0) if since and d >= since else None)
    tot = lambda src: sum(src.values()) if status == "ok" else None
    return {"status": status, "range": {"from": fs, "to": ts, "days": len(days), "timezone": "UTC"}, "source": "first-party",
            "collecting_since": since, "generated_at": int(time.time()),
            "totals": {"visitor_days": tot(vis), "pageviews": tot(pv), "cta_clicks": tot(ct), "outbound_clicks": tot(oc)},
            "daily": [{"date": d, "visitors": m(d, vis), "pageviews": m(d, pv), "cta_clicks": m(d, ct), "outbound_clicks": m(d, oc)} for d in days],
            "outbound": [{"host": k, "clicks": v} for k, v in sorted(outs.items(), key=lambda x: (-x[1], x[0]))[:top]],
            "top_pages": [{"path": k, "pageviews": v} for k, v in sorted(pages.items(), key=lambda x: (-x[1], x[0]))[:top]],
            "ctas": [{"cta": k, "clicks": v} for k, v in sorted(ctas.items(), key=lambda x: (-x[1], x[0]))],
            "definitions": {"visitors": "unique visitors per UTC day (salted daily hash of IP bucket + browser; not linkable across days). "
                                        "totals.visitor_days is the sum of daily uniques, not unique people over the range.",
                            "pageviews": "website page loads with JavaScript on platform hosts; excludes API (/v1), dashboard, admin and auth pages, bots, and logged-in test/admin accounts.",
                            "cta_clicks": "clicks on public-site buttons/links marked data-cta (sign-up, pricing, deploy CTAs).",
                            "outbound_clicks": "clicks on public-site links to another website, counted by destination host only.",
                            "null": "not measured (before collecting_since); 0 means measured and zero."}}
def _admin(request: Request):
    from .www import require_admin; return require_admin(request)
@R.get("/v1/admin/site-analytics")
def admin_site_analytics(request: Request, frm: str = None, to: str = None, top: int = 20, _=Depends(_admin)):
    frm = request.query_params.get("from") or frm
    try: return report(frm, to, max(1, min(int(top), 100)))
    except HTTPException: raise
    except Exception as e:   # unavailable is never shown as zero
        import logging; logging.getLogger("paas.site_stats").warning("site analytics failed: %s", type(e).__name__)
        raise HTTPException(503, "site analytics unavailable")
