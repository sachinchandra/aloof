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
