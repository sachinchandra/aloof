import os
"""Server-side PostHog analytics: allow-listed props, no PII/secrets, skip test accounts, no-op without a key, never breaks requests."""
import json, time, hmac, hashlib, httpx, pytest
from fastapi.testclient import TestClient
from paas import api, analytics, engine, stripe_billing
from paas.db import q
c = TestClient(api.app)
@pytest.fixture
def sent(monkeypatch):
    out = []
    def handler(req): out.append((str(req.url), json.loads(req.content))); return httpx.Response(200)
    monkeypatch.setattr(analytics, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_testkey"); monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    def get(): analytics.flush(); return out
    return get
def _blob(ev): return json.dumps(ev)
def test_noop_without_key(monkeypatch):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    assert analytics.capture(1, "deploy_succeeded", {}) is False
def test_register_signup_event_with_utm(sent):
    r = c.post("/v1/auth/register", json={"email": "ana1@real.dev", "password": "long-enough-pw", "utm_source": "hn" * 200,
               "utm_medium": "social\x00", "utm_campaign": "launch ana1@real.dev", "referrer": "https://news.ycombinator.com/item?id=1&tok=sk_live_abcdefghijk"})
    assert r.status_code == 200
    ev = [b for u, b in sent() if b["event"] == "signup_completed"][-1]
    a = q("select id from accounts where email='ana1@real.dev'", one=True)
    assert ev["distinct_id"] == str(a["id"]) and ev["api_key"] == "phc_testkey"
    p = ev["properties"]; assert p["method"] == "password" and len(p["utm_source"]) <= 100 and p["utm_medium"] == "social"
    p_ev = p   # email is never sent: not as an event property, not as a person property
    assert p["referrer"] == "https://news.ycombinator.com/item" and "@" not in _blob(p_ev) and "sk_live" not in _blob(ev)
    assert sent()[-1][0] == "https://us.i.posthog.com/i/v0/e/"
def test_test_accounts_sent_flagged(sent):
    assert c.post("/v1/auth/register", json={"email": "t@example.com", "password": "long-enough-pw"}).status_code == 200
    ev = [b for u, b in sent() if b["event"] == "signup_completed"][-1]["properties"]
    assert ev["is_test"] is True and ev["$set"] == {"is_test": True}
def test_real_signup_sets_person_and_login_identifies(sent):
    assert c.post("/v1/auth/register", json={"email": "real-person@real.dev", "password": "long-enough-pw"}).status_code == 200
    ev = [b for u, b in sent() if b["event"] == "signup_completed"][-1]
    assert ev["properties"]["is_test"] is False and ev["properties"]["$set"] == {"is_test": False}
    assert c.post("/v1/auth/login", json={"email": "real-person@real.dev", "password": "long-enough-pw"}).status_code == 200
    idf = [b for u, b in sent() if b["event"] == "$identify"][-1]
    assert idf["distinct_id"] == ev["distinct_id"] and idf["properties"]["$set"] == {"is_test": False} and "real-person@real.dev" not in str(idf)
def test_analytics_only_on_platform_hosts():
    from paas import site
    hs = site.analytics_hosts(); assert "aloof.test" in hs and "www.aloof.test" in hs and "api.aloof.test" in hs
    assert all(h.count(".") <= 2 for h in hs) and not any(h.startswith("my-app.") for h in hs)
    js = open(os.path.join(os.path.dirname(__file__), "..", "paas", "static", "analytics.js")).read()
    assert "C.hosts.indexOf(location.hostname)<0)return" in js
def test_caddy_http_redirects_only():
    t = open(os.path.join(os.path.dirname(__file__), "..", "deploy", "Caddyfile.prod")).read()
    i = t.index("http:// {"); blk = t[i:i+400]
    assert "redir https" not in blk and "rewrite * /v1/tls/http-redirect" in blk and "127.0.0.1:8000" in blk   # never the website
def test_magic_link_signup(sent, monkeypatch):
    toks = []
    import paas.auth as A
    real = A.secrets.token_urlsafe; monkeypatch.setattr(A.secrets, "token_urlsafe", lambda n=32: toks.append(real(n)) or toks[-1])
    assert c.post("/v1/auth/magic", json={"email": "ana2@real.dev", "utm_source": "x", "referrer": "https://x.com/"}).status_code == 200
    r = c.get("/auth/magic", params={"token": "ml_" + toks[0]}, follow_redirects=False); assert r.status_code == 303
    ev = [b for u, b in sent() if b["event"] == "signup_completed"][-1]
    assert ev["properties"] == ev["properties"] | {"method": "magic_link", "utm_source": "x", "referrer": "https://x.com/"}
def test_allow_list_and_failure_never_raises(sent, monkeypatch):
    a = engine.create_account("ana3@real.dev")
    analytics.capture(a["id"], "deploy_succeeded", {"app_id": "x", "email": "ana3@real.dev", "api_key": a["api_key"]})
    b = sent()[-1][1]; assert "email" not in b["properties"] and a["api_key"] not in _blob(b)
    def boom(req): raise httpx.ConnectTimeout("t")
    monkeypatch.setattr(analytics, "_client", httpx.Client(transport=httpx.MockTransport(boom)))
    t = time.time(); assert analytics.capture(a["id"], "deploy_succeeded", {}) is True and time.time() - t < 0.1; analytics.flush()
def test_first_deploy_once(sent):
    a = engine.create_account("ana4@real.dev")
    app = {"name": "ana4app", "account_id": a["id"]}; spec = {"region": "nbg1", "source": {"image": "nginx"}}
    engine._track_deploy(app, spec, 1, None); engine._track_deploy(app, spec, 2, None)
    evs = [b["event"] for u, b in sent() if b["distinct_id"] == str(a["id"])]
    assert evs.count("deploy_succeeded") == 2 and evs.count("first_deploy_succeeded") == 1
    d = [b for u, b in sent() if b["event"] == "first_deploy_succeeded" and b["distinct_id"] == str(a["id"])][0]["properties"]
    assert d["app_id"] == "ana4app" and d["region"] == "nbg1" and d["source_type"] == "image"
def _sig(body):
    t = str(int(time.time())); s = hmac.new(b"whsec_test", f"{t}.".encode() + body, hashlib.sha256).hexdigest(); return f"t={t},v1={s}"
def test_plan_upgraded_from_webhook(sent):
    a = engine.create_account("ana5@real.dev")
    ev = {"id": "evt_ana5", "type": "checkout.session.completed", "data": {"object": {"id": "cs_ana5", "mode": "subscription", "payment_status": "paid", "amount_total": 2000,
          "customer": "cus_x", "subscription": "sub_x", "client_reference_id": str(a["id"]), "metadata": {"tier": "pro", "account_id": str(a["id"])}}}}
    body = json.dumps(ev).encode(); code, _ = stripe_billing.handle_webhook(body, _sig(body)); assert code == 200
    e = [b for u, b in sent() if b["event"] == "plan_upgraded"][-1]
    assert e["distinct_id"] == str(a["id"]) and e["properties"] | {} == e["properties"] and e["properties"]["plan"] == "pro"
    assert e["properties"]["amount_usd"] == 20.0 and e["properties"]["interval"] == "month"
def test_gpu_waitlist(sent):
    a = engine.create_account("ana6@real.dev")
    assert c.post("/v1/waitlist", json={"email": "ana6@real.dev", "kind": "gpu"}).status_code == 200
    assert any(b["event"] == "gpu_waitlist_joined" and b["distinct_id"] == str(a["id"]) for u, b in sent())
def test_mcp_connected(sent):
    from types import SimpleNamespace as N
    from mcp_server import oauth
    a = engine.create_account("ana7@real.dev")
    oauth._track_connected(N(client_id="abc", client_name="Claude"), a["api_key"], N(redirect_uri="https://claude.ai/api/mcp/auth_callback?x=1"))
    e = [b for u, b in sent() if b["event"] == "mcp_connected"][-1]
    assert e["distinct_id"] == str(a["id"]) and e["properties"]["client_name"] == "Claude" and e["properties"]["client_host"] == "claude.ai"
    assert a["api_key"] not in _blob(e)
def test_config_exposes_public_key(monkeypatch):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False); assert c.get("/v1/config").json()["posthog_key"] is None
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_pub"); j = c.get("/v1/config").json()
    assert j["posthog_key"] == "phc_pub" and j["posthog_host"] == "https://us.i.posthog.com"
def test_nested_attribution_from_site(sent):
    assert c.post("/v1/auth/register", json={"email": "ana8@real.dev", "password": "long-enough-pw", "attribution": {"utm_source": "hn", "referrer": "https://news.ycombinator.com/"}}).status_code == 200
    p = [b for u, b in sent() if b["event"] == "signup_completed"][-1]["properties"]
    assert p["utm_source"] == "hn" and p["referrer"] == "https://news.ycombinator.com/"
def test_signup_full_utm_and_first_touch_person_props(sent):
    r = c.post("/v1/auth/register", json={"email": "utm5@real.dev", "password": "long-enough-pw",
               "attribution": {"utm_source": "hn", "utm_medium": "social", "utm_campaign": "launch", "utm_term": "paas", "utm_content": "top", "referrer": "https://news.ycombinator.com/"}})
    assert r.status_code == 200
    p = [b for u, b in sent() if b["event"] == "signup_completed"][-1]["properties"]
    assert (p["utm_term"], p["utm_content"]) == ("paas", "top")
    assert p["$set_once"] == {"initial_utm_source": "hn", "initial_utm_medium": "social", "initial_utm_campaign": "launch",
                              "initial_utm_term": "paas", "initial_utm_content": "top", "initial_referrer": "https://news.ycombinator.com/"}


def test_identify_js_never_sends_email():
    import os
    js = open(os.path.join(os.path.dirname(__file__), "..", "paas", "static", "analytics.js")).read()
    assert "st.email" not in js and "email" not in js.split("aloofIdentify")[1].split("};")[0].replace("function(id,email,isTest)", "")

import os
def test_http_redirect_only_known_hosts():
    from fastapi.testclient import TestClient
    from paas import api
    from paas.db import q
    c = TestClient(api.app)
    get = lambda h, u="/x?y=1": c.get("/v1/tls/http-redirect", headers={"host": h, "x-aloof-uri": u}, follow_redirects=False)
    base = api.APP_DOMAINS[0]
    for h in (base, "www." + base, "api." + base, "mcp." + base, "anything." + base, "deep.app." + base):
        r = get(h); assert r.status_code == 301 and r.headers["location"] == f"https://{h}/x?y=1", h
    q("insert into domains(domain, app, verified) values(?,?,?) on conflict do nothing", ("shop.customer.dev", "x", 1))
    assert get("shop.customer.dev").status_code == 301
    for h in ("evil.example", "aloof.si.evil.example", "x" + base, ""):
        r = get(h); assert r.status_code == 404 and "location" not in r.headers, h
    assert get(base, "//evil.example/").headers["location"] == f"https://{base}/"     # no open redirect via the uri
    caddy = open(os.path.join(os.path.dirname(__file__), "..", "deploy", "Caddyfile.prod")).read()
    blk = caddy[caddy.index("http:// {"):]
    assert "redir https://{host}" not in blk and "/v1/tls/http-redirect" in blk
