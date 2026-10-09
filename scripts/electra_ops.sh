#!/usr/bin/env bash
# Start (or confirm) the Electra scraper loop in a Claude Code cloud session.
#
#   scripts/electra_ops.sh start    start tinygs_tle/electra_ops.py run in the background unless it's running
#   scripts/electra_ops.sh status   print whether it's running and the log tail
#
# Builds the venv and TinyGS auth state first when the session-start hook hasn't
# run (e.g. the session was started outside this repo). Results go to the
# proves-electra-ops checkout next to this repo (ELECTRA_OPS_REPO overrides).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ROOT="${TINYGS_CLOUD_ROOT:-$REPO/data/cloud/proves/tinygs}"
PIDF="$ROOT/electra_ops.pid"
LOG="$ROOT/logs/electra_ops.log"
mkdir -p "$ROOT/logs"

running() { [[ -s "$PIDF" ]] && [[ "$(cat "$PIDF")" != "$$" ]] && { cat "/proc/$(cat "$PIDF")/cmdline" 2>/dev/null | tr "\0" " " | grep -q "electra_ops.* run"; }; }

case "${1:-}" in
  start)
    if running; then
      echo "electra_ops running (pid $(cat "$PIDF"))"
      exit 0
    fi
    if [[ ! -x "$REPO/.venv/bin/python" || ! -r "${TINYGS_AUTH_STATE:-$HOME/.config/tinygs/auth.json}" ]]; then
      CLAUDE_PROJECT_DIR="$REPO" "$REPO/.claude/hooks/session-start.sh"
    fi
    "$REPO/.venv/bin/python" -c 'import skyfield' 2>/dev/null ||
      VIRTUAL_ENV="$REPO/.venv" uv pip install --quiet skyfield
    nohup "$REPO/.venv/bin/python" "$REPO/tinygs_tle/electra_ops.py" run >>"$LOG" 2>&1 &
    sleep 2
    echo "electra_ops started (pid $(cat "$PIDF" 2>/dev/null || echo "$!")); log $LOG"
    ;;
  status)
    if running; then echo "running (pid $(cat "$PIDF"))"; else echo "NOT running"; fi
    tail -n 20 "$LOG" 2>/dev/null || true
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
