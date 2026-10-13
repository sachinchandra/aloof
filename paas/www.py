"""Content site (blog, guides, changelog, example architectures, regions, about, security, status), SEO files, uptime checks and the owner admin dashboard."""
import os, time, json, html, ssl, socket, shutil, threading, logging, csv, io, datetime, urllib.request
from email.utils import formatdate
from fastapi import APIRouter, HTTPException, Request, Header, Depends
from fastapi.responses import HTMLResponse, Response, PlainTextResponse, FileResponse
from . import site
from .db import q
from .plans import PLANS, TIERS
from . import plans as _plans
from .www_blog import posts as _posts
from .www_content import GUIDES, guide_html, CHANGELOG, ARCHES, LIVE_REGIONS, SOON_REGIONS, ABOUT, SECURITY
log = logging.getLogger("www"); R = APIRouter()
ROOT = os.path.dirname(os.path.dirname(__file__)); SITE = site.SITE
ADMIN_EMAIL = os.environ.get("PAAS_ADMIN_EMAIL", "sachin@aloof.si").lower()
POSTS = _posts(); BY_SLUG = {p["slug"]: p for p in POSTS}
def _d(s): return datetime.date.fromisoformat(s).strftime("%b %-d, %Y")
def render(path, title, desc, body, og="default", typ="website"):
    site.CUR.update(path=path, og=og, type=typ)
    try: return site.page(title, body, desc=desc)
    finally: site.CUR.update(path="/", og="default", type="website")
def at(path, og, fn, *a):
    site.CUR.update(path=path, og=og)
    try: return fn(*a)
    finally: site.CUR.update(path="/", og="default", type="website")
def shell(eyebrow, h1, lead, inner, narrow=False):
    w = "max-width:820px" if narrow else ""
    return f"""<section class="hero hero-sm"><div class=wrap style="display:block;{w}"><div class=eyebrow>{eyebrow}</div><h1>{h1}</h1><p class=lead>{lead}</p></div></section>
<section class=s><div class=wrap style="{w}">{inner}</div></section>"""
# ---------------- blog ----------------
@R.get("/blog", response_class=HTMLResponse)
def blog_index():
    cards = "".join(f"""<a class="card reveal post-card" href="/blog/{p['slug']}"><div class=meta><time datetime="{p['date']}">{_d(p['date'])}</time>{''.join(f'<span class=tag>{t}</span>' for t in p['tags'])}</div><h3>{p['title']}</h3><p>{p['summary']}</p><span class=more>Read →</span></a>""" for p in POSTS)
    return render("/blog", "Blog · Aloof", "Engineering notes on deploying backends, scale-to-zero, private networking, backups and MCP.",
                  shell("Blog", "Engineering notes", "How the platform works, with real commands and the arithmetic shown. <a href='/rss.xml'>RSS</a>", f"<div class='grid g2'>{cards}</div>"), og="blog")
@R.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(slug: str):
    p = BY_SLUG.get(slug)
    if not p: raise HTTPException(404, "no such post")
    more = "".join(f"<li><a href='/blog/{x['slug']}'>{x['title']}</a></li>" for x in POSTS if x is not p)[:3000]
    ld = json.dumps({"@context": "https://schema.org", "@type": "BlogPosting", "headline": p["title"], "datePublished": p["date"], "description": p["summary"], "publisher": {"@type": "Organization", "name": "Aloof"}, "url": f"{SITE}/blog/{slug}"})
    body = f"""<article class="wrap prose" style="max-width:760px;padding:64px 24px 40px"><a class=muted href="/blog">← Blog</a><div class=meta style="margin-top:22px"><time datetime="{p['date']}">{_d(p['date'])}</time>{''.join(f'<span class=tag>{t}</span>' for t in p['tags'])}</div>
<h1>{p['title']}</h1><p class=lead>{p['summary']}</p>{p['html']}
<div class="card" style="margin-top:44px"><h3>Try it</h3><p>Free tier, hard-capped at $1 of usage. No card needed.</p><div class=cta style="margin-top:14px"><a class="btn pri" href="/login#register">Start free</a><a class=btn href="/guides">Framework guides</a></div></div>
<h2 style="margin-top:48px">More posts</h2><ul>{more}</ul></article><script type="application/ld+json">{ld}</script>"""
    return render(f"/blog/{slug}", f"{p['title']} · Aloof", p["summary"], body, og=f"blog-{slug}", typ="article")
@R.get("/rss.xml")
def rss():
    items = "".join(f"<item><title>{html.escape(p['title'])}</title><link>{SITE}/blog/{p['slug']}</link><guid>{SITE}/blog/{p['slug']}</guid><pubDate>{formatdate(time.mktime(time.strptime(p['date'], '%Y-%m-%d')), usegmt=True)}</pubDate><description>{html.escape(p['summary'])}</description></item>" for p in POSTS)
    x = f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>Aloof blog</title><link>{SITE}/blog</link><atom:link href="{SITE}/rss.xml" rel="self" type="application/rss+xml"/><description>Engineering notes from Aloof</description><language>en</language>{items}</channel></rss>'
    return Response(x, media_type="application/rss+xml")
# ---------------- guides ----------------
@R.get("/guides", response_class=HTMLResponse)
def guides_index():
    cards = "".join(f"<a class='card reveal post-card' href='/guides/{k}'><div class=meta><span class=tag>{v[1]}</span></div><h3>{v[0]}</h3><p>Detected from {v[2]}. {('Release command: <code>' + v[4] + '</code>.') if v[4] else ''}</p><span class=more>Open guide →</span></a>" for k, v in GUIDES.items())
    return render("/guides", "Framework guides · Aloof", "Quickstarts for FastAPI, Django, Flask, Next.js, Express, Rails, Go and any Docker image.",
                  shell("Guides", "Framework quickstarts", "Each guide uses <code>aloof launch</code> to generate config, then <code>aloof deploy</code>. Same workflow for every stack.", f"<div class=grid>{cards}</div>"), og="guides")
@R.get("/guides/{slug}", response_class=HTMLResponse)
def guide(slug: str):
    if slug not in GUIDES: raise HTTPException(404, "no such guide")
    n = GUIDES[slug][0]
    return render(f"/guides/{slug}", f"Deploy {n} · Aloof guides", f"Deploy a {n} app to Aloof: aloof launch, aloof.toml, Postgres, deploy.",
                  f"<article class='wrap prose' style='max-width:760px;padding:64px 24px 60px'><a class=muted href='/guides'>← Guides</a><h1>Deploy {n}</h1>{guide_html(slug)}</article>", og="guides")
# ---------------- changelog ----------------
@R.get("/changelog", response_class=HTMLResponse)
def changelog():
    rows = "".join(f"<div class='cl reveal'><time datetime='{d}'>{_d(d)}</time><div><h3>{t}</h3><ul>{''.join(f'<li>{i}</li>' for i in items)}</ul></div></div>" for d, t, items in CHANGELOG)
    return render("/changelog", "Changelog · Aloof", "What has shipped on Aloof, by date.", shell("Changelog", "What's shipped", "Taken from the repository history. Only things that are live are listed.", rows, narrow=True), og="changelog")
# ---------------- example architectures ----------------
PARTNER_FORM = """<form class="card form-card" id=dp onsubmit="event.preventDefault();var f=this;fetch('/v1/waitlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:f.email.value,kind:'design_partner',company:f.company.value,note:f.note.value})}).then(r=>r.json().then(j=>{f.innerHTML='<p class='+(r.ok?'ok':'bad')+'><b>'+(j.message||j.detail)+'</b></p>'}))">
<h3>Become a design partner</h3><p>Run a real workload on Aloof and tell us what's missing. In return: direct access to the people building it, priority on the features you need, and credits while we iterate.</p>
<div class=fgrid><input name=email type=email required placeholder="you@company.com" aria-label=Email><input name=company placeholder="Company or project" aria-label=Company maxlength=200></div>
<textarea name=note rows=3 placeholder="What would you run? (stack, traffic, region)" aria-label="What would you run" maxlength=1000></textarea><button class="btn pri">Apply</button></form>"""
@R.get("/customers", response_class=HTMLResponse)
def customers():
    cards = "".join(f"<div class='card reveal'><span class=tag>Reference build</span><h3>{t}</h3><p>{d}</p><ul>{''.join(f'<li>{i}</li>' for i in items)}</ul>{f'<a href=/blog/{l}>Read the walkthrough →</a>' if l else ''}</div>" for t, d, items, l in ARCHES)
    inner = f"""<p class=muted style="margin:0 0 22px">These are reference architectures we built and tested ourselves. They're not customer stories, and no company names, logos or metrics are implied.</p>
<div class='grid g3'>{cards}</div><div id=partner style="margin-top:56px">{PARTNER_FORM}</div>"""
    return render("/customers", "Example architectures · Aloof", "Reference architectures on Aloof: SaaS API with worker, Next.js with private API, scale-to-zero webhooks, MCP-operated backends.",
                  shell("Example architectures", "What you can build on Aloof", "Clearly labeled reference builds, each with the plans and features it uses.", inner), og="customers")
# ---------------- regions ----------------
@R.get("/regions", response_class=HTMLResponse)
def regions():
    live = "".join(f"<tr><td><code>{c}</code></td><td><b>{l}</b></td><td>{p}</td><td>{w}</td><td><span class='status s-running'>live</span></td></tr>" for c, l, p, w in LIVE_REGIONS)
    soon = "".join(f"<div class='card reveal'><span class='tag tag-soon'>Coming soon · not live</span><h3>{l}</h3><p class=muted style='font-size:13px'>{p}</p><p>{w}</p><button class='btn sm' style='margin-top:14px' onclick=\"wl('{c}','{l}')\">Join waitlist</button></div>" for c, l, p, w in SOON_REGIONS)
    opts = "".join(f"<option value='{c}'>{l}</option>" for c, l, _, _ in SOON_REGIONS)
    inner = f"""<h2 class=t>Live</h2><div class=scroll><table class=tbl><tr><th>Code</th><th>Location</th><th>Provider</th><th>What runs there</th><th>Status</th></tr>{live}</table></div>
<p class=muted>Typical round trip to Nuremberg: about 10–40 ms within Europe, 90–110 ms from US East, 150–200 ms from India or US West. Apps can't choose another region yet.</p>
<h2 class=t style="margin-top:56px">Coming soon</h2><p class=t>None of these is available today, and we don't have dates. Join the waitlist for a region and we'll email you once, when it's live.</p><div class='grid g3'>{soon}</div>
<form class="card form-card" id=rw style="margin-top:28px" onsubmit="event.preventDefault();var f=this;fetch('/v1/waitlist',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:f.email.value,kind:'region',region:f.region.value}})}}).then(r=>r.json().then(j=>{{rwmsg.className=r.ok?'ok':'bad';rwmsg.textContent=j.message||j.detail}}))">
<h3>Region waitlist</h3><div class=fgrid><input name=email type=email required placeholder="you@company.com" aria-label=Email><select name=region aria-label=Region>{opts}</select></div><button class="btn pri">Join waitlist</button><p id=rwmsg role=status aria-live=polite></p></form>
<script>function wl(c){{rw.region.value=c;rw.scrollIntoView({{behavior:'smooth',block:'center'}});rw.email.focus()}}</script>"""
    return render("/regions", "Regions · Aloof", "Aloof runs compute in Nuremberg, Germany, with encrypted off-site backups in Helsinki, Finland. Planned regions and waitlist.",
                  shell("Regions", "Where your apps run", "One live compute region today: Nuremberg, Germany. We list other regions only as coming soon, never as available.", inner), og="regions")
@R.get("/about", response_class=HTMLResponse)
def about(): return render("/about", "About · Aloof", "Aloof is a small, independent hosting platform for backends, APIs and workers.", shell("About", "Hosting for backends, without the bill shock", "What we are, what we aren't, and how we make money.", f"<div class=prose>{ABOUT}</div>", narrow=True), og="about")
@R.get("/security", response_class=HTMLResponse)
def security(): return render("/security", "Security · Aloof", "What Aloof does today for transport, secrets, isolation, access, backups and disclosure. No certifications claimed.", shell("Security", "Security, stated plainly", "Only what the platform does today. Gaps are listed too.", f"<div class=prose>{SECURITY}</div>", narrow=True), og="security")
# ---------------- status: server-side probes ----------------
def _targets():
    from . import api
    return {"www": f"{SITE}/healthz", "api": f"{api.PUBLIC_API_URL}/healthz", "mcp": api.PUBLIC_MCP_URL.rsplit("/mcp", 1)[0] + "/.well-known/oauth-protected-resource/mcp"}
LABELS = {"www": "Website (www.aloof.si)", "api": "API (api.aloof.si)", "mcp": "MCP server (mcp.aloof.si)"}
def probe(url, timeout=8):
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "aloof-status/1"}), timeout=timeout) as r: code = r.status
    except urllib.error.HTTPError as e: code = e.code
    except Exception as e: return False, (time.time() - t) * 1000, None, f"{type(e).__name__}: {e}"[:200]
    return code < 500, (time.time() - t) * 1000, code, None
def status_tick():
    for c, u in _targets().items():
        ok, ms, code, err = probe(u)
        q("insert into status_checks(ts,component,ok,ms,code,error) values(?,?,?,?,?,?)", (time.time(), c, int(ok), round(ms, 1), code, err))
    q("delete from status_checks where ts<?", (time.time() - 91 * 86400,))
def status_data():
    now = time.time(); out = {}
    for c in LABELS:
        last = q("select * from status_checks where component=? order by ts desc limit 1", (c,), one=True)
        def up(sec):
            r = q("select count(*) n, sum(ok) k from status_checks where component=? and ts>?", (c, now - sec), one=True)
            return round(100.0 * (r["k"] or 0) / r["n"], 3) if r and r["n"] else None
        days = []
        for i in range(89, -1, -1):
            a = time.mktime(datetime.date.fromtimestamp(now - i * 86400).timetuple())
            r = q("select count(*) n, sum(ok) k from status_checks where component=? and ts>=? and ts<?", (c, a, a + 86400), one=True)
            days.append({"day": datetime.date.fromtimestamp(a).isoformat(), "checks": r["n"], "up": round(100.0 * (r["k"] or 0) / r["n"], 2) if r["n"] else None})
        out[c] = {"label": LABELS[c], "ok": bool(last and last["ok"]), "last_check": last and last["ts"], "ms": last and last["ms"], "code": last and last["code"],
                  "uptime_24h": up(86400), "uptime_7d": up(7 * 86400), "uptime_30d": up(30 * 86400), "days": days}
    first = q("select min(ts) t from status_checks", one=True)
    return {"components": out, "since": first and first["t"], "interval_seconds": 60}
@R.get("/v1/status")
def status_json(): return status_data()
@R.get("/status", response_class=HTMLResponse)
def status_page():
    d = status_data(); comps = d["components"]
    allok = all(c["ok"] for c in comps.values()) and any(c["last_check"] for c in comps.values())
    def bar(x):
        cls = "na" if x["up"] is None else ("ok" if x["up"] >= 99.9 else ("warn" if x["up"] >= 95 else "bad"))
        return f"<i class={cls} title='{x['day']}: {'no data' if x['up'] is None else str(x['up']) + '% of ' + str(x['checks']) + ' checks'}'></i>"
    def pct(v): return "—" if v is None else f"{v:g}%"
    rows = "".join(f"""<div class="card st-row"><div class=st-h><h3>{c['label']}</h3><span class="status {'s-running' if c['ok'] else 's-failed'}">{'operational' if c['ok'] else ('down' if c['last_check'] else 'no data')}</span></div>
<div class=bars aria-label="90-day history">{''.join(bar(x) for x in c['days'])}</div><div class="st-f muted"><span>90 days ago</span><span>24 h {pct(c['uptime_24h'])} · 7 d {pct(c['uptime_7d'])} · 30 d {pct(c['uptime_30d'])}{f" · last {c['ms']:.0f} ms" if c['ms'] else ''}</span><span>today</span></div></div>""" for c in comps.values())
    since = time.strftime("%b %-d, %Y %H:%M UTC", time.gmtime(d["since"])) if d["since"] else "not started"
    head = f"<div class='card banner {'ok' if allok else 'bad'}'><b>{'All systems operational' if allok else 'Some systems are not operational'}</b></div>"
    inner = f"""{head}{rows}<p class=muted>Our server probes each endpoint every 60 seconds over HTTPS; anything other than a 5xx or timeout counts as up. History since {since}. Because the probe runs on the same host, a full host outage shows up as a gap in data ("no data"), not as downtime. Machine-readable: <a href="/v1/status">/v1/status</a>.</p>"""
    return render("/status", "Status · Aloof", "Live uptime for the Aloof website, API and MCP server, with 90-day history.", shell("Status", "System status", "Real checks run by our server, not by your browser.", inner, narrow=True), og="status")
# ---------------- SEO ----------------
@R.get("/robots.txt", response_class=PlainTextResponse)
def robots(): return f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /dashboard\nDisallow: /v1/\nSitemap: {SITE}/sitemap.xml\n"
def urls():
    u = ["/", "/pricing", "/docs", "/blog", "/guides", "/changelog", "/customers", "/regions", "/about", "/security", "/status", "/privacy", "/terms", "/login"]
    u += [f"/blog/{p['slug']}" for p in POSTS] + [f"/guides/{k}" for k in GUIDES]
    u += [f"/docs/{k}" for k in ("quickstart", "cli", "deploy", "config", "private", "github", "ci", "processes", "networking", "mcp")]
    return u
@R.get("/sitemap.xml")
def sitemap():
    lm = CHANGELOG[0][0]
    x = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{SITE}{p}</loc><lastmod>{lm}</lastmod></url>" for p in urls()) + "</urlset>"
    return Response(x, media_type="application/xml")
@R.get("/static/og/{name}.png", include_in_schema=False)
def og(name: str):
    f = os.path.join(ROOT, "paas", "static", "og", os.path.basename(name) + ".png")
    if not os.path.exists(f): f = os.path.join(ROOT, "paas", "static", "og", "default.png")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
# ---------------- admin ----------------
def mark_flags():
    q("update accounts set is_admin=1 where lower(email)=?", (ADMIN_EMAIL,))
    q("update accounts set is_admin=0 where lower(email)!=? and is_admin=1", (ADMIN_EMAIL,))
    for pat in ("%@example.com", "%@example.org", "%@example.net", "%.test", "%.invalid", "%@localhost"):
        q("update accounts set is_test=1 where email like ? and (is_test is null or is_test=0)", (pat,))
def _session(request):
    from . import auth
    from .crypto import sha256
    tok = request.cookies.get(auth.COOKIE)
    if not tok: return None
    return q("select a.*, s.mfa_verified from sessions s join accounts a on a.id=s.account_id where s.token_hash=? and s.expires>?", (sha256(tok), time.time()), one=True)
def admin_session(request):
    """Owner check: logged-in session for the configured owner email with is_admin=1. 2FA-ready: PAAS_ADMIN_REQUIRE_2FA=1 also needs sessions.mfa_verified."""
    a = _session(request)
    if not a or not a.get("is_admin") or (a.get("email") or "").lower() != ADMIN_EMAIL: return None
    if os.environ.get("PAAS_ADMIN_REQUIRE_2FA") == "1" and not a.get("mfa_verified"): return None
    return a
def require_admin(request: Request, authorization: str = Header(None), x_admin_token: str = Header(None)):
    """Either the admin token as Bearer (automation), or owner session + admin token in X-Admin-Token (dashboard)."""
    import hmac
    from . import api
    if authorization and hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), api.ADMIN): return {"via": "token"}
    a = admin_session(request)
    if a and x_admin_token and hmac.compare_digest(x_admin_token.strip(), api.ADMIN): return {"via": "session", "account": a}
    raise HTTPException(401, "admin token required")
def _ids_test(): return {r["id"] for r in q("select id from accounts where is_test=1")}
def _day(ts): return time.strftime("%Y-%m-%d", time.localtime(ts))
def _series(tss, days=30):
    now = time.time(); keys = [_day(now - i * 86400) for i in range(days - 1, -1, -1)]; c = dict.fromkeys(keys, 0)
    for t in tss:
        k = _day(t)
        if k in c: c[k] += 1
    return [{"day": k, "n": v} for k, v in c.items()]
_cert_cache = {}
def cert_expiry(host):
    c = _cert_cache.get(host)
    if c and time.time() - c[0] < 3600: return c[1]
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as s, ctx.wrap_socket(s, server_hostname=host) as t: na = t.getpeercert()["notAfter"]
        exp = ssl.cert_time_to_seconds(na); r = {"host": host, "expires": exp, "days_left": round((exp - time.time()) / 86400, 1)}
    except Exception as e: r = {"host": host, "expires": None, "days_left": None, "error": f"{type(e).__name__}: {e}"[:160]}
    _cert_cache[host] = (time.time(), r); return r
def _cpu_pct():
    def rd():
        v = [int(x) for x in open("/proc/stat").readline().split()[1:]]; return sum(v), v[3] + v[4]
    t1, i1 = rd(); time.sleep(0.2); t2, i2 = rd(); return round(100.0 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)
def server_health():
    h = {"cpus": os.cpu_count()}
    try: h["load"] = [round(x, 2) for x in os.getloadavg()]; h["cpu_pct"] = _cpu_pct()
    except Exception: pass
    try:
        m = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        h["mem_total_mb"] = m["MemTotal"] // 1024; h["mem_used_mb"] = (m["MemTotal"] - m["MemAvailable"]) // 1024; h["mem_pct"] = round(100.0 * h["mem_used_mb"] / h["mem_total_mb"], 1)
    except Exception: pass
    du = shutil.disk_usage(ROOT); h["disk_total_gb"] = round(du.total / 1e9, 1); h["disk_used_gb"] = round(du.used / 1e9, 1); h["disk_pct"] = round(100.0 * du.used / du.total, 1)
    try: h["uptime_seconds"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception: pass
    h["nodes"] = q("select id,provider,status,public_ip,vcpu,mem_mb,last_seen from nodes order by created desc limit 20")
    return h
def _tier_fee(t): return float((TIERS.get(t or "free") or {}).get("fee_usd") or 0)
def metrics():
    from . import api, billing, offsite
    test = _ids_test(); now = time.time(); mk = billing.month_key(now)
    m0 = time.mktime(time.strptime(mk + "-01", "%Y-%m-%d"))
    accts = [a for a in q("select id,email,tier,created,credit_cents,stripe_subscription,is_test,is_admin from accounts") if a["id"] not in test]
    real = {a["id"] for a in accts}
    tiers = {t: 0 for t in TIERS}
    for a in accts: tiers[a["tier"] or "free"] = tiers.get(a["tier"] or "free", 0) + 1
    paid = q("""select distinct id from (select account_id as id from stripe_sessions where status='paid'
      union select id from accounts where stripe_subscription is not null and stripe_subscription != ''
      union select account_id as id from payments where status='paid') t""")
    n_paid = len({r["id"] for r in paid} & real); goal = _plans.GPU_UNLOCK_PAID_CUSTOMERS
    subs = [a for a in accts if a["stripe_subscription"] and (a["tier"] or "free") != "free"]
    mrr = sum(_tier_fee(a["tier"]) for a in subs)
    rev = sum((r["amount_cents"] or 0) for r in q("select account_id, amount_cents from stripe_sessions where status='paid' and created>=?", (m0,)) if r["account_id"] in real) / 100
    apps = [a for a in q("select name,account_id,spec,status,created,error,updated from apps") if a["account_id"] in real]
    acct_tier = {a["id"]: a["tier"] or "free" for a in accts}
    by_plan, by_tier = {}, {}
    for a in apps:
        try: pl = json.loads(a["spec"] or "{}").get("plan", "?")
        except Exception: pl = "?"
        by_plan[pl] = by_plan.get(pl, 0) + 1; t = acct_tier.get(a["account_id"], "free"); by_tier[t] = by_tier.get(t, 0) + 1
    app_names = {a["name"] for a in apps}
    inst = q("select app, count(*) n from instances where status='running' group by app")
    running = sum(r["n"] for r in inst if r["app"] in app_names); active = sum(1 for r in inst if r["app"] in app_names)
    rels = [r for r in q("select app,version,status,reason,created from releases where created>?", (now - 30 * 86400,)) if r["app"] in app_names]
    usage = q("select account_id, sum(cost_cents) c from usage_month where month=? group by account_id order by c desc", (mk,))
    email = {a["id"]: a["email"] for a in accts}
    top = [{"account_id": r["account_id"], "email": email[r["account_id"]], "tier": acct_tier[r["account_id"]], "usage_usd": round((r["c"] or 0) / 100, 4),
            "apps": sum(1 for a in apps if a["account_id"] == r["account_id"])} for r in usage if r["account_id"] in real][:15]
    wl = lambda k: q("select count(*) n from kv where kind=?", (k,), one=True)["n"]
    reg = {}
    for r in q("select v from kv where kind='region_waitlist'"):
        try: x = json.loads(r["v"])["region"]; reg[x] = reg.get(x, 0) + 1
        except Exception: pass
    partners = []
    for r in q("select k, v from kv where kind='design_partner'"):
        try: v = json.loads(r["v"])
        except Exception: v = {}
        partners.append({"email": r["k"], "company": v.get("company"), "note": v.get("note"), "ts": v.get("ts")})
    partners.sort(key=lambda x: -(x["ts"] or 0))
    failed = [r for r in q("select app,version,status,reason,created from releases where status='failed' order by created desc limit 40") if r["app"] in app_names][:20]
    errs = q("select name as app, status, error, updated from apps where error is not null and error != '' order by updated desc limit 15")
    st_fail = q("select component, ts, code, error from status_checks where ok=0 order by ts desc limit 10")
    aud = q("select au.ts, au.account_id, ac.email, au.app, au.action, substr(au.detail,1,200) detail, au.ip from audit au left join accounts ac on ac.id=au.account_id order by au.id desc limit 120")
    aud = [r for r in aud if r["account_id"] is None or r["account_id"] in real][:30]
    hosts = sorted({h for h in [site.SITE.split("//")[-1], api.API_HOST, api.MCP_HOST, api.BASE_DOMAIN] if h and "." in h and h != "localhost"})
    return {"generated": now, "excluded_test_accounts": len(test),
        "users": {"total": len(accts), "by_tier": tiers, "signups_per_day": _series([a["created"] for a in accts if a["created"]]),
                  "new_7d": sum(1 for a in accts if (a["created"] or 0) > now - 7 * 86400)},
        "paid_customers": n_paid, "gpu_unlock": {"goal": goal, "have": n_paid, "pct": min(100, round(100 * n_paid / goal)) if goal else 100, "enabled": _plans.GPU_ENABLED},
        "revenue": {"mrr_usd": round(mrr, 2), "subscriptions": len(subs), "revenue_this_month_usd": round(rev, 2), "month": mk,
                    "credit_balance_usd": round(sum((a["credit_cents"] or 0) for a in accts) / 100, 2), "usage_this_month_usd": round(sum((r["c"] or 0) for r in usage if r["account_id"] in real) / 100, 2)},
        "deployments": {"apps_total": len(apps), "apps_active": active, "running_instances": running, "apps_by_plan": by_plan, "apps_by_tier": by_tier,
                        "deploys_per_day": _series([r["created"] for r in rels]), "deploys_30d": len(rels), "failed_30d": sum(1 for r in rels if r["status"] == "failed")},
        "data": {"postgres_dbs": q("select count(*) n from pg_dbs", one=True)["n"], "volumes": q("select count(*) n from volumes", one=True)["n"],
                 "volume_gb": q("select coalesce(sum(size_gb),0) n from volumes", one=True)["n"]},
        "waitlist": {"gpu": wl("gpu_waitlist"), "regions": wl("region_waitlist"), "by_region": reg, "design_partners": len(partners)},
        "design_partners": partners[:50], "top_users": top, "server": server_health(), "backups": offsite.status(),
        "certs": [cert_expiry(h) for h in hosts], "errors": {"failed_deploys": failed, "app_errors": errs, "status_failures": st_fail}, "audit": aud,
        "accounts": [{"id": a["id"], "email": a["email"], "tier": a["tier"] or "free", "created": a["created"], "credit_usd": round((a["credit_cents"] or 0) / 100, 2)} for a in sorted(accts, key=lambda a: -(a["created"] or 0))][:200],
        "test_accounts": q("select id,email,created from accounts where is_test=1 order by created desc limit 100")}
@R.get("/v1/admin/dashboard")
def admin_dashboard_json(_=Depends(require_admin)): return metrics()
EXPORTS = {"users": lambda m: m["accounts"], "top_users": lambda m: m["top_users"], "signups": lambda m: m["users"]["signups_per_day"], "deploys": lambda m: m["deployments"]["deploys_per_day"],
           "audit": lambda m: m["audit"], "failed_deploys": lambda m: m["errors"]["failed_deploys"], "design_partners": lambda m: m["design_partners"],
           "summary": lambda m: [{"metric": k, "value": v} for k, v in _flat(m).items()]}
def _flat(m):
    out = {"users_total": m["users"]["total"], "paid_customers": m["paid_customers"], "mrr_usd": m["revenue"]["mrr_usd"], "revenue_this_month_usd": m["revenue"]["revenue_this_month_usd"],
           "credit_balance_usd": m["revenue"]["credit_balance_usd"], "apps_total": m["deployments"]["apps_total"], "apps_active": m["deployments"]["apps_active"], "running_instances": m["deployments"]["running_instances"],
           "postgres_dbs": m["data"]["postgres_dbs"], "volumes": m["data"]["volumes"], "gpu_waitlist": m["waitlist"]["gpu"], "region_waitlist": m["waitlist"]["regions"], "design_partners": m["waitlist"]["design_partners"]}
    out.update({f"users_{k}": v for k, v in m["users"]["by_tier"].items()}); return out
@R.get("/v1/admin/export.csv")
def admin_export(table: str = "summary", _=Depends(require_admin)):
    if table not in EXPORTS: raise HTTPException(400, f"table must be one of {list(EXPORTS)}")
    rows = EXPORTS[table](metrics()); buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore"); w.writeheader()
        for r in rows: w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="aloof-{table}-{time.strftime("%Y%m%d")}.csv"'})
from pydantic import BaseModel
class TestFlag(BaseModel): is_test: bool
@R.post("/v1/admin/accounts/{account_id}/test")
def admin_flag_test(account_id: int, b: TestFlag, ctx=Depends(require_admin)):
    if not q("select 1 from accounts where id=?", (account_id,), one=True): raise HTTPException(404, "no such account")
    q("update accounts set is_test=? where id=?", (int(b.is_test), account_id))
    from .db import audit; audit((ctx.get("account") or {}).get("id"), None, "admin.flag_test", f"account={account_id} is_test={b.is_test}"); return {"ok": True}
@R.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request):
    if not admin_session(request): raise HTTPException(404, "Not Found")
    return open(os.path.join(ROOT, "dashboard", "admin.html")).read()
def start():
    def loop():
        time.sleep(5)
        while True:
            try: status_tick()
            except Exception: log.exception("status tick")
            time.sleep(60)
    try: mark_flags()
    except Exception: log.exception("admin flags")
    if os.environ.get("PAAS_STATUS_PROBES", "1") == "1": threading.Thread(target=loop, daemon=True).start()
