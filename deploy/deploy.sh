#!/bin/bash
# Zero-downtime rolling app deploy on the edge: pull, then restart paas (A, :8000) and paas-b (B, :8010) ONE AT A TIME,
# waiting for each to answer /healthz 200 before touching the other. NEVER restarts caddy (it health-checks both upstreams
# and retries for up to 10s, so requests go to whichever instance is up).
# If an instance does not come healthy within HEALTH_TIMEOUT seconds the deploy aborts and the other instance keeps serving.
# Usage: deploy/deploy.sh            (pull + rolling restart)
#        deploy/deploy.sh --restart-only   (rolling restart without git pull)
set -e; cd "$(dirname "$0")/.."
SC="supervisorctl -c deploy/supervisord.conf"
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-60}
wait_healthy() {  # $1=port
  local t=0
  until [ "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://127.0.0.1:$1/healthz")" = 200 ]; do
    sleep 0.5; t=$((t+1)); [ $t -ge $((HEALTH_TIMEOUT*2)) ] && return 1
  done; return 0
}
roll() {  # $1=program $2=port $3=other port
  wait_healthy "$3" || { echo "ABORT: other instance (:$3) is not healthy, refusing to restart $1"; exit 1; }
  echo "$(date '+%F %T %Z') restarting $1 (:$2)"; $SC restart "$1"
  wait_healthy "$2" || { echo "ABORT: $1 (:$2) not healthy after ${HEALTH_TIMEOUT}s; other instance (:$3) still serving"; exit 1; }
  # wait until caddy also sees it as up again (no recent passive failures), else taking down the other one would
  # leave caddy with no usable upstream and requests would stall until fail_duration expires
  t=0; until curl -s -m 2 http://localhost:2019/reverse_proxy/upstreams | tr '}' '\n' | grep "127.0.0.1:$2\"" | grep -q '"fails":0'; do
    sleep 0.5; t=$((t+1)); [ $t -ge 60 ] && { echo "ABORT: caddy still marks :$2 failed after 30s; not restarting the other instance"; exit 1; }; done
  sleep 2.5   # one more active health-check interval (2s)
  echo "$(date '+%F %T %Z') $1 healthy (caddy sees it up)"
}
if [ "$1" = "--restart-only" ]; then OLD=$(git rev-parse HEAD); NEW=$OLD; CH=""
else OLD=$(git rev-parse HEAD); git pull --ff-only; NEW=$(git rev-parse HEAD); CH=$(git diff --name-only "$OLD" "$NEW"); fi
if $SC status paas-b 2>/dev/null | grep -q RUNNING; then
  roll paas 8000 8010
  roll paas-b 8010 8000
else
  echo "paas-b not running (single-instance mode): plain restart of paas"; $SC restart paas; wait_healthy 8000
fi
echo "$CH" | grep -q '^mcp_server/' && $SC restart mcp
if echo "$CH" | grep -q '^deploy/Caddyfile'; then
  echo "Caddyfile.prod changed: re-render data/Caddyfile.live, then: caddy validate && caddy reload --config data/Caddyfile.live --adapter caddyfile (never restart caddy)"
fi
echo "deployed $OLD -> $NEW"
