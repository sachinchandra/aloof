import os, json, hmac, hashlib, base64, re, threading, time, pytest, httpx, respx
from fastapi.testclient import TestClient
from paas import api, billing, auth, engine, crypto, domains
from paas.db import q
from paas.drivers import providers as P

def sig(secret, msg): return hmac.new(secret.encode(), msg if isinstance(msg, bytes) else msg.encode(), hashlib.sha256).hexdigest()
@pytest.fixture
def client(): return TestClient(api.app, base_url="http://api.aloof.test")

# ---------------- auth ----------------
def test_register_login_session_csrf(client):
    r = client.post("/v1/auth/register", json={"email": "Ann@Example.in", "password": "short"}); assert r.status_code == 400
    r = client.post("/v1/auth/register", json={"email": "ann@example.in", "password": "correct horse 1"}); assert r.status_code == 200, r.text
    key = r.json()["api_key"]; assert key.startswith("pk_")
    assert client.get("/v1/auth/me").json()["email"] == "ann@example.in"          # session cookie set by register
    assert client.post("/v1/volumes", json={"name": "vol-one"}).status_code == 403      # cookie write without CSRF header
    assert client.post("/v1/volumes", json={"name": "vol-one"}, headers={"X-Aloof-CSRF": "1"}).status_code == 200
    client.post("/v1/auth/logout", headers={"X-Aloof-CSRF": "1"}); assert client.get("/v1/auth/me").status_code == 401
    assert client.post("/v1/auth/login", json={"email": "ann@example.in", "password": "wrong password!"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": "ANN@example.in", "password": "correct horse 1"}).status_code == 200
    assert client.get("/v1/auth/me").status_code == 200
    # API key works stateless; stored only hashed
    c2 = TestClient(api.app); assert c2.get("/v1/auth/me", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    row = q("select * from accounts where email='ann@example.in'", one=True)
    assert key not in json.dumps(row, default=str) and row["api_key_hash"] == crypto.sha256(key) and row["password_hash"].startswith("scrypt$")
    # MCP connector key doesn't revoke the CLI key
    mk = client.post("/v1/auth/mcp-key", headers={"X-Aloof-CSRF": "1"}).json()["api_key"]
    for k in (key, mk): assert c2.get("/v1/auth/me", headers={"Authorization": f"Bearer {k}"}).status_code == 200
    # rotation revokes the old primary key
    nk = client.post("/v1/auth/api-key", headers={"X-Aloof-CSRF": "1"}).json()["api_key"]
    assert c2.get("/v1/auth/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert c2.get("/v1/auth/me", headers={"Authorization": f"Bearer {nk}"}).status_code == 200

def test_magic_link_single_use(client):
    open(auth.OUTBOX, "w").close()
    assert client.post("/v1/auth/magic", json={"email": "bob@example.in"}).status_code == 200
    link = re.search(r"http\S+/auth/magic\?token=[\w\-]+", open(auth.OUTBOX).read()).group(0)
    tok = link.split("token=")[1]
    c = TestClient(api.app, base_url="http://api.aloof.test")
    r = c.get(f"/auth/magic?token={tok}", follow_redirects=False); assert r.status_code in (302, 303, 307), r.text
    assert c.get("/v1/auth/me").json()["email"] == "bob@example.in"
    assert TestClient(api.app).get(f"/auth/magic?token={tok}", follow_redirects=False).status_code in (400, 401, 410)  # reused

# ---------------- encryption at rest ----------------
def test_env_encrypted_at_rest():
    s = crypto.dump_spec({"plan": "cpu-small", "env": {"DATABASE_PASSWORD": "hunter2-very-secret"}})
    assert "hunter2" not in s and "env_enc" in s
    assert crypto.load_spec(s)["env"] == {"DATABASE_PASSWORD": "hunter2-very-secret"}
    a = engine.create_account("enc@example.in")
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", ("encapp", a["id"], s, "idle", "local-docker", 0, 0))
    raw = q("select spec from apps where name='encapp'", one=True)["spec"]; assert "hunter2" not in raw
    assert engine.load_spec(raw)["env"]["DATABASE_PASSWORD"] == "hunter2-very-secret"

# ---------------- Razorpay (optional) ----------------
def _acct_client(email):
    c = TestClient(api.app, base_url="http://api.aloof.test")
    k = c.post("/v1/signup", json={"email": email}).json()["api_key"]; c.headers["Authorization"] = f"Bearer {k}"; return c
@respx.mock
def test_razorpay_disabled_by_default():
    c = _acct_client("norzp@example.in")
    assert c.post("/v1/billing/razorpay/orders", json={"credit_inr": 1000}).status_code == 404
    assert TestClient(api.app).post("/v1/billing/razorpay/webhook", content=b"{}").status_code == 404
@respx.mock
def test_razorpay_optional_checkout_and_webhook_idempotent(monkeypatch):
    monkeypatch.setenv("PAAS_RAZORPAY_ENABLED", "1")
    c = _acct_client("pay@example.in")
    route = respx.post("https://api.razorpay.com/v1/orders").mock(return_value=httpx.Response(200, json={"id": "order_T1", "amount": 118000, "currency": "INR"}))
    assert c.post("/v1/billing/razorpay/orders", json={"credit_inr": 50}).status_code == 400
    o = c.post("/v1/billing/razorpay/orders", json={"credit_inr": 1000}).json()
    assert o["order_id"] == "order_T1" and o["amount"] == 118000 and o["key_id"] == "rzp_test_X"
    sent = json.loads(route.calls[0].request.content); assert sent["amount"] == 118000 and sent["currency"] == "INR"
    assert route.calls[0].request.headers["authorization"] == "Basic " + base64.b64encode(b"rzp_test_X:ks_secret").decode()
    before = c.get("/v1/billing").json()["credit_cents"]
    bad = {"razorpay_order_id": "order_T1", "razorpay_payment_id": "pay_1", "razorpay_signature": "00"}
    assert c.post("/v1/billing/razorpay/verify", json=bad).status_code == 400
    good = dict(bad, razorpay_signature=sig("ks_secret", "order_T1|pay_1"))
    r = c.post("/v1/billing/razorpay/verify", json=good).json(); assert r["ok"] and r["credited_inr"] == 1000 and abs(r["credited_cents"] - 1136.36) < 0.01
    ev = json.dumps({"id": "evt_1", "event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_1", "order_id": "order_T1", "amount": 118000}}}}).encode()
    w = TestClient(api.app)
    assert w.post("/v1/billing/razorpay/webhook", content=ev, headers={"X-Razorpay-Signature": "bad"}).status_code == 400
    r = w.post("/v1/billing/razorpay/webhook", content=ev, headers={"X-Razorpay-Signature": sig("wh_secret", ev)}); assert r.status_code == 200 and r.json().get("duplicate")
    assert w.post("/v1/billing/razorpay/webhook", content=ev, headers={"X-Razorpay-Signature": sig("wh_secret", ev)}).json().get("duplicate_event")
    b = c.get("/v1/billing").json(); assert abs(b["credit_cents"] - before - 1136.36) < 0.01      # credited exactly once, ₹1000 @ ₹88/$
    inv = q("select * from invoices where order_id='order_T1'", one=True)
    assert inv["igst"] == 180 and inv["cgst"] == 0 and inv["total_inr"] == 1180 and re.match(r"ALF/\d{4}-\d\d/\d{6}", inv["number"])
    assert "IGST @18%" in c.get(f"/v1/billing/invoices/{inv['id']}").text

@respx.mock
def test_razorpay_webhook_only_capture_intrastate_cgst_sgst(monkeypatch):
    monkeypatch.setenv("PAAS_RAZORPAY_ENABLED", "1")
    c = _acct_client("ka@example.in")
    assert c.put("/v1/billing/profile", json={"billing_name": "KA Pvt Ltd", "gstin": "29AAACK1234A1Z5"}).json()["billing_state"] == "Karnataka"
    assert c.put("/v1/billing/profile", json={"gstin": "nonsense"}).status_code == 400
    assert c.put("/v1/billing/profile", json={"country": "Germany"}).status_code == 400
    c.put("/v1/billing/profile", json={"billing_name": "KA Pvt Ltd", "gstin": "29AAACK1234A1Z5", "country": "IN"})
    respx.post("https://api.razorpay.com/v1/orders").mock(return_value=httpx.Response(200, json={"id": "order_K"}))
    c.post("/v1/billing/razorpay/orders", json={"credit_inr": 500})
    ev = json.dumps({"id": "evt_k", "event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_k", "order_id": "order_K", "amount": 59000}}}}).encode()
    r = TestClient(api.app).post("/v1/billing/razorpay/webhook", content=ev, headers={"X-Razorpay-Signature": sig("wh_secret", ev)}).json()
    assert r["credited_inr"] == 500 and r["invoice"]["cgst"] == 45 and r["invoice"]["sgst"] == 45 and r["invoice"]["igst"] == 0
    respx.post("https://api.razorpay.com/v1/orders").mock(return_value=httpx.Response(200, json={"id": "order_K2"}))
    c.post("/v1/billing/razorpay/orders", json={"credit_inr": 500})
    ev2 = json.dumps({"id": "evt_k2", "event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_k2", "order_id": "order_K2", "amount": 100}}}}).encode()
    assert TestClient(api.app).post("/v1/billing/razorpay/webhook", content=ev2, headers={"X-Razorpay-Signature": sig("wh_secret", ev2)}).json()["error"] == "amount mismatch"

# ---------------- Stripe (primary) ----------------
def _stripe_ev(eid, typ, obj):
    raw = json.dumps({"id": eid, "type": typ, "data": {"object": obj}}).encode()
    from paas import stripe_billing
    return raw, {"Stripe-Signature": stripe_billing.sign(raw, "whsec_test")}
@respx.mock
def test_stripe_topup_checkout_webhook_idempotent():
    c = _acct_client("stripe@example.com"); w = TestClient(api.app)
    cust = respx.post("https://api.stripe.com/v1/customers").mock(return_value=httpx.Response(200, json={"id": "cus_1"}))
    sess = respx.post("https://api.stripe.com/v1/checkout/sessions").mock(return_value=httpx.Response(200, json={"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"}))
    assert c.post("/v1/billing/checkout", json={"credit_usd": 5}).status_code == 400          # below $10 minimum
    o = c.post("/v1/billing/checkout", json={"credit_usd": 25}).json(); assert o["url"].startswith("https://checkout.stripe.com") and o["credit_usd"] == 25
    form = dict(httpx.QueryParams(sess.calls[-1].request.content.decode()))
    assert form["mode"] == "payment" and form["line_items[0][price_data][currency]"] == "usd" and form["line_items[0][price_data][unit_amount]"] == "2500"
    assert form["customer"] == "cus_1" and form["metadata[credit_cents]"] == "2500" and form["invoice_creation[enabled]"] == "true"
    assert "automatic_tax[enabled]" not in form                                                  # STRIPE_TAX off by default
    assert sess.calls[-1].request.headers["authorization"] == "Bearer sk_test_X" and sess.calls[-1].request.headers.get("idempotency-key")
    before = c.get("/v1/billing").json()["credit_cents"]
    obj = {"id": "cs_1", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "amount_subtotal": 2500, "amount_total": 2975,
           "customer": "cus_1", "payment_intent": "pi_1", "invoice": "in_1", "metadata": {"account_id": "x"}}
    raw, h = _stripe_ev("evt_s1", "checkout.session.completed", obj)
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400
    old = {"Stripe-Signature": __import__("paas.stripe_billing").stripe_billing.sign(raw, "whsec_test", ts=time.time() - 3600)}
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=old).status_code == 400      # replay outside tolerance
    r = w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json(); assert r["ok"] and r["credited_cents"] == 2500
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["duplicate_event"]
    raw2, h2 = _stripe_ev("evt_s1b", "checkout.session.async_payment_succeeded", obj)            # same session, new event id
    assert w.post("/v1/billing/stripe/webhook", content=raw2, headers=h2).json()["duplicate"]
    raw3, h3 = _stripe_ev("evt_inv", "invoice.paid", {"id": "in_1", "customer": "cus_1", "amount_paid": 2975, "hosted_invoice_url": "https://invoice.stripe.com/i/1"})
    w.post("/v1/billing/stripe/webhook", content=raw3, headers=h3)
    b = c.get("/v1/billing").json()
    assert b["currency"] == "USD" and abs(b["credit_cents"] - before - 2500) < 1e-6 and b["credit_usd"] == round((before + 2500) / 100, 2)
    assert any(i["invoice_url"] == "https://invoice.stripe.com/i/1" for i in b["invoices"]) and b["ledger"][0]["kind"] == "topup"
    # tampered amount rejected
    respx.post("https://api.stripe.com/v1/checkout/sessions").mock(return_value=httpx.Response(200, json={"id": "cs_2", "url": "u"}))
    c.post("/v1/billing/checkout", json={"credit_usd": 10})
    raw4, h4 = _stripe_ev("evt_s2", "checkout.session.completed", dict(obj, id="cs_2", amount_subtotal=1))
    assert w.post("/v1/billing/stripe/webhook", content=raw4, headers=h4).json()["error"] == "amount mismatch"
    assert cust.call_count == 1                                                                   # customer reused

@respx.mock
def test_stripe_subscription_tier_and_cancel(monkeypatch):
    monkeypatch.setenv("STRIPE_TAX", "1")
    c = _acct_client("sub@example.com"); w = TestClient(api.app)
    aid = c.get("/v1/auth/me").json()["id"]
    respx.post("https://api.stripe.com/v1/customers").mock(return_value=httpx.Response(200, json={"id": "cus_s"}))
    sess = respx.post("https://api.stripe.com/v1/checkout/sessions").mock(return_value=httpx.Response(200, json={"id": "cs_sub", "url": "https://checkout.stripe.com/c/cs_sub"}))
    assert c.post("/v1/billing/subscribe", json={"tier": "free"}).status_code == 400
    assert c.post("/v1/billing/subscribe", json={"tier": "pro"}).status_code == 200
    form = dict(httpx.QueryParams(sess.calls[-1].request.content.decode()))
    assert form["mode"] == "subscription" and form["line_items[0][price]"] == "price_pro" and form["automatic_tax[enabled]"] == "true" and form["tax_id_collection[enabled]"] == "true"
    raw, h = _stripe_ev("evt_sub1", "checkout.session.completed", {"id": "cs_sub", "mode": "subscription", "customer": "cus_s", "subscription": "sub_1",
                                                                   "metadata": {"account_id": str(aid), "tier": "pro"}})
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["tier"] == "pro"
    b = c.get("/v1/billing").json(); assert b["month"]["tier"] == "pro" and b["subscription"] == "sub_1"
    assert c.put("/v1/billing/tier", json={"tier": "hobby"}).status_code == 409                 # managed by Stripe
    credit0 = b["credit_cents"]; billing.meter_tick(time.time() + 40 * 86400)                     # next month: no wallet fee for subscribers
    assert c.get("/v1/billing").json()["credit_cents"] == credit0
    portal = respx.post("https://api.stripe.com/v1/billing_portal/sessions").mock(return_value=httpx.Response(200, json={"url": "https://billing.stripe.com/p/1"}))
    assert c.post("/v1/billing/portal").json()["url"] == "https://billing.stripe.com/p/1"
    assert dict(httpx.QueryParams(portal.calls[0].request.content.decode()))["customer"] == "cus_s"
    raw, h = _stripe_ev("evt_sub2", "customer.subscription.deleted", {"id": "sub_1", "status": "canceled"})
    assert w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()["tier"] == "free"
    b = c.get("/v1/billing").json(); assert b["month"]["tier"] == "free" and not b["subscription"]

def test_stripe_signature_helper():
    from paas import stripe_billing as S
    raw = b'{"id":"evt"}'; h = S.sign(raw, "whsec_test", ts=1000)
    assert S.verify_signature(raw, h, "whsec_test", now=1100) and not S.verify_signature(raw, h, "whsec_test", now=2000)
    assert not S.verify_signature(raw + b" ", h, "whsec_test", now=1100) and not S.verify_signature(raw, h, "other", now=1100)
    assert S.verify_signature(raw, h + ",v1=deadbeef", "whsec_test", now=1100)                  # multiple v1 (secret rotation)

def test_meter_charges_and_suspends(monkeypatch):
    a = engine.create_account("meter@example.in")
    q("update accounts set credit_cents=0.001, tier='hobby' where id=?", (a["id"],))   # free tier never draws on the wallet
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)",
      ("meterapp", a["id"], crypto.dump_spec({"plan": "cpu-small", "instances": 1}), "running", "local-docker", 0, 0))
    q("insert into instances(id,app,status,started,host,port) values(?,?,?,?,?,?)", ("i-meter", "meterapp", "running", time.time() - 3600 * 1000, "127.0.0.1", 1))   # ~₹229, beyond the Free tier included ₹100
    calls = []; monkeypatch.setattr(engine, "scale", lambda n, k, reason="": calls.append((n, k)))
    billing.meter_tick()
    assert q("select credit_cents from accounts where id=?", (a["id"],), one=True)["credit_cents"] < 0
    assert calls == [("meterapp", 0)] and q("select status from apps where name='meterapp'", one=True)["status"] == "suspended"

# ---------------- TXT verification against a real DNS server (dnslib) ----------------
@pytest.fixture
def fakedns(monkeypatch):
    from dnslib import RR, QTYPE, TXT, CNAME, A
    from dnslib.server import DNSServer, BaseResolver
    zone = {}
    class R(BaseResolver):
        def resolve(self, req, h):
            rep = req.reply(); n = str(req.q.qname).rstrip("."); t = QTYPE[req.q.qtype]
            for (zn, zt), v in zone.items():
                if zn == n and zt == t: rep.add_answer(RR(n, req.q.qtype, rdata={"TXT": TXT, "CNAME": CNAME, "A": A}[t](v), ttl=5))
            return rep
    s = DNSServer(R(), port=0, address="127.0.0.1"); s.start_thread()
    monkeypatch.setenv("PAAS_DNS_SERVER", f"127.0.0.1:{s.server.server_address[1]}")
    yield zone; s.stop()
def test_domain_txt_flow(client, fakedns, monkeypatch):
    monkeypatch.setattr(domains, 'VERIFY_MIN_GAP', 0)
    c = _acct_client("dom@example.in")
    aid = engine.account_by_key(c.headers["Authorization"][7:])["id"]; q("update accounts set tier='hobby' where id=?", (aid,))
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", ("webx", aid, crypto.dump_spec({"plan": "cpu-small"}), "running", "local-docker", 0, 0))
    d = c.post("/v1/apps/webx/domains", json={"domain": "api.shop.in"}).json()
    assert d["verified"] is False and d["dns_records"]["ownership"]["name"] == "_aloof-challenge.api.shop.in"
    fakedns[("api.shop.in", "CNAME")] = "webx.aloof.test."                    # routed but not owned: still pending
    r = c.post("/v1/apps/webx/domains/api.shop.in/verify").json(); assert r["routed"] and not r["owned"] and not r["verified"]
    assert TestClient(api.app).get("/v1/tls/ask?domain=api.shop.in").status_code != 200
    fakedns[("_aloof-challenge.api.shop.in", "TXT")] = d["dns_records"]["ownership"]["value"]
    r = c.post("/v1/apps/webx/domains/api.shop.in/verify").json(); assert r["verified"], r
    assert TestClient(api.app).get("/v1/tls/ask?domain=api.shop.in").status_code == 200
    # another account can't hijack a verified domain
    assert _acct_client("evil@example.in").post("/v1/apps/webx/domains", json={"domain": "api.shop.in"}).status_code in (403, 404, 409)
    # A record route to edge IP + TXT
    c.post("/v1/apps/webx/domains", json={"domain": "shop2.in"}); tok = q("select txt_token from domains where domain='shop2.in'", one=True)["txt_token"]
    fakedns[("shop2.in", "A")] = "203.0.113.10"; fakedns[("_aloof-challenge.shop2.in", "TXT")] = f"aloof-verify={tok}"
    assert c.post("/v1/apps/webx/domains/shop2.in/verify").json()["verified"]

# ---------------- provider drivers (mocked HTTP) ----------------
SCRIPT = "#!/bin/bash\necho join\n"
@respx.mock
def test_e2e(monkeypatch):
    for k, v in dict(E2E_API_KEY="ak", E2E_AUTH_TOKEN="tok", E2E_PROJECT_ID="42").items(): monkeypatch.setenv(k, v)
    p = P.E2EProvisioner(); assert p.available()[0]
    base = "https://api.e2enetworks.com/myaccount/api/v1"
    cr = respx.post(f"{base}/nodes/").mock(return_value=httpx.Response(200, json={"code": 200, "data": {"id": 9911, "status": "Creating"}}))
    assert p.create("n1", "cpu-medium", SCRIPT, False) == "9911"
    rq = cr.calls[0].request; body = json.loads(rq.content)
    assert rq.headers["authorization"] == "Bearer tok" and rq.url.params["apikey"] == "ak" and rq.url.params["project_id"] == "42"
    assert body["start_scripts"] == [SCRIPT] and body["plan"].startswith("C3-") and body["name"] == "aloof-n1"
    respx.get(f"{base}/nodes/9911/").mock(side_effect=[httpx.Response(200, json={"data": {"status": "Creating"}}), httpx.Response(200, json={"data": {"status": "Running", "public_ip_address": "164.52.1.2"}})])
    assert p.status("9911")["state"] == "provisioning"; s = p.status("9911"); assert s == {"state": "running", "public_ip": "164.52.1.2", "raw_status": "Running"}
    d = respx.delete(f"{base}/nodes/9911/").mock(return_value=httpx.Response(200, json={})); p.destroy("9911"); assert d.called
    with pytest.raises(P.ProvisionError): p.create("n2", "gpu-h100", SCRIPT, True)      # GPU plan string must be configured
    monkeypatch.setenv("PAAS_E2E_PLAN_GPU_H100", "GDC3.H100-1x"); respx.post(f"{base}/nodes/").mock(return_value=httpx.Response(200, json={"data": [{"id": 5}]}))
    assert p.create("n2", "gpu-h100", SCRIPT, True) == "5"
    respx.post(f"{base}/nodes/").mock(return_value=httpx.Response(403, json={"message": "bad key"}))
    with pytest.raises(P.ProvisionError, match="403"): p.create("n3", "cpu-medium", SCRIPT, False)

@respx.mock
def test_jarvislabs(monkeypatch):
    monkeypatch.setenv("JARVISLABS_TOKEN", "jt"); p = P.JarvislabsProvisioner(); assert p.available()[0]
    b = p._base()
    up = respx.post(f"{b}/scripts/add").mock(return_value=httpx.Response(200, json={"success": True}))
    respx.get(f"{b}/scripts/").mock(return_value=httpx.Response(200, json={"script_meta": [{"script_name": "aloof-n1", "script_id": 77}]}))
    cr = respx.post(f"{b}/templates/vm/create").mock(return_value=httpx.Response(200, json={"machine_id": 314}))
    assert p.create("n1", "gpu-a100", SCRIPT, True) == "314"
    assert up.calls[0].request.headers["authorization"] == "Bearer jt" and b"echo join" in up.calls[0].request.content
    body = json.loads(cr.calls[0].request.content); assert body["script_id"] == "77" and body["gpu_type"]
    respx.get(f"{b}/misc/status").mock(return_value=httpx.Response(200, json={"status": "Running"}))
    respx.get(f"{b}/users/fetch/314").mock(return_value=httpx.Response(200, json={"instance": {"public_ip": "1.2.3.4"}}))
    assert p.status("314")["public_ip"] == "1.2.3.4"
    dl = respx.post(f"{b}/templates/vm/destroy").mock(return_value=httpx.Response(200, json={})); p.destroy("314"); assert dl.calls[0].request.url.params["machine_id"] == "314"
    with pytest.raises(P.ProvisionError): p.create("n2", "cpu-small", SCRIPT, False)

@respx.mock
def test_krutrim(monkeypatch):
    for k, v in dict(KRUTRIM_EMAIL="e@x", KRUTRIM_PASSWORD="pw", KRUTRIM_REGION="In-Bangalore-1", KRUTRIM_VPC_ID="vpc", KRUTRIM_SUBNET_ID="sn",
                     KRUTRIM_SECURITY_GROUP="sg1,sg2", KRUTRIM_SSHKEY_NAME="k", KRUTRIM_IMAGE_KRN="krn:img").items(): monkeypatch.setenv(k, v)
    p = P.KrutrimProvisioner(); P.KrutrimProvisioner._tok = None; assert p.available()[0]
    b = "https://cloud.olakrutrim.com"
    si = respx.post(f"{b}/iam/v1/signInAsRootUser").mock(return_value=httpx.Response(200, json={"access_token": "AT"}))
    cr = respx.post(f"{b}/vm/v1/create_instance_async").mock(return_value=httpx.Response(200, json={"task_id": "t-1"}))
    ref = p.create("n1", "gpu-h100", SCRIPT, True); assert json.loads(ref) == {"task_id": "t-1", "name": "aloof-n1"}
    rq = cr.calls[0].request; body = json.loads(rq.content)
    assert rq.headers["authorization"] == "Bearer AT" and rq.headers["x-region"] == "In-Bangalore-1"
    assert base64.b64decode(body["user_data"]).decode() == SCRIPT and body["security_groups"] == ["sg1", "sg2"] and body["isGpu"]
    respx.get(f"{b}/vm/v1/get_instance_task_status").mock(return_value=httpx.Response(200, json={"status": "SUCCESS"}))
    respx.get(f"{b}/vm/v1/search_instances").mock(return_value=httpx.Response(200, json={"instances": [{"instanceName": "aloof-n1", "status": "ACTIVE", "krn": "krn:vm1", "ip_address": "10.1.1.5"}]}))
    assert p.status(ref)["state"] == "running"
    dl = respx.delete(f"{b}/vm/v1/delete_instance_async").mock(return_value=httpx.Response(200, json={})); p.destroy(ref)
    assert dl.calls[0].request.url.params["instanceKrn"] == "krn:vm1" and si.call_count == 1   # token cached

def test_aws_spot(monkeypatch):
    from botocore.stub import Stubber, ANY
    import boto3
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AK"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SK")
    s = boto3.session.Session(region_name="ap-south-1"); ec2, ssm = s.client("ec2"), s.client("ssm")
    p = P.AWSSpotProvisioner(); p._clients = (ec2, ssm); assert p.available()[0]
    with Stubber(ec2) as se, Stubber(ssm) as ss:
        ss.add_response("get_parameter", {"Parameter": {"Value": "ami-gpu123"}}, {"Name": p.AMI_PARAMS[True]})
        se.add_response("run_instances", {"Instances": [{"InstanceId": "i-0abc"}]},
                        {"ImageId": "ami-gpu123", "InstanceType": "g4dn.xlarge", "MinCount": 1, "MaxCount": 1, "UserData": SCRIPT,
                         "InstanceMarketOptions": {"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate", "MaxPrice": "0.3000"}},
                         "BlockDeviceMappings": ANY, "MetadataOptions": {"HttpTokens": "required"}, "TagSpecifications": ANY})
        assert p.create("n1", "gpu-t4-spot", SCRIPT, True) == "i-0abc"
        se.add_response("describe_instances", {"Reservations": [{"Instances": [{"InstanceId": "i-0abc", "State": {"Name": "running", "Code": 16}, "PublicIpAddress": "13.2.3.4"}]}]}, {"InstanceIds": ["i-0abc"]})
        assert p.status("i-0abc")["public_ip"] == "13.2.3.4"
        se.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-0abc"]}); p.destroy("i-0abc")

def test_inactive_without_keys():
    for n in ("e2e", "jarvislabs", "krutrim", "aws-ap-south-1-spot"): assert P.PROVISIONERS[n].available()[0] is False
    from paas.drivers import bootstrap
    s = bootstrap.render("n9", "e2e", "gpu-h100", "jt_secret", True)
    assert "nvidia-container-toolkit" in s and "jt_secret" in s and "wireguard" in s.lower() and s.startswith("#!")

def test_scheduler_bin_packing():
    from paas.drivers import remote
    now = time.time()
    for nid, cpu, mem in (("nA", 4, 8192), ("nB", 4, 8192)):
        q("insert into nodes(id,provider,status,vcpu,mem_mb,gpus,created,last_seen,plan,agent_url) values(?,?,?,?,?,?,?,?,?,?)", (nid, "self-hosted", "ready", cpu, mem, 0, now, now, "cpu-large", "http://127.0.0.1:1"))
    picks = [remote.place("self-hosted", "cpu-small", "app%d" % i, {"volumes": []}) for i in range(3)]
    assert all(p["id"] in ("nA", "nB") for p in picks)

def test_agent_gpu_container_args(monkeypatch):
    from paas.agent import agent as ag
    calls = []
    def fake_sh(*a, check=True):
        calls.append(list(a))
        if a[1] == "create":
            ef = a[a.index("--env-file") + 1]; calls.append(("envfile", open(ef).read(), oct(os.stat(ef).st_mode & 0o777)))
        return "abcdef123456789" if a[1] == "create" else ""
    monkeypatch.setattr(ag, "sh", fake_sh); monkeypatch.setattr(ag, "host_port", lambda cid, port: 31000)
    monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: None)
    r = ag.run_container({"wg_ip": "10.88.0.7"}, {"name": "aloof-llm-0", "image": "10.88.0.1:5000/llm:1", "port": 8000, "gpu": 1, "cpus": 8, "mem_mb": 65536,
                                              "env": {"HF_TOKEN": "hf_secret"}, "volumes": [{"name": "models", "path": "/models"}]})
    run = next(c for c in calls if isinstance(c, list) and c[1] == "create")
    assert ["docker", "start", "abcdef123456"] in calls
    assert ["--gpus", "all"] == run[run.index("--gpus"):run.index("--gpus") + 2] and "10.88.0.7::8000" in run
    assert "aloof-vol-models:/models" in run and "hf_secret" not in " ".join(run)
    ef = next(c for c in calls if c[0] == "envfile"); assert "HF_TOKEN=hf_secret" in ef[1] and ef[2] == "0o600"
    assert r == {"id": "abcdef123456", "host_port": 31000, "host": "10.88.0.7"}

def test_local_volume_is_bind_backed(tmp_path, monkeypatch):
    from paas.drivers.local_docker import LocalDockerDriver
    created = {}
    class Vols:
        def get(self, n):
            import docker; raise docker.errors.NotFound("x")
        def create(self, n, **kw): created[n] = kw
    d = LocalDockerDriver.__new__(LocalDockerDriver); d.VOL_DIR = str(tmp_path); monkeypatch.setattr(LocalDockerDriver, "c", type("C", (), {"volumes": Vols()})(), raising=False)
    d._ensure_volume("pgdata", 5)
    assert created["paas-vol-pgdata"]["driver_opts"] == {"type": "none", "o": "bind", "device": str(tmp_path / "pgdata")} and (tmp_path / "pgdata").is_dir()

def test_pricing_v3_usd_margins_and_structure():
    from paas.plans import PLANS, TIERS, RATES, RATE_COST, margin, fee_rate, rate_margin, CURRENCY
    assert CURRENCY == "USD" and fee_rate() > 0.10        # worst case: $0.30 on a $5 charge + 2.9% + 1.5% intl + 0.5% tax
    sellable = {k: p for k, p in PLANS.items() if not p["legacy"]}
    assert {"shared", "performance"} <= {p["cpu_class"] for p in sellable.values() if p["kind"] == "cpu"}
    for k, p in sellable.items():
        assert margin(p) >= 0.30, f"{k} below 30% margin after fees"
        c = p.get("compare_usd_month") or {}
        if c.get("fly"): assert p["price_usd_month"] <= c["fly"], f"{k} above Fly"
        if c.get("render"): assert p["price_usd_month"] <= c["render"], f"{k} above Render"
        assert round(p["price_usd_month"] or p["price_usd_hr"], 2) == (p["price_usd_month"] or p["price_usd_hr"])   # clean cent prices
    for k in RATE_COST: assert rate_margin(k) >= 0.30
    assert (TIERS["free"]["fee_usd"], TIERS["hobby"]["fee_usd"], TIERS["pro"]["fee_usd"]) == (0, 5, 20)
    assert all(t["included_usage_usd"] == t["fee_usd"] for k, t in TIERS.items() if k != "free")
    assert all(PLANS[k]["legacy"] for k in ("cpu-nano", "cpu-small", "cpu-medium", "cpu-large"))

def test_margin_floor_tightens_with_worse_fx(monkeypatch):
    import paas.plans as P
    monkeypatch.setattr(P, "COST_USD_INR", 60)       # rupee much stronger -> USD costs up ~38% -> GPU pools must be refused
    assert not P.pool_allowed("gpu-h100", "e2e") and P.check_floor()
    monkeypatch.undo(); assert P.check_floor() == []

def test_meter_volume_egress_included_usage_and_tier_fee():
    a = engine.create_account("meter2@example.in"); aid = a["id"]; now = time.time()
    billing.meter_tick(now)   # first tick of the month: sets month_key, free tier fee 0
    q("update accounts set credit_cents=100000, tier='hobby' where id=?", (aid,))   # $1000; hobby: $5 included (fee already paid this month)
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", ("bwapp", aid, crypto.dump_spec({"plan": "perf-1x-2gb"}), "running", "local-docker", 0, 0))
    q("insert into instances(id,app,status,started,host,port) values(?,?,?,?,?,?)", ("i-bw", "bwapp", "running", now - 3600 * 10, "127.0.0.1", 1))
    q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", ("bigvol", aid, 100, now - 720 * 3600))   # 100 GB for a full month
    billing.EGRESS["bwapp"] = int(35e9)   # 35 GB, counts toward included usage
    billing.meter_tick(now)
    m = billing.month_summary(aid); assert m["currency"] == "USD"
    assert abs(m["usage"]["volume"]["cost_cents"] - 2200) < 0.5           # 100 GB-month × $0.22
    assert abs(m["usage"]["egress"]["cost_cents"] - 210) < 0.01            # 35 GB × $0.06
    assert abs(m["usage"]["compute"]["cost_cents"] - 10 * 2400 / 720) < 0.05
    credit = q("select credit_cents from accounts where id=?", (aid,), one=True)["credit_cents"]
    assert abs((100000 - credit) - (m["usage_total_cents"] - 500)) < 0.05   # first $5 of usage included on Hobby
    billing.EGRESS["bwapp"] = int(1e9); billing.meter_tick(now)
    assert abs(billing.month_summary(aid)["usage"]["egress"]["cost_cents"] - 216) < 0.01
    # tier fee charged once at month rollover (wallet-paid)
    q("update accounts set tier='pro', month_key='1999-01', credit_cents=500000 where id=?", (aid,)); q("delete from instances where id='i-bw'"); q("delete from volumes where name='bigvol'")
    billing.meter_tick(now); billing.meter_tick(now)
    assert abs(q("select credit_cents from accounts where id=?", (aid,), one=True)["credit_cents"] - (500000 - 2000)) < 0.01

def test_tier_api(client):
    c = _acct_client("tier@example.in")
    assert c.get("/v1/billing").json()["credit_usd"] == 5.0                     # $5 signup credit
    assert c.put("/v1/billing/tier", json={"tier": "gold"}).status_code == 400
    r = c.put("/v1/billing/tier", json={"tier": "hobby"}); assert r.status_code == 200
    b = c.get("/v1/billing").json(); assert b["month"]["tier"] == "hobby" and b["credit_cents"] < 500 and any(l["kind"] == "plan" for l in b["ledger"])
    p = TestClient(api.app).get("/v1/plans").json(); assert p["currency"] == "USD" and "inr" not in json.dumps(p["plans"]).lower() and "₹" not in json.dumps(p["plans"], ensure_ascii=False) and "cpu-small" not in [x["id"] for x in p["plans"]]
    assert list(p["regions"])[0] == "nbg1" and "Hetzner" in p["region_note"] and "runs in India" not in p["region_note"]


def test_min_margin_floor_every_plan_every_pool():
    """Fails if any plan could be scheduled on a pool at under 30% gross margin (worst-case cost), or any rate is under 30%."""
    from paas import plans
    assert plans.MIN_MARGIN >= 0.30
    assert plans.check_floor() == [], plans.check_floor()
    for k in plans.PLANS:
        for pool in plans.allowed_pools(k):
            m = plans.pool_margin(k, pool); assert m is not None and m >= 0.30, f"{k}@{pool} {m}"
    for k, c in plans.RATE_COST.items(): assert 1 - c / plans.RATES[k] >= 0.30, k
    # known-bad combos are refused
    assert not plans.pool_allowed("shared-1x-256mb", "krutrim") and not plans.pool_allowed("perf-1x-2gb", "aws-ap-south-1-spot")
    assert not plans.pool_allowed("gpu-l4", "jarvislabs")          # cost unknown -> refused
    assert plans.pool_allowed("gpu-a100", "e2e") and plans.pool_allowed("gpu-h100", "e2e")

def test_scheduler_refuses_under_floor_pool(monkeypatch):
    from paas import plans
    class D:
        supports_gpu = False
        def available(self): return True, "ok"
    monkeypatch.setattr(engine, "get_drivers", lambda: {"krutrim": D(), "e2e": D(), "local-docker": D()})
    with pytest.raises(ValueError, match="floor"): engine.pick_provider({"plan": "shared-1x-1gb", "provider": "krutrim"})
    monkeypatch.setitem(plans.PLANS["shared-1x-1gb"], "providers", ["krutrim", "e2e"])
    assert engine.pick_provider({"plan": "shared-1x-1gb"}) == "e2e"            # krutrim skipped
    monkeypatch.setitem(plans.PLANS["shared-1x-1gb"], "providers", ["krutrim"])
    assert engine.pick_provider({"plan": "shared-1x-1gb"}) == "local-docker"
    monkeypatch.setenv("PAAS_COST_LOCAL_DOCKER_SHARED_1X_1GB", "1.0")          # $720/mo cost > $5.99 price
    with pytest.raises(engine.NoCapacity, match="floor"): engine.pick_provider({"plan": "shared-1x-1gb"})

def test_free_tier_limits_and_hard_cap():
    a = engine.create_account("free-cap@example.in"); aid = a["id"]
    with pytest.raises(ValueError, match="free tier"): engine.apply_tier_limits(aid, engine.validate({"name": "fc-big", "plan": "perf-1x-2gb", "source": {"image": "x"}}))
    sp = engine.apply_tier_limits(aid, engine.validate({"name": "fc-ok", "plan": "shared-1x-256mb", "instances": 4, "source": {"image": "x"}, "autoscale": {"min": 2, "max": 5}}))
    assert sp["instances"] <= 1 and sp["autoscale"]["min"] == 0 and sp["autoscale"]["max"] == 1 and sp["autoscale"]["idle_seconds"] <= 300
    with pytest.raises(ValueError, match="GB"): engine.create_volume(aid, "fc-vol", 5)
    now = time.time(); billing.meter_tick(now)
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)",
      ("fc-ok", aid, json.dumps(sp), "running", "local-docker", now, now))
    q("insert into instances(id,app,provider,host,port,status,started) values(?,?,?,?,?,?,?)", ("fc-i1", "fc-ok", "local-docker", "x", 1, "running", now - 3600 * 24 * 30 * 2))
    credit0 = q("select credit_cents from accounts where id=?", (aid,), one=True)["credit_cents"]
    import unittest.mock as um
    with um.patch.object(engine, "scale", lambda *a, **k: q("update instances set status='stopped' where app=?", (a[0],))): billing.meter_tick(now)
    assert q("select status from apps where name='fc-ok'", one=True)["status"] == "capped"
    assert q("select credit_cents from accounts where id=?", (aid,), one=True)["credit_cents"] == credit0   # free never draws on wallet
    billing.uncap_account(aid, "test"); assert q("select status from apps where name='fc-ok'", one=True)["status"] == "idle"

def test_autoscale_does_not_idle_right_after_redeploy(monkeypatch):
    from paas import controller
    now = time.time(); sp = engine.validate({"name": "redep-x", "plan": "shared-1x-256mb", "source": {"image": "x"}, "autoscale": {"min": 0, "max": 1, "idle_seconds": 60}})
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", ("redep-x", 1, json.dumps(sp), "running", "local-docker", now - 9999, now))
    q("insert into instances(id,app,provider,host,port,status,started) values(?,?,?,?,?,?,?)", ("redep-i", "redep-x", "local-docker", "x", 1, "running", now))
    controller.M.last["redep-x"] = now - 9999        # last request long before the redeploy
    calls = []; monkeypatch.setattr(engine, "scale", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(controller, "q", lambda sql, args=(), one=False: [r for r in q(sql, args, one) if r["name"] == "redep-x"] if "from apps" in sql else q(sql, args, one))
    controller.autoscale_tick()
    assert not any(c[0] == "redep-x" and c[1] == 0 for c in calls), calls

# ---------------- health endpoints ----------------
def test_api_health_endpoint(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
