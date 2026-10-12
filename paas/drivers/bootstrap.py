"""Renders the first-boot script every provider runs (E2E start_scripts, Krutrim user_data, Jarvislabs script, AWS UserData).
Installs Docker (+ NVIDIA container toolkit on GPU nodes) and WireGuard, joins the mesh with a single-use token, then runs the agent."""
import os, shlex
TEMPLATE = r"""#!/bin/bash
# Aloof node bootstrap: node {node_id} ({provider}/{plan})
set -euo pipefail
exec > >(tee -a /var/log/aloof-bootstrap.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
API={api}
JOIN_TOKEN={join_token}
GPU={gpu}
REGISTRY={registry}
apt-get update -y
apt-get install -y ca-certificates curl wireguard-tools python3 jq
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh; fi
mkdir -p /etc/docker /etc/aloof
cat > /etc/docker/daemon.json <<JSON
{{"insecure-registries": ["$REGISTRY"], "log-driver": "json-file", "log-opts": {{"max-size": "20m", "max-file": "3"}}}}
JSON
if [ "$GPU" = "1" ]; then
  if ! command -v nvidia-smi >/dev/null; then apt-get install -y ubuntu-drivers-common && ubuntu-drivers install --gpgpu || true; fi
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -y && apt-get install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
fi
systemctl restart docker
curl -fsSL "$API/v1/nodes/agent.py" -o /usr/local/bin/aloof-agent && chmod 755 /usr/local/bin/aloof-agent
python3 /usr/local/bin/aloof-agent register --api "$API" --join-token "$JOIN_TOKEN"
systemctl enable --now wg-quick@wg0
cat > /etc/systemd/system/aloof-agent.service <<UNIT
[Unit]
Description=Aloof node agent
After=network-online.target docker.service wg-quick@wg0.service
Requires=docker.service wg-quick@wg0.service
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/aloof-agent serve
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now aloof-agent
echo "aloof bootstrap complete"
"""
def render(node_id, provider, plan, join_token, gpu=False):
    api = os.environ.get("PAAS_NODE_API_URL") or os.environ.get("PAAS_PUBLIC_API_URL", "http://localhost:8000")
    reg = os.environ.get("PAAS_REGISTRY_NODE", os.environ.get("PAAS_REGISTRY", "10.88.0.1:5000"))
    return TEMPLATE.format(node_id=node_id, provider=provider, plan=plan, api=shlex.quote(api), join_token=shlex.quote(join_token),
                           gpu="1" if gpu else "0", registry=shlex.quote(reg))
