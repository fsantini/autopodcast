#!/usr/bin/env bash
# Keep the NotebookLM login alive between the weekly runs (cron, every 20 minutes - the interval notebooklm-py recommends).
# Silent while everything works. On the first failure it writes to data/logs/keepalive.log and to stderr (cron mails that);
# further failures are only logged, so an expired login does not send a mail every 20 minutes. Recovery is logged once.
# If it keeps failing, log in again:  venv3.13/bin/notebooklm login
set -u

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export PATH="/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
export LC_ALL="${LC_ALL:-C.UTF-8}"

LOGDIR="$ROOT/data/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/keepalive.log"
MARK="$LOGDIR/.keepalive_failing"

out="$(timeout 120 "$ROOT/venv3.13/bin/notebooklm" auth refresh --quiet --verify 2>&1)"
code=$?

if [ "$code" -eq 0 ]; then
  if [ -e "$MARK" ]; then
    rm -f "$MARK"
    echo "$(date '+%F %T') keepalive recovered" >>"$LOG"
  fi
  exit 0
fi

msg="$(date '+%F %T') NotebookLM keepalive failed (exit $code): $out"
echo "$msg" >>"$LOG"
if [ ! -e "$MARK" ]; then
  touch "$MARK"
  echo "$msg" >&2
  echo "If this persists, run: $ROOT/venv3.13/bin/notebooklm login" >&2
fi
exit "$code"
