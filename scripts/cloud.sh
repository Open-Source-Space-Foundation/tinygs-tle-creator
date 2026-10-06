#!/usr/bin/env bash
# Run the TinyGS pipeline in a Claude Code cloud session and save what it
# scrapes to the `tinygs-archive` branch of proves-electra-ops.
#
#   scripts/cloud.sh cycle     one fetch + archive + track pass (scripts/cycle.sh)
#   scripts/cloud.sh details   one rate-limited detail batch (scripts/details.sh)
#   scripts/cloud.sh save      commit new raw snapshots and details to the archive
#                              branch and push (run by the Stop hook every turn)
#
# cycle/details are the Mac mini's own wrappers, pointed at a data root inside
# the container (data/cloud) instead of the NVMe. The container is ephemeral,
# so anything not saved is lost when it is reclaimed. The session must have
# proves-electra-ops attached, or the push is refused.
#
# Environment overrides (all optional):
#   TINYGS_CLOUD_VOLUME     local stand-in for the data volume  (data/cloud)
#   TINYGS_ARCHIVE_REPO     archive remote   (https://github.com/Open-Source-Space-Foundation/proves-electra-ops)
#   TINYGS_ARCHIVE_BRANCH   archive branch   (tinygs-archive)
#   TINYGS_ARCHIVE_CLONE    local clone of that branch          ($HOME/.cache/tinygs-archive)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VOLUME="${TINYGS_CLOUD_VOLUME:-$REPO/data/cloud}"
ARCHIVE_REPO="${TINYGS_ARCHIVE_REPO:-https://github.com/Open-Source-Space-Foundation/proves-electra-ops}"
ARCHIVE_BRANCH="${TINYGS_ARCHIVE_BRANCH:-tinygs-archive}"
ARCHIVE_CLONE="${TINYGS_ARCHIVE_CLONE:-$HOME/.cache/tinygs-archive}"

export TINYGS_VOLUME="$VOLUME"
export TINYGS_DATA_ROOT="$VOLUME/proves/tinygs"
export TINYGS_SENTINEL="$VOLUME/proves/.tinygs-data-root"
export TINYGS_SKIP_UUID_CHECK=1 # no NVMe here; the guard still wants the sentinel
ROOT="$TINYGS_DATA_ROOT"

say() { printf 'cloud.sh: %s\n' "$*" >&2; }

prepare_root() {
  mkdir -p "$ROOT/logs" "$ROOT/status"
  touch "$TINYGS_SENTINEL"
}

# Paths (relative to $ROOT) of the write-once files: raw snapshots and details.
archivable() {
  [[ -d "$ROOT" ]] || return 0
  (cd "$ROOT" && find . -mindepth 3 -type f \
    \( -path './*/raw/*.json.gz' -o -path './*/details/*.json' \) | sed 's#^\./##' | LC_ALL=C sort)
}

push_with_retry() {
  local delay=2 attempt
  for attempt in 1 2 3 4 5; do
    if git -C "$ARCHIVE_CLONE" push -q origin "HEAD:$ARCHIVE_BRANCH" 2>/dev/null; then
      return 0
    fi
    [[ $attempt -lt 5 ]] || break
    # Another session may have pushed first. Its files never collide with ours
    # (unique names), so a rebase onto it is always clean.
    git -C "$ARCHIVE_CLONE" pull -q --rebase origin "$ARCHIVE_BRANCH" 2>/dev/null || true
    sleep "$delay"
    delay=$((delay * 2))
  done
  return 1
}

save() {
  local files new=() f n_raw=0 n_det=0
  files="$(archivable)"
  if [[ -z "$files" ]]; then
    say "nothing scraped yet; nothing to save"
    return 0
  fi

  if [[ ! -d "$ARCHIVE_CLONE/.git" ]]; then
    mkdir -p "$(dirname "$ARCHIVE_CLONE")"
    if ! git clone -q --depth 1 --single-branch --branch "$ARCHIVE_BRANCH" \
      "$ARCHIVE_REPO" "$ARCHIVE_CLONE" 2>/dev/null; then
      rm -rf "$ARCHIVE_CLONE"
      say "cannot reach $ARCHIVE_REPO ($ARCHIVE_BRANCH)."
      say "Start the session with proves-electra-ops attached. Unsaved data stays in $ROOT until the container is reclaimed."
      return 1
    fi
  else
    git -C "$ARCHIVE_CLONE" pull -q --rebase origin "$ARCHIVE_BRANCH" 2>/dev/null || true
  fi

  while IFS= read -r f; do
    [[ -e "$ARCHIVE_CLONE/$f" ]] && continue # write-once: never overwrite
    mkdir -p "$ARCHIVE_CLONE/$(dirname "$f")"
    cp "$ROOT/$f" "$ARCHIVE_CLONE/$f"
    new+=("$f")
    case "$f" in */raw/*) n_raw=$((n_raw + 1)) ;; *) n_det=$((n_det + 1)) ;; esac
  done <<<"$files"

  if [[ ${#new[@]} -eq 0 ]] && git -C "$ARCHIVE_CLONE" diff --quiet "origin/$ARCHIVE_BRANCH" HEAD 2>/dev/null; then
    return 0 # everything already saved and pushed
  fi

  if [[ ${#new[@]} -gt 0 ]]; then
    git -C "$ARCHIVE_CLONE" add -- "${new[@]}"
    git -C "$ARCHIVE_CLONE" commit -q -m "Archive $n_raw snapshot(s) and $n_det detail file(s) from a cloud session" \
      -m "Session: ${CLAUDE_CODE_REMOTE_SESSION_ID:-unknown}"
  fi

  if push_with_retry; then
    say "saved $n_raw snapshot(s) and $n_det detail file(s) to $ARCHIVE_BRANCH"
  else
    say "push to $ARCHIVE_REPO ($ARCHIVE_BRANCH) failed; committed locally in $ARCHIVE_CLONE, will retry on the next save"
    return 1
  fi
}

case "${1:-}" in
  cycle)
    prepare_root
    exec "$REPO/scripts/cycle.sh"
    ;;
  details)
    prepare_root
    exec "$REPO/scripts/details.sh"
    ;;
  save)
    save
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
