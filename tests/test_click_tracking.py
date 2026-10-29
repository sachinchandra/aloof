"""Click tracking: $autocapture + cta_clicked in the site script, is_test marker, FIRST1 claim, social signup keeps first-touch UTM."""
import os, json
from fastapi.testclient import TestClient
from paas import api, oauth_login, engine, analytics, site
from paas.db import q
JS = open(os.path.join(os.path.dirname(api.__file__), "static", "analytics.js")).read()
def test_js_autocapture_and_ctas():
    assert "send('$autocapture',{$event_type:'click',$elements_chain:" in JS and "send('cta_clicked',{cta:" in JS and "cta_click'" not in JS
    assert ".value" not in JS.split("function esc")[1]                     # never captures input values
    assert "aloof_test|is_test" not in JS and "location.search))sessionStorage.setItem('aloof_is_test'" not in JS   # F3: no URL opt-in
    assert "T?{is_test:true}:{}" in JS and "T=!!isTest;var st={is_test:T}" in JS
    assert "$current_url:cu(location.href)" in JS and "location.href," not in JS and "$referrer:document.referrer?cu(" in JS   # F4
    assert "first_promo" in JS and "+'_claim'" in JS and "subscribe_card" in JS
    assert "analytics.js?v=9" in site.analytics_tag() or not site.analytics_tag()
def test_cta_markup():
    d = open(os.path.join(os.path.dirname(api.__file__), "..", "dashboard", "index.html")).read()
    assert "data-cta=deploy_app" in d and "data-cta=subscribe_card" in d
    h = site.sso_buttons([{"id": "github", "enabled": True}])
    assert 'data-cta="github_signin"' in h
    assert "searchParams.set(k" in site.SSO_JS and "utm_source" in site.SSO_JS
def test_social_start_keeps_utm_and_callback_sends_it(monkeypatch):
    monkeypatch.setattr(oauth_login, "start", lambda p, n, link_account=None: ("https://x.example/a", "st"))
    monkeypatch.setattr(api, "SECURE_COOKIES", False); api._login_hits.clear()
    c = TestClient(api.app)
    r = c.get("/auth/github/start?utm_source=prospects_w44&utm_campaign=w44&evil=1", follow_redirects=False)
    assert r.status_code == 303 and "aloof_attr=" in r.headers["set-cookie"] and "evil" not in r.headers["set-cookie"]
    acct = engine.create_account(f"gh{engine.secrets.token_hex(3)}@example.com")
    monkeypatch.setattr(oauth_login, "enabled", lambda p: True)
    monkeypatch.setattr(oauth_login, "take_state", lambda *a: {"ver": "v", "nonce": "n"})
    monkeypatch.setattr(oauth_login, "_exchange", lambda *a: {}); monkeypatch.setattr(oauth_login, "profile", lambda *a: {})
    monkeypatch.setattr(oauth_login, "resolve", lambda *a: (acct, True, None))
    got = []; monkeypatch.setattr(analytics, "capture", lambda i, e, p=None: got.append((i, e, p)))
    c2 = TestClient(api.app); c2.cookies.set("aloof_attr", "utm_source=prospects_w44&utm_campaign=w44", path="/auth/")
    r = c2.get("/auth/github/callback?code=c&state=s", follow_redirects=False)
    assert r.status_code == 303
    assert got and got[0][1] == "signup_completed" and got[0][2]["utm_source"] == "prospects_w44" and got[0][2]["method"] == "github"
    assert json.loads(q("select signup_attribution s from accounts where id=?", (acct["id"],), one=True)["s"])["utm_campaign"] == "w44"
