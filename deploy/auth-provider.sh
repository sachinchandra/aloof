#!/bin/sh
# usage (on prod, in /workspace/paas): sh deploy/auth-provider.sh <google|github|x> <on|off>
# Flips AUTH_<P>_ENABLED in deploy/golive.env (keys untouched, mode kept), then redeploys.
set -e
P=$(echo "$1" | tr a-z A-Z); case "$P" in GOOGLE|GITHUB|X) ;; *) echo "provider: google|github|x"; exit 2;; esac
case "$2" in on) V=1;; off) V=0;; *) echo "state: on|off"; exit 2;; esac
F=deploy/golive.env; K="AUTH_${P}_ENABLED"
if grep -q "^$K=" $F; then sed -i "s/^$K=.*/$K=$V/" $F; else echo "$K=$V" >> $F; fi
umask 022; bash deploy/golive.sh >/tmp/golive.log 2>&1 && echo "deployed; $K=$V"
