"""privacy@aloof.si is shown as written (owner decision Oct 30); other role addresses still map to the contact mailbox."""
from fastapi.testclient import TestClient
from paas.api import app
from paas import contact
c = TestClient(app)
def test_apply_keeps_privacy_only():
    assert contact.apply("privacy@aloof.si") == "privacy@aloof.si"
    assert contact.apply("legal@aloof.si") == contact.CONTACT_EMAIL
def test_pages_show_privacy():
    for u in ("/privacy", "/docs/billing", "/terms"):
        t = c.get(u).text; assert "privacy@aloof.si" in t, u
    assert 'id=delete' in c.get("/docs/billing").text
def test_dashboard_delete_card():
    d = open("dashboard/index.html").read()
    assert 'href="mailto:privacy@aloof.si?subject=Delete%20my%20Aloof%20account">Request deletion' in d
