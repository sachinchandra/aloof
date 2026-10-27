#!/bin/sh
# Block the cloud metadata service (169.254.169.254):
#  - for every container (forwarded traffic -> DOCKER-USER), immediately;
#  - for the host itself (OUTPUT), once cloud-init has finished, because cloud-init's Hetzner datasource reads metadata at boot.
#    DHCP doesn't need it (systemd-networkd leases from 172.31.1.1), and nothing else on the host uses it.
# Idempotent; `stop` removes both rules. Set ALOOF_HOST_METADATA_BLOCK=0 to keep only the container rule.
set -e
IPT="iptables -w"
R="-d 169.254.169.254/32 -j DROP"
RO="-d 169.254.169.254/32 -m comment --comment aloof-metadata-block -j DROP"
cloud_done() {
  command -v cloud-init >/dev/null 2>&1 || return 0
  cloud-init status 2>/dev/null | grep -Eq 'status: (done|disabled|error)'
}
ensure() {
  $IPT -N DOCKER-USER 2>/dev/null || true
  $IPT -C DOCKER-USER $R 2>/dev/null || $IPT -I DOCKER-USER 1 $R
  if [ "${ALOOF_HOST_METADATA_BLOCK:-1}" != 0 ] && cloud_done; then
    $IPT -C OUTPUT $RO 2>/dev/null || $IPT -I OUTPUT 1 $RO
  fi
}
case "$1" in
  stop)  while $IPT -C DOCKER-USER $R 2>/dev/null; do $IPT -D DOCKER-USER $R; done
         while $IPT -C OUTPUT $RO 2>/dev/null; do $IPT -D OUTPUT $RO; done ;;
  # dockerd (run by supervisord here, not systemd) rebuilds its chains on every (re)start and drops the DOCKER-USER rule,
  # and ufw reloads can reorder OUTPUT, so the service re-checks every 10 s: both rules are back within seconds.
  watch) set +e; while :; do ensure; sleep 10; done ;;
  *)     ensure ;;
esac
