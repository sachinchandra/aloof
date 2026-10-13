#!/bin/bash
# Rebuild everything outside /workspace after a fresh box/VM: system packages, Caddy, venv, docker group, Postgres,
# registry, WireGuard hub, example images; then start supervisord. Idempotent. Durable state lives in data/.
set -euo pipefail; cd "$(dirname "$0")/.."; ROOT=$PWD
apt_install() { for i in 1 2 3; do sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --fix-missing "$@" && return 0; sleep 5; done; return 1; }
need=(); for p in docker.io supervisor wireguard-tools iproute2 iptables sqlite3 git curl python3-venv; do dpkg -s $p >/dev/null 2>&1 || need+=($p); done
[ ${#need[@]} -eq 0 ] || apt_install "${need[@]}"
# Caddy v2.11.7 (pinned; update version after testing new releases)
CADDY_VERSION=2.11.7
if ! [ -x /usr/local/bin/caddy ] || ! /usr/local/bin/caddy version 2>/dev/null | grep -q "v$CADDY_VERSION"; then
  arch=$(dpkg --print-architecture); curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${arch}.tar.gz" -o /tmp/caddy.tar.gz
  tar -xzf /tmp/caddy.tar.gz -C /tmp caddy && sudo install -m755 /tmp/caddy /usr/local/bin/caddy && rm -f /tmp/caddy.tar.gz /tmp/caddy
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
# Postgres 16.15 (pinned; update version after testing new releases)
POSTGRES_IMAGE=postgres:16.15-alpine3.23
if ! sudo docker inspect aloof-postgres >/dev/null 2>&1; then
  [ -f data/database_url ] || { pw=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))"); (umask 077; echo -n "postgresql://aloof:$pw@127.0.0.1:5432/aloof" > data/database_url); }
  pw=$(python3 -c "import urllib.parse as u;print(u.urlparse(open('data/database_url').read()).password)")
  sudo docker run -d --name aloof-postgres --restart unless-stopped -p 127.0.0.1:5432:5432 -e POSTGRES_USER=aloof -e POSTGRES_DB=aloof \
    -e POSTGRES_PASSWORD="$pw" -e PGDATA=/var/lib/postgresql/data/pgdata -v "$ROOT/data/pg:/var/lib/postgresql/data" $POSTGRES_IMAGE >/dev/null
fi
for i in $(seq 60); do sudo docker exec aloof-postgres pg_isready -U aloof >/dev/null 2>&1 && break; sleep 1; done
# Image registry for remote nodes (127.0.0.1 for the control plane, 10.88.0.1 for nodes over WireGuard)
# Registry 3.1.2 (pinned; update version after testing new releases)
REGISTRY_IMAGE=registry:3.1.2
sudo docker inspect aloof-registry >/dev/null 2>&1 || sudo docker run -d --name aloof-registry --restart unless-stopped \
  -p 127.0.0.1:5000:5000 -p 10.88.0.1:5000:5000 -v "$ROOT/data/registry:/var/lib/registry" $REGISTRY_IMAGE >/dev/null
# example images used by demos/tests
for t in 2 3; do sudo docker image inspect paas-examples/hello:$t >/dev/null 2>&1 || sudo docker build -q -t paas-examples/hello:$t examples/hello >/dev/null; done
.venv/bin/python -c "import paas.db" && echo "db migrated"
sudo supervisorctl -c deploy/supervisord.conf update >/dev/null 2>&1 || true
sudo supervisorctl -c deploy/supervisord.conf restart paas mcp >/dev/null

# Health monitor cron job (runs every 5 minutes if HEALTH_CHECK_ENABLED=1 in env)
# To enable: set HEALTH_CHECK_ENABLED=1 and HEALTH_ALERT_EMAIL in your environment
CRON_FILE=/etc/cron.d/aloof-health
if [ ! -f "$CRON_FILE" ]; then
  echo "# Aloof health monitor - runs every 5 minutes" | sudo tee "$CRON_FILE" >/dev/null
  echo "*/5 * * * * root cd $ROOT && .venv/bin/python scripts/health_monitor.py >> data/health_monitor.log 2>&1" | sudo tee -a "$CRON_FILE" >/dev/null
  sudo chmod 644 "$CRON_FILE"
  echo "Health monitor cron installed (enable via HEALTH_CHECK_ENABLED=1 and HEALTH_ALERT_EMAIL)"
fi

sleep 5; sudo supervisorctl -c deploy/supervisord.conf status
