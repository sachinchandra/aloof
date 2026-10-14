import os, time, re, xml.dom.minidom as md
os.environ["PAAS_STATUS_PROBES"] = "0"
from fastapi.testclient import TestClient
from paas import api, www, site
from paas.db import q
from paas.plans import PLANS
c = TestClient(api.app, base_url="http://api.aloof.test")
PAGES = ["/blog", "/guides", "/examples", "/regions", "/about", "/security", "/status", "/", "/pricing", "/docs"]
def test_pages_render_with_seo():
    for p in PAGES + [f"/blog/{x['slug']}" for x in www.POSTS] + [f"/guides/{k}" for k in www.GUIDES]:
        r = c.get(p); assert r.status_code == 200, p
        h = r.text; assert '<link rel=canonical href="https://www.aloof.si' + (p if p != "/" else "/") in h, p
        assert 'property="og:image"' in h and "twitter:card" in h and "<meta name=description" in h, p
    assert len(www.POSTS) == 8 and len(set(p["slug"] for p in www.POSTS)) == 8
def test_og_images_exist():
    for x in ["default", "blog", "status"] + ["blog-" + p["slug"] for p in www.POSTS]:
        r = c.get(f"/static/og/{x}.png"); assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
def test_no_fabricated_claims():
    allh = " ".join(c.get(p).text for p in PAGES)
    for bad in ("SOC 2 certified", "SOC 2 compliant", "ISO 27001 certified", "trusted by", "Trusted by", "99.99%"):
        assert bad not in allh
    reg = c.get("/regions").text
    assert "Nuremberg" in reg and reg.count("Coming soon · not live") == 6
    for l in ("Falkenstein", "Vint Hill", "Singapore", "Mumbai"): assert l in reg
    assert "Reference build" in c.get("/examples").text and "Become a design partner" in c.get("/examples").text
    assert "Germany and Finland" not in allh
def test_prices_in_posts_match_plans():
    t = c.get("/blog/aloof-vs-fly-render-railway").text
    assert f"${PLANS['shared-1x-256mb']['price_usd_month']:,.2f}" in t
    t = c.get("/blog/scale-to-zero-economics").text
    assert f"${PLANS['shared-1x-1gb']['price_usd_month']:,.2f}" in t
def test_rss_sitemap_robots():
    r = c.get("/rss.xml"); md.parseString(r.content); assert r.text.count("<item>") == 8
    s = c.get("/sitemap.xml"); md.parseString(s.content); assert "/blog/postgres-offsite-backups" in s.text and "/regions" in s.text
    assert "Sitemap: https://www.aloof.si/sitemap.xml" in c.get("/robots.txt").text and "Disallow: /admin" in c.get("/robots.txt").text
def test_waitlists():
    e = f"r{time.time_ns()}@x.com"
    assert c.post("/v1/waitlist", json={"email": e, "kind": "region", "region": "ap-sin"}).status_code == 200
    assert c.post("/v1/waitlist", json={"email": e, "kind": "region", "region": "mars"}).status_code == 400
    assert c.post("/v1/waitlist", json={"email": e, "kind": "design_partner", "company": "Acme", "note": "api"}).status_code == 200
    assert c.post("/v1/waitlist", json={"email": e, "gpu": "gpu-h100"}).status_code in (200, 400)
def test_status_probe_and_page(monkeypatch):
    monkeypatch.setattr(www, "probe", lambda u, timeout=8: (("mcp" not in u), 12.0, 200 if "mcp" not in u else None, None if "mcp" not in u else "timeout"))
    www.status_tick(); d = c.get("/v1/status").json()
    assert d["components"]["api"]["ok"] and not d["components"]["mcp"]["ok"] and d["components"]["www"]["uptime_24h"] == 100.0
    h = c.get("/status").text; assert "Some systems are not operational" in h and "90-day history" in h
def _login(email):
    cl = TestClient(api.app, base_url="http://api.aloof.test")
    assert cl.post("/v1/auth/register", json={"email": email, "password": "correct horse 1"}).status_code == 200; return cl
def test_admin_access_control_and_metrics():
    owner = www.ADMIN_EMAIL
    if not q("select 1 from accounts where lower(email)=?", (owner,), one=True): o = _login(owner)
    else:
        o = TestClient(api.app, base_url="http://api.aloof.test"); assert o.post("/v1/auth/login", json={"email": owner, "password": "correct horse 1"}).status_code == 200
    other = _login(f"z{time.time_ns()}@x.com"); www.mark_flags()
    tester = _login(f"t{time.time_ns()}@example.com"); www.mark_flags()
    assert q("select is_test from accounts where email like 't%@example.com' order by id desc limit 1", one=True)["is_test"] == 1
    r = c.get("/admin", follow_redirects=False); assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin"
    assert other.get("/admin").status_code == 404
    assert o.get("/admin").status_code == 200
    assert c.get("/v1/admin/dashboard").status_code == 401
    assert other.get("/v1/admin/dashboard", headers={"X-Admin-Token": api.ADMIN}).status_code == 401   # token + wrong user
    m = o.get("/v1/admin/dashboard").json()
    for k in ("users", "paid_customers", "gpu_unlock", "revenue", "deployments", "data", "waitlist", "server", "backups", "certs", "errors", "audit", "top_users", "design_partners"): assert k in m
    assert m["excluded_test_accounts"] >= 1 and all("example.com" not in a["email"] for a in m["accounts"])
    assert len(m["users"]["signups_per_day"]) == 30 and m["waitlist"]["design_partners"] >= 1
    j = c.get("/v1/admin/metrics", headers={"Authorization": f"Bearer {api.ADMIN}"}).json(); assert "dashboard" in j and "paid_customers" in j
    assert c.get("/v1/admin/metrics").status_code == 401
    r = c.get("/v1/admin/export.csv?table=users", headers={"Authorization": f"Bearer {api.ADMIN}"}); assert r.status_code == 200 and r.text.startswith("id,email")
    assert c.get("/v1/admin/export.csv?table=summary", headers={"Authorization": f"Bearer {api.ADMIN}"}).text.startswith("metric,value")
    os.environ["PAAS_ADMIN_REQUIRE_2FA"] = "1"
    try: assert o.get("/admin").status_code == 404
    finally: os.environ.pop("PAAS_ADMIN_REQUIRE_2FA")
    aid = q("select id from accounts where email like 'z%@x.com' order by id desc limit 1", one=True)["id"]
    assert o.post(f"/v1/admin/accounts/{aid}/test", json={"is_test": True}, headers={"X-Aloof-CSRF": "1"}).status_code == 200
def test_login_next_redirect_js_has_no_backslash_regex():
    h = c.get("/login?next=/admin").text; assert "get('next')" in h and "fromCharCode(92)" in h and "/^\\/" not in h
def test_qa_head_404_docs():
    from fastapi.testclient import TestClient
    from paas import api
    c = TestClient(api.app)
    r = c.head("/"); assert r.status_code == 200 and r.content == b""
    assert c.head("/pricing").status_code == 200
    r = c.get("/nonexistent", headers={"sec-fetch-mode": "navigate"}); assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    assert c.get("/v1/nonexistent").headers["content-type"].startswith("application/json")
    d = c.get("/api/docs").text; assert "integrity=" in d and "5.17.14" in d and "meta name=description" in d
    assert c.get("/static/swagger/swagger-ui.css").status_code == 200

def test_changelog_removed():
    from fastapi.testclient import TestClient
    from paas import api
    c = TestClient(api.app); r = c.get("/changelog", follow_redirects=False); assert r.status_code == 301 and r.headers["location"] == "/"
    assert "changelog" not in c.get("/").text.lower() and "/changelog" not in c.get("/sitemap.xml").text
def test_support_doc_fixes():
    qs = c.get("/docs/quickstart").text; assert "sudo curl" in qs and "WSL" in qs and '{"api": "' in qs and "~/.local/bin" in qs
    d = c.get("/docs/domains").text; assert "Cloudflare" in d and "DNS only" in d and "id=root" in d and "id=verify-failed" in d and "Propagation" in d
    t = c.get("/docs/troubleshooting"); assert t.status_code == 200 and "HTTP 402" in t.text and "id=failed-releases" in t.text and "build logs" in t.text.lower() and "release_command" in t.text
    b = c.get("/docs/billing").text; assert b.index("The short version") < b.index("/v1/billing/subscribe") and "id=cancel" in b and "id=refunds" in b and "id=delete" in b and "pending" not in b.lower() and "Railway" not in b and "Fly" not in b and "Render" not in b
    p = c.get("/docs/postgres").text; assert "aloof proxy 5432 -a pg-" in p and "max_connections" in p and "id=resize" in p and "Support will resize" in p
    assert "Support contact: pending" in c.get("/docs/support").text and "Remix" in c.get("/guides").text
def test_terms_billing_section():
    t = c.get("/terms").text; assert "Billing, cancellation and refunds" in t and "/docs/billing#cancel" in t
    assert "$5 of prepaid credit" in c.get("/docs/quickstart").text
def test_seo_examples_sitemap_title():
    r = c.get("/customers", follow_redirects=False); assert r.status_code == 301 and r.headers["location"] == "/examples"
    sm = c.get("/sitemap.xml").text; assert "/login<" not in sm and "/examples<" in sm and "/customers" not in sm
    import re as _r; assert _r.findall(r"<lastmod>(\d{4}-\d\d-\d\d)</lastmod>", sm)
    h = c.get("/").text; assert "<title>Aloof: EU backend hosting, scale to zero, deploy via MCP</title>" in h and "superintelligence" in h
    d = c.get("/docs").text; assert 'content="Aloof documentation: quickstart' in d
def test_analytics_off_without_key(monkeypatch):
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    from paas import site
    monkeypatch.setattr(site, "posthog_key", lambda: "")
    assert "analytics.js" not in c.get("/").text and c.get("/v1/config").json()["posthog"] is None
def test_analytics_on_with_key(monkeypatch):
    from paas import site
    monkeypatch.setattr(site, "posthog_key", lambda: "phc_testkey1234567890")
    h = c.get("/").text; assert "/static/analytics.js" in h and "eu.i.posthog.com" in h and 'data-cta=deploy_free' in h and "data-cta=connect_mcp" in h
    assert c.get("/v1/config").json()["posthog"]["key"] == "phc_testkey1234567890"
    assert "PostHog Cloud EU" in c.get("/privacy").text
def test_client_events_no_server_duplicates():
    import os
    r = os.path.join(os.path.dirname(__file__), "..")
    js = "".join(open(os.path.join(r, f)).read() for f in ("paas/static/analytics.js", "paas/site.py", "dashboard/index.html"))
    for e in ("signup_completed", "first_deploy_succeeded", "deploy_succeeded", "mcp_connected", "plan_upgraded", "gpu_waitlist_joined"): assert f"'{e}'" not in js, e
    assert "aloofIdentify(String(me.id))" in js and "auth/magic',{{method:'POST'" in js and js.count("attribution:window.aloofAttribution") == 2
