#!/bin/bash
# Bring up the control-plane WireGuard hub (idempotent). Key: data/wg-hub.key. Peers are added by the control plane.
set -e; cd "$(dirname "$0")/.."
[ -s data/wg-hub.key ] || (umask 077; mkdir -p data; wg genkey > data/wg-hub.key)
ip link show wg0 >/dev/null 2>&1 || ip link add wg0 type wireguard
wg set wg0 listen-port "${PAAS_WG_PORT:-51820}" private-key data/wg-hub.key
ip addr replace 10.88.0.1/16 dev wg0; ip link set wg0 up
# Nodes reach the platform registry (a docker-published port on 10.88.0.1) through wg0 -> docker0. Some hosts carry
# stale iptables-legacy docker rules (FORWARD DROP); allow just this path in both backends.
for IPT in iptables iptables-legacy; do command -v $IPT >/dev/null || continue
  $IPT -S DOCKER-USER >/dev/null 2>&1 || continue
  $IPT -C DOCKER-USER -i wg0 -o docker0 -j ACCEPT 2>/dev/null || $IPT -I DOCKER-USER -i wg0 -o docker0 -j ACCEPT
done
echo "hub up: $(wg show wg0 public-key) on UDP ${PAAS_WG_PORT:-51820}"
