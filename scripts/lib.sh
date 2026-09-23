# shellcheck shell=bash
# Shared helpers for the unattended TinyGS wrappers (cycle.sh, details.sh,
# daily.sh, merge_import.sh). Source it; don't execute it.
#
# Must stay compatible with macOS /bin/bash 3.2 (launchd runs the wrappers
# with it): no associative arrays, no mapfile, no ${x,,}, and guard empty
# array expansions under `set -u`.
#
# Environment overrides (all optional):
#   TINYGS_VOLUME           mount point of the data volume   (/Volumes/nvme-1tb-m4)
#   TINYGS_VOLUME_UUID      expected APFS Volume UUID        (53A32C48-...)
#   TINYGS_DATA_ROOT        data root on that volume          ($TINYGS_VOLUME/proves/tinygs)
#   TINYGS_SENTINEL         file that must exist on the volume ($TINYGS_VOLUME/proves/.tinygs-data-root)
#   TINYGS_SKIP_UUID_CHECK  =1 skips the mount-point and UUID checks (tests only;
#                           the volume dir and sentinel must still exist)
#   TINYGS_SATS_TSV         satellite table                   (deploy/satellites.tsv)
#   TINYGS_PYTHON           interpreter                       (.venv/bin/python)
#   TINYGS_PY_DIR           directory holding the pipeline scripts (tinygs_tle)
#   TINYGS_ENV_FILE         secrets file sourced if present   ($HOME/tinygs.env)
#   NOTIFY_URL, HC_PING_URL see notify() / hc_ping() below

# --- repo root, PATH, defaults ----------------------------------------------
TINYGS_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$TINYGS_REPO" || exit 1

export PATH="/Users/ossf-1/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ -n "${HOME:-}" && "$HOME" != "/Users/ossf-1" ]]; then
  PATH="$HOME/.local/bin:$PATH"
fi
export TZ=UTC
export LC_ALL=C

# Secrets (NOTIFY_URL, HC_PING_URL) live outside the repo and are never committed.
TINYGS_ENV_FILE="${TINYGS_ENV_FILE:-${HOME:-/nonexistent}/tinygs.env}"
if [[ -f "$TINYGS_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$TINYGS_ENV_FILE"
  set +a
fi

TINYGS_VOLUME="${TINYGS_VOLUME:-/Volumes/nvme-1tb-m4}"
TINYGS_VOLUME_UUID="${TINYGS_VOLUME_UUID:-53A32C48-941F-4BE3-BEA9-9640199F5D52}"
TINYGS_DATA_ROOT="${TINYGS_DATA_ROOT:-$TINYGS_VOLUME/proves/tinygs}"
TINYGS_SENTINEL="${TINYGS_SENTINEL:-$TINYGS_VOLUME/proves/.tinygs-data-root}"
TINYGS_SKIP_UUID_CHECK="${TINYGS_SKIP_UUID_CHECK:-0}"
TINYGS_SATS_TSV="${TINYGS_SATS_TSV:-$TINYGS_REPO/deploy/satellites.tsv}"
TINYGS_PYTHON="${TINYGS_PYTHON:-$TINYGS_REPO/.venv/bin/python}"
TINYGS_PY_DIR="${TINYGS_PY_DIR:-$TINYGS_REPO/tinygs_tle}"
ROOT="$TINYGS_DATA_ROOT"
ALERTS_LOG="$ROOT/alerts.log"

# Make `set -e` deaths visible in the job log instead of silent.
trap 'printf "%s [%s] ERROR: command failed (rc=%s) at %s:%s: %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${JOB:-tinygs}" "$?" "${BASH_SOURCE[0]##*/}" "$LINENO" "$BASH_COMMAND" >&2' ERR

# --- time & logging ---------------------------------------------------------
utc_ts() { date -u +%Y%m%dT%H%M%SZ; }       # filename-safe
utc_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }  # human / JSON
log() { printf '%s [%s] %s\n' "$(utc_iso)" "${JOB:-tinygs}" "$*"; }
warn() { printf '%s [%s] WARN %s\n' "$(utc_iso)" "${JOB:-tinygs}" "$*" >&2; }

# --- alert / heartbeat hook points -----------------------------------------
# notify MESSAGE...
#   HOOK POINT for alert sinks (deferred). No-op unless NOTIFY_URL is set.
#   With NOTIFY_URL set it POSTs the message as the request body, which works
#   for ntfy.sh topics (https://ntfy.sh/<topic>) and similar plain-text hooks.
#   Swap in Slack/Discord JSON, email, etc. here; callers don't need to change.
#   Never fails the caller.
notify() {
  [[ -n "${NOTIFY_URL:-}" ]] || return 0
  printf '%s\n' "$*" | curl -fsS -m 10 --data-binary @- "$NOTIFY_URL" >/dev/null 2>&1 || true
}

# hc_ping [SUFFIX]
#   Dead-man-switch hook (e.g. healthchecks.io). No-op unless HC_PING_URL is set.
#   cycle.sh pings only when every enabled satellite's fetch succeeded, so a
#   silent satellite still pings but a Cloudflare-blocked fetch does not.
hc_ping() {
  [[ -n "${HC_PING_URL:-}" ]] || return 0
  curl -fsS -m 10 --retry 2 "${HC_PING_URL}${1:-}" >/dev/null 2>&1 || true
}

# alert_line LINE — record one ALERT line in alerts.log and hand it to notify().
alert_line() {
  printf '%s %s\n' "$(utc_iso)" "$*" >>"$ALERTS_LOG"
  notify "$*"
}

# --- drive guard ------------------------------------------------------------
# Refuses (exit 3, message on stderr, nothing created) unless the data volume
# is really mounted, is the expected volume, and carries the sentinel file.
# This prevents writing the archive onto the internal disk under a stale
# /Volumes/<name> directory when the USB drive is missing.
drive_missing() {
  local diag
  diag="$(usb_bridge_diag)"
  printf '%s [%s] DRIVE-MISSING: %s\n' "$(utc_iso)" "${JOB:-tinygs}" "$*" >&2
  printf '%s [%s] DRIVE-MISSING diag: %s\n' "$(utc_iso)" "${JOB:-tinygs}" "$diag" >&2
  # Can't use alerts.log (it lives on the missing drive); network hook only.
  notify "tinygs ${JOB:-}: DRIVE-MISSING: $* ($diag)"
  exit 3
}

# usb_bridge_diag — one-line hint about the SABRENT enclosure's Realtek RTL9210
# USB-NVMe bridge (vendor 0x0bda, product 0x9210). Called only on the failure
# path. The RTL9210 is known to hang and drop its media when macOS sends SCSI
# START STOP UNIT on idle (disksleep): the bridge then stays enumerated on the
# USB bus but exposes no disk, and only a physical replug recovers it.
# TINYGS_USB_PROFILE_CMD overrides the profiler command (tests).
usb_bridge_diag() {
  local cmd="${TINYGS_USB_PROFILE_CMD:-/usr/sbin/system_profiler SPUSBDataType}"
  # shellcheck disable=SC2086
  $cmd 2>/dev/null | awk '
    /Product ID: 0x9210/ { inblk = 1; bridge = 1; next }
    inblk && (/Host Controller Driver:/ || /Bus:$/) { inblk = 0 }
    inblk && (/BSD Name:/ || /Media:/) { media = 1 }
    END {
      if (!bridge) print "no Realtek 0x9210 USB-NVMe bridge on the USB bus (enclosure unplugged or unpowered)"
      else if (!media) print "bridge present, media detached - replug required"
      else print "bridge present with media attached - volume not mounted? try: diskutil list external; diskutil mountDisk <disk>"
    }' || echo "USB diagnostic unavailable"
}

guard() {
  local vol="$TINYGS_VOLUME" uuid
  [[ -d "$vol" ]] || drive_missing "$vol does not exist"
  if [[ "$TINYGS_SKIP_UUID_CHECK" != 1 ]]; then
    /sbin/mount | grep -qF " on $vol (" \
      || drive_missing "$vol is not a mount point"
    uuid="$(/usr/sbin/diskutil info "$vol" 2>/dev/null \
      | awk -F': *' '/Volume UUID/ {print $2; exit}')"
    [[ "$uuid" == "$TINYGS_VOLUME_UUID" ]] \
      || drive_missing "$vol Volume UUID '${uuid:-?}' != expected $TINYGS_VOLUME_UUID"
  fi
  [[ -f "$TINYGS_SENTINEL" ]] || drive_missing "sentinel $TINYGS_SENTINEL missing"
  # Only now is it safe to create anything under the volume.
  mkdir -p "$ROOT/status" "$ROOT/logs"
}

# --- job log ----------------------------------------------------------------
# start_job_log NAME — after guard(): send all further output to
# $ROOT/logs/NAME-YYYY-MM.log (also to the terminal when interactive). The
# original stdout stays open on fd 3 for a one-line summary, which is what
# lands in the launchd log on the internal disk.
start_job_log() {
  local f
  f="$ROOT/logs/$1-$(date -u +%Y-%m).log"
  exec 3>&1
  if [[ -t 1 ]]; then
    exec > >(tee -a "$f") 2>&1
  else
    exec >>"$f" 2>&1
  fi
}
summary() { printf '%s [%s] %s\n' "$(utc_iso)" "${JOB:-tinygs}" "$*" >&3; }

# --- single-instance lock -----------------------------------------------------
# take_lock NAME — mkdir-based lock under $ROOT; a lock whose PID is gone is
# treated as stale. Exits 0 (skip) if another live run holds it.
take_lock() {
  local d="$ROOT/.$1.lock" pid
  if ! mkdir "$d" 2>/dev/null; then
    pid="$(cat "$d/pid" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "another $1 run (pid $pid) holds $d; skipping"
      exit 0
    fi
    warn "removing stale lock $d (pid ${pid:-?})"
    rm -rf "$d"
    mkdir "$d"
  fi
  echo $$ >"$d/pid"
  TINYGS_LOCK_DIR="$d"
  trap 'rm -rf "$TINYGS_LOCK_DIR"' EXIT
}

# --- satellites table ---------------------------------------------------------
# sat_rows — print enabled rows of satellites.tsv as TAB-separated
# "key slug kind norad f0" lines. Comments/blank/header rows are ignored;
# rows whose slug starts with TODO are skipped with a warning on stderr.
sat_rows() {
  local key slug kind norad f0
  while IFS=$'\t' read -r key slug kind norad f0 || [[ -n "${key:-}" ]]; do
    case "$key" in '' | '#'* | key) continue ;; esac
    if [[ "$slug" == TODO* ]]; then
      warn "skipping $key: slug not set ($slug) in $TINYGS_SATS_TSV"
      continue
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$key" "$slug" "$kind" "${norad:--}" "${f0:--}"
  done <"$TINYGS_SATS_TSV"
}

# sat_field KEY COLUMN(2..5) — look up one field for KEY (empty if absent/TODO).
sat_field() {
  sat_rows 2>/dev/null | awk -F'\t' -v k="$1" -v c="$2" '$1 == k {print $c; exit}'
}

is_int() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }

# py SCRIPT ARGS... — run a pipeline script with the venv interpreter.
py() {
  local s="$1"
  shift
  "$TINYGS_PYTHON" "$TINYGS_PY_DIR/$s" "$@" </dev/null
}

# --- status JSON ---------------------------------------------------------------
json_str() {
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  printf '"%s"' "$s"
}

# write_atomic PATH — write stdin to PATH via a temp file + rename.
write_atomic() {
  local tmp="$1.tmp.$$"
  cat >"$tmp"
  mv -f "$tmp" "$1"
}
