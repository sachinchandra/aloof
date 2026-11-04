#!/bin/bash
# Zero-downtime app deploy on the edge: pull, restart only what changed. NEVER restarts caddy.
# paas always restarts; mcp only if mcp_server/ changed; Caddyfile changes -> re-render via golive-style reload (graceful).
set -e; cd "$(dirname "$0")/.."
OLD=$(git rev-parse HEAD); git pull --ff-only; NEW=$(git rev-parse HEAD)
CH=$(git diff --name-only "$OLD" "$NEW")
SC="supervisorctl -c deploy/supervisord.conf"
$SC restart paas
echo "$CH" | grep -q '^mcp_server/' && $SC restart mcp
if echo "$CH" | grep -q '^deploy/Caddyfile'; then
  echo "Caddyfile.prod changed: re-render data/Caddyfile.live, then: caddy validate && caddy reload --config data/Caddyfile.live --adapter caddyfile (never restart caddy)"
fi
echo "deployed $OLD -> $NEW"
