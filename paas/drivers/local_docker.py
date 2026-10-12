import docker, subprocess, shutil, os, re
from .base import Driver, Instance
from ..plans import PLANS
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
        if source.get("image"):
            img = source["image"]; log(f"pulling {img}")
            try: self.c.images.get(img)
            except docker.errors.ImageNotFound: self.c.images.pull(img)
            return img
        tag = f"paas/{app}:latest"; ctx = source["git"]
        if source.get("ref"): ctx += f"#{source['ref']}"
        if source.get("subdir"): ctx += (":" if source.get("ref") else "#:") + source["subdir"]
        if source.get("path"): ctx = source["path"]   # local dir (dev)
        log(f"docker build {ctx}")
        p = subprocess.run(["docker","build","-t",tag,ctx], capture_output=True, text=True)
        for line in (p.stdout+p.stderr).splitlines()[-40:]: log(line)
        if p.returncode: raise RuntimeError("build failed")
        return tag
    def start_instance(self, app, image, spec, index):
        plan = PLANS[spec["plan"]]; port = int(spec.get("port", 8080))
        kw = dict(detach=True, name=f"paas-{app}-{index}", environment={**spec.get("env",{}), "PORT":str(port)},
                  ports={f"{port}/tcp": ("127.0.0.1", None)}, labels={"paas.app":app},
                  nano_cpus=int(plan["vcpu"]*1e9), mem_limit=f"{plan['memory_mb']}m", restart_policy={"Name":"unless-stopped"})
        if spec.get("command"): kw["command"] = spec["command"]
        for v in spec.get("volumes") or []: self._ensure_volume(v["name"])
        if spec.get("volumes"): kw["volumes"] = {self.volume_ref(v["name"]): {"bind": v["path"], "mode": "rw"} for v in spec["volumes"]}
        if plan["kind"] == "gpu":
            if not self.supports_gpu: raise RuntimeError("no NVIDIA runtime on this host; GPU plans need a GPU provider driver")
            kw["device_requests"] = [docker.types.DeviceRequest(count=1, capabilities=[["gpu"]])]
        try: self.c.containers.get(kw["name"]).remove(force=True)
        except docker.errors.NotFound: pass
        ct = self.c.containers.run(image, **kw); ct.reload()
        hp = int(ct.attrs["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostPort"])
        return Instance(id=ct.id[:12], host="127.0.0.1", port=hp)
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
    def stop_instance(self, iid):
        try: self.c.containers.get(iid).remove(force=True)
        except docker.errors.NotFound: pass
    def logs(self, iid, tail=200):
        try: return self.c.containers.get(iid).logs(tail=tail, timestamps=True).decode(errors="replace")
        except docker.errors.NotFound: return "(instance gone)\n"
    def endpoint(self, iid):
        try:
            ports = self.c.containers.get(iid).attrs["NetworkSettings"]["Ports"] or {}
            for v in ports.values():
                if v: return v[0]["HostIp"] or "127.0.0.1", int(v[0]["HostPort"])
        except Exception: return None
    def inspect(self, iid):
        try: return self.c.containers.get(iid).status
        except docker.errors.NotFound: return "gone"
