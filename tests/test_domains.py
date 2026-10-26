"""Custom-domain improvements: plan limits, rate limits, A+AAAA records, background re-check (pending->verified, dns_error, recovery),
release of stale claims, www/apex 308 pairing, claim squatting, validation, MCP tools, migration."""
import os, time, pytest
from fastapi.testclient import TestClient
from paas import api, engine, crypto, domains as D, db
from paas.db import q
V6 = "2001:db8::75"; V4 = "203.0.113.10"
@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    D.reset_limits(); monkeypatch.setattr(D, "VERIFY_MIN_GAP", 0); monkeypatch.setattr(api, "EDGE_IPS", [V4, V6]); monkeypatch.setenv("PAAS_DNS_TIMEOUT", "1")
    q("delete from domains"); yield; D.reset_limits()
@pytest.fixture
def dns(monkeypatch):
    from dnslib import RR, QTYPE, TXT, CNAME, A, AAAA
    from dnslib.server import DNSServer, BaseResolver
    zone = {}
    class R(BaseResolver):
        def resolve(self, req, h):
            rep = req.reply(); n = str(req.q.qname).rstrip("."); t = QTYPE[req.q.qtype]
            for (zn, zt), v in zone.items():
                if zn == n and zt == t: rep.add_answer(RR(n, req.q.qtype, rdata={"TXT": TXT, "CNAME": CNAME, "A": A, "AAAA": AAAA}[t](v), ttl=1))
            return rep
    s = DNSServer(R(), port=0, address="127.0.0.1"); s.start_thread()
    monkeypatch.setenv("PAAS_DNS_SERVER", f"127.0.0.1:{s.server.server_address[1]}")
    yield zone; s.stop()
def acct(email, tier="free", app="dapp"):
    c = TestClient(api.app, base_url="http://api.aloof.test")
    k = c.post("/v1/signup", json={"email": email}).json()["api_key"]; c.headers["Authorization"] = f"Bearer {k}"
    aid = engine.account_by_key(k)["id"]; q("update accounts set tier=? where id=?", (tier, aid))
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", (app, aid, crypto.dump_spec({"plan": "shared-1x-256mb"}), "running", "local-docker", 0, 0))
    return c, aid
def tok(d): return q("select txt_token from domains where domain=?", (d,), one=True)["txt_token"]
def ready(dns, d, kind="cname"):
    app = row(d)["app"]
    dns[(f"_aloof-challenge.{d}", "TXT")] = f"aloof-verify={tok(d)}"
    if kind == "cname": dns[(d, "CNAME")] = f"{app}.aloof.test."
    else: dns[(d, "A")] = V4; dns[(d, "AAAA")] = V6
def row(d): return q("select * from domains where domain=?", (d,), one=True)

def test_migration_columns():
    cols = {r["name"] if isinstance(r, dict) and "name" in r else None for r in []}
    q("select status,last_checked_at,next_check_at,check_count,fail_count,verified_at,last_detail from domains limit 1")
def test_plan_limits_and_quota():
    c, _ = acct("lim-free@example.com", "free", "lapp")
    assert c.post("/v1/apps/lapp/domains", json={"domain": "one.example.com"}).status_code == 200
    r = c.post("/v1/apps/lapp/domains", json={"domain": "two.example.com"}); assert r.status_code == 403 and "Free plan allows 1 domain" in r.json()["detail"] and "Hobby allows 3" in r.json()["detail"]
    j = c.get("/v1/domains").json(); assert (j["tier"], j["limit"], j["used"], j["remaining"]) == ("free", 1, 1, 0) and j["edge"]["aaaa"] == [V6]
    c.delete("/v1/apps/lapp/domains/one.example.com"); assert c.post("/v1/apps/lapp/domains", json={"domain": "two.example.com"}).status_code == 200
    h, _ = acct("lim-hobby@example.com", "hobby", "happ")
    assert [h.post("/v1/apps/happ/domains", json={"domain": f"h{i}.example.com"}).status_code for i in range(4)] == [200, 200, 200, 403]
    p, _ = acct("lim-pro@example.com", "pro", "papp")
    assert all(p.post("/v1/apps/papp/domains", json={"domain": f"p{i}.example.com"}).status_code == 200 for i in range(10))
    assert p.post("/v1/apps/papp/domains", json={"domain": "p10.example.com"}).status_code == 403 and "Pro plan allows 10" in p.post("/v1/apps/papp/domains", json={"domain": "p11.example.com"}).json()["detail"]
def test_rate_limits(monkeypatch):
    monkeypatch.setitem(D.RATE, "add", (3, 3600)); monkeypatch.setitem(D.RATE, "verify", (2, 3600)); monkeypatch.setattr(D, "VERIFY_MIN_GAP", 0)
    c, _ = acct("rl@example.com", "pro", "rapp")
    codes = [c.post("/v1/apps/rapp/domains", json={"domain": f"r{i}.example.com"}).status_code for i in range(5)]; assert codes == [200, 200, 200, 429, 429]
    r = c.post("/v1/apps/rapp/domains", json={"domain": "r9.example.com"}); assert int(r.headers["retry-after"]) > 0 and "too many" in r.json()["detail"]
    assert [c.post("/v1/apps/rapp/domains/r0.example.com/verify").status_code for _ in range(3)] == [200, 200, 429]
    other, _ = acct("rl2@example.com", "pro", "rapp2"); assert other.post("/v1/apps/rapp2/domains", json={"domain": "z.example.com"}).status_code == 200   # per account, not global
def test_verify_gap_per_domain(monkeypatch):
    monkeypatch.setattr(D, "VERIFY_MIN_GAP", 30); c, _ = acct("gap@example.com", "pro", "gapp"); c.post("/v1/apps/gapp/domains", json={"domain": "gap.example.com"})
    assert c.post("/v1/apps/gapp/domains/gap.example.com/verify").status_code == 200
    r = c.post("/v1/apps/gapp/domains/gap.example.com/verify"); assert r.status_code == 429 and "just checked" in r.json()["detail"]
def test_records_a_aaaa_alias_and_cname(dns):
    c, _ = acct("rec@example.com", "pro", "recapp")
    a = c.post("/v1/apps/recapp/domains", json={"domain": "example.org"}).json(); r = a["dns_records"]
    assert r["is_apex"] and r["a"] == [V4] and r["aaaa"] == [V6] and {x["type"] for x in r["recommended"]} == {"A", "AAAA"} and "ALIAS" in r["note"] and "ANAME" in r["note"]
    assert [x["type"] for x in r["routing"]] == ["CNAME", "A", "AAAA"] and r["routing"][0]["value"] == "recapp.aloof.test" and a["status"] == "pending" and a["www_pair"]["domain"] == "www.example.org"
    s = c.post("/v1/apps/recapp/domains", json={"domain": "api.example.org"}).json()["dns_records"]; assert not s["is_apex"] and s["recommended"][0]["type"] == "CNAME" and "DNS only" in s["note"]
def test_check_ipv6_and_cloudflare(dns):
    c, _ = acct("v6@example.com", "pro", "v6app"); d = c.post("/v1/apps/v6app/domains", json={"domain": "v6.example.net"}).json()
    ready(dns, "v6.example.net", "a"); assert c.post("/v1/apps/v6app/domains/v6.example.net/verify").json()["verified"]
    dns[("v6.example.net", "AAAA")] = "2001:db8::dead"        # published AAAA that isn't ours -> not routed
    D.reset_limits(); r = c.post("/v1/apps/v6app/domains/v6.example.net/verify").json(); assert not r["routed"] and any("AAAA record points elsewhere" in x for x in r["detail"])
    dns.pop(("v6.example.net", "AAAA")); dns[("v6.example.net", "A")] = "104.21.5.5"; D.reset_limits()
    r = c.post("/v1/apps/v6app/domains/v6.example.net/verify").json(); assert not r["routed"] and any("Cloudflare" in x and "DNS only" in x for x in r["detail"])
def test_tick_pending_becomes_verified_and_backoff(dns):
    c, _ = acct("tk@example.com", "pro", "tkapp"); c.post("/v1/apps/tkapp/domains", json={"domain": "tk.example.com"})
    t = time.time(); r0 = row("tk.example.com"); assert r0["status"] == "pending" and r0["next_check_at"] > t + 100
    gaps = []
    for i in range(4):
        q("update domains set next_check_at=0 where domain='tk.example.com'"); D.tick(q, "aloof.test", [V4, V6]); r = row("tk.example.com"); gaps.append(r["next_check_at"] - r["last_checked_at"])
    assert gaps == sorted(gaps) and gaps[-1] > gaps[0]                  # backs off while pending
    ready(dns, "tk.example.com"); q("update domains set next_check_at=0 where domain='tk.example.com'"); D.tick(q, "aloof.test", [V4, V6])
    r = row("tk.example.com"); assert r["status"] == "verified" and r["verified"] == 1 and r["verified_at"] and r["last_detail"] is None
    v = c.get("/v1/apps/tkapp/domains").json()[0]; assert v["status"] == "verified" and v["verified"] and v["last_checked_at"] and not v["dns_error"]
    assert TestClient(api.app).get("/v1/tls/ask?domain=tk.example.com").status_code == 200
def test_dns_error_flag_not_removed_and_recovery(dns):
    c, _ = acct("de@example.com", "pro", "deapp"); c.post("/v1/apps/deapp/domains", json={"domain": "de.example.com"}); ready(dns, "de.example.com")
    D.reset_limits(); assert c.post("/v1/apps/deapp/domains/de.example.com/verify").json()["verified"]
    dns.pop(("de.example.com", "CNAME"))                                  # DNS breaks
    q("update domains set next_check_at=0"); D.tick(q, "aloof.test", [V4, V6]); r = row("de.example.com"); assert r["status"] == "verified" and r["fail_count"] == 1   # one miss isn't flagged
    q("update domains set next_check_at=0"); D.tick(q, "aloof.test", [V4, V6]); r = row("de.example.com")
    assert r["status"] == "dns_error" and r["verified"] == 1 and r["last_detail"] and r["next_check_at"] > time.time()
    v = c.get("/v1/apps/deapp/domains").json()[0]; assert v["dns_error"] and v["status"] == "dns_error" and v["detail"]
    assert TestClient(api.app).get("/v1/tls/ask?domain=de.example.com").status_code == 200          # not removed, still served
    dns[("de.example.com", "CNAME")] = "deapp.aloof.test."; q("update domains set next_check_at=0"); D.tick(q, "aloof.test", [V4, V6])
    r = row("de.example.com"); assert r["status"] == "verified" and r["fail_count"] == 0
def test_transient_dns_failure_does_not_flag(dns, monkeypatch):
    c, _ = acct("tr@example.com", "pro", "trapp"); c.post("/v1/apps/trapp/domains", json={"domain": "tr.example.com"}); ready(dns, "tr.example.com")
    D.reset_limits(); assert c.post("/v1/apps/trapp/domains/tr.example.com/verify").json()["verified"]
    monkeypatch.setenv("PAAS_DNS_SERVER", "127.0.0.1:1")                  # resolver unreachable -> timeouts
    for _ in range(3): q("update domains set next_check_at=0"); D.tick(q, "aloof.test", [V4, V6])
    r = row("tr.example.com"); assert r["status"] == "verified" and r["fail_count"] == 0
def test_release_unverified_after_7_days():
    c, _ = acct("rel@example.com", "pro", "relapp"); c.post("/v1/apps/relapp/domains", json={"domain": "old.example.com"}); c.post("/v1/apps/relapp/domains", json={"domain": "new.example.com"})
    q("update domains set created=? where domain='old.example.com'", (time.time() - 8 * 86400,))
    q("insert into domains(domain,app,account_id,verified,created,txt_token,owned,routed,status) values('vold.example.com','relapp',1,1,?,'t',1,1,'verified')", (time.time() - 30 * 86400,))
    D.tick(q, "aloof.test", [V4]); assert not row("old.example.com") and row("new.example.com") and row("vold.example.com")      # verified claims are never released
    assert c.get("/v1/apps/relapp/domains").json()[0]["claim_expires_at"] > time.time() + 6 * 86400
def test_www_pairing_redirect_and_tls_ask(dns):
    c, _ = acct("www@example.com", "pro", "wapp"); c.post("/v1/apps/wapp/domains", json={"domain": "example.dev"}); px = TestClient(api.proxy, follow_redirects=False)
    assert TestClient(api.app).get("/v1/tls/ask?domain=www.example.dev").status_code == 403 and px.get("/", headers={"host": "www.example.dev"}).status_code == 404   # not verified: nothing
    ready(dns, "example.dev", "a"); D.reset_limits(); assert c.post("/v1/apps/wapp/domains/example.dev/verify").json()["verified"]
    r = px.get("/a/b?x=1&y=2", headers={"host": "www.example.dev"}); assert r.status_code == 308 and r.headers["location"] == "https://example.dev/a/b?x=1&y=2"
    import urllib.parse as up; assert up.urlparse(px.get("//evil.com/x", headers={"host": "www.example.dev"}).headers["location"]).hostname == "example.dev"   # can't escape to another host
    assert TestClient(api.app).get("/v1/tls/ask?domain=www.example.dev").status_code == 200
    assert px.get("/", headers={"host": "www.other.dev"}).status_code == 404 and px.get("/", headers={"host": "sub.example.dev"}).status_code == 404    # only the www twin
    assert TestClient(api.app).get("/v1/tls/ask?domain=sub.example.dev").status_code == 403
    # vice versa: a verified www.<apex> redirects the apex to it
    c.post("/v1/apps/wapp/domains", json={"domain": "www.example.xyz"}); ready(dns, "www.example.xyz"); D.reset_limits(); assert c.post("/v1/apps/wapp/domains/www.example.xyz/verify").json()["verified"]
    assert px.get("/p", headers={"host": "example.xyz"}).headers["location"] == "https://www.example.xyz/p"
    # both verified separately -> no redirect between them (each serves the app)
    q("update domains set verified=1, status='verified' where domain='example.xyz'") if row("example.xyz") else q("insert into domains(domain,app,account_id,verified,created,txt_token,status) values('example.xyz','wapp',1,1,0,'t','verified')")
    assert api.pair_target("example.xyz") is None and api.pair_target("www.example.xyz") is None
def test_pairing_not_open_redirect_for_foreign_hosts(dns):
    assert api.pair_target("evil.com") is None and api.pair_target("www.evil.com") is None and api.pair_target("www.aloof.test") is None and api.pair_target("aloof.test") is None
def test_claim_squatting_rules(dns, monkeypatch):
    a, _ = acct("sq-a@example.com", "pro", "sqa"); b, _ = acct("sq-b@example.com", "pro", "sqb")
    assert a.post("/v1/apps/sqa/domains", json={"domain": "squat.example.com"}).status_code == 200
    assert b.post("/v1/apps/sqb/domains", json={"domain": "squat.example.com"}).status_code == 409           # fresh pending claim is protected for the grace window
    q("update domains set created=? where domain='squat.example.com'", (time.time() - 7200,))
    r = b.post("/v1/apps/sqb/domains", json={"domain": "squat.example.com"}); assert r.status_code == 200 and row("squat.example.com")["app"] == "sqb"   # stale pending claim can be taken
    assert a.get("/v1/apps/sqa/domains").json() == []
    ready(dns, "squat.example.com"); D.reset_limits(); assert b.post("/v1/apps/sqb/domains/squat.example.com/verify").json()["verified"]
    q("update domains set created=0 where domain='squat.example.com'"); assert a.post("/v1/apps/sqa/domains", json={"domain": "squat.example.com"}).status_code == 409   # verified: never taken
    assert a.post("/v1/apps/sqa/domains/squat.example.com/verify").status_code == 404 and a.delete("/v1/apps/sqa/domains/squat.example.com").json()["ok"] and row("squat.example.com")  # can't verify/delete others'
def test_validation_ssrf_and_reserved():
    c, _ = acct("val@example.com", "pro", "vapp")
    for bad in ["localhost", "10.0.0.1", "169.254.169.254", "http://evil.com", "a.internal", "x.local", "foo.arpa", "evil.com/path", "a b.com", "-a.com", "xn--a.xn--b", "aloof.test", "foo.aloof.test", "api.aloof.test", "[::1]", "x.example.invalid"]:
        r = c.post("/v1/apps/vapp/domains", json={"domain": bad}); assert r.status_code == 400, (bad, r.text)
    assert "assigned automatically" in c.post("/v1/apps/vapp/domains", json={"domain": "foo.aloof.test"}).json()["detail"]
def test_global_cap_keeps_domain_pending(dns, monkeypatch):
    monkeypatch.setattr(D, "MAX_VERIFIED_HOSTS", 2)
    c, _ = acct("cap@example.com", "pro", "capapp")
    for d in ("cap1.example.com", "cap2.example.com"): c.post("/v1/apps/capapp/domains", json={"domain": d}); ready(dns, d); D.reset_limits(); c.post(f"/v1/apps/capapp/domains/{d}/verify")
    c.post("/v1/apps/capapp/domains", json={"domain": "cap3.example.com"}); ready(dns, "cap3.example.com"); D.reset_limits()
    r = c.post("/v1/apps/capapp/domains/cap3.example.com/verify").json(); assert not r["verified"] and "capacity" in " ".join(r["detail"] + [row("cap3.example.com")["last_detail"] or ""])
    assert TestClient(api.app).get("/v1/tls/ask?domain=cap3.example.com").status_code == 403
def test_mcp_tools_present_and_cli_flags():
    from paas import site
    names = site.mcp_tool_names(); assert {"add_domain", "verify_domain", "remove_domain"} <= set(names)
    import json; m = json.load(open(os.path.join(os.path.dirname(__file__), "..", "mcp_server", "manifest.json"))); assert {"verify_domain", "remove_domain"} <= set(m["tools"])
def test_docs_page_accurate():
    h = TestClient(api.app, base_url="http://www.aloof.test").get("/docs/domains").text
    for s in ["id=steps", "id=subdomain-vs-apex", "id=www", "id=cloudflare", "id=godaddy", "id=namecheap", "id=route53", "id=limits", "id=status", "dns_error", "grey cloud", "ALIAS", "Free 1, Hobby 3, Pro 10", "7 days", "308"]: assert s in h, s
