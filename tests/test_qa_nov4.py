"""QA regress Nov 4: past-due refuses deploy/new app/scale (402), payment_failed on an old subscription ignored, public_errors
denylist gaps + build-timeout message, no credentials in image refs (and masked in the view-as-user spec). Test fixtures only."""
import json, time, pytest
from fastapi.testclient import TestClient
from paas import api, engine, public_errors as PE, imp_mask
from paas.db import q
from test_unit import _acct_client, _stripe_ev
w = TestClient(api.app)
def _post(evid, typ, o): raw, h = _stripe_ev(evid, typ, o); return w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()

def test_past_due_refuses_deploy_new_app_and_scale_402(monkeypatch):
    monkeypatch.setenv("PAAS_PAST_DUE_ACTION", "limit")
    c = _acct_client("qa4pd@example.com"); aid = c.get("/v1/auth/me").json()["id"]
    q("update accounts set tier='hobby', stripe_customer='cus_qa4', stripe_subscription='sub_qa4', past_due_since=? where id=?", (time.time(), aid))
    now = time.time()
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)",
      ("qa4-app", aid, json.dumps({"name": "qa4-app", "plan": "shared-1x-256mb", "instances": 0, "_image": "img", "source": {"image": "img"}}), "stopped", "local-docker", now, now))
    try:
        for body in ({"name": "qa4-new", "source": {"image": "nginx"}}, {"name": "qa4-app", "source": {"image": "nginx"}}):
            r = c.post("/v1/apps", json=body); assert r.status_code == 402 and "payment" in r.json()["detail"] and r.json()["code"] == "payment_past_due"
        r = c.post("/v1/apps/qa4-app/scale", json={"instances": 1}); assert r.status_code == 402
        assert not q("select 1 from apps where name='qa4-new'", one=True)
        monkeypatch.setenv("PAAS_PAST_DUE_ACTION", "none"); assert not engine.past_due_limited(aid)
    finally: q("delete from apps where name like 'qa4-%'")

def test_payment_failed_old_subscription_ignored(monkeypatch):
    monkeypatch.setenv("PAAS_PAST_DUE_ACTION", "limit")
    c = _acct_client("qa4old@example.com"); aid = c.get("/v1/auth/me").json()["id"]
    q("update accounts set stripe_customer='cus_qa4o', stripe_subscription='sub_new' where id=?", (aid,))
    r = _post("evt_qa4o", "invoice.payment_failed", {"id": "in_o", "customer": "cus_qa4o", "subscription": "sub_old", "metadata": {}})
    assert "current subscription" in r["ignored"]
    assert not q("select past_due_since from accounts where id=?", (aid,), one=True)["past_due_since"]

@pytest.mark.parametrize("line", [
    "Command '['nixpacks', 'build', '/tmp/aloof-build-x']' timed out after 1800 seconds",
    "subprocess.TimeoutExpired: Command 'pack build' timed out after 1800 seconds",
    "build failed: [Errno 2] No such file or directory: '/tmp/aloof-build-x/Dockerfile'",
    "OCI runtime create failed: runc create failed: unable to start container process",
    "write /var/lib/docker/tmp/x: No space left on device",
    "failed to do request: context deadline exceeded",
    "ValueError: invalid literal for int()", "RuntimeError: driver gave up", "subprocess.CalledProcessError: returned non-zero exit status 1"])
def test_internal_lines_hidden(line):
    out = PE.clean_text(line)
    assert out != line and "/tmp" not in out and "nixpacks" not in out and "Errno" not in out

def test_build_timeout_has_own_message():
    out = PE.clean_text("Command '['nixpacks', 'build', '/tmp/aloof-build-x']' timed out after 1800 seconds")
    assert "took longer than 30 minutes" in out and PE.MSG not in out

@pytest.mark.parametrize("line", ["pull access denied for foo/bar, repository does not exist", "ERROR: failed to solve: process \"/bin/sh -c npm ci\" did not complete successfully: exit code: 1",
                                  "Step 4/9 : COPY Dockerfile ."])
def test_genuine_lines_kept(line): assert PE.clean_text(line) == line

@pytest.mark.parametrize("img,bad", [("user:tok@registry.io/x/y:1", True), ("https://u:p@ghcr.io/a/b", True), ("bob@registry.io/x", True),
                                      ("nginx:1.27", False), ("ghcr.io/a/b@sha256:" + "a" * 64, False), ("alpine@sha256:" + "b" * 64, False), ("localhost:5000/x:1", False)])
def test_image_userinfo_rejected(img, bad):
    spec = {"name": "qa4-img", "source": {"image": img}}
    if bad:
        with pytest.raises(ValueError, match="credentials"): engine.validate(spec)
    else:
        try: engine.validate(spec)
        except ValueError as e: assert "credentials" not in str(e)
        except Exception: pass   # image existence checks are out of scope here

def test_view_as_masks_image_userinfo():
    out = imp_mask.mask_spec({"spec": {"image": "user:tok@registry.io/x", "source": {"image": "u:p@ghcr.io/a"}, "name": "a"}})
    assert "tok" not in json.dumps(out) and "u:p" not in json.dumps(out) and out["spec"]["image"].endswith("@registry.io/x") and out["spec"]["name"] == "a"

def test_no_processor_name_in_messages():
    src = open(api.__file__).read()
    assert "use Stripe (/v1/billing/checkout)" not in src and "managed by your Stripe subscription" not in src

# ---- QA retest of 4f722c6 (R5) ----
def _pd_app(monkeypatch, tag, cron=False, status="stopped"):
    monkeypatch.setenv("PAAS_PAST_DUE_ACTION", "limit")
    c = _acct_client(f"qa5{tag}@example.com"); aid = c.get("/v1/auth/me").json()["id"]
    q("update accounts set tier='hobby', stripe_customer=?, stripe_subscription=?, past_due_since=? where id=?", (f"cus_q5{tag}", f"sub_q5{tag}", time.time(), aid))
    spec = {"name": f"qa5-{tag}", "plan": "shared-1x-256mb", "instances": 0, "_image": "img", "source": {"image": "img"}}
    if cron: spec["cron"] = [{"name": "nightly", "schedule": "0 3 * * *", "command": "echo hi"}]
    now = time.time()
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", (f"qa5-{tag}", aid, json.dumps(spec), status, "local-docker", now, now))
    q("insert into releases(app,version,image,spec,status,created) values(?,?,?,?,?,?)", (f"qa5-{tag}", 1, "img:old", json.dumps(spec), "superseded", now))
    q("insert into releases(app,version,image,spec,status,created) values(?,?,?,?,?,?)", (f"qa5-{tag}", 2, "img", json.dumps(spec), "active", now))
    return c, aid

@pytest.fixture
def qa5_clean():
    yield
    for t in ("apps", "releases"): q(f"delete from {t} where " + ("name" if t == "apps" else "app") + " like 'qa5-%'")
    try: q("delete from volumes where name='qa5vol'")
    except Exception: pass

@pytest.mark.parametrize("status", ["stopped", "past_due"])
def test_r5a_rollback_blocked_past_due(monkeypatch, qa5_clean, status):
    c, _ = _pd_app(monkeypatch, "rb" + status[:2], status=status)
    with pytest.raises(engine.PastDue): engine.rollback(f"qa5-rb{status[:2]}")
    r = c.post(f"/v1/apps/qa5-rb{status[:2]}/rollback", json={}); assert r.status_code == 402 and r.json()["code"] == "payment_past_due"

def test_r5b_manual_cron_run_blocked(monkeypatch, qa5_clean):
    from paas import cron as cronmod
    ran = []; monkeypatch.setattr(cronmod, "run_job", lambda *a, **k: ran.append(1) or 0)
    c, _ = _pd_app(monkeypatch, "cr", cron=True)
    r = c.post("/v1/apps/qa5-cr/cron/nightly/run"); assert r.status_code == 402 and r.json()["code"] == "payment_past_due" and not ran

def test_r5_volumes_still_allowed_past_due(monkeypatch, qa5_clean):
    c, _ = _pd_app(monkeypatch, "vo")
    assert c.post("/v1/volumes", json={"name": "qa5vol", "size_gb": 1}).status_code != 402

@pytest.mark.parametrize("line", ["failed to read Dockerfile at /tmp/aloof-build-abc123/Dockerfile",
                                  "COPY failed: stat /var/lib/aloof/builds/x: not found", "loading /workspace/paas/paas/engine.py line 3"])
def test_r5d_internal_path_always_hidden(line):
    out = PE.clean_text(line); assert "/tmp/aloof-build" not in out and "/var/lib/aloof" not in out and "/workspace/paas" not in out

@pytest.mark.parametrize("line", ["COPY failed: file not found in build context or excluded by .dockerignore: stat package.json: file does not exist",
                                  "ERROR: failed to solve: failed to read dockerfile: open Dockerfile: no such file or directory",
                                  "npm ERR! code ELIFECYCLE", "Step 3/7 : WORKDIR /workspace/app"])
def test_r5d_customer_build_errors_kept(line): assert PE.clean_text(line) == line

@pytest.mark.parametrize("img,bad", [("nginx:alpine@sha256:" + "c" * 64, False), ("ghcr.io/a/b:1.2@sha256:" + "d" * 64, False),
                                      ("user:sha256@registry.io/x", True), ("user:tok@registry.io/x:1@sha256:" + "e" * 64, True)])
def test_r5e_tag_plus_digest(img, bad):
    spec = {"name": "qa5-img", "source": {"image": img}}
    if bad:
        with pytest.raises(ValueError, match="credentials"): engine.validate(spec)
    else:
        try: engine.validate(spec)
        except ValueError as e: assert "credentials" not in str(e)
        except Exception: pass

def test_r5f_no_processor_in_diagnose():
    from paas import diagnose; assert "Stripe" not in open(diagnose.__file__).read().split("past_due", 1)[1][:2000]
