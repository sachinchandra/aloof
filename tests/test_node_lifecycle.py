"""Full node lifecycle against a mocked E2E Networks API with a REAL agent + REAL docker:
deploy -> provision (POST /nodes with cloud-init) -> agent joins with the token from that script -> container scheduled
-> served -> app destroyed -> idle GC tears the node down (DELETE /nodes/<id>). Needs docker access (run via `sg docker`)."""
import os, re, json, time, socket, threading, subprocess, sys, shutil, tempfile, pytest, httpx, respx, uvicorn
from paas import api, engine, db
from paas.drivers import remote
pytestmark = pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode, reason="needs docker")
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def test_e2e_mocked_full_lifecycle(monkeypatch, tmp_path):
    for k, v in dict(E2E_API_KEY="ak", E2E_AUTH_TOKEN="tok", E2E_PROJECT_ID="1", PAAS_ALLOW_DIRECT_AGENT="1", PAAS_CPU_NODE_IDLE_TTL="0",
                     PAAS_CPU_POOLS="e2e").items(): monkeypatch.setenv(k, v)
    port = free_port(); monkeypatch.setenv("PAAS_NODE_API_URL", f"http://127.0.0.1:{port}")
    srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")); threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started: time.sleep(0.1)
    base = "https://api.e2enetworks.com/myaccount/api/v1"; agent_proc = {}
    def on_create(request):
        body = json.loads(request.content); script = body["start_scripts"][0]
        assert script.startswith("#!/bin/bash") and "/v1/nodes/agent.py" in script
        join = re.search(r"JOIN_TOKEN='?(jt_[\w\-]+)", script).group(1)
        # what cloud-init would do on the VM: fetch agent from the control plane, register, serve
        agent_py = tmp_path / "agent.py"; agent_py.write_text(httpx.get(f"http://127.0.0.1:{port}/v1/nodes/agent.py").text)
        aport = free_port(); env = dict(os.environ, ALOOF_AGENT_CONF=str(tmp_path / "agent.json"))
        subprocess.run([sys.executable, str(agent_py), "register", "--api", f"http://127.0.0.1:{port}", "--join-token", join, "--no-wg", "--port", str(aport)], env=env, check=True, capture_output=True)
        agent_proc["p"] = subprocess.Popen([sys.executable, str(agent_py), "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        agent_proc["join"] = join
        return httpx.Response(200, json={"code": 200, "data": {"id": 4242}})
    with respx.mock(assert_all_called=False) as m:
        m.route(host="127.0.0.1").pass_through()
        create = m.post(f"{base}/nodes/").mock(side_effect=on_create)
        m.get(f"{base}/nodes/4242/").mock(return_value=httpx.Response(200, json={"data": {"status": "Running", "public_ip_address": "127.0.0.1"}}))
        delete = m.delete(f"{base}/nodes/4242/").mock(return_value=httpx.Response(200, json={}))
        a = engine.create_account("node@example.in"); db.q("update accounts set tier='hobby' where id=?", (a["id"],))
        try:
            # nginx:alpine exists locally; registry push is bypassed by pointing build at a pass-through
            monkeypatch.setattr(remote.RemoteDriver, "build", lambda self, app, source, log_: source["image"])
            engine.deploy(a["id"], {"name": "nodeapp", "source": {"image": "nginx:alpine"}, "plan": "cpu-small", "port": 80, "provider": "e2e", "instances": 1, "env": {"K": "v"}})
            for _ in range(240):
                ap = engine.get_app("nodeapp")
                if ap["status"] in ("running", "failed"): break
                time.sleep(0.5)
            assert ap["status"] == "running", ap.get("error")
            assert create.call_count == 1
            n = db.q("select * from nodes where provider='e2e'", one=True); assert n["status"] == "ready" and n["provider_ref"] == "4242"
            # join token is single-use
            assert httpx.post(f"http://127.0.0.1:{port}/v1/nodes/register", json={"join_token": agent_proc["join"]}).status_code in (401, 403)
            inst = db.q("select * from instances where app='nodeapp' and status='running'", one=True)
            assert inst["node_id"] == n["id"]
            assert "nginx" in httpx.get(f"http://{inst['host']}:{inst['port']}/").text.lower()
            # second app bin-packs onto the same node (no second VM)
            engine.deploy(a["id"], {"name": "nodeapp2", "source": {"image": "nginx:alpine"}, "plan": "cpu-small", "port": 80, "provider": "e2e"})
            for _ in range(120):
                if engine.get_app("nodeapp2")["status"] in ("running", "failed"): break
                time.sleep(0.5)
            assert engine.get_app("nodeapp2")["status"] == "running" and create.call_count == 1
            for name in ("nodeapp", "nodeapp2"): engine.destroy(name)
            remote.node_tick(); remote.node_tick()     # idle TTL 0 -> GC
            assert delete.call_count == 1 and db.q("select status from nodes where id=?", (n["id"],), one=True)["status"] == "destroyed"
        finally:
            if "p" in agent_proc: agent_proc["p"].terminate()
            subprocess.run("docker ps -aq --filter label=aloof.app=nodeapp --filter label=aloof.app=nodeapp2 | xargs -r docker rm -f", shell=True, capture_output=True)
            srv.should_exit = True
