#!/bin/bash
# Rebuild everything outside /workspace after a fresh box/VM: system packages, Caddy, venv, docker group, Postgres,
# registry, WireGuard hub, example images; then start supervisord. Idempotent. Durable state lives in data/.
set -euo pipefail; cd "$(dirname "$0")/.."; ROOT=$PWD
apt_install() { for i in 1 2 3; do sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --fix-missing "$@" && return 0; sleep 5; done; return 1; }
need=(); for p in docker.io supervisor wireguard-tools iproute2 iptables sqlite3 git curl python3-venv; do dpkg -s $p >/dev/null 2>&1 || need+=($p); done
[ ${#need[@]} -eq 0 ] || apt_install "${need[@]}"
if ! [ -x /usr/local/bin/caddy ]; then
  arch=$(dpkg --print-architecture); curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$arch" -o /tmp/caddy && sudo install -m755 /tmp/caddy /usr/local/bin/caddy
fi
id -nG "$(whoami)" | grep -qw docker || sudo usermod -aG docker "$(whoami)"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p data/pg data/registry data/volumes data/caddy
# supervisord (dockerd, paas, mcp, caddy)
sudo test -S /run/paas-supervisor.sock || { sudo supervisord -c deploy/supervisord.conf; }
for i in $(seq 60); do sudo docker info >/dev/null 2>&1 && break; sleep 1; done
# WireGuard hub (before the registry, which also publishes on the hub address)
sudo deploy/wg-hub.sh
# Postgres 16 (data bind-mounted into data/pg so it survives box resets)
if ! sudo docker inspect aloof-postgres >/dev/null 2>&1; then
  [ -f data/database_url ] || { pw=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))"); (umask 077; echo -n "postgresql://aloof:$pw@127.0.0.1:5432/aloof" > data/database_url); }
  pw=$(python3 -c "import urllib.parse as u;print(u.urlparse(open('data/database_url').read()).password)")
  sudo docker run -d --name aloof-postgres --restart unless-stopped -p 127.0.0.1:5432:5432 -e POSTGRES_USER=aloof -e POSTGRES_DB=aloof \
    -e POSTGRES_PASSWORD="$pw" -e PGDATA=/var/lib/postgresql/data/pgdata -v "$ROOT/data/pg:/var/lib/postgresql/data" postgres:16-alpine >/dev/null
fi
for i in $(seq 60); do sudo docker exec aloof-postgres pg_isready -U aloof >/dev/null 2>&1 && break; sleep 1; done
# Image registry for remote nodes (127.0.0.1 for the control plane, 10.88.0.1 for nodes over WireGuard)
sudo docker inspect aloof-registry >/dev/null 2>&1 || sudo docker run -d --name aloof-registry --restart unless-stopped \
  -p 127.0.0.1:5000:5000 -p 10.88.0.1:5000:5000 -v "$ROOT/data/registry:/var/lib/registry" registry:2 >/dev/null
# example images used by demos/tests
for t in 2 3; do sudo docker image inspect paas-examples/hello:$t >/dev/null 2>&1 || sudo docker build -q -t paas-examples/hello:$t examples/hello >/dev/null; done
.venv/bin/python -c "import paas.db" && echo "db migrated"
sudo supervisorctl -c deploy/supervisord.conf update >/dev/null 2>&1 || true
sudo supervisorctl -c deploy/supervisord.conf restart paas mcp >/dev/null
sleep 5; sudo supervisorctl -c deploy/supervisord.conf status
