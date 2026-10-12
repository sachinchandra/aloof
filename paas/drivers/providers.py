"""Cloud node provisioners (one VM/GPU node per call). Each activates only when its credentials are set.
Endpoints follow the providers' public docs/SDKs (checked Oct 2026):
  E2E Networks MyAccount API  https://docs.e2enetworks.com/api/myaccount/  (POST/GET/DELETE /myaccount/api/v1/nodes/)
  Jarvislabs  (official `jarvislabs` SDK 0.2.17 transport): backendn|backendc.jarvislabs.net  templates/vm/create, misc/status,
              users/fetch/{id}, templates/vm/destroy, scripts/add
  Krutrim Cloud (krutrim-client 0.8.4): cloud.olakrutrim.com /vm/v1/create_instance_async, get_instance_task_status,
              search_instances, delete_instance_async (x-region header); IAM /iam/v1/signInAsRootUser
  AWS EC2 ap-south-1 spot (boto3 RunInstances InstanceMarketOptions=spot, AMI via SSM public parameters)"""
import os, time, base64, json, threading, logging, httpx
from ..plans import PLANS
log = logging.getLogger("paas.providers")
E = lambda k, d=None: os.environ.get(k, d)
class ProvisionError(RuntimeError): pass
class Provisioner:
    name = "base"; env = []; supports_gpu = False; regions = []; docs = ""
    def available(self):
        missing = [e for e in self.env if not E(e)]
        return (False, f"missing credentials: {', '.join(missing)}") if missing else (True, "ok")
    def node_size(self, plan_id):
        """Provider-specific machine for a plan. Override per provider; env PAAS_<PROVIDER>_PLAN_<PLAN> wins."""
        key = f"PAAS_{self.name.upper().replace('-', '_')}_PLAN_{plan_id.upper().replace('-', '_')}"
        return E(key) or self.DEFAULT_SIZES.get(plan_id) or self.DEFAULT_SIZES.get("cpu-node" if PLANS[plan_id]["kind"] == "cpu" else "")
    DEFAULT_SIZES = {}
    def create(self, node_id, plan_id, script, gpu): raise NotImplementedError
    def status(self, ref): raise NotImplementedError
    def destroy(self, ref): raise NotImplementedError
    def http(self): return httpx.Client(timeout=60)
def _raise(r, what):
    if r.status_code >= 400: raise ProvisionError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
    try: return r.json()
    except Exception: return {}
# ---------------- E2E Networks ----------------
class E2EProvisioner(Provisioner):
    name = "e2e"; supports_gpu = True; regions = ["in-del", "in-chn"]; env = ["E2E_API_KEY", "E2E_AUTH_TOKEN", "E2E_PROJECT_ID"]
    docs = "https://docs.e2enetworks.com/api/myaccount/compute/nodes/create-node/"
    # CPU node plan from the official example; GPU plan strings come from GET /myaccount/api/v1/images/ (list_plans()).
    DEFAULT_SIZES = {"cpu-node": "C3-4vCPU-8RAM-100DISK-C3-Onetemplate-Ubuntu24.04-Delhi"}
    base = property(lambda s: E("E2E_API_BASE", "https://api.e2enetworks.com/myaccount/api/v1"))
    def _q(self): return {"apikey": E("E2E_API_KEY"), "project_id": E("E2E_PROJECT_ID"), "location": E("E2E_LOCATION", "Delhi")}
    def _h(self): return {"Authorization": f"Bearer {E('E2E_AUTH_TOKEN')}", "Content-Type": "application/json"}
    def create(self, node_id, plan_id, script, gpu):
        plan = self.node_size(plan_id)
        if not plan: raise ProvisionError(f"e2e: no E2E plan mapped for {plan_id}; set PAAS_E2E_PLAN_{plan_id.upper().replace('-','_')} (see list_plans())")
        body = {"label": "aloof", "name": f"aloof-{node_id}", "region": E("E2E_REGION", "ncr"), "plan": plan,
                "image": E("E2E_IMAGE", "Ubuntu-24.04-Distro"), "ssh_keys": [k for k in [E("PAAS_SSH_PUBKEY")] if k],
                "start_scripts": [script], "backups": False, "enable_bitninja": False, "disable_password": True,
                "is_saved_image": False, "default_public_ip": False, "is_ipv6_availed": False, "number_of_instances": 1}
        if E("E2E_VPC_ID"): body["vpc_id"] = int(E("E2E_VPC_ID"))
        if E("E2E_SECURITY_GROUP_ID"): body["security_group_id"] = int(E("E2E_SECURITY_GROUP_ID"))
        with self.http() as c: d = _raise(c.post(f"{self.base}/nodes/", params=self._q(), headers=self._h(), json=body), "e2e create node")
        data = d.get("data", d); data = data[0] if isinstance(data, list) else data
        ref = data.get("id") or data.get("node_id") or (data.get("node") or {}).get("id")
        if not ref: raise ProvisionError(f"e2e: no node id in response {str(d)[:200]}")
        return str(ref)
    def status(self, ref):
        with self.http() as c: r = c.get(f"{self.base}/nodes/{ref}/", params=self._q(), headers=self._h())
        if r.status_code == 404: return {"state": "gone"}
        d = _raise(r, "e2e get node").get("data", {})
        st = (d.get("status") or "").lower()
        return {"state": "running" if st in ("running", "active") else "failed" if "fail" in st or "error" in st else "provisioning",
                "public_ip": d.get("public_ip_address") or d.get("public_ip"), "raw_status": d.get("status")}
    def destroy(self, ref):
        with self.http() as c: r = c.delete(f"{self.base}/nodes/{ref}/", params=self._q(), headers=self._h())
        if r.status_code not in (200, 202, 204, 404): _raise(r, "e2e delete node")
    def list_plans(self, **filters):
        with self.http() as c: return _raise(c.get(f"{self.base}/images/", params={**self._q(), **filters}, headers=self._h()), "e2e plans")
# ---------------- Jarvislabs ----------------
class JarvislabsProvisioner(Provisioner):
    name = "jarvislabs"; supports_gpu = True; regions = ["in-noida", "in-chn"]; env = ["JARVISLABS_TOKEN"]
    docs = "https://docs.jarvislabs.ai/sdk/ (endpoints from the official jarvislabs SDK transport)"
    REGION_URLS = {"india-noida-01": "https://backendn.jarvislabs.net", "india-chennai-01": "https://backendc.jarvislabs.net"}
    DEFAULT_SIZES = {"gpu-a100": "A100-80GB", "gpu-h100": "H100", "gpu-l4": "L4"}
    def _base(self): return E("JARVISLABS_API_BASE") or self.REGION_URLS[E("JARVISLABS_REGION", "india-noida-01")]
    def _h(self): return {"Authorization": f"Bearer {E('JARVISLABS_TOKEN')}"}
    def _script_id(self, c, node_id, script):
        """Startup scripts are uploaded once per node (multipart) and referenced by id at create time."""
        name = f"aloof-{node_id}"
        _raise(c.post(f"{self._base()}/scripts/add", params={"name": name}, headers=self._h(), files={"script": ("startup.sh", script.encode(), "application/x-sh")}), "jarvislabs script upload")
        meta = _raise(c.get(f"{self._base()}/scripts/", headers=self._h()), "jarvislabs scripts").get("script_meta", [])
        for m in meta:
            if m.get("script_name") == name or m.get("name") == name: return str(m.get("script_id") or m.get("id"))
        raise ProvisionError("jarvislabs: uploaded script not found")
    def create(self, node_id, plan_id, script, gpu):
        if not gpu: raise ProvisionError("jarvislabs: CPU VMs don't support startup scripts; use e2e/krutrim for CPU pools")
        gpu_type = self.node_size(plan_id)
        if not gpu_type: raise ProvisionError(f"jarvislabs: no GPU type for {plan_id}")
        with self.http() as c:
            sid = self._script_id(c, node_id, script)
            body = {"gpu_type": gpu_type, "num_gpus": 1, "hdd": int(E("JARVISLABS_STORAGE_GB", "100")), "region": E("JARVISLABS_REGION", "india-noida-01"),
                    "name": f"aloof-{node_id}"[:40], "is_spot": False, "duration": "hour", "disk_type": "ssd", "http_ports": "",
                    "script_id": sid, "script_args": "", "fs_id": None, "arguments": ""}
            d = _raise(c.post(f"{self._base()}/templates/vm/create", headers=self._h(), json=body), "jarvislabs create")
        if not d.get("machine_id"): raise ProvisionError(f"jarvislabs create failed: {str(d)[:200]}")
        return str(d["machine_id"])
    def status(self, ref):
        with self.http() as c:
            r = c.get(f"{self._base()}/misc/status", params={"machine_id": ref}, headers=self._h())
            if r.status_code == 404: return {"state": "provisioning"}
            st = _raise(r, "jarvislabs status").get("status", "")
            ip = None
            if st == "Running":
                f = c.get(f"{self._base()}/users/fetch/{ref}", headers=self._h())
                ip = ((f.json() if f.status_code == 200 else {}).get("instance") or {}).get("public_ip")
        return {"state": {"Running": "running", "Failed": "failed"}.get(st, "provisioning"), "public_ip": ip, "raw_status": st}
    def destroy(self, ref):
        with self.http() as c: r = c.post(f"{self._base()}/templates/vm/destroy", params={"machine_id": ref}, headers=self._h())
        if r.status_code not in (200, 404): _raise(r, "jarvislabs destroy")
# ---------------- Ola Krutrim ----------------
class KrutrimProvisioner(Provisioner):
    name = "krutrim"; supports_gpu = True; regions = ["in-blr"]
    env = ["KRUTRIM_REGION", "KRUTRIM_VPC_ID", "KRUTRIM_SUBNET_ID", "KRUTRIM_SECURITY_GROUP", "KRUTRIM_SSHKEY_NAME", "KRUTRIM_IMAGE_KRN"]
    docs = "https://docs.cloud.olakrutrim.com/sdk-guide/core-infra-sdk"
    DEFAULT_SIZES = {"cpu-node": "CPU-4x-16GB", "gpu-h100": "H100-NVLINK-1x", "gpu-a100": "A100-80GB-NVLINK-1x"}
    base = property(lambda s: E("KRUTRIM_API_BASE", "https://cloud.olakrutrim.com"))
    _tok = None; _tok_at = 0; _tl = threading.Lock()
    def available(self):
        ok, msg = super().available()
        if ok and not (E("KRUTRIM_API_KEY") or (E("KRUTRIM_EMAIL") and E("KRUTRIM_PASSWORD"))): return False, "missing credentials: KRUTRIM_API_KEY or KRUTRIM_EMAIL+KRUTRIM_PASSWORD"
        return ok, msg
    def _token(self, c):
        if E("KRUTRIM_API_KEY"): return E("KRUTRIM_API_KEY")
        with self._tl:   # IAM tokens expire every ~5 min: refresh at 4
            if not self._tok or time.time() - self._tok_at > 240:
                d = _raise(c.post(f"{self.base}/iam/v1/signInAsRootUser", json={"email": E("KRUTRIM_EMAIL"), "password": E("KRUTRIM_PASSWORD")}), "krutrim sign-in")
                self._tok, self._tok_at = d["access_token"], time.time()
            return self._tok
    def _h(self, c): return {"Authorization": f"Bearer {self._token(c)}", "x-region": E("KRUTRIM_REGION"), "Content-Type": "application/json"}
    def create(self, node_id, plan_id, script, gpu):
        itype = self.node_size(plan_id)
        body = {"instanceName": f"aloof-{node_id}", "instanceType": itype, "subnet_id": E("KRUTRIM_SUBNET_ID"), "vpc_id": E("KRUTRIM_VPC_ID"),
                "region": E("KRUTRIM_REGION"), "floating_ip": False, "security_groups": E("KRUTRIM_SECURITY_GROUP").split(","),
                "sshkey_name": E("KRUTRIM_SSHKEY_NAME"), "user_data": base64.b64encode(script.encode()).decode(), "delete_on_termination": True,
                "isGpu": bool(gpu), "volumes": [], "tags": [{"key": "aloof-node", "value": node_id}], "count": 1,
                "image_krn": E("KRUTRIM_IMAGE_KRN"), "volume_name": f"aloof-{node_id}-boot", "volume_size": int(E("KRUTRIM_BOOT_GB", "100")),
                "volumetype": E("KRUTRIM_VOLUME_TYPE", "HNSS")}
        with self.http() as c: d = _raise(c.post(f"{self.base}/vm/v1/create_instance_async", headers=self._h(c), json=body), "krutrim create")
        if not d.get("task_id"): raise ProvisionError(f"krutrim create: no task_id {str(d)[:200]}")
        return json.dumps({"task_id": d["task_id"], "name": f"aloof-{node_id}"})
    def _find(self, c, name):
        d = _raise(c.get(f"{self.base}/vm/v1/search_instances", params={"vpc_id": E("KRUTRIM_VPC_ID"), "page": 1, "limit": 100}, headers=self._h(c)), "krutrim search")
        items = d.get("instances") or d.get("data") or d.get("items") or (d if isinstance(d, list) else [])
        for it in items:
            if (it.get("instanceName") or it.get("name")) == name: return it
    def status(self, ref):
        r = json.loads(ref)
        with self.http() as c:
            t = _raise(c.get(f"{self.base}/vm/v1/get_instance_task_status", params={"task_id": r["task_id"]}, headers=self._h(c)), "krutrim task")
            st = str(t.get("status") or t.get("task_status") or "").upper()
            if st in ("FAILED", "ERROR"): return {"state": "failed", "raw_status": st}
            inst = self._find(c, r["name"]) if st in ("SUCCESS", "COMPLETED", "ACTIVE") else None
        if inst and str(inst.get("status", "")).upper() == "ACTIVE":
            return {"state": "running", "public_ip": inst.get("floating_ip") or inst.get("ip_address"), "krn": inst.get("krn") or inst.get("instanceKrn")}
        return {"state": "provisioning", "raw_status": st}
    def destroy(self, ref):
        r = json.loads(ref)
        with self.http() as c:
            inst = self._find(c, r["name"])
            if not inst: return
            krn = inst.get("krn") or inst.get("instanceKrn")
            _raise(c.delete(f"{self.base}/vm/v1/delete_instance_async", params={"instanceKrn": krn, "deleteVolume": "true"}, headers=self._h(c)), "krutrim delete")
# ---------------- AWS ap-south-1 spot ----------------
class AWSSpotProvisioner(Provisioner):
    name = "aws-ap-south-1-spot"; supports_gpu = True; regions = ["in-mum"]; env = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    docs = "EC2 RunInstances (InstanceMarketOptions spot) in ap-south-1"
    DEFAULT_SIZES = {"cpu-node": "t3a.xlarge", "gpu-t4-spot": "g4dn.xlarge", "gpu-l4": "g6.xlarge", "gpu-a100": "p4d.24xlarge"}
    AMI_PARAMS = {False: "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
                  True: "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"}
    _clients = None
    def clients(self):
        if self._clients is None:
            import boto3; s = boto3.session.Session(region_name=E("AWS_REGION", "ap-south-1"))
            self._clients = (s.client("ec2"), s.client("ssm"))
        return self._clients
    def max_price(self, plan_id, gpu):
        """Spot bid capped at the cost the margin floor assumes (plans.GPU_COSTS / CPU_HOSTS), so AWS can't bill more."""
        from .. import plans
        c = plans.GPU_COSTS.get(plan_id, {}).get(self.name) if gpu else ("usd", plans.CPU_HOSTS[self.name]["usd_hr"])
        return E("PAAS_AWS_SPOT_MAX_USD") or f"{plans._usd(c) if c is not None else 0:.4f}"
    def create(self, node_id, plan_id, script, gpu):
        ec2, ssm = self.clients()
        ami = E("PAAS_AWS_AMI_GPU" if gpu else "PAAS_AWS_AMI") or ssm.get_parameter(Name=self.AMI_PARAMS[bool(gpu)])["Parameter"]["Value"]
        kw = dict(ImageId=ami, InstanceType=self.node_size(plan_id), MinCount=1, MaxCount=1, UserData=script,
                  InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate", "MaxPrice": self.max_price(plan_id, gpu)}},
                  BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": int(E("PAAS_AWS_DISK_GB", "100")), "VolumeType": "gp3", "DeleteOnTermination": True}}],
                  MetadataOptions={"HttpTokens": "required"},
                  TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": f"aloof-{node_id}"}, {"Key": "aloof-node", "Value": node_id}]}])
        if E("PAAS_AWS_SUBNET_ID"): kw["SubnetId"] = E("PAAS_AWS_SUBNET_ID")
        if E("PAAS_AWS_SECURITY_GROUP_IDS"): kw["SecurityGroupIds"] = E("PAAS_AWS_SECURITY_GROUP_IDS").split(",")
        if E("PAAS_AWS_KEY_NAME"): kw["KeyName"] = E("PAAS_AWS_KEY_NAME")
        return ec2.run_instances(**kw)["Instances"][0]["InstanceId"]
    def status(self, ref):
        ec2, _ = self.clients()
        res = ec2.describe_instances(InstanceIds=[ref])["Reservations"]
        if not res: return {"state": "gone"}
        i = res[0]["Instances"][0]; st = i["State"]["Name"]
        return {"state": {"running": "running", "pending": "provisioning"}.get(st, "gone" if st in ("terminated", "shutting-down") else "provisioning"),
                "public_ip": i.get("PublicIpAddress"), "raw_status": st}
    def destroy(self, ref): self.clients()[0].terminate_instances(InstanceIds=[ref])
# ---------------- self-hosted (Yotta bare metal, own servers): nodes join with `curl .../bootstrap.sh | sudo bash` ----------------
class SelfHostedProvisioner(Provisioner):
    name = "self-hosted"; supports_gpu = True; regions = ["any"]; env = []
    docs = "Admin creates a join token (POST /v1/admin/nodes); run the one-line installer on any Ubuntu host (e.g. Yotta Shakti bare metal)."
    def available(self): return False, "manual pool: nodes are added by an admin, never auto-provisioned"
    def create(self, *a): raise ProvisionError("self-hosted pool cannot auto-provision; add a node with POST /v1/admin/nodes")
    def status(self, ref): return {"state": "running"}
    def destroy(self, ref): pass
PROVISIONERS = {p.name: p for p in (E2EProvisioner(), JarvislabsProvisioner(), KrutrimProvisioner(), AWSSpotProvisioner(), SelfHostedProvisioner())}
