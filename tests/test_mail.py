"""Mailer: production never writes secrets to the outbox; SMTP failures are logged and surface as 503 for magic links."""
import os, smtplib, logging
from fastapi.testclient import TestClient
from paas import api, auth
c = TestClient(api.app)
def _size(): return os.path.getsize(auth.OUTBOX) if os.path.exists(auth.OUTBOX) else 0
def test_dev_outbox_still_works():
    n = _size(); assert c.post("/v1/auth/magic", json={"email": "dev@x.dev"}).status_code == 200 and _size() > n
def test_production_without_smtp_refuses(monkeypatch, caplog):
    monkeypatch.setenv("PAAS_PUBLIC_API_URL", "https://api.aloof.si"); monkeypatch.delenv("SMTP_HOST", raising=False)
    n = _size()
    with caplog.at_level(logging.ERROR, "paas.mail"):
        r = c.post("/v1/auth/magic", json={"email": "prod@x.dev"})
    assert r.status_code == 503 and _size() == n and "SMTP_HOST not set" in caplog.text and "token=" not in caplog.text
    r = c.post("/v1/auth/register", json={"email": "prod-reg@x.dev", "password": "long-enough-pw"})
    assert r.status_code == 200 and r.json()["api_key"].startswith("pk_") and _size() == n
def test_smtp_failure_logged(monkeypatch, caplog):
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    def boom(*a, **k): raise smtplib.SMTPAuthenticationError(535, b"bad creds")
    monkeypatch.setattr(smtplib, "SMTP", boom)
    with caplog.at_level(logging.ERROR, "paas.mail"):
        assert c.post("/v1/auth/magic", json={"email": "f@x.dev"}).status_code == 503
    assert "smtp send failed" in caplog.text and "SMTPAuthenticationError" in caplog.text
