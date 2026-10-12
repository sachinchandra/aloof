#!/bin/bash
# Start everything under supervisord (auto-restart). Usage: ./run.sh [status|restart <prog>|stop]
cd "$(dirname "$0")"; C="sudo supervisorctl -c deploy/supervisord.conf"
case "$1" in
  status) $C status ;; restart) $C restart "${2:-all}" ;; stop) $C shutdown ;;
  *) if sudo test -S /run/paas-supervisor.sock; then $C reread; $C update; $C status; exit; fi; sudo supervisord -c deploy/supervisord.conf; sleep 6; $C status ;;
esac
