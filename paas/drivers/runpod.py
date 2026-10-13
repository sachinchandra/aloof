"""Runpod pod driver (phase 2 GPUs: L4, RTX 4090; behind PAAS_GPU_ENABLED). Pods are containers, not VMs, so there is no node
agent or WireGuard here: each instance is one pod running the app image directly, reached at its public IP + mapped TCP port.
REST API https://rest.runpod.io/v1 (POST /pods, GET /pods/{id}, DELETE /pods/{id}); Bearer RUNPOD_API_KEY.
Images must be pullable by Runpod: a public image, or our build pushed to PAAS_PUBLIC_REGISTRY with RUNPOD_REGISTRY_AUTH_ID."""
import os, time, subprocess, logging, httpx
from .base import Driver, Instance
log = logging.getLogger("paas.runpod")
E = lambda k, d=None: os.environ.get(k, d)
GPU_TYPES = {"gpu-l4": ("NVIDIA L4", "SECURE"), "gpu-rtx4090": ("NVIDIA GeForce RTX 4090", "COMMUNITY")}
class RunpodError(RuntimeError): pass
class RunpodDriver(Driver):
    name = "runpod"; supports_gpu = True; regions = ["eu", "us", "ca"]
    docs = "https://docs.runpod.io/api-reference/pods/POST/pods"
    base = property(lambda s: E("RUNPOD_API_BASE", "https://rest.runpod.io/v1"))
    READY_TIMEOUT = int(E("PAAS_RUNPOD_READY_TIMEOUT", "600"))
    def _h(self): return {"Authorization": f"Bearer {E('RUNPOD_API_KEY')}", "Content-Type": "application/json"}
    def http(self): return httpx.Client(timeout=60)
    def _j(self, r, what):
        if r.status_code >= 400: raise RunpodError(f"runpod {what}: HTTP {r.status_code} {r.text[:300]}")
        try: return r.json()
        except Exception: return {}
    def available(self):
        from .. import plans
        if not plans.GPU_ENABLED: return False, "GPUs are coming soon (PAAS_GPU_ENABLED=0)"
        return (True, "ok") if E("RUNPOD_API_KEY") else (False, "missing credentials: RUNPOD_API_KEY")
    def build(self, app, source, log_):
        if source.get("image") and not E("PAAS_PUBLIC_REGISTRY"): log_(f"runpod pulls {source['image']} directly"); return source["image"]
        reg = E("PAAS_PUBLIC_REGISTRY")
        if not reg: raise RunpodError("runpod: git builds need PAAS_PUBLIC_REGISTRY (+ RUNPOD_REGISTRY_AUTH_ID) so pods can pull the image")
        from .local_docker import LocalDockerDriver
        img = LocalDockerDriver().build(app, source, log_); tag = f"{reg}/{app}:{int(time.time())}"
        for cmd in (["docker", "tag", img, tag], ["docker", "push", tag]):
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode: raise RunpodError(f"registry push failed: {p.stderr[-300:]}")
        log_(f"pushed {tag}"); return tag
    def pod_body(self, app, image, spec, index):
        from ..plans import PLANS
        gpu, cloud = GPU_TYPES.get(spec["plan"], (None, None))
        if not gpu: raise RunpodError(f"runpod: plan {spec['plan']} not offered on runpod")
        port = int(spec.get("port", 8080)); plan = PLANS[spec["plan"]]
        b = {"name": f"aloof-{app}-{index}", "imageName": image, "gpuTypeIds": [gpu], "gpuCount": 1, "cloudType": cloud,
             "ports": [f"{port}/tcp"], "env": {**spec.get("env", {}), "PORT": str(port)}, "supportPublicIp": True,
             "containerDiskInGb": int(E("PAAS_RUNPOD_DISK_GB", "50")), "volumeInGb": 0, "interruptible": False,
             "minVCPUPerGPU": min(plan["vcpu"], 8), "minRAMPerGPU": max(8, plan["memory_mb"] // 1024 // 2)}
        if spec.get("command"): b["dockerStartCmd"] = spec["command"]
        if E("RUNPOD_REGISTRY_AUTH_ID"): b["containerRegistryAuthId"] = E("RUNPOD_REGISTRY_AUTH_ID")
        if E("RUNPOD_DATA_CENTERS"): b["dataCenterIds"] = E("RUNPOD_DATA_CENTERS").split(",")
        return b
    def get(self, pod_id):
        with self.http() as c: r = c.get(f"{self.base}/pods/{pod_id}", headers=self._h())
        return None if r.status_code == 404 else self._j(r, "get pod")
    def _ep(self, pod, port):
        m = pod.get("portMappings") or {}
        return (pod["publicIp"], int(m[str(port)])) if pod.get("publicIp") and m.get(str(port)) else None
    def start_instance(self, app, image, spec, index):
        port = int(spec.get("port", 8080))
        with self.http() as c: pod = self._j(c.post(f"{self.base}/pods", headers=self._h(), json=self.pod_body(app, image, spec, index)), "create pod")
        pid = pod.get("id")
        if not pid: raise RunpodError(f"runpod: no pod id in {str(pod)[:200]}")
        end = time.time() + self.READY_TIMEOUT
        while time.time() < end:
            ep = self._ep(pod, port)
            if ep: return Instance(id=f"{pid}:{port}", host=ep[0], port=ep[1])
            if pod.get("desiredStatus") in ("EXITED", "TERMINATED"): break
            time.sleep(float(E("PAAS_RUNPOD_POLL_S", "5"))); pod = self.get(pid) or {}
        self.stop_instance(f"{pid}:{port}"); raise RunpodError(f"runpod pod {pid} got no public port within {self.READY_TIMEOUT}s")
    def stop_instance(self, iid):
        with self.http() as c: r = c.delete(f"{self.base}/pods/{iid.split(':')[0]}", headers=self._h())
        if r.status_code not in (200, 202, 204, 404): log.warning("runpod delete %s: %s", iid, r.status_code)
    def inspect(self, iid):
        try: pod = self.get(iid.split(":")[0])
        except Exception: return "unreachable"
        if not pod: return "gone"
        return {"RUNNING": "running", "EXITED": "exited", "TERMINATED": "gone"}.get(pod.get("desiredStatus"), "starting")
    def endpoint(self, iid):
        pid, port = iid.split(":")
        try: pod = self.get(pid); return pod and self._ep(pod, port)
        except Exception: return None
    def logs(self, iid, tail=200): return "(runpod: container logs are in the Runpod console; the REST API has no log endpoint)\n"
    def cpu_percent(self, iid): return 0.0
    def create_volume(self, name, size_gb): raise RunpodError("volumes are not supported on runpod GPU pods yet")
    def delete_volume(self, name): pass
