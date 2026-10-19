#!/bin/sh
# Noctyx repository endpoint ON the NAS, run as the NAS user that owns the repositories.
# Deployed (rendered for the site) to ~/<nas_ssh.dir>/run.sh by `noctyx nas-rest deploy`; the head's tick
# calls `start` every 15 minutes, so it comes back by itself after a NAS reboot without a boot task.
#   run.sh start|stop|restart|status
# The TLS key and htpasswd live in that home directory (0700), outside the shared backup folder.
BASE=$(cd "$(dirname "$0")" && pwd)
PIDF=$BASE/rest-server.pid
LOG=$BASE/rest-server.log
REPO_ROOT=@NAS_REPO_ROOT@

running() { [ -s "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

start() {
  if running; then echo "running pid $(cat "$PIDF")"; return 0; fi
  [ -d "$REPO_ROOT" ] || { echo "repo root $REPO_ROOT missing: volume not mounted?" >&2; return 1; }
  ulimit -n 4096 2>/dev/null
  # keep the log bounded: one previous generation
  [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 10000000 ] && mv -f "$LOG" "$LOG.1"
  setsid nohup "$BASE/bin/rest-server" \
    --path "$REPO_ROOT" \
    --listen :@REST_PORT@ \
    --tls --tls-cert "$BASE/tls/server.crt" --tls-key "$BASE/tls/server.key" \
    --tls-min-ver 1.2 \
    --htpasswd-file "$BASE/htpasswd" \
    --private-repos --append-only --max-size 0 \
    >> "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDF"
  sleep 1
  if running; then echo "started pid $(cat "$PIDF")"; else echo "failed to start: see $LOG" >&2; tail -5 "$LOG" >&2; return 1; fi
}

stop() {
  if running; then kill "$(cat "$PIDF")"; sleep 1; running && kill -9 "$(cat "$PIDF")"; fi
  rm -f "$PIDF"; echo stopped
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) if running; then echo "running pid $(cat "$PIDF")"; else echo "not running"; exit 3; fi ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
