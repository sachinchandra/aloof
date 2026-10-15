import re
from paas import site, auth
from paas.contact import CONTACT_EMAIL

def test_pages_use_single_contact():
    for fn in (lambda: site.legal("privacy", "Aloof"), lambda: site.legal("terms", "Aloof")):
        h = fn()
        assert h and not re.search(r"(privacy|legal|security|sales|hello)@aloof\.si", h)
        assert CONTACT_EMAIL in h

def test_reply_to(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "OUTBOX", str(tmp_path / "o.log")); monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("PAAS_DEV_OUTBOX", "1")
    auth.send_mail("a@example.com", "s", "b")
    assert f"Reply-To: {CONTACT_EMAIL}" in open(tmp_path / "o.log").read()
