#!/usr/bin/env bash
# Daily at 03:00 local (launchd space.proves.tinygs.daily). For each enabled
# satellite with a numeric NORAD id:
#   1. CelesTrak TLE -> <root>/<key>/tle/celestrak/YYYYMMDD.tle (+ latest.tle)
#   2. Doppler TLE fit into <root>/<key>/tle/fit/<ts>/ when at least
#      FIT_MIN_NEW (20) detail files arrived since the last fit dir, the
#      satellite has an f0_hz and a reference TLE exists.
#
# Exit: 0 all ok, 1 some step failed, 3 drive missing.
{ # whole script parsed before it runs, so updating it mid-run (git pull) is safe
set -euo pipefail
JOB=daily
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

FIT_MIN_NEW="${TINYGS_FIT_MIN_NEW:-20}"

guard
start_job_log daily
take_lock daily

log "=== daily start root=$ROOT"
run_ts="$(utc_ts)"
day="${run_ts:0:8}"
n_fail=0
sats_json=""

rows="$(sat_rows)"
while IFS=$'\t' read -r key slug _kind norad f0; do
  [[ -n "$key" ]] || continue
  if ! is_int "$norad"; then
    log "skip $key: no numeric NORAD id ($norad)"
    continue
  fi
  dir="$ROOT/$key"
  cdir="$dir/tle/celestrak"
  mkdir -p "$cdir"
  log "--- $key (slug=$slug norad=$norad f0=$f0)"

  # 1. CelesTrak
  tle="$cdir/$day.tle"
  cel_rc=0
  py celestrak_fetch.py --catnr "$norad" --out "$tle" || cel_rc=$?
  if [[ $cel_rc -eq 0 && -s "$tle" ]]; then
    cp -f "$tle" "$cdir/latest.tle.tmp.$$"
    mv -f "$cdir/latest.tle.tmp.$$" "$cdir/latest.tle"
    log "$key celestrak ok -> ${tle#"$ROOT"/}"
  else
    log "CELESTRAK-FAILED $key rc=$cel_rc (keeping previous latest.tle)"
    rm -f "$tle" # may be partial
    n_fail=$((n_fail + 1))
    [[ $cel_rc -ne 0 ]] || cel_rc=1
  fi

  # 2. fit
  fit_status=skipped
  fit_reason=""
  fit_rc=null
  fit_dir_json=null
  new_details=0
  ddir="$dir/details"
  if ! is_int "$f0"; then
    fit_reason="no f0_hz"
  elif [[ ! -s "$cdir/latest.tle" ]]; then
    fit_reason="no reference TLE"
  elif [[ ! -d "$ddir" ]]; then
    fit_reason="no details dir"
  else
    last_fit=""
    if [[ -d "$dir/tle/fit" ]]; then
      last_fit="$(find "$dir/tle/fit" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)"
    fi
    if [[ -n "$last_fit" ]]; then
      new_details="$(find "$ddir" -type f -name '*.json' -newer "$last_fit" | wc -l | tr -d ' ')"
    else
      new_details="$(find "$ddir" -type f -name '*.json' | wc -l | tr -d ' ')"
    fi
    if [[ $new_details -lt $FIT_MIN_NEW ]]; then
      since="${last_fit:-start}"
      fit_reason="only $new_details new detail files since ${since#"$ROOT"/} (< $FIT_MIN_NEW)"
    else
      out="$dir/tle/fit/$(utc_ts)"
      mkdir -p "$dir/tle/fit"
      fit_rc=0
      fit_out="$(py fit_tle.py --details-dir "$ddir" --out "$out" \
        --ref-tle "$cdir/latest.tle" --f0 "$f0" --since-days 14 --min-obs 20 \
        --output-stem "$key" 2>&1)" || fit_rc=$?
      printf '%s\n' "$fit_out" | sed 's/^/  /'
      if [[ $fit_rc -eq 0 ]] && printf '%s\n' "$fit_out" | grep -q '^SKIP:'; then
        fit_status=skipped
        fit_reason="$(printf '%s\n' "$fit_out" | grep -m1 '^SKIP:')"
        rm -rf "$out"
      elif [[ $fit_rc -eq 0 ]]; then
        fit_status=ok
        fit_dir_json="$(json_str "${out#"$ROOT"/}")"
      else
        fit_status=failed
        fit_reason="fit_tle.py rc=$fit_rc"
        n_fail=$((n_fail + 1))
        # drop the dir if the fit left nothing behind, so it doesn't count as "last fit"
        if [[ -d "$out" ]] && [[ -z "$(ls -A "$out")" ]]; then rmdir "$out"; fi
      fi
    fi
  fi
  log "$key fit: $fit_status${fit_reason:+ ($fit_reason)}"
  sats_json="${sats_json:+$sats_json,}$(json_str "$key"):{\"norad\":$norad,\"celestrak_rc\":$cel_rc,\"fit\":$(json_str "$fit_status"),\"fit_reason\":$(json_str "$fit_reason"),\"fit_rc\":$fit_rc,\"fit_dir\":$fit_dir_json,\"new_details\":$new_details}"
done <<<"$rows"

ok=false
[[ $n_fail -eq 0 ]] && ok=true
write_atomic "$ROOT/status/daily.json" <<EOF
{"job":"daily","time_utc":"$(utc_iso)","ok":$ok,"n_failed":$n_fail,"sats":{${sats_json}}}
EOF

log "=== daily end failed=$n_fail"
summary "failed=$n_fail"
[[ $n_fail -eq 0 ]]
exit
}
