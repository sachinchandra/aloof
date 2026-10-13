"""Private registries and private git, end to end against local servers (needs docker; run as a docker-capable user, else skipped):
- registry:2 with htpasswd auth: pull denied without creds, works with per-app creds, and the edge/self-hosted path mirrors it
  into the platform registry that nodes pull from;
- git over HTTPS (git-http-backend behind basic auth) with an app token; git over SSH (sshd container) with a generated deploy key;
- an uploaded build context without a Dockerfile builds via the generated Dockerfile."""
import os, io, json, time, base64, socket, shutil, tarfile, tempfile, threading, subprocess, pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
os.environ["PAAS_ALLOW_PRIVATE_GIT"] = "1"; os.environ["PAAS_NO_NIXPACKS"] = "1"; os.environ["PAAS_NO_BUILDPACKS"] = "1"
def _docker():
    try: return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception: return False
pytestmark = pytest.mark.skipif(not _docker(), reason="docker not accessible")
REG = "127.0.0.1:5055"
def sh(*a, **kw): return subprocess.run(list(a), capture_output=True, text=True, **kw)
@pytest.fixture(scope="module")
def registry():
    d = tempfile.mkdtemp(); sh("docker", "rm", "-f", "aloof-test-reg")
    p = sh("docker", "run", "--rm", "alpine:3.20", "sh", "-c", "apk add -q apache2-utils >/dev/null && htpasswd -Bbn ci s3cret-pw")
    assert p.returncode == 0, p.stderr
    open(os.path.join(d, "htpasswd"), "w").write(p.stdout.strip() + "\n")
    p = sh("docker", "run", "-d", "--name", "aloof-test-reg", "-p", f"{REG}:5000", "-v", f"{d}:/auth", "-e", "REGISTRY_AUTH=htpasswd",
           "-e", "REGISTRY_AUTH_HTPASSWD_REALM=test", "-e", "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd", "registry:2")
    assert p.returncode == 0, p.stderr
    for _ in range(50):
        try: socket.create_connection(("127.0.0.1", 5055), 1).close(); break
        except OSError: time.sleep(0.2)
    time.sleep(1)
    cfg = tempfile.mkdtemp(); env = {**os.environ, "DOCKER_CONFIG": cfg}
    open(os.path.join(cfg, "config.json"), "w").write(json.dumps({"auths": {REG: {"auth": base64.b64encode(b"ci:s3cret-pw").decode()}}}))
    img = f"{REG}/team/private-hello:1"
    sh("docker", "tag", "busybox:latest", img)
    p = sh("docker", "push", img, env=env); assert p.returncode == 0, p.stderr
    sh("docker", "rmi", img)
    yield img
    sh("docker", "rm", "-f", "aloof-test-reg"); shutil.rmtree(cfg, ignore_errors=True)
def _acct_app(name):
    from paas import engine, crypto
    from paas.db import q
    a = engine.create_account(f"{name}-{time.time_ns()}@example.com")
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?) on conflict(name) do update set account_id=excluded.account_id",
      (name, a["id"], json.dumps({"name": name, "env_enc": crypto.enc_json({})}), "running", "local-docker", time.time(), time.time()))
    return a
def test_private_registry_pull_and_edge_mirror(registry):
    from paas import creds
    from paas.db import q
    from paas.drivers.local_docker import LocalDockerDriver
    from paas.drivers.remote import RemoteDriver
    logs = []; a = _acct_app("privreg")
    with pytest.raises(RuntimeError, match="registry login"): creds.pull("privreg", registry, logs.append)
    creds.registry_set(a["id"], REG, "ci", "s3cret-pw", app="privreg")
    raw = q("select secret_enc from registry_creds where app='privreg'", one=True)["secret_enc"]; assert "s3cret-pw" not in raw
    assert LocalDockerDriver().build("privreg", {"image": registry}, logs.append) == registry
    assert any("with registry credentials" in l for l in logs)
    _acct_app("privreg-other")   # another account must not reuse the cached private image
    with pytest.raises(RuntimeError): creds.pull("privreg-other", registry, logs.append)
    # edge/self-hosted pool: control plane pulls with the creds, then mirrors into the platform registry nodes pull from
    if socket.socket().connect_ex(("127.0.0.1", 5000)) == 0:
        os.environ["PAAS_REGISTRY"] = "127.0.0.1:5000"; os.environ.pop("PAAS_REGISTRY_NODE", None)
        tag = RemoteDriver.__new__(RemoteDriver).build("privreg", {"image": registry}, logs.append)
        assert tag.startswith("127.0.0.1:5000/privreg:")
        sh("docker", "rmi", tag); assert sh("docker", "pull", tag).returncode == 0; sh("docker", "rmi", tag)
def _repo(path, dockerfile=True):
    os.makedirs(path, exist_ok=True)
    if dockerfile: open(os.path.join(path, "Dockerfile"), "w").write("FROM busybox:latest\nCOPY hello.txt /hello.txt\nCMD [\"cat\", \"/hello.txt\"]\n")
    open(os.path.join(path, "hello.txt"), "w").write("private repo ok\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    for c in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "init"]): assert sh("git", "-C", path, *c, env=env).returncode == 0
    return sh("git", "-C", path, "rev-parse", "HEAD").stdout.strip()
@pytest.fixture(scope="module")
def https_git():
    root = tempfile.mkdtemp(); work = os.path.join(root, "work"); sha = _repo(work)
    assert sh("git", "clone", "-q", "--bare", work, os.path.join(root, "app.git")).returncode == 0
    want = "Basic " + base64.b64encode(b"x-access-token:ghp_testtoken123").decode()
    class H(BaseHTTPRequestHandler):
        def _go(self):
            if self.headers.get("Authorization") != want:
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="git"'); self.send_header("Content-Length", "0"); self.end_headers(); return
            path, _, qs = self.path.partition("?"); n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n) if n else b""
            env = {"GIT_PROJECT_ROOT": root, "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path, "REQUEST_METHOD": self.command, "QUERY_STRING": qs,
                   "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(n), "REMOTE_USER": "x", "GIT_PROTOCOL": self.headers.get("Git-Protocol", "")}
            out = subprocess.run(["/usr/lib/git-core/git-http-backend"], input=body, capture_output=True, env=env).stdout
            head, _, data = out.partition(b"\r\n\r\n")
            self.send_response(200)
            for line in head.split(b"\r\n"):
                k, _, v = line.decode().partition(":")
                if k and k.lower() != "status": self.send_header(k.strip(), v.strip())
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        do_GET = do_POST = _go
        def log_message(self, *a): pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/app.git", sha
    srv.shutdown(); shutil.rmtree(root, ignore_errors=True)
def test_private_git_https_token(https_git):
    from paas import creds
    from paas.drivers.local_docker import LocalDockerDriver
    url, sha = https_git; a = _acct_app("privgit"); logs = []
    with pytest.raises(RuntimeError, match="private repo"): creds.clone("privgit", {"git": url}, tempfile.mkdtemp(), logs.append)
    creds.git_set(a["id"], "127.0.0.1", "token", app="privgit", token="ghp_testtoken123", username="x-access-token")
    d = tempfile.mkdtemp(); assert creds.clone("privgit", {"git": url, "ref": "main"}, d, logs.append) == sha
    assert open(os.path.join(d, "hello.txt")).read() == "private repo ok\n" and not os.path.exists(os.path.join(d, ".git"))
    assert not any("ghp_testtoken123" in l for l in logs)
    tag = LocalDockerDriver().build("privgit", {"git": url, "ref": sha}, logs.append)
    assert sh("docker", "run", "--rm", tag).stdout == "private repo ok\n"; sh("docker", "rmi", tag)
@pytest.fixture(scope="module")
def ssh_git():
    from paas import creds
    a = _acct_app("privssh"); r = creds.git_set(a["id"], "127.0.0.1", "ssh", app="privssh")
    root = tempfile.mkdtemp(); os.chmod(root, 0o755); work = os.path.join(root, "work"); sha = _repo(work)
    assert sh("git", "clone", "-q", "--bare", work, os.path.join(root, "app.git")).returncode == 0
    open(os.path.join(root, "authorized_keys"), "w").write(r["public_key"] + "\n")
    sh("docker", "rm", "-f", "aloof-test-sshgit")
    script = ("apk add -q openssh-server git >/dev/null && ssh-keygen -A >/dev/null && adduser -D -s /usr/bin/git-shell git && sed -i 's/^git:!/git:*/' /etc/shadow && "
              "mkdir -p /home/git/.ssh && cp /src/authorized_keys /home/git/.ssh/ && cp -r /src/app.git /srv/app.git && chown -R git /home/git /srv/app.git && "
              "chmod 700 /home/git/.ssh && chmod 600 /home/git/.ssh/authorized_keys && exec /usr/sbin/sshd -D -e -p 22")
    p = sh("docker", "run", "-d", "--name", "aloof-test-sshgit", "-p", "127.0.0.1:2223:22", "-v", f"{root}:/src:ro", "alpine:3.20", "sh", "-c", script)
    assert p.returncode == 0, p.stderr
    for _ in range(300):
        try:
            s = socket.create_connection(("127.0.0.1", 2223), 1); b = s.recv(10); s.close()
            if b.startswith(b"SSH"): break
        except OSError: pass
        time.sleep(0.2)
    yield "ssh://git@127.0.0.1:2223/srv/app.git", sha
    sh("docker", "rm", "-f", "aloof-test-sshgit"); shutil.rmtree(root, ignore_errors=True)
def test_private_git_ssh_deploy_key(ssh_git):
    from paas import creds
    url, sha = ssh_git; d = tempfile.mkdtemp(); logs = []
    assert creds.clone("privssh", {"git": url}, d, logs.append) == sha
    assert "deploy key" in logs[-1] and open(os.path.join(d, "hello.txt")).read() == "private repo ok\n"
    _acct_app("privssh-x")   # another account has no key -> denied
    with pytest.raises(RuntimeError): creds.clone("privssh-x", {"git": url}, tempfile.mkdtemp(), logs.append)
def test_upload_without_dockerfile_builds_generated(tmp_path):
    from paas import creds
    from paas.drivers.local_docker import LocalDockerDriver
    a = _acct_app("upstatic"); src = tmp_path / "site"; src.mkdir(); (src / "index.html").write_text("<h1>static ok</h1>")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t: t.add(src / "index.html", arcname="index.html")
    up = creds.save_upload(a["id"], "upstatic", iter([buf.getvalue()])); logs = []
    tag = LocalDockerDriver().build("upstatic", {"upload": up["upload_id"]}, logs.append)
    assert any("generated one for static" in l for l in logs)
    p = sh("docker", "run", "--rm", "--entrypoint", "cat", tag, "/usr/share/nginx/html/index.html"); assert "static ok" in p.stdout
    sh("docker", "rmi", tag)
def test_private_network_internal_dns_and_run_once():
    """Real docker: two apps of one account resolve each other as <app>.internal; another account's network is separate;
    run_once (release_command/cron) sees the same network."""
    from paas.drivers.local_docker import LocalDockerDriver
    d = LocalDockerDriver(); net = "aloof-acct-test1"; ids = []
    def spec(name, n=net): return {"plan": "shared-1x-256mb", "port": 8080, "env": {}, "networks": [n], "net_aliases": {n: [name, f"{name}.internal"]},
                                   "command": ["sh", "-c", "mkdir -p /w && echo hi-$HOSTNAME > /w/index.html && httpd -f -p 8080 -h /w"]}
    try:
        a = d.start_instance("netapp-a", "busybox:latest", spec("netapp-a"), "1"); ids.append(a.id)
        b = d.start_instance("netapp-b", "busybox:latest", spec("netapp-b"), "1"); ids.append(b.id)
        x = d.start_instance("netapp-x", "busybox:latest", spec("netapp-x", "aloof-acct-test2"), "1"); ids.append(x.id)
        time.sleep(1)
        code, out = d.exec_run(b.id, ["wget", "-qO-", "http://netapp-a.internal:8080/"]); assert code == 0 and out.decode().startswith("hi-"), out
        code, out = d.exec_run(x.id, ["wget", "-T", "2", "-qO-", "http://netapp-a.internal:8080/"]); assert code != 0   # other account: no route/name
        code, out = d.run_once("netapp-b", "busybox:latest", spec("netapp-b"), "wget -qO- http://netapp-a.internal:8080/ && echo release-ok")
        assert code == 0 and "release-ok" in out, out
        code, out = d.run_once("netapp-b", "busybox:latest", spec("netapp-b"), "exit 3"); assert code == 3
    finally:
        for i in ids: d.stop_instance(i)
        for n in (net, "aloof-acct-test2"): d.remove_network(n)
