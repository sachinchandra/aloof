#!/bin/bash
# Join the simulated node (deploy/localnode.sh) to the control plane exactly like cloud-init would on a VM:
# admin join token -> download agent from the API -> register -> WireGuard up -> agent serve. Run as root.
set -e; cd "$(dirname "$0")/.."; NAME=${1:-aloof-n1}; IDX=${2:-1}; HOST=192.168.77.$((IDX*4-3)); API=http://$HOST:8000
D=/var/lib/$NAME/agent; RUN=/run/$NAME; N="ip netns exec $NAME"; mkdir -p $D
deploy/localnode.sh up $NAME $IDX
JT=$(curl -fsS -X POST localhost:8000/v1/admin/nodes -H "Authorization: Bearer $(cat data/admin_token)" -H 'Content-Type: application/json' \
     -d '{"pool":"self-hosted","plan":"cpu-large"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['join_token'])")
$N curl -fsS $API/v1/nodes/agent.py -o $D/agent.py
$N env DOCKER_HOST=unix://$RUN/docker.sock ALOOF_AGENT_CONF=$D/agent.json ALOOF_WG_CONF=$D/wg0.conf python3 $D/agent.py register --api $API --join-token "$JT"
grep -v '^Address' $D/wg0.conf > $D/wg0.setconf; chmod 600 $D/wg0.setconf
$N ip link show wg0 >/dev/null 2>&1 || $N ip link add wg0 type wireguard
$N wg setconf wg0 $D/wg0.setconf; $N ip addr replace $(awk '/^Address/{print $3}' $D/wg0.conf) dev wg0; $N ip link set wg0 up
$N bash -c "DOCKER_HOST=unix://$RUN/docker.sock ALOOF_AGENT_CONF=$D/agent.json setsid nohup python3 $D/agent.py serve > /var/lib/$NAME/agent.log 2>&1 & echo \$! > $RUN/agent.pid"
sleep 2; cat /var/lib/$NAME/agent.log
