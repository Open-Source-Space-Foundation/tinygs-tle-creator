#!/bin/bash
# Stop hook for Claude Code cloud sessions: after every turn, save newly
# scraped TinyGS data to the tinygs-archive branch of proves-electra-ops
# (scripts/cloud.sh save). A no-op when nothing new has been scraped. Never
# blocks the turn; a failed save surfaces as a warning instead.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

out="$("$CLAUDE_PROJECT_DIR/scripts/cloud.sh" save 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then
  python3 -c 'import json, sys; print(json.dumps({"systemMessage": "TinyGS archive save failed:\n" + sys.argv[1]}))' "$out"
fi
exit 0
