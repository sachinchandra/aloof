"""OAuth scopes paas:read < paas:write < paas:admin ('paas' = admin), enforced in the MCP server and the API; no-destroy deploy tokens."""
import os, sys, re, anyio, httpx, pytest
from fastapi.testclient import TestClient
from paas import api
from test_parity import fake, user, deploy   # noqa: F401 (fixtures)
def _client(user, scope=None, token=None):
    c = TestClient(api.app, base_url="http://api.aloof.test")
    c.headers["Authorization"] = token and f"Bearer {token}" or user.headers["Authorization"]
    if scope: c.headers["X-Aloof-Scope"] = scope
    return c
def test_required_levels():
    r = api.required_level
    assert r("GET", "/v1/apps/x") == 1 and r("POST", "/v1/apps") == 2 and r("DELETE", "/v1/apps/x/domains/a.com") == 2
    assert r("DELETE", "/v1/apps/x") == 3 and r("POST", "/v1/volumes/v/restore") == 3 and r("POST", "/v1/pg/d/offsite-restore") == 3
    assert r("DELETE", "/v1/pg/d") == 3 and r("POST", "/v1/auth/keys") == 3 and r("POST", "/v1/tokens") == 2
def test_read_scope_cannot_deploy(fake, user):
    deploy(user, "sc-app", "good:v1"); c = _client(user, "paas:read")
    assert c.get("/v1/apps/sc-app").status_code == 200
    r = c.post("/v1/apps", json={"name": "sc-app", "source": {"image": "good:v2"}}); assert r.status_code == 403 and "paas:write" in r.json()["detail"]
    assert c.post("/v1/apps/sc-app/env", json={"env": {"A": "1"}}).status_code == 403
    assert _client(user, "bogus").post("/v1/apps/sc-app/env", json={"env": {"A": "1"}}).status_code == 403   # unknown scope = read
def test_write_scope_cannot_destroy_or_restore(fake, user):
    deploy(user, "sc-w", "good:v1"); c = _client(user, "paas:write")
    assert c.post("/v1/apps/sc-w/env", json={"env": {"A": "1"}}).status_code == 200
    r = c.delete("/v1/apps/sc-w"); assert r.status_code == 403 and "paas:admin" in r.json()["detail"]
    assert c.post("/v1/volumes/v1/restore", json={"snapshot_id": "s"}).status_code == 403
    assert c.post("/v1/auth/keys", json={"label": "x"}).status_code == 403   # can't mint a full key
    assert c.post("/v1/tokens", json={"can_destroy": True}).status_code == 403
    t = c.post("/v1/tokens", json={"app": "sc-w"}).json(); assert t["can_destroy"] is False
    assert _client(user, "paas:admin").delete("/v1/apps/sc-w").status_code == 200
def test_deploy_tokens_default_no_destroy(fake, user):
    deploy(user, "sc-t", "good:v1")
    t = user.post("/v1/tokens", json={"app": "sc-t"}).json(); assert t["can_destroy"] is False and "can't destroy" in t["access"]
    c = _client(user, token=t["token"])
    assert c.post("/v1/apps", json={"name": "sc-t", "source": {"image": "good:v2"}, "provider": "local-docker"}).status_code == 200
    assert c.delete("/v1/apps/sc-t").status_code == 403
    assert [x["can_destroy"] for x in user.get("/v1/tokens").json() if x["id"] == t["id"]] == [False]
    t2 = user.post("/v1/tokens", json={"app": "sc-t", "can_destroy": True}).json(); assert t2["can_destroy"] is True
    assert _client(user, token=t2["token"]).delete("/v1/apps/sc-t").status_code == 200
def test_v16_keeps_existing_tokens_destroy_capable():
    from paas import db
    sql = dict(db.MIGRATIONS)[16] if hasattr(db, "MIGRATIONS") else ""
    assert "can_destroy INTEGER default 0" in sql and "update deploy_tokens set can_destroy=1" in sql
def test_token_list_flags_pre_oct26_destroy_tokens(fake, user):
    """doc-bug 41: list shows can_destroy, and flags tokens that kept destroy rights from before the no-destroy default."""
    from paas.db import q
    from paas import devx
    deploy(user, "sc-l", "good:v1")
    new = user.post("/v1/tokens", json={"app": "sc-l", "label": "new"}).json()
    allowed = user.post("/v1/tokens", json={"app": "sc-l", "label": "allowed", "can_destroy": True}).json()
    old = user.post("/v1/tokens", json={"app": "sc-l", "label": "old"}).json()
    q("update deploy_tokens set can_destroy=1, created=? where id=?", (devx.NO_DESTROY_DEFAULT_SINCE - 86400, old["id"]))
    rows = {x["id"]: x for x in user.get("/v1/tokens").json()}
    assert rows[new["id"]]["can_destroy"] is False and rows[new["id"]]["legacy_destroy"] is False and "note" not in rows[new["id"]]
    assert rows[allowed["id"]]["can_destroy"] is True and rows[allowed["id"]]["legacy_destroy"] is False
    assert rows[old["id"]]["can_destroy"] is True and rows[old["id"]]["legacy_destroy"] is True and "revoke it and create a new token" in rows[old["id"]]["note"]
def test_cli_and_dashboard_show_destroy():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cli = open(os.path.join(root, "cli", "aloof")).read(); dash = open(os.path.join(root, "dashboard", "index.html")).read()
    assert "DESTROY" in cli and "legacy_destroy" in cli and "created before Oct 26 2026 can still destroy" in cli
    assert "legacy_destroy" in dash and "Can deploy and delete" in dash and "Can deploy, no delete" in dash and "nk_destroy" in dash and "id=keys_legacy" in dash
