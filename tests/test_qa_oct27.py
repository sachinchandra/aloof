"""QA regression Oct 27: F1 registry/image host SSRF, F2 console scope, F3 org tokens + OAuth revoke, F4 health path, F6 numeric hosts."""
import json, time, pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from paas import api, engine, creds
from test_parity import fake, user, deploy   # noqa: F401
from test_scopes import _client
@pytest.mark.parametrize("img", ["169.254.169.254/x", "169.254.169.254:80/x:1", "0xa9fea9fe:5000/x", "[::1]:5000/x", "[::ffff:169.254.169.254]/x",
                                 "localhost:5000/x", "10.0.0.5:5000/x", "192.168.1.1/x", "100.100.100.200/x", "metadata.google.internal/x", "2852039166/x", "0xa9fea9fe/x"])
def test_f1_bad_image_hosts(img, monkeypatch):
    monkeypatch.delenv("PAAS_ALLOW_PRIVATE_REGISTRY", raising=False)
    with pytest.raises(ValueError): creds.check_image(img)
def test_f1_dns_resolved_private(monkeypatch):
    monkeypatch.delenv("PAAS_ALLOW_PRIVATE_REGISTRY", raising=False)
    monkeypatch.setattr(creds.socket, "getaddrinfo", lambda h, p: [(2, 1, 6, "", ("169.254.169.254", 0))])
    with pytest.raises(ValueError): creds.check_image("evil.example.com/x")
    with pytest.raises(ValueError): creds.registry_set("acc", "evil.example.com", "u", "p")
def test_f1_good_and_platform(monkeypatch):
    monkeypatch.delenv("PAAS_ALLOW_PRIVATE_REGISTRY", raising=False)
    monkeypatch.setenv("PAAS_REGISTRY", "127.0.0.1:5000"); monkeypatch.setenv("PAAS_REGISTRY_NODE", "10.88.0.1:5000")
    monkeypatch.setattr(creds.socket, "getaddrinfo", lambda h, p: [(2, 1, 6, "", ("140.82.112.33", 0))])
    for i in ("nginx", "nginx:1.27", "library/nginx", "ghcr.io/o/a:1", "127.0.0.1:5000/app:1", "10.88.0.1:5000/app:2", "img@sha256:" + "a" * 64):
        assert creds.check_image(i) == i
def test_f1_validate_rejects(monkeypatch):
    monkeypatch.delenv("PAAS_ALLOW_PRIVATE_REGISTRY", raising=False)
    with pytest.raises(ValueError): engine.validate({"name": "ssrf-x", "source": {"image": "169.254.169.254/latest/meta-data"}})
def test_f1_host_output_rule_script():
    import os
    s = open(os.path.join(os.path.dirname(__file__), "..", "deploy", "metadata-block.sh")).read()
    assert "-I OUTPUT 1" in s and "cloud-init status" in s and "-D OUTPUT" in s and "DOCKER-USER" in s
def test_f2_console_scope(fake, user):
    deploy(user, "f2-a", "good:v1"); deploy(user, "f2-b", "good:v1")
    t = user.post("/v1/tokens", json={"app": "f2-a"}).json()["token"]
    c = TestClient(api.app)
    with c.websocket_connect("/v1/apps/f2-b/console", headers={"authorization": f"Bearer {t}"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e: ws.receive_bytes()
    assert e.value.code == 4403 and "scoped to app f2-a" in e.value.reason
    key = user.headers["Authorization"].split()[1]
    with c.websocket_connect("/v1/apps/f2-a/console", headers={"authorization": f"Bearer {key}", "x-aloof-scope": "paas:read"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e: ws.receive_bytes()
    assert e.value.code == 4403 and "paas:write" in e.value.reason
def test_f3_org_token_needs_admin_and_dies_with_grant(fake, user):
    deploy(user, "f3-a", "good:v1")
    w = _client(user, "paas:write")
    r = w.post("/v1/tokens", json={}); assert r.status_code == 403 and "org-wide" in r.json()["detail"]
    assert w.post("/v1/tokens", json={"app": "f3-a"}).status_code == 200
    w.headers["X-Aloof-Client"] = "cid-f3"
    via = w.post("/v1/tokens", json={"app": "f3-a"}).json()
    adm = _client(user, "paas:admin"); adm.headers["X-Aloof-Client"] = "cid-f3"
    org = adm.post("/v1/tokens", json={}).json(); assert org["token"].startswith("dt_")
    mine = user.post("/v1/tokens", json={"app": "f3-a"}).json()
    r = user.delete("/v1/auth/connected-apps/cid-f3"); assert r.status_code == 200 and r.json()["deploy_tokens_revoked"] == 2
    ids = {x["id"] for x in user.get("/v1/tokens").json()}
    assert via["id"] not in ids and org["id"] not in ids and mine["id"] in ids
    assert _client(user, token=org["token"]).get("/v1/apps").status_code == 401
def test_f3_mcp_sends_client_header():
    import os
    s = open(os.path.join(os.path.dirname(__file__), "..", "mcp_server", "server.py")).read()
    assert 'h["X-Aloof-Client"] = t.client_id' in s
@pytest.mark.parametrize("p", ["@178.104.200.75:9/x", "/@evil", "http://x/", "//evil/x", "/a b", "/a\\b", "healthz", "/x\n"])
def test_f4_bad_health_paths(p, fake, user):
    import secrets; n = "f4-" + secrets.token_hex(3); deploy(user, n, "good:v1")
    r = user.put(f"/v1/apps/{n}/health", json={"path": p}); assert r.status_code == 422, p
    with pytest.raises(ValueError): engine.health_url("10.0.0.1", 80, p)
def test_f4_good_health_paths():
    assert engine.health_url("10.0.0.1", 8080, "/healthz") == "http://10.0.0.1:8080/healthz"
    assert engine.health_url("10.0.0.1", 8080, "/h?x=1") == "http://10.0.0.1:8080/h?x=1"
    assert engine.health_url("10.0.0.1", 8080, None) == "http://10.0.0.1:8080/"
