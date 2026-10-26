from fastapi.testclient import TestClient
from paas import api

def test_llms_txt_tax_wording():
    t = TestClient(api.app).get("/llms.txt").text
    assert "excl. tax" not in t and "Prices are in US dollars; taxes may apply where required." in t

def test_expired_magic_link_page_is_branded_and_offers_next_step():
    r = TestClient(api.app).get("/auth/magic?token=nope", follow_redirects=False)
    assert r.status_code == 400 and "Get a new link" in r.text and "/login" in r.text and "<html" in r.text.lower()

def test_dashboard_empty_state_links_templates_and_warns_on_unready():
    h = open("dashboard/index.html").read()
    assert 'href="/templates"' in h and "isn't answering yet" in h and "portGuess" in h

def test_doc_bugs_33_to_37():
    c = TestClient(api.app)
    g = c.get("/guides/fastapi").text
    assert "-o aloof &amp;&amp;" not in g and "~/.local/bin/aloof" in g and "api.aloof.si/cli/aloof" in g and "aloof login" in g and "./aloof" in g
    assert c.get("/guides").text.count("href='/guides/docker'") == 1
    r = c.get("/guides/node", follow_redirects=False); assert r.status_code == 301 and r.headers["location"] == "/guides/express"
    qs = c.get("/docs/quickstart").text; assert "live: https://&lt;your-app-name&gt;." in qs
    assert "aloof health app " not in c.get("/docs/deploy").text and "aloof health &lt;app&gt;" in c.get("/docs/deploy").text

def _client(email):
    from paas import auth
    c = TestClient(api.app); r = c.post("/v1/auth/register", json={"email": email, "password": "correct-horse-9"}); assert r.status_code == 200, r.text
    return c

def test_byo_waitlist_join_once_admin_and_event(monkeypatch):
    from paas import analytics, waitlist as W
    sent = []; monkeypatch.setattr(analytics, "capture", lambda a, e, p=None: sent.append((e, p)))
    monkeypatch.setattr(W, "send_emails", lambda i: None)
    import time; c = _client(f"byo{int(time.time()*1000)}@example.org")
    assert c.get("/v1/waitlist/byo-server", headers={"X-Aloof-CSRF": "1"}).json()["joined"] is False
    assert c.post("/v1/waitlist/byo-server", json={"provider": "mars"}, headers={"X-Aloof-CSRF": "1"}).status_code == 400
    r = c.post("/v1/waitlist/byo-server", json={"provider": "hetzner"}, headers={"X-Aloof-CSRF": "1"}).json(); assert r["joined"] and not r["duplicate"]
    r = c.post("/v1/waitlist/byo-server", json={"provider": "aws"}, headers={"X-Aloof-CSRF": "1"}).json(); assert r["duplicate"]
    g = c.get("/v1/waitlist/byo-server").json(); assert g["joined"] and g["provider"] == "hetzner"
    assert [e for e, _ in sent].count("byo_waitlist_join") == 1 and ("byo_waitlist_join", {"provider": "hetzner"}) in sent
    assert "byo_waitlist_join" in analytics.EVENTS and "byo_server" in W.counts()
    assert any(x["kind"] == "byo_server" for x in W.rows(kind="byo_server"))
    assert TestClient(api.app).post("/v1/waitlist/byo-server", json={}).status_code in (401, 403)
    assert TestClient(api.app).post("/v1/waitlist", json={"email": "x@example.org", "kind": "byo_server"}).status_code == 400

def test_byo_ui_and_docs():
    h = open("dashboard/index.html").read(); assert "Deploy to your own server" in h and "Join the waitlist" in h and "id=own-server" in h
    assert "byo_server" in open("dashboard/admin.html").read()
    d = TestClient(api.app).get("/docs/mcp").text; assert "Coming: your own servers" in d and "next=%2Fdashboard%23own-server#register" in d
    assert "byo_waitlist_join" in open("docs/analytics-events.md").read()
