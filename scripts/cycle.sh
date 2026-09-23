#!/usr/bin/env bash
# Every 30 min (launchd space.proves.tinygs.cycle, :00/:30): for each enabled
# satellite in deploy/satellites.tsv, fetch the TinyGS packet window, archive a
# gzipped raw snapshot, and run the tracker that appends new frames to log.csv.
#
#   <root>/<key>/latest.json                        last fetch (overwritten)
#   <root>/<key>/raw/YYYY/MM/DD/<key>_<ts>.json.gz  permanent raw archive
#   <root>/<key>/log.csv                            decoded/deduped log
#   <root>/status/cycle.json                        last run summary
#   <root>/logs/cycle-YYYY-MM.log                   run output
#   <root>/alerts.log                               ALERT lines only
#
# STALE-FEED: TinyGS sometimes serves a frozen packet list (newest packet many
# hours older than the satellite's lastPacketTime) while the satellite is still
# being heard. That looks exactly like a quiet satellite (new_frames=0), so the
# trackers print `feed_lag_h=` and a lag over TINYGS_STALE_H counts as failure.
#
# Exit: 0 all ok, 1 some satellite failed (fetch, track, or stale feed),
#       3 drive missing.
{ # whole script parsed before it runs, so updating it mid-run (git pull) is safe
set -euo pipefail
JOB=cycle
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

SAT_SPACING_S="${TINYGS_SAT_SPACING_S:-20}"

guard
start_job_log cycle
take_lock cycle

run_ts="$(utc_ts)"
log "=== cycle start ($run_ts) root=$ROOT"

sats_json=""
n_ok=0
n_fail=0
n_fetch_fail=0
n_stale=0
first=1

add_sat_json() { # key json-object
  sats_json="${sats_json:+$sats_json,}$(json_str "$1"):$2"
}

# Skipped (TODO) rows go into the status too, so they're visible.
while IFS=$'\t' read -r key slug _ || [[ -n "${key:-}" ]]; do
  case "$key" in '' | '#'* | key) continue ;; esac
  if [[ "$slug" == TODO* ]]; then
    add_sat_json "$key" "{\"skipped\":true,\"reason\":$(json_str "slug not set: $slug")}"
  fi
done <"$TINYGS_SATS_TSV"

rows="$(sat_rows)"
while IFS=$'\t' read -r key slug kind _norad _f0; do
  [[ -n "$key" ]] || continue
  if [[ $first -eq 0 && "$SAT_SPACING_S" -gt 0 ]]; then sleep "$SAT_SPACING_S"; fi
  first=0

  dir="$ROOT/$key"
  latest="$dir/latest.json"
  mkdir -p "$dir"
  log "--- $key (slug=$slug kind=$kind)"

  # 1. fetch (remove the previous window first so a failed fetch can't be
  #    mistaken for a fresh one and re-archived)
  rm -f "$latest"
  fetch_rc=0
  py tinygs_fetch.py --sat "$slug" --out "$latest" ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} || fetch_rc=$?
  if [[ $fetch_rc -ne 0 || ! -s "$latest" ]]; then
    log "FETCH-FAILED $key: tinygs_fetch.py rc=$fetch_rc, output ${latest}$([[ -s "$latest" ]] || echo ' missing/empty')"
    n_fail=$((n_fail + 1))
    n_fetch_fail=$((n_fetch_fail + 1))
    add_sat_json "$key" "{\"slug\":$(json_str "$slug"),\"ok\":false,\"fetch_ok\":false,\"fetch_rc\":$fetch_rc,\"track_rc\":null,\"new_frames\":null,\"snapshot\":null}"
    continue
  fi

  # 2. archive (even if the tracker later rejects it: a Cloudflare challenge
  #    page is worth keeping for diagnosis)
  ts="$(utc_ts)"
  day_dir="$dir/raw/${ts:0:4}/${ts:4:2}/${ts:6:2}"
  snap="$day_dir/${key}_${ts}.json.gz"
  [[ ! -e "$snap" ]] || snap="$day_dir/${key}_${ts}_$$.json.gz" # same-second rerun
  mkdir -p "$day_dir"
  gzip -c "$latest" >"$snap.tmp.$$"
  mv -f "$snap.tmp.$$" "$snap"
  log "archived ${snap#"$ROOT"/}"

  # 3. track
  track_args=()
  case "$kind" in
    proves)
      track_script=proves_track.py
      track_args=(--source-sat "$slug" --lasttlm-csv "$dir/lasttlm.csv" --alerts-file "$ALERTS_LOG")
      if [[ "$key" != electra ]]; then
        # Electra frames (SCID 3) that TinyGS files under another satellite get
        # routed into Electra's log.
        track_args=(--source-sat "$slug" --route "3=$ROOT/electra/log.csv"
          --lasttlm-csv "$dir/lasttlm.csv" --alerts-file "$ALERTS_LOG")
      fi
      ;;
    raw)
      track_script=raw_track.py
      track_args=(--source-sat "$slug")
      ;;
    *)
      log "ERROR $key: unknown kind '$kind' in $TINYGS_SATS_TSV"
      n_fail=$((n_fail + 1))
      add_sat_json "$key" "{\"slug\":$(json_str "$slug"),\"ok\":false,\"error\":$(json_str "unknown kind $kind")}"
      continue
      ;;
  esac

  track_rc=0
  out="$(py "$track_script" "$latest" "$dir/log.csv" "${track_args[@]}" 2>&1)" || track_rc=$?
  # ALERT lines go only to alerts.log (proves_track writes them there itself via
  # --alerts-file; raw_track doesn't, so the wrapper records them) + notify().
  n_alerts=0
  while IFS= read -r line; do
    if [[ "$line" == ALERT* ]]; then
      n_alerts=$((n_alerts + 1))
      if [[ "$kind" == proves ]]; then notify "$key: $line"; else alert_line "$key: $line"; fi
    else
      printf '  %s\n' "$line"
    fi
  done <<<"$out"
  [[ $n_alerts -eq 0 ]] || log "$key: $n_alerts ALERT line(s) -> alerts.log"

  new_frames="$(printf '%s\n' "$out" | sed -n 's/.*new_frames=\([0-9][0-9]*\).*/\1/p' | tail -1)"
  [[ -n "$new_frames" ]] || new_frames=null
  feed_lag_h="$(printf '%s\n' "$out" | sed -n 's/.*feed_lag_h=\([0-9.]*\).*/\1/p' | tail -1)"
  auth="$(printf '%s\n' "$out" | sed -n 's/.*feed_lag_h=[^ ]* auth=\([a-z]*\).*/\1/p' | tail -1)"
  stale=false
  if [[ -n "$feed_lag_h" ]] && awk -v l="$feed_lag_h" -v t="$TINYGS_STALE_H" 'BEGIN{exit !(l > t)}'; then
    stale=true
  fi
  if [[ ${#AUTH_ARGS[@]} -gt 0 && "$auth" == false ]]; then
    alert_line "$key: AUTH-NOT-SENT: $TINYGS_AUTH_STATE is set but the packets request carried no session token"
  fi
  [[ -n "$feed_lag_h" ]] || feed_lag_h=null
  [[ -n "$auth" && "$auth" != na ]] || auth=null

  fetch_ok=true
  ok=false
  if [[ $track_rc -eq 0 && $stale == true ]]; then
    n_fail=$((n_fail + 1))
    n_stale=$((n_stale + 1))
    log "STALE-FEED $key: newest listed packet is ${feed_lag_h} h behind lastPacketTime (auth=$auth)"
    alert_line "$key: STALE-FEED: TinyGS packet list ${feed_lag_h} h behind lastPacketTime (auth=$auth)"
  elif [[ $track_rc -eq 0 ]]; then
    ok=true
    n_ok=$((n_ok + 1))
    log "$key ok new_frames=$new_frames feed_lag_h=$feed_lag_h auth=$auth"
  elif [[ $track_rc -eq 2 ]]; then
    fetch_ok=false
    n_fail=$((n_fail + 1))
    n_fetch_fail=$((n_fetch_fail + 1))
    log "FETCH-FAILED $key: $track_script rc=2 (no packets response captured)"
  else
    n_fail=$((n_fail + 1))
    log "TRACK-FAILED $key: $track_script rc=$track_rc"
  fi
  add_sat_json "$key" "{\"slug\":$(json_str "$slug"),\"ok\":$ok,\"fetch_ok\":$fetch_ok,\"fetch_rc\":$fetch_rc,\"track_rc\":$track_rc,\"new_frames\":$new_frames,\"feed_lag_h\":$feed_lag_h,\"auth\":$auth,\"stale\":$stale,\"alerts\":$n_alerts,\"snapshot\":$(json_str "${snap#"$ROOT"/}")}"
done <<<"$rows"

all_ok=false
[[ $n_fail -eq 0 && $n_ok -gt 0 ]] && all_ok=true

write_atomic "$ROOT/status/cycle.json" <<EOF
{"job":"cycle","time_utc":"$(utc_iso)","run_ts":"$run_ts","ok":$all_ok,"n_ok":$n_ok,"n_failed":$n_fail,"n_fetch_failed":$n_fetch_fail,"n_stale":$n_stale,"sats":{${sats_json}}}
EOF

# Dead-man switch: ping only if every enabled satellite's fetch worked and
# none of the packet lists is stale.
if [[ $n_fetch_fail -eq 0 && $n_stale -eq 0 && $n_ok -gt 0 ]]; then hc_ping; fi

log "=== cycle end ok=$n_ok failed=$n_fail (fetch_failed=$n_fetch_fail stale=$n_stale)"
summary "ok=$n_ok failed=$n_fail fetch_failed=$n_fetch_fail stale=$n_stale"
[[ $n_fail -eq 0 ]]
exit
}
