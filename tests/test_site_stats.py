"""First-party website visitors/clicks for the admin dashboard."""
import pytest
from fastapi.testclient import TestClient
from paas import api, site_stats as S, netaddr
from paas.db import q
UA = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0"}
@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr("paas.site.analytics_hosts", lambda: ["aloof.si", "www.aloof.si", "api.aloof.si"])
    for t in ("site_visitors", "site_counts", "site_salt"): q(f"delete from {t}")
    netaddr._lim.clear(); yield
    for t in ("site_visitors", "site_counts", "site_salt"): q(f"delete from {t}")
def C(ip="203.0.113.7"): return TestClient(api.app, base_url="http://www.aloof.si", client=(ip, 5000))
def hit(c, b, h=UA): return c.post("/v1/site/hit", json=b, headers=h)

def test_pageviews_unique_visitors_and_ctas():
    a, b = C("203.0.113.7"), C("198.51.100.9")
    for _ in range(3): assert hit(a, {"k": "pv", "p": "/pricing"}).status_code == 204
    hit(b, {"k": "pv", "p": "/"}); hit(b, {"k": "cta", "p": "/", "c": "signup_hero"}); hit(a, {"k": "cta", "p": "/pricing", "c": "signup_hero"})
    hit(a, {"k": "out", "p": "/", "c": "github.com"}); hit(a, {"k": "out", "p": "/", "c": "www.aloof.si"}); hit(a, {"k": "out", "p": "/", "c": "x/y?z"})
    r = S.report()
    assert r["status"] == "ok" and r["totals"] == {"visitor_days": 2, "pageviews": 4, "cta_clicks": 2, "outbound_clicks": 1} and r["outbound"] == [{"host": "github.com", "clicks": 1}]
    assert r["daily"][0]["visitors"] is None and r["daily"][-1]["cta_clicks"] == 2   # before collection: null
    assert r["top_pages"][0] == {"path": "/pricing", "pageviews": 3} and r["ctas"] == [{"cta": "signup_hero", "clicks": 2}]
    assert r["daily"][-1]["visitors"] == 2 and len(r["daily"]) == 30 and r["range"]["timezone"] == "UTC"

def test_never_counts_api_bots_other_hosts_or_bad_input():
    c = C()
    for b in ({"k": "pv", "p": "/v1/apps"}, {"k": "pv", "p": "/dashboard"}, {"k": "pv", "p": "/admin"}, {"k": "pv", "p": "no-slash"},
              {"k": "pv", "p": "/x?email=a@b.c"}, {"k": "cta", "p": "/", "c": "Bad CTA!"}, {"k": "evil", "p": "/"}, "junk"):
        assert hit(c, b).status_code == 204
    hit(c, {"k": "pv", "p": "/"}, {"user-agent": "Googlebot/2.1"}); hit(c, {"k": "pv", "p": "/"}, {"user-agent": ""}); hit(c, {"k": "pv", "p": "/"}, {"user-agent": "testclient"})
    TestClient(api.app, base_url="http://celeste.aloof.si").post("/v1/site/hit", json={"k": "pv", "p": "/"}, headers=UA)
    r = S.report(); assert r["status"] == "no_data" and r["totals"]["pageviews"] is None and r["collecting_since"] is None

def test_no_ip_or_ua_stored_and_salt_rotates():
    hit(C("203.0.113.77"), {"k": "pv", "p": "/"})
    rows = q("select * from site_visitors"); assert len(rows) == 1 and len(rows[0]["vh"]) == 16
    dump = repr(rows) + repr(q("select * from site_counts"))
    assert "203.0.113" not in dump and "Firefox" not in dump
    q("insert into site_salt(day, salt) values('2000-01-01', 'old')"); S._salt("2099-01-01")
    assert not q("select 1 from site_salt where day='2000-01-01'", one=True)

def test_admin_route_requires_admin_and_ranges(monkeypatch):
    c = TestClient(api.app)
    assert c.get("/v1/admin/site-analytics").status_code in (401, 403)
    monkeypatch.setattr("paas.www.require_admin", lambda request: {"via": "test"})
    r = c.get("/v1/admin/site-analytics?from=2026-11-01&to=2026-11-05").json()
    assert r["range"] == {"from": "2026-11-01", "to": "2026-11-05", "days": 5, "timezone": "UTC"} and [d["date"] for d in r["daily"]][0] == "2026-11-01"
    assert set(r) >= {"totals", "daily", "top_pages", "ctas", "definitions", "source", "collecting_since"}
    assert c.get("/v1/admin/site-analytics?from=2026-11-05&to=2026-11-01").status_code == 400
    assert c.get("/v1/admin/site-analytics?from=2024-01-01&to=2026-11-01").status_code == 400
    assert c.get("/v1/admin/site-analytics?from=nope").status_code == 400

def test_beacon_in_site_script():
    import os
    js = open(os.path.join(os.path.dirname(api.__file__), "static", "analytics.js")).read()
    assert "fp('pv')" in js and "fp('cta'" in js and "/v1/site/hit" in js and "location.search" not in js.split("function fp")[1].split("}")[0]

def test_zero_vs_no_data():
    q("insert into site_counts(day, kind, key, n) values('2026-01-10', 'pv', '/', 5)")
    r = S.report("2026-01-08", "2026-01-12")
    assert r["status"] == "ok" and [d["pageviews"] for d in r["daily"]] == [None, None, 5, 0, 0] and r["totals"]["pageviews"] == 5
    assert S.report("2025-12-01", "2025-12-05")["status"] == "no_data"

def test_ux_shape():
    q("insert into site_counts(day, kind, key, n) values('2026-01-10','cta','signup',2),('2026-01-10','out','github.com',3)")
    r = S.ux_shape(S.report("2026-01-10", "2026-01-11"), None)
    assert r["clicks"] == 5 and r["series"] == [{"day": "2026-01-10", "visitors": 0, "clicks": 5}, {"day": "2026-01-11", "visitors": 0, "clicks": 0}]
    assert r["top_clicks"][0] == {"label": "github.com", "kind": "outbound", "clicks": 3} and isinstance(r["generated"], int)
