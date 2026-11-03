"""Nov 3 UX: internal deploy failures are shown as one plain sentence; customer build errors stay; admin keeps raw. Bug 59: no Stripe name."""
import time
from fastapi.testclient import TestClient
from paas.api import app
from paas import public_errors as pe, stripe_billing
from paas.crypto import dump_spec
from paas.db import q
from test_admin_cockpit import _reg, acct

RAW = "[Errno 2] No such file or directory: 'docker'"

def test_classifier():
    for s in (RAW, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "Traceback (most recent call last):\n  File x\nKeyError: 'id'",
              "HTTPConnectionPool(host='10.0.0.2', port=8081): Read timed out.", "'NoneType' object has no attribute 'id'",
              "500 Server Error for http+docker://localhost/v1.43/containers/create"):
        assert pe.MSG in pe.clean_text(s) and "Errno" not in pe.clean_text(s) and "docker" not in pe.clean_text(s).lower(), s
    for s in ("The command '/bin/sh -c npm ci' returned a non-zero code: 1", "pull access denied for me/nope, repository does not exist",
              "health check failed: app did not answer on port 8080 within 60s", "failed to solve: process \"/bin/sh -c make\" did not complete successfully: exit code: 2",
              "manifest unknown: manifest unknown"):
        assert pe.clean_text(s) == s, s
    assert pe.clean_text(f"v3 failed: {RAW}; still serving v2") == f"v3 failed: {pe.MSG}; still serving v2"
    assert pe.clean_text(f"release v1: deploy\nERROR: {RAW}") == f"release v1: deploy\nERROR: {pe.MSG}"

def test_api_masks_internal_keeps_admin_raw():
    from test_impersonation_secrets import _imp   # reuse a session helper, then make it a normal session
    c, a = _imp(); q("update sessions set impersonated_by=null where account_id=?", (a["id"],))
    n = f"pe{time.time_ns()}"; now = time.time()
    q("insert into apps(name,account_id,status,spec,created,updated,error) values(?,?,?,?,?,?,?)",
      (n, a["id"], "failed", dump_spec({"name": n, "image": "nginx:alpine", "plan": "shared-1x-256mb", "port": 80, "instances": 1}), now, now, RAW))
    q("insert into releases(app,version,created,status,reason,error,spec) values(?,?,?,?,?,?,?)", (n, 1, now, "failed", "deploy", RAW, dump_spec({"name": n, "plan": "shared-1x-256mb"})))
    q("insert into events(app,ts,msg) values(?,?,?)", (n, now, f"ERROR: {RAW}"))
    for u in ("/v1/apps", f"/v1/apps/{n}", f"/v1/apps/{n}/deploy", f"/v1/apps/{n}/releases", f"/v1/apps/{n}/events", f"/v1/apps/{n}/status"):
        r = c.get(u); assert r.status_code in (200, 404) and "Errno" not in r.text, (u, r.text[:300])
    assert pe.MSG in c.get(f"/v1/apps/{n}").text and pe.MSG in c.get(f"/v1/apps/{n}/events").text
    assert q("select error from apps where name=?", (n,), one=True)["error"] == RAW   # raw kept for logs/admin
    assert not pe.applies("/v1/admin/accounts/5") and pe.applies("/v1/apps")

def test_customer_build_error_untouched():
    from test_impersonation_secrets import _imp
    c, a = _imp(); q("update sessions set impersonated_by=null where account_id=?", (a["id"],))
    n = f"pb{time.time_ns()}"; now = time.time(); err = "The command '/bin/sh -c npm ci' returned a non-zero code: 1"
    q("insert into apps(name,account_id,status,spec,created,updated,error) values(?,?,?,?,?,?,?)",
      (n, a["id"], "failed", dump_spec({"name": n, "image": "x", "plan": "shared-1x-256mb", "port": 80, "instances": 1}), now, now, err))
    assert err in c.get(f"/v1/apps/{n}").text

def test_payments_soon_no_stripe(monkeypatch):
    assert "Stripe" not in stripe_billing.PAYMENTS_SOON and "card payments" in stripe_billing.PAYMENTS_SOON
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    try: stripe_billing._call("GET", "/x"); assert False
    except RuntimeError as e: assert "Stripe" not in str(e) and "card payments" in str(e)
    try: stripe_billing.require_payments_open(); assert False
    except RuntimeError as e: assert str(e) == stripe_billing.PAYMENTS_SOON
