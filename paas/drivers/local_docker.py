import docker, subprocess, shutil, os, re, time
from .base import Driver, Instance
from ..plans import PLANS
def _docker_ts(v):
    """Docker's RFC3339 with nanoseconds ('2026-10-27T03:43:35.123456789Z' / '+00:00') -> epoch, or None."""
    import re as _re, datetime as _dt
    m = _re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$", v or "")
    if not m: return None
    return _dt.datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(2) in (None, "Z") else m.group(2))).timestamp()
class LocalDockerDriver(Driver):
    name = "local-docker"; regions = ["local"]
    def __init__(self): self._c = None; self.err = None; self._connect()
    def _connect(self):
        try:
            c = docker.from_env(); c.ping(); self._c, self.err = c, None
            self.supports_gpu = "nvidia" in (c.info().get("Runtimes") or {})
        except Exception as e: self._c, self.err = None, str(e)
    @property
    def c(self):   # reconnect lazily if dockerd was down/restarted
        if self._c is None: self._connect()
        if self._c is None: raise RuntimeError(f"docker unavailable: {self.err}")
        return self._c
    def available(self):
        try: self.c.ping(); return True, "ok"
        except Exception as e: self._c = None; return False, str(e)
    def build(self, app, source, log):
        """image: pull with the owner's registry credentials. git/upload/path: fetch into a private temp dir, then docker build
        (base images from private registries use the same credentials); no Dockerfile -> Nixpacks / buildpacks / generated."""
        from .. import creds, launch
        import tempfile
        if source.get("image"):
            img = source["image"]
            if img.startswith("paas/"):   # built here (owner checked in engine._check_platform_image); never on a registry
                try: self.c.images.get(img); return img
                except docker.errors.ImageNotFound: raise RuntimeError(f"No such image {img} (built image was pruned; redeploy from source)")
            creds.pull(app, img, log); return img
        tag = f"paas/{app}:r{int(time.time()*1000)}"   # unique per build so releases can roll back
        work = tempfile.mkdtemp(prefix="aloof-build-")
        try:
            if source.get("git"): creds.clone(app, source, work, log)
            elif source.get("upload"): creds.extract_upload(app, source["upload"], work); log("extracted uploaded build context")
            elif source.get("path"):
                if os.environ.get("PAAS_ALLOW_PATH_SOURCE") != "1": raise RuntimeError("source.path is disabled on this server; use `aloof deploy` (upload) or git")
                shutil.rmtree(work); shutil.copytree(source["path"], work, symlinks=True)
            ctx = os.path.realpath(os.path.join(work, source.get("subdir") or "."))
            if not ctx.startswith(os.path.realpath(work)) or not os.path.isdir(ctx): raise RuntimeError("subdir not found in source")
            with creds.docker_config(app) as (env, _):
                df = source.get("dockerfile")
                if df:
                    dfp = os.path.realpath(os.path.join(ctx, df))
                    if not dfp.startswith(os.path.realpath(work)) or not os.path.isfile(dfp): raise RuntimeError(f"dockerfile {df} not found")
                    args = ["docker", "build", "-t", tag, "-f", dfp, ctx]
                else:
                    if launch.prepare_context(ctx, log, tag, env): return tag
                    args = ["docker", "build", "-t", tag, ctx]
                for k, v in (source.get("build_args") or {}).items(): args[2:2] = ["--build-arg", f"{k}={v}"]
                log("docker build")
                p = subprocess.run(args, capture_output=True, text=True, env=env, timeout=3600)
            for line in (p.stdout+p.stderr).splitlines()[-40:]: log(line)
            if p.returncode: raise RuntimeError("build failed")
            return tag
        finally: subprocess.run(["rm", "-rf", work]) if os.path.exists(work) else None
    def run_once(self, app, image, spec, command, timeout=900):
        """One-off machine (release_command, cron): same image/env/networks/volumes as the app, removed afterwards."""
        plan = PLANS[spec["plan"]]; nets = list(spec.get("networks") or [])
        for n in nets: self._ensure_network(n)
        kw = dict(name=f"paas-{app}-run-{int(time.time()*1000)%10**8}", environment={**spec.get("env", {}), "PORT": str(spec.get("port", 8080))},
                  labels={"paas.app": app, "paas.oneoff": "1"}, nano_cpus=int(plan["vcpu"]*1e9), mem_limit=f"{plan['memory_mb']}m",
                  command=command if isinstance(command, list) else ["sh", "-c", command])
        if nets: kw["network"] = nets[0]
        if spec.get("volumes"): kw["volumes"] = {self.volume_ref(v["name"]): {"bind": v["path"], "mode": "rw"} for v in spec["volumes"]}
        ct = self.c.containers.create(image, **kw)
        try:
            for n in nets[1:]: self.c.networks.get(n).connect(ct)
            ct.start()
            try: code = ct.wait(timeout=timeout)["StatusCode"]
            except Exception: ct.kill(); code = 124
            out = ct.logs().decode(errors="replace")
            return code, out
        finally:
            try: ct.remove(force=True)
            except Exception: pass
    def start_instance(self, app, image, spec, index):
        plan = PLANS[spec["plan"]]; port = int(spec.get("port", 8080))
        from .. import capacity; capacity.admit(spec["plan"], 1)   # memory admission: refuse instead of overcommitting the host
        kw = dict(detach=True, name=f"paas-{app}-{index}", environment={**spec.get("env",{}), "PORT":str(port)},
                  ports={f"{port}/tcp": ("127.0.0.1", None)}, labels={"paas.app":app},
                  nano_cpus=int(plan["vcpu"]*1e9), mem_limit=f"{plan['memory_mb']}m", restart_policy={"Name":"unless-stopped"})
        if spec.get("command"): kw["command"] = spec["command"]
        nets = list(spec.get("networks") or [])
        for n in nets: self._ensure_network(n)
        if nets: kw["network"] = nets[0]
        for v in spec.get("volumes") or []: self._ensure_volume(v["name"])
        if spec.get("volumes"): kw["volumes"] = {self.volume_ref(v["name"]): {"bind": v["path"], "mode": "rw"} for v in spec["volumes"]}
        if plan["kind"] == "gpu":
            if not self.supports_gpu: raise RuntimeError("no NVIDIA runtime on this host; GPU plans need a GPU provider driver")
            kw["device_requests"] = [docker.types.DeviceRequest(count=1, capabilities=[["gpu"]])]
        try: self.c.containers.get(kw["name"]).remove(force=True)
        except docker.errors.NotFound: pass
        ct = self.c.containers.run(image, **kw)
        for n in nets: self._join(ct, n, (spec.get("net_aliases") or {}).get(n) or spec.get("net_alias"), first=(n == nets[0]))
        try:
            ct.reload(); hp = int(ct.attrs["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostPort"])
        except Exception:   # exited immediately (no port mapping): don't leave it restarting forever
            tail = ""
            try: tail = ct.logs(tail=5).decode(errors="replace").strip().replace("\n", " | ")[-300:]
            except Exception: pass
            try: ct.remove(force=True)
            except Exception: pass
            raise RuntimeError(f"container exited at start ({ct.status})" + (f": {tail}" if tail else ""))
        return Instance(id=ct.id[:12], host="127.0.0.1", port=hp)
    # ---- private networks (managed Postgres <-> attached apps) ----
    def _ensure_network(self, n):
        try: self.c.networks.get(n)
        except docker.errors.NotFound: self.c.networks.create(n, driver="bridge", internal=False, labels={"paas.net": n})
    def _join(self, ct, n, alias, first):
        net = self.c.networks.get(n)
        if first:   # docker run attached it without alias; re-attach with alias so peers resolve the stable name
            if not alias: return
            try: net.disconnect(ct)
            except Exception: pass
        try: net.connect(ct, aliases=(alias if isinstance(alias, list) else [alias]) if alias else None)
        except docker.errors.APIError as e:
            if "already exists" not in str(e): raise
    def remove_network(self, n):
        try: self.c.networks.get(n).remove()
        except Exception: pass
    def remove_network_if_empty(self, n):
        """Remove one of our networks only when no container (running or stopped) is attached. Returns True if removed."""
        try:
            net = self.c.networks.get(n); net.reload()
            if not (net.attrs.get("Labels") or {}).get("paas.net") or net.attrs.get("Containers"): return False
            if any(n in ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}) for c in self.c.containers.list(all=True)): return False
            net.remove(); return True
        except docker.errors.NotFound: return False
        except docker.errors.APIError: return False   # in use (docker refuses) or racing a new container
    def aloof_networks(self):
        """[(name, created_epoch)] for networks we created (label paas.net, name aloof-*)."""
        import datetime
        out = []
        for net in self.c.networks.list(filters={"label": "paas.net"}):
            n = net.name
            if not n.startswith("aloof-"): continue
            ts = _docker_ts(net.attrs.get("Created"))
            out.append((n, ts))
        return out
    def remove_image(self, ref):
        if ref.startswith("paas/"): self.c.images.remove(ref, noprune=False)
    # ---- exec / console ----
    def exec_run(self, iid, cmd, timeout=60, stdout_only=False):
        ct = self.c.containers.get(iid)
        if stdout_only:   # binary output (pg_dump): never mix stderr into it
            r = ct.exec_run(cmd, demux=True); out, err = r.output or (None, None)
            return r.exit_code, (out or b"") if r.exit_code == 0 else (err or out or b"")
        r = ct.exec_run(cmd, demux=False, stdout=True, stderr=True)
        return r.exit_code, (r.output or b"")
    def exec_stdin(self, iid, cmd, data: bytes, timeout=120):
        """Run cmd with `data` on stdin (never in argv, so it isn't visible to `ps` in the container). Returns (rc, stdout+stderr)."""
        import socket as _s, struct
        api = self.c.api; e = api.exec_create(iid, cmd, stdin=True, stdout=True, stderr=True, tty=False)
        sock = api.exec_start(e["Id"], socket=True); raw = getattr(sock, "_sock", sock); raw.settimeout(timeout)
        raw.sendall(data)
        try: raw.shutdown(_s.SHUT_WR)
        except OSError: pass
        buf = b""
        while True:
            chunk = raw.recv(65536)
            if not chunk: break
            buf += chunk
        raw.close(); out = b""
        while len(buf) >= 8:   # demux docker's stream framing: [stream, 0, 0, 0, size(4)] payload
            n = struct.unpack(">I", buf[4:8])[0]; out += buf[8:8 + n]; buf = buf[8 + n:]
        for _ in range(50):
            r = api.exec_inspect(e["Id"])
            if not r.get("Running"): return r.get("ExitCode"), out
            time.sleep(0.1)
        return None, out
    def exec_socket(self, iid, cmd=None):
        """Interactive TTY exec; returns (exec_id, raw socket)."""
        api = self.c.api; cmd = cmd or ["/bin/sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"]
        e = api.exec_create(iid, cmd, stdin=True, tty=True, environment={"TERM": "xterm-256color"})
        sock = api.exec_start(e["Id"], tty=True, socket=True); raw = getattr(sock, "_sock", sock)
        return e["Id"], raw
    def exec_resize(self, exec_id, rows, cols):
        try: self.c.api.exec_resize(exec_id, height=rows, width=cols)
        except Exception: pass
    # ---- volume snapshots (tar.gz via a helper container, so root-owned files are captured) ----
    HELPER = os.environ.get("PAAS_HELPER_IMAGE", "alpine:3.20")
    def _helper(self, vol, mount_dir, cmd):
        try: self.c.images.get(self.HELPER)
        except docker.errors.ImageNotFound: self.c.images.pull(self.HELPER)
        self._ensure_volume(vol); os.makedirs(mount_dir, exist_ok=True)
        out = self.c.containers.run(self.HELPER, ["sh", "-c", cmd], remove=True, network_mode="none",
                                    volumes={self.volume_ref(vol): {"bind": "/vol", "mode": "rw"}, os.path.realpath(mount_dir): {"bind": "/arc", "mode": "rw"}})
        return out
    def snapshot_volume(self, vol, path):
        d, f = os.path.split(path); self._helper(vol, d, f"tar czf /arc/{f} -C /vol . && chown {os.getuid()}:{os.getgid()} /arc/{f} && chmod 600 /arc/{f}")
        return os.path.getsize(path)
    def restore_volume(self, vol, path):
        d, f = os.path.split(path); self._helper(vol, d, f"find /vol -mindepth 1 -delete && tar xzf /arc/{f} -C /vol")
    def volume_ref(self, name): return f"paas-vol-{name}"
    VOL_DIR = os.environ.get("PAAS_VOLUME_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data", "volumes"))
    def _ensure_volume(self, name, size_gb=None):
        """Named docker volume backed by a host directory under data/volumes/<name> (durable across daemon/box resets).
        Size is a billing/quota attribute; enforce with XFS project quotas or provider block volumes in prod."""
        import docker as _d
        try: self.c.volumes.get(self.volume_ref(name)); return
        except _d.errors.NotFound: pass
        path = os.path.realpath(os.path.join(self.VOL_DIR, name)); os.makedirs(path, exist_ok=True); os.chmod(path, 0o777)
        self.c.volumes.create(self.volume_ref(name), driver="local", driver_opts={"type": "none", "o": "bind", "device": path},
                              labels={"paas.volume": name, "paas.size_gb": str(size_gb or "")})
    def create_volume(self, name, size_gb): self._ensure_volume(name, size_gb)
    def delete_volume(self, name):
        try: self.c.volumes.get(self.volume_ref(name)).remove(force=True)
        except docker.errors.NotFound: pass
        path = os.path.realpath(os.path.join(self.VOL_DIR, name))
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name) and path.startswith(os.path.realpath(self.VOL_DIR) + os.sep) and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)   # files written by root in containers need root:
            if os.path.exists(path): subprocess.run(["sudo", "-n", "rm", "-rf", "--", path], capture_output=True)
    def cpu_percent(self, iid):
        try: st = self.c.containers.get(iid).stats(stream=False)
        except Exception: return 0.0
        cd = st["cpu_stats"]["cpu_usage"]["total_usage"] - st["precpu_stats"]["cpu_usage"]["total_usage"]
        sd = st["cpu_stats"].get("system_cpu_usage",0) - st["precpu_stats"].get("system_cpu_usage",0)
        lim = (st["cpu_stats"].get("online_cpus") or 1)
        quota = self.c.containers.get(iid).attrs["HostConfig"].get("NanoCpus") or 1e9
        return (cd / sd * lim * 100.0) / (quota/1e9) if sd > 0 else 0.0   # % of the plan's vCPU allocation
    def remove_app_containers(self, app):
        n = 0
        for c in self.c.containers.list(all=True, filters={"label": f"paas.app={app}"}):
            if (c.labels or {}).get("paas.app") != app: continue
            try: c.remove(force=True); n += 1
            except docker.errors.NotFound: pass
            except docker.errors.APIError as e:
                if "already in progress" not in str(e): raise
        return n
    def app_labels(self):
        return [(c.labels or {}).get("paas.app") for c in self.c.containers.list(all=True, filters={"label": "paas.app"}) if (c.labels or {}).get("paas.app")]
    def stop_instance(self, iid):
        try: self.c.containers.get(iid).remove(force=True)
        except docker.errors.NotFound: pass
        except docker.errors.APIError as e:
            if "already in progress" not in str(e): raise   # concurrent stop of the same container
    def logs(self, iid, tail=200):
        try: return self.c.containers.get(iid).logs(tail=tail, timestamps=True).decode(errors="replace")
        except docker.errors.NotFound: return "(instance gone)\n"
    def endpoint(self, iid):
        try:
            ports = self.c.containers.get(iid).attrs["NetworkSettings"]["Ports"] or {}
            for v in ports.values():
                if v: return v[0]["HostIp"] or "127.0.0.1", int(v[0]["HostPort"])
        except Exception: return None
    def exit_info(self, iid):
        """Read-only: exit code / OOM flag of a container that has stopped (None while running or gone)."""
        try: st = self.c.containers.get(iid).attrs.get("State") or {}
        except Exception: return None
        if st.get("Running"): return {"restart_count": None}
        return {"exit_code": st.get("ExitCode"), "oom_killed": bool(st.get("OOMKilled")), "finished_at": st.get("FinishedAt")}
    def inspect(self, iid):
        try: return self.c.containers.get(iid).status
        except docker.errors.NotFound: return "gone"
