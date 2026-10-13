#!/usr/bin/env bash
# Run the pipeline for ONE topic and publish the episode. Meant for cron.
#   run_topic.sh "MSK MRI"                          full run, uploads to the site
#   run_topic.sh "MSK MRI" --until screen --no-state   safe dry run (extra arguments go to `podcast run`)
#
# - one run at a time (lock), so a long run can never overlap with the next one
# - everything goes to data/logs/podcast-YYYY-MM.log; failures are also appended to data/logs/failures.log
#   and printed on stderr, which cron mails to MAILTO / the local user when mail is set up
# - a run that hangs is stopped after PODCAST_TIMEOUT (default 4h) so it cannot hold the lock forever
# Exit code: the pipeline's (0 ok, 1 a topic or upload failed), 75 = skipped because another run was active.
set -u

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$ROOT" || exit 2
TOPIC="${1:?usage: run_topic.sh \"TOPIC NAME\" [extra podcast-run arguments]}"
shift

# cron starts with a minimal environment
export PATH="/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
export LC_ALL="${LC_ALL:-C.UTF-8}" PYTHONUNBUFFERED=1

LOGDIR="$ROOT/data/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/podcast-$(date +%Y-%m).log"

code=0
{
  echo "=== $(date '+%F %T %Z') start: $TOPIC"
  exec 9>"$ROOT/data/podcast.lock"
  if ! flock -n 9; then
    echo "SKIPPED: another pipeline run is still active"
    code=75
  else
    timeout --signal=TERM --kill-after=60 "${PODCAST_TIMEOUT:-4h}" \
      "$ROOT/venv3.13/bin/podcast" run --topic "$TOPIC" --publish "$@"
    code=$?
    [ "$code" -eq 124 ] && echo "TIMEOUT: stopped after ${PODCAST_TIMEOUT:-4h}"
  fi
  echo "=== $(date '+%F %T %Z') end: $TOPIC (exit $code)"
} >>"$LOG" 2>&1

if [ "$code" -ne 0 ]; then
  msg="$(date '+%F %T') podcast run for \"$TOPIC\" ended with exit code $code - see $LOG"
  echo "$msg" >>"$LOGDIR/failures.log"
  echo "$msg" >&2
fi

find "$LOGDIR" -name 'podcast-*.log' -mtime +120 -delete 2>/dev/null
exit "$code"
