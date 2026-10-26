#!/bin/sh
# Block the cloud metadata service (169.254.169.254) for every container (forwarded traffic -> DOCKER-USER).
# The host's own traffic goes through OUTPUT, so the host can still reach metadata. Idempotent; `stop` removes the rule.
set -e
R="-d 169.254.169.254/32 -j DROP"
iptables -N DOCKER-USER 2>/dev/null || true
ensure() { iptables -N DOCKER-USER 2>/dev/null || true; iptables -C DOCKER-USER $R 2>/dev/null || iptables -I DOCKER-USER 1 $R; }
case "$1" in
  stop)  while iptables -C DOCKER-USER $R 2>/dev/null; do iptables -D DOCKER-USER $R; done ;;
  # dockerd (run by supervisord here, not systemd) rebuilds its chains on every (re)start and drops this rule,
  # so the service re-checks every 10 s: the rule is back within seconds after any Docker restart or reboot.
  watch) set +e; while :; do ensure; sleep 10; done ;;
  *)     ensure ;;
esac
