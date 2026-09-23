#!/usr/bin/env bash
# At :15/:45 (launchd space.proves.tinygs.details): one rate-limited batch of
# per-packet detail JSON (per-station rssi/snr/frequency_error) for the logs
# listed in DETAILS_KEYS. At most 25 page loads per run, 1/min, so a run takes
# up to ~25 min; tinygs_details_batch.py's own lockfile prevents overlap.
#
# Exit: tinygs_details_batch.py's exit code (0 when nothing to do), 3 drive missing.
set -euo pipefail
JOB=details
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

# Satellites whose log.csv gets detail collection. (alcyone has no entry of its
# own: Electra frames misfiled under Alcyone are routed into electra/log.csv.)
DETAILS_KEYS="${TINYGS_DETAILS_KEYS:-electra hucsat-1}"
MAX_PER_RUN="${TINYGS_DETAILS_MAX_PER_RUN:-25}"
SPACING_S="${TINYGS_DETAILS_SPACING_S:-60}"

guard
start_job_log details

log "=== details start root=$ROOT"
rows="$(sat_rows)"
src_args=()
keys_json=""
used_keys=""
before=0
for key in $DETAILS_KEYS; do
  if ! printf '%s\n' "$rows" | awk -F'\t' -v k="$key" '$1 == k {f = 1} END {exit !f}'; then
    log "skip $key: not enabled in $(basename "$TINYGS_SATS_TSV") (missing or TODO slug)"
    continue
  fi
  if [[ ! -s "$ROOT/$key/log.csv" ]]; then
    log "skip $key: no $ROOT/$key/log.csv yet"
    continue
  fi
  mkdir -p "$ROOT/$key/details"
  n="$(find "$ROOT/$key/details" -type f -name '*.json' | wc -l | tr -d ' ')"
  before=$((before + n))
  src_args+=(--source "$ROOT/$key/log.csv:$ROOT/$key/details")
  keys_json="${keys_json:+$keys_json,}$(json_str "$key")"
  used_keys="${used_keys:+$used_keys }$key"
done

rc=0
after=$before
if [[ ${#src_args[@]} -eq 0 ]]; then
  log "no sources with a log yet; nothing to do"
else
  py tinygs_details_batch.py "${src_args[@]}" \
    --max-per-run "$MAX_PER_RUN" --spacing-s "$SPACING_S" \
    --lockfile "$ROOT/.details_batch.lock" \
    ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} || rc=$?
  after=0
  for key in $used_keys; do
    n="$(find "$ROOT/$key/details" -type f -name '*.json' | wc -l | tr -d ' ')"
    after=$((after + n))
  done
fi

ok=false
[[ $rc -eq 0 ]] && ok=true
write_atomic "$ROOT/status/details.json" <<EOF
{"job":"details","time_utc":"$(utc_iso)","ok":$ok,"exit_code":$rc,"sources":[${keys_json}],"details_before":$before,"details_after":$after,"new_details":$((after - before))}
EOF

log "=== details end rc=$rc new_details=$((after - before))"
summary "rc=$rc new_details=$((after - before))"
exit "$rc"
