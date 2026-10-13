"""Private registries and private git, per account (default) or per app (override). Secrets are Fernet-encrypted at rest and
only ever materialised in 0700 temp dirs for the duration of one pull/clone/build (never on a command line, never logged).
Registries: Docker Hub, ghcr.io, GitLab, Quay, ECR (AWS keys -> short-lived token), GCR/Artifact Registry (_json_key), any v2 registry.
Git: SSH deploy keys (generated here, ed25519; add the public key to the repo), HTTPS tokens (GitHub/GitLab/Bitbucket/any),
and GitHub App installation tokens (see github_app.py)."""
import os, re, json, time, base64, secrets, shutil, tempfile, subprocess, contextlib, ipaddress, socket, urllib.parse
from .db import q
from . import crypto
DOCKER_HUB = "docker.io"
def registry_of(image):
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"): return first.lower()
    return DOCKER_HUB
def _norm_server(s):
    s = (s or DOCKER_HUB).strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if s in ("index.docker.io", "registry-1.docker.io", "index.docker.io/v1", "docker.io"): return DOCKER_HUB
    return s.split("/", 1)[0]
def _kind_for(server):
    if re.match(r"^\d+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com$", server): return "ecr"
    if server == "gcr.io" or server.endswith(".gcr.io") or server.endswith("-docker.pkg.dev"): return "gcr"
    return {"ghcr.io": "ghcr", DOCKER_HUB: "dockerhub"}.get(server, "basic")
def registry_set(account_id, server, username, password, app=None):
    server = _norm_server(server)
    if not username or not password: raise ValueError("username and password/token required")
    kind = _kind_for(server)
    if kind == "gcr" and username == "_json_key":
        try: json.loads(password)
        except Exception: raise ValueError("GCR/Artifact Registry with _json_key needs the service-account JSON key as the password")
    q("delete from registry_creds where account_id=? and server=? and coalesce(app,'')=?", (account_id, server, app or ""))
    rid = "rc_" + secrets.token_hex(6)
    q("insert into registry_creds(id,account_id,app,server,kind,username,secret_enc,created) values(?,?,?,?,?,?,?,?)",
      (rid, account_id, app, server, kind, username, crypto.enc(password), time.time()))
    return registry_view(q("select * from registry_creds where id=?", (rid,), one=True))
def registry_view(r): return {"id": r["id"], "server": r["server"], "kind": r["kind"], "username": r["username"], "app": r["app"], "created": r["created"]}
def registry_list(account_id, app=None):
    return [registry_view(r) for r in q("select * from registry_creds where account_id=? order by server", (account_id,)) if app is None or r["app"] in (None, app)]
def registry_delete(account_id, server, app=None):
    q("delete from registry_creds where account_id=? and server=? and coalesce(app,'')=?", (account_id, _norm_server(server), app or ""))
def _app_account(app):
    r = q("select account_id from apps where name=?", (app,), one=True); return r["account_id"] if r else None
def _ecr_token(r, server):
    import boto3
    region = server.split(".")[3]
    c = boto3.client("ecr", region_name=region, aws_access_key_id=r["username"], aws_secret_access_key=crypto.dec(r["secret_enc"]))
    d = c.get_authorization_token()["authorizationData"][0]
    return base64.b64decode(d["authorizationToken"]).decode().split(":", 1)
def registry_auths(app, account_id=None):
    """{server: (user, password)} for every registry this app may pull from (app override beats account default)."""
    account_id = account_id or _app_account(app)
    if not account_id: return {}
    out = {}
    for r in sorted(q("select * from registry_creds where account_id=?", (account_id,)), key=lambda r: r["app"] is not None):
        if r["app"] not in (None, app): continue
        try: out[r["server"]] = tuple(_ecr_token(r, r["server"])) if r["kind"] == "ecr" else (r["username"], crypto.dec(r["secret_enc"]))
        except Exception as e: out[r["server"]] = None; print(f"registry cred {r['server']}: {type(e).__name__}")
    return {k: v for k, v in out.items() if v}
@contextlib.contextmanager
def docker_config(app, account_id=None):
    """Env for `docker pull/build/push` whose config.json holds only this app's registry logins (and the platform registry needs none)."""
    d = tempfile.mkdtemp(prefix="aloof-dcfg-"); os.chmod(d, 0o700)
    try:
        auths = {}
        for server, (u, pw) in registry_auths(app, account_id).items():
            key = "https://index.docker.io/v1/" if server == DOCKER_HUB else server
            auths[key] = {"auth": base64.b64encode(f"{u}:{pw}".encode()).decode()}
        fd = os.open(os.path.join(d, "config.json"), os.O_WRONLY | os.O_CREAT, 0o600); os.write(fd, json.dumps({"auths": auths}).encode()); os.close(fd)
        yield {**os.environ, "DOCKER_CONFIG": d}, bool(auths)
    finally: shutil.rmtree(d, ignore_errors=True)
def pull(app, image, log):
    """Always pull with the app owner's credentials, so a private image cached on the host by one account is never usable by another.
    Falls back to the local copy only on transient errors when this same app pulled that image before."""
    with docker_config(app) as (env, has):
        p = subprocess.run(["docker", "pull", "-q", image], capture_output=True, text=True, env=env, timeout=1800)
    seen = f"{app}|{image}"
    from .db import kv_get, kv_put
    if p.returncode == 0: kv_put("pulled", seen, "1"); log(f"pulled {image}" + (" (with registry credentials)" if has else "")); return
    err = (p.stderr or p.stdout).strip()[-400:]
    denied = any(x in err.lower() for x in ("denied", "unauthorized", "authentication required", "not found", "manifest unknown", "no basic auth"))
    if not denied and kv_get("pulled", seen):
        log(f"pull failed ({err[:120]}); using cached copy previously pulled by this app"); return
    hint = "" if has else f"; private image? run: aloof registry login {registry_of(image)} -u <user> (or --app {app})"
    raise RuntimeError(f"pull {image} failed: {err}{hint}")
# ---------------- git ----------------
GIT_USERS = {"github.com": "x-access-token", "gitlab.com": "oauth2", "bitbucket.org": "x-token-auth"}
def git_host(url):
    m = re.match(r"^[\w.-]+@([\w.-]+):", url)
    if m: return m.group(1).lower()
    return (urllib.parse.urlsplit(url).hostname or "").lower()
def _check_url(url):
    if re.match(r"^[\w.-]+@[\w.-]+:[\w./~-]+$", url): mode, host = "ssh", git_host(url)
    else:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("https", "http", "ssh"): raise ValueError("git URL must be https://, ssh://, or git@host:owner/repo")
        if u.password or (u.username and u.scheme != "ssh"): raise ValueError("don't put credentials in the git URL; use `aloof git token` or `aloof git deploy-key`")
        mode, host = ("ssh" if u.scheme == "ssh" else "https"), u.hostname
    if os.environ.get("PAAS_ALLOW_PRIVATE_GIT") != "1":
        try:
            for fam, *_, sa in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(sa[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved: raise ValueError(f"git host {host} resolves to a private address")
        except socket.gaierror: raise ValueError(f"git host {host} does not resolve")
    return mode
def gen_deploy_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as S
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(S.Encoding.PEM, S.PrivateFormat.OpenSSH, S.NoEncryption()).decode()
    pub = k.public_key().public_bytes(S.Encoding.OpenSSH, S.PublicFormat.OpenSSH).decode()
    return priv, pub
def git_set(account_id, host, kind, app=None, token=None, username=None, private_key=None):
    host = host.strip().lower().removeprefix("https://").split("/")[0]
    if not re.match(r"^[a-z0-9.-]+(:\d+)?$", host): raise ValueError("host like github.com, gitlab.example.com")
    pub = None
    if kind == "token":
        if not token: raise ValueError("token required")
        secret = token; username = username or GIT_USERS.get(host, "git")
    elif kind == "ssh":
        if private_key:
            if "PRIVATE KEY" not in private_key: raise ValueError("private_key must be an OpenSSH/PEM private key")
            secret = private_key.strip() + "\n"
            try:
                from cryptography.hazmat.primitives import serialization as S
                pub = S.load_ssh_private_key(secret.encode(), None).public_key().public_bytes(S.Encoding.OpenSSH, S.PublicFormat.OpenSSH).decode()
            except Exception: raise ValueError("could not parse private key (passphrase-protected keys are not supported)")
        else: secret, pub = gen_deploy_key()
        pub = f"{pub} aloof-{app or 'account'}@{host}"; username = username or "git"
    else: raise ValueError("kind must be token or ssh")
    q("delete from git_creds where account_id=? and host=? and kind=? and coalesce(app,'')=?", (account_id, host, kind, app or ""))
    gid = "gc_" + secrets.token_hex(6)
    q("insert into git_creds(id,account_id,app,host,kind,username,secret_enc,public_key,created) values(?,?,?,?,?,?,?,?,?)",
      (gid, account_id, app, host, kind, username, crypto.enc(secret), pub, time.time()))
    return git_view(q("select * from git_creds where id=?", (gid,), one=True))
def git_view(r): return {"id": r["id"], "host": r["host"], "kind": r["kind"], "app": r["app"], "username": r["username"], "public_key": r["public_key"], "created": r["created"]}
def git_list(account_id): return [git_view(r) for r in q("select * from git_creds where account_id=? order by host", (account_id,))]
def git_delete(account_id, cid): q("delete from git_creds where account_id=? and id=?", (account_id, cid))
def _git_cred(account_id, app, host, kind):
    rows = [r for r in q("select * from git_creds where account_id=? and host=? and kind=?", (account_id, host, kind)) if r["app"] in (None, app)]
    rows.sort(key=lambda r: r["app"] is None); return rows[0] if rows else None
def _run(args, env, cwd=None, timeout=600):
    p = subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    if p.returncode: raise RuntimeError(re.sub(r"(Authorization: \w+ )\S+", r"\1***", (p.stderr or p.stdout).strip())[-500:])
    return p.stdout.strip()
def clone(app, source, dest, log, account_id=None):
    """Shallow-fetch source.git at source.ref (branch, tag or commit; default HEAD) into dest. Returns the commit sha."""
    url = source["git"]; ref = source.get("ref") or "HEAD"; account_id = account_id or _app_account(app)
    if not re.match(r"^[\w./@^~+-]+$", ref) or ref.startswith("-"): raise ValueError("bad git ref")
    mode = _check_url(url); host = git_host(url)
    tmp = tempfile.mkdtemp(prefix="aloof-git-"); os.chmod(tmp, 0o700)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    pre = ["git", "-c", "protocol.file.allow=never", "-c", "core.hooksPath=/dev/null"]; how = "public"
    try:
        if mode == "https":
            tok = None
            if source.get("github_installation"):
                from . import github_app; tok, user = github_app.installation_token(int(source["github_installation"])), "x-access-token"; how = "GitHub App"
            else:
                c = _git_cred(account_id, app, host, "token")
                if c: tok, user = crypto.dec(c["secret_enc"]), c["username"]; how = "HTTPS token"
            if tok:   # header via env-provided config so the token is never in argv or .git/config
                env |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + base64.b64encode(f"{user}:{tok}".encode()).decode()}
        else:
            c = _git_cred(account_id, app, host, "ssh")
            kf = os.path.join(tmp, "key"); kh = os.path.join(tmp, "known_hosts")
            if c:
                fd = os.open(kf, os.O_WRONLY | os.O_CREAT, 0o600); os.write(fd, crypto.dec(c["secret_enc"]).encode()); os.close(fd); how = "deploy key"
            env["GIT_SSH_COMMAND"] = f"ssh -F /dev/null -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={kh}" + (f" -i {kf}" if c else "")
        os.makedirs(dest, exist_ok=True)
        _run(pre + ["init", "-q", dest], env)
        _run(pre + ["-C", dest, "fetch", "-q", "--depth", "1", url, ref], env, timeout=900)
        _run(pre + ["-C", dest, "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"], env)
        sha = _run(pre + ["-C", dest, "rev-parse", "HEAD"], env)
        if os.path.exists(os.path.join(dest, ".gitmodules")):
            try: _run(pre + ["-C", dest, "submodule", "update", "--init", "--depth", "1", "--recursive"], env, timeout=900)
            except Exception as e: log(f"submodules: {e}")
        shutil.rmtree(os.path.join(dest, ".git"), ignore_errors=True)
        log(f"cloned {url}@{ref} -> {sha[:12]} ({how})"); return sha
    except RuntimeError as e:
        hint = "" if how != "public" else f" (private repo? add access: aloof git deploy-key {host} --app {app}  or  aloof git token {host} --app {app})"
        raise RuntimeError(f"git fetch failed: {e}{hint}")
    finally: shutil.rmtree(tmp, ignore_errors=True)
# ---------------- uploads (aloof deploy from a local dir) ----------------
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "uploads")
MAX_UPLOAD = int(os.environ.get("PAAS_MAX_UPLOAD_MB", "500")) * 1024 * 1024
def save_upload(account_id, app, stream):
    os.makedirs(UPLOAD_DIR, exist_ok=True); uid = "up_" + secrets.token_hex(8); path = os.path.join(UPLOAD_DIR, uid + ".tgz"); n = 0
    with open(path, "wb") as f:
        for chunk in stream:
            n += len(chunk)
            if n > MAX_UPLOAD: f.close(); os.remove(path); raise ValueError(f"build context over {MAX_UPLOAD >> 20} MB (check .dockerignore)")
            f.write(chunk)
    import tarfile
    try:
        with tarfile.open(path, "r:gz") as t: t.getmembers()
    except Exception: os.remove(path); raise ValueError("upload must be a .tar.gz of the build context")
    q("insert into uploads(id,account_id,app,path,size_bytes,created) values(?,?,?,?,?,?)", (uid, account_id, app, path, n, time.time()))
    for old in q("select * from uploads where app=? order by created desc", (app,))[5:]:   # keep the last 5 per app (rebuilds/rollbacks use images)
        try: os.remove(old["path"])
        except OSError: pass
        q("delete from uploads where id=?", (old["id"],))
    return {"upload_id": uid, "size_bytes": n}
def extract_upload(app, uid, dest):
    r = q("select * from uploads where id=?", (uid,), one=True)
    if not r or r["app"] != app: raise ValueError("unknown upload id for this app")
    import tarfile
    with tarfile.open(r["path"], "r:gz") as t: t.extractall(dest, filter="data")   # rejects absolute paths, .., links outside, devices
    return r
