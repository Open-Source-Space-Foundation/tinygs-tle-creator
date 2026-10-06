#!/bin/bash
# SessionStart hook for Claude Code cloud sessions: builds the venv and, when
# TINYGS_SESSION_TOKEN / TINYGS_USER_ID are set in the environment, writes the
# Playwright storage_state the fetch scripts take via --auth-state.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Cloud images ship a preinstalled Chromium (/opt/pw-browsers) and block
# `playwright install`, so pin the Playwright release built for that Chromium
# instead of running `make setup`.
PLAYWRIGHT_PIN=1.56.0
VENV="$CLAUDE_PROJECT_DIR/.venv"
uv venv "$VENV" --allow-existing --quiet
VIRTUAL_ENV="$VENV" uv pip install --quiet --requirement requirements.txt "playwright==$PLAYWRIGHT_PIN"
touch "$VENV/.stamp" # so `make` targets don't re-run `playwright install`

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export VIRTUAL_ENV=\"$VENV\" PATH=\"$VENV/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

if [ -n "${TINYGS_SESSION_TOKEN:-}" ] && [ -n "${TINYGS_USER_ID:-}" ]; then
  AUTH_STATE="$HOME/.config/tinygs/auth.json"
  mkdir -p "$(dirname "$AUTH_STATE")"
  umask 077
  # Values come from the environment, never the command line or the log.
  python3 - "$AUTH_STATE" <<'PY'
import json, os, sys, time

expire = os.environ.get("TINYGS_SESSION_EXPIRE") or str(int((time.time() + 30 * 86400) * 1000))
local = {
    "sessionToken": os.environ["TINYGS_SESSION_TOKEN"],
    "userId": os.environ["TINYGS_USER_ID"],
    "sessionExpireDate": expire,
}
state = {
    "cookies": [],
    "origins": [
        {
            "origin": "https://app.tinygs.com",
            "localStorage": [{"name": k, "value": v} for k, v in local.items()],
        }
    ],
}
with open(sys.argv[1], "w") as f:
    json.dump(state, f, indent=1)
PY
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export TINYGS_AUTH_STATE=\"$AUTH_STATE\"" >> "$CLAUDE_ENV_FILE"
  fi
  echo "TinyGS auth state written to $AUTH_STATE"
else
  echo "TINYGS_SESSION_TOKEN/TINYGS_USER_ID not set; TinyGS fetches will run logged out"
fi
