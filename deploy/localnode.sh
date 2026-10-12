#!/bin/bash
# Simulated remote node for integration tests: its own network namespace (no route to the control plane except the
# "internet" veth + WireGuard), its own dockerd. Usage: localnode.sh up NAME IDX | down NAME IDX
set -e; NAME=${2:-aloof-n1}; IDX=${3:-1}; HOST=192.168.77.$((IDX*4-3)); PEER=192.168.77.$((IDX*4-2))
ROOT=/var/lib/$NAME; RUN=/run/$NAME
if [ "$1" = up ]; then
  ip netns add $NAME 2>/dev/null || true
  if ! ip link show veth-$IDX >/dev/null 2>&1; then
    ip link add veth-$IDX type veth peer name eth0 netns $NAME
    ip addr add $HOST/30 dev veth-$IDX; ip link set veth-$IDX up
    ip -n $NAME addr add $PEER/30 dev eth0; ip -n $NAME link set eth0 up; ip -n $NAME link set lo up
  fi
  mkdir -p $ROOT $RUN
  if ! [ -S $RUN/docker.sock ]; then
    setsid nsenter --net=/run/netns/$NAME dockerd --data-root $ROOT/docker --exec-root $RUN/exec --pidfile $RUN/dockerd.pid -H unix://$RUN/docker.sock \
      --insecure-registry 10.88.0.1:5000 > $ROOT/dockerd.log 2>&1 &
    for i in $(seq 60); do [ -S $RUN/docker.sock ] && DOCKER_HOST=unix://$RUN/docker.sock docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
  echo "node $NAME: veth $PEER (host side $HOST), docker unix://$RUN/docker.sock"
elif [ "$1" = down ]; then
  [ -f $RUN/agent.pid ] && kill $(cat $RUN/agent.pid) 2>/dev/null || true
  [ -f $RUN/dockerd.pid ] && kill $(cat $RUN/dockerd.pid) 2>/dev/null || true; sleep 3
  ip link del veth-$IDX 2>/dev/null || true; ip netns del $NAME 2>/dev/null || true; rm -rf $RUN
fi
