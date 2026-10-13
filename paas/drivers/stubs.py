"""Stub drivers (legacy India/AWS pools; real provisioners live in providers.py). Each documents the API it will call.
They report unavailable until credentials are set; start_instance raises NotImplemented.
Implementation pattern (all): create VM/GPU node from an image with Docker preinstalled,
cloud-init installs our node agent, agent pulls the app image from our registry and runs it,
reports host:port (private network / WireGuard) back to the control plane."""
import os
from .base import Driver
class _Stub(Driver):
    env = []; docs = ""
    def available(self):
        missing = [e for e in self.env if not os.environ.get(e)]
        return (False, f"missing credentials: {', '.join(missing)}" if missing else "credentials present; driver not yet implemented")
    def build(self, app, source, log): raise NotImplementedError(f"{self.name}: images are built on the control plane and pushed to registry")
    def start_instance(self, *a, **k): raise NotImplementedError(f"{self.name} driver is a stub — see {self.docs}")
    def stop_instance(self, iid): raise NotImplementedError
    def logs(self, iid, tail=200): return ""
class E2EDriver(_Stub):
    name="e2e"; supports_gpu=True; regions=["in-del","in-mum","in-chn"]
    env=["E2E_API_KEY","E2E_AUTH_TOKEN","E2E_PROJECT_ID"]
    docs="https://docs.e2enetworks.com (MyAccount API: POST /myaccount/api/v1/nodes/ ; also E2E TIR GPU)"
class JarvislabsDriver(_Stub):
    name="jarvislabs"; supports_gpu=True; regions=["in-mum"]
    env=["JARVISLABS_TOKEN"]; docs="https://docs.jarvislabs.ai (jlclient python SDK / CLI)"
class AWSMumbaiSpotDriver(_Stub):
    name="aws-ap-south-1-spot"; supports_gpu=True; regions=["in-mum"]
    env=["AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY"]; docs="EC2 RunInstances with InstanceMarketOptions=spot, region ap-south-1"
class YottaDriver(_Stub):
    name="yotta"; supports_gpu=True; regions=["in-mum"]
    env=["YOTTA_API_KEY"]; docs="Yotta Shakti Cloud (GPU-as-a-service; API access via sales onboarding)"
class KrutrimDriver(_Stub):
    name="krutrim"; supports_gpu=True; regions=["in-blr"]
    env=["KRUTRIM_API_KEY"]; docs="Ola Krutrim Cloud GPU instances (console/API)"
