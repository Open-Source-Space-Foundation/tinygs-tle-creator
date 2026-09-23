#!/usr/bin/env bash
# One-off: merge the July 2026 laptop archive into <root>/electra/.
#
#   scripts/merge_import.sh [IMPORT_DIR]     (default <root>/import/laptop-2026-07)
#
# IMPORT_DIR is what was rsynced from the laptop's proves-pass-data/tinygs/:
#   tinygs_packets_<ts>.json   raw fetch snapshots
#   details/*.json             per-packet detail
#   tinygs_log.csv             decoded log (incl. 45 legacy lasttlm-* rows)
#
# What it does (idempotent; IMPORT_DIR is only read, never modified):
#   - each snapshot -> electra/raw/YYYY/MM/DD/electra_<ts>.json.gz (skip if present)
#   - details/*.json -> cp -n into electra/details/
#   - lasttlm-* rows of tinygs_log.csv -> electra/lasttlm_legacy.csv (header kept)
#   - packet rows: re-run proves_track.py over the snapshots in chronological
#     order into electra/log.csv; it dedupes by packet id, so re-runs and
#     overlap with live data are harmless. Historical ALERT lines are kept in
#     the merge log only (not alerts.log / notify).
#
# Takes the cycle lock, so it never races a scheduled cycle on electra/log.csv.
{ # whole script parsed before it runs, so updating it mid-run (git pull) is safe
set -euo pipefail
JOB=merge_import
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

guard
IMPORT="${1:-$ROOT/import/laptop-2026-07}"
[[ -d "$IMPORT" ]] || { echo "no import dir $IMPORT" >&2; exit 2; }
IMPORT="$(cd "$IMPORT" && pwd -P)"
start_job_log merge_import
take_lock cycle

E="$ROOT/electra"
mkdir -p "$E/details"
log "=== merge_import from $IMPORT into $E"

# 1. raw snapshots
snaps="$(find "$IMPORT" -maxdepth 1 -type f -name 'tinygs_packets_*.json' | sort)"
n_new=0
n_have=0
n_bad=0
good=""
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  base="$(basename "$f" .json)"
  ts="${base#tinygs_packets_}"
  digits="$(printf '%s' "$ts" | tr -cd '0-9')"
  if [[ ${#digits} -lt 8 ]]; then
    log "WARN cannot parse date from $base; skipped"
    n_bad=$((n_bad + 1))
    continue
  fi
  good="${good:+$good
}$f"
  d="$E/raw/${digits:0:4}/${digits:4:2}/${digits:6:2}"
  dest="$d/electra_${ts}.json.gz"
  if [[ -e "$dest" ]]; then
    n_have=$((n_have + 1))
    continue
  fi
  mkdir -p "$d"
  gzip -c "$f" >"$dest.tmp.$$"
  mv -f "$dest.tmp.$$" "$dest"
  n_new=$((n_new + 1))
done <<<"$snaps"
log "raw: $n_new archived, $n_have already present, $n_bad unparsable"

# 2. details (cp -n never overwrites)
n_det_before="$(find "$E/details" -type f -name '*.json' | wc -l | tr -d ' ')"
if [[ -d "$IMPORT/details" ]]; then
  find "$IMPORT/details" -maxdepth 1 -type f -name '*.json' -exec cp -n {} "$E/details/" \;
fi
n_det_after="$(find "$E/details" -type f -name '*.json' | wc -l | tr -d ' ')"
log "details: $((n_det_after - n_det_before)) copied ($n_det_after total)"

# 3. legacy lasttlm rows
if [[ -f "$IMPORT/tinygs_log.csv" ]]; then
  {
    head -1 "$IMPORT/tinygs_log.csv"
    grep '^lasttlm-' "$IMPORT/tinygs_log.csv" || true
  } | write_atomic "$E/lasttlm_legacy.csv"
  log "lasttlm_legacy.csv: $(($(wc -l <"$E/lasttlm_legacy.csv") - 1)) rows"
else
  log "no tinygs_log.csv in import; skipping lasttlm split"
fi

# 4. packet rows via proves_track over the dated snapshots, oldest first
rows_before=0
[[ -f "$E/log.csv" ]] && rows_before=$(($(wc -l <"$E/log.csv")))
n_track_fail=0
n_hist_alerts=0
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  rc=0
  out="$(py proves_track.py "$f" "$E/log.csv" --source-sat PROVES_Electra 2>&1)" || rc=$?
  n_hist_alerts=$((n_hist_alerts + $(printf '%s\n' "$out" | grep -c '^ALERT' || true)))
  printf '%s\n' "$out" | sed "s|^|  $(basename "$f"): |"
  if [[ $rc -ne 0 ]]; then
    log "WARN proves_track rc=$rc on $(basename "$f")"
    n_track_fail=$((n_track_fail + 1))
  fi
done <<<"$good"
rows_after=0
[[ -f "$E/log.csv" ]] && rows_after=$(($(wc -l <"$E/log.csv")))
log "log.csv: $rows_before -> $rows_after lines; $n_track_fail snapshot(s) failed; $n_hist_alerts historical ALERT line(s) (not forwarded)"
log "=== merge_import done"
summary "raw_new=$n_new details_new=$((n_det_after - n_det_before)) log_lines=$rows_before->$rows_after track_failed=$n_track_fail"
exit
}
