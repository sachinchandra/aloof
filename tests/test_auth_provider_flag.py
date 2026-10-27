"""AUTH_<PROVIDER>_ENABLED=0 hides a provider while its keys stay configured."""
import pytest
from paas import oauth_login as ol
@pytest.fixture
def keys(monkeypatch):
    for p in ("GOOGLE", "GITHUB"):
        monkeypatch.setenv(f"{p}_OAUTH_CLIENT_ID", "id"); monkeypatch.setenv(f"{p}_OAUTH_CLIENT_SECRET", "sec")
    monkeypatch.delenv("AUTH_GOOGLE_ENABLED", raising=False)
def test_enabled_by_default(keys):
    assert ol.enabled("google") and ol.enabled("github")
def test_flag_off_hides_and_blocks_start(keys, monkeypatch):
    monkeypatch.setenv("AUTH_GOOGLE_ENABLED", "0")
    m = {p["id"]: p["enabled"] for p in ol.providers()}
    assert m["google"] is False and m["github"] is True
    with pytest.raises(ol.AuthError, match="provider_disabled"): ol.start("google", "/")
def test_flag_on_again(keys, monkeypatch):
    monkeypatch.setenv("AUTH_GOOGLE_ENABLED", "1"); assert ol.enabled("google")
