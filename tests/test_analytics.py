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
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_testkey"); monkeypatch.setenv("POSTHOG_HOST", "https://eu.i.posthog.com")
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
    assert p["referrer"] == "https://news.ycombinator.com/item" and "@" not in _blob(p) and "sk_live" not in _blob(ev)
    assert sent()[-1][0] == "https://eu.i.posthog.com/i/v0/e/"
def test_test_accounts_skipped(sent):
    n = len(sent()); assert c.post("/v1/auth/register", json={"email": "t@example.com", "password": "long-enough-pw"}).status_code == 200
    assert len(sent()) == n
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
    ev = {"id": "evt_ana5", "type": "checkout.session.completed", "data": {"object": {"id": "cs_ana5", "mode": "subscription", "amount_total": 2000,
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
    assert j["posthog_key"] == "phc_pub" and j["posthog_host"] == "https://eu.i.posthog.com"
def test_nested_attribution_from_site(sent):
    assert c.post("/v1/auth/register", json={"email": "ana8@real.dev", "password": "long-enough-pw", "attribution": {"utm_source": "hn", "referrer": "https://news.ycombinator.com/"}}).status_code == 200
    p = [b for u, b in sent() if b["event"] == "signup_completed"][-1]["properties"]
    assert p["utm_source"] == "hn" and p["referrer"] == "https://news.ycombinator.com/"
