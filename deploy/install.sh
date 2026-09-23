#!/usr/bin/env bash
# Install the TinyGS pipeline as three LaunchDaemons (system domain, run as the
# invoking user). Idempotent: re-run after any change to deploy/launchd/ or
# after moving the repo.
#
#   sudo deploy/install.sh                 install / update everything
#   deploy/install.sh --render-only DIR    just render the plists into DIR (no root)
#
# Steps:
#   1. pmset: disksleep 0 FIRST (verified; aborts otherwise), then autorestart 1, sleep 0
#   2. mount external disks at boot without a user login (autodiskmount)
#   3. internal log dir ~/Library/Logs/tinygs owned by the user
#   4. data root + sentinel on the NVMe, only if the right volume is mounted
#   5. render deploy/launchd/*.plist -> /Library/LaunchDaemons (root:wheel 644)
#   6. launchctl bootout + bootstrap each job into `system`, print its state
set -euo pipefail

VOLUME="${TINYGS_VOLUME:-/Volumes/nvme-1tb-m4}"
VOLUME_UUID="${TINYGS_VOLUME_UUID:-53A32C48-941F-4BE3-BEA9-9640199F5D52}"
DATA_ROOT="${TINYGS_DATA_ROOT:-$VOLUME/proves/tinygs}"
SENTINEL="${TINYGS_SENTINEL:-$VOLUME/proves/.tinygs-data-root}"
JOBS="cycle details daily"
LABEL_PREFIX="space.proves.tinygs"
DAEMON_DIR=/Library/LaunchDaemons

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEMPLATES="$REPO/deploy/launchd"

say() { printf '==> %s\n' "$*"; }
die() { printf 'install.sh: ERROR: %s\n' "$*" >&2; exit 1; }

render() { # user outdir
  local user="$1" outdir="$2" job src
  mkdir -p "$outdir"
  for job in $JOBS; do
    src="$TEMPLATES/$LABEL_PREFIX.$job.plist"
    [[ -f "$src" ]] || die "missing template $src"
    sed -e "s|__USER__|$user|g" -e "s|__REPO__|$REPO|g" "$src" >"$outdir/$LABEL_PREFIX.$job.plist"
    /usr/bin/plutil -lint -s "$outdir/$LABEL_PREFIX.$job.plist" >/dev/null \
      || die "rendered $job plist fails plutil -lint"
    if grep -q '__[A-Z]*__' "$outdir/$LABEL_PREFIX.$job.plist"; then
      die "unsubstituted placeholder in $job plist"
    fi
  done
}

if [[ "${1:-}" == "--render-only" ]]; then
  [[ -n "${2:-}" ]] || die "usage: $0 --render-only DIR"
  render "${TINYGS_USER:-$(id -un)}" "$2"
  echo "rendered into $2"
  exit 0
fi

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"
USER_NAME="${SUDO_USER:-}"
[[ -n "$USER_NAME" && "$USER_NAME" != root ]] || die "run via sudo from the account that should own the jobs (SUDO_USER unset/root)"
id "$USER_NAME" >/dev/null 2>&1 || die "no such user $USER_NAME"
USER_HOME="/Users/$USER_NAME"
[[ -d "$USER_HOME" ]] || die "$USER_HOME does not exist (templates assume /Users/<user>)"

# Pre-flight (warnings only).
[[ -x "$REPO/.venv/bin/python" ]] || say "WARNING: $REPO/.venv/bin/python missing - run 'make setup' as $USER_NAME"
ls -d "$USER_HOME"/Library/Caches/ms-playwright/chromium* >/dev/null 2>&1 \
  || say "WARNING: no Playwright Chromium in $USER_HOME/Library/Caches/ms-playwright - run 'make setup' as $USER_NAME"

# 1. Power. disksleep MUST be off before anything else: the SABRENT RTL9210
#    USB-NVMe bridge hangs and detaches when macOS spins it down with SCSI
#    START STOP UNIT on idle, and only a physical replug recovers it.
say "pmset -a disksleep 0"
pmset -a disksleep 0
ds="$(pmset -g | awk '/ disksleep /{print $2}')"
[[ "$ds" == 0 ]] || die "pmset -g reports disksleep='${ds:-?}', expected 0 - aborting (the NVMe enclosure will detach on idle)"
say "pmset -a autorestart 1 sleep 0"
pmset -a autorestart 1 sleep 0

# 2. Mount external disks at boot even with nobody logged in.
say "AutomountDisksWithoutUserLogin = true"
defaults write /Library/Preferences/SystemConfiguration/autodiskmount AutomountDisksWithoutUserLogin -bool true

# 3. Internal log dir (launchd StandardOut/ErrorPath).
LOG_DIR="$USER_HOME/Library/Logs/tinygs"
say "log dir $LOG_DIR"
install -d -o "$USER_NAME" -g staff -m 755 "$LOG_DIR"

# 4. Data root + sentinel, only on the verified volume.
vol_ok=0
if /sbin/mount | grep -qF " on $VOLUME ("; then
  uuid="$(/usr/sbin/diskutil info "$VOLUME" 2>/dev/null | awk -F': *' '/Volume UUID/ {print $2; exit}')"
  if [[ "$uuid" == "$VOLUME_UUID" ]]; then
    vol_ok=1
  else
    say "WARNING: $VOLUME has Volume UUID '${uuid:-?}', expected $VOLUME_UUID - not touching it"
  fi
else
  say "WARNING: $VOLUME is not mounted - data root/sentinel NOT created; jobs will log DRIVE-MISSING until you mount it and re-run this script"
fi
if [[ $vol_ok -eq 1 ]]; then
  say "data root $DATA_ROOT, sentinel $SENTINEL"
  mkdir -p "$DATA_ROOT"
  chown "$USER_NAME:staff" "$(dirname "$SENTINEL")" "$DATA_ROOT"
  if [[ ! -f "$SENTINEL" ]]; then
    printf 'TinyGS pipeline data root marker (volume %s). Created %s by deploy/install.sh.\nDo not delete: the jobs refuse to write without it.\n' \
      "$VOLUME_UUID" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$SENTINEL"
  fi
  chown "$USER_NAME:staff" "$SENTINEL"
  chmod 644 "$SENTINEL"
fi

# 5. Render + install plists. The jobs run deploy/bin/tinygs-launch, which is
#    what needs Full Disk Access (TCC blocks daemons from external volumes).
[[ -x "$REPO/deploy/bin/tinygs-launch" ]] \
  || die "missing $REPO/deploy/bin/tinygs-launch - run 'make launcher' as $USER_NAME first"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
render "$USER_NAME" "$tmp"
for job in $JOBS; do
  install -o root -g wheel -m 644 "$tmp/$LABEL_PREFIX.$job.plist" "$DAEMON_DIR/$LABEL_PREFIX.$job.plist"
done
say "installed plists into $DAEMON_DIR"

# 6. (Re)load.
for job in $JOBS; do
  label="$LABEL_PREFIX.$job"
  plist="$DAEMON_DIR/$label.plist"
  launchctl bootout "system/$label" 2>/dev/null || true
  ok=0
  for _ in 1 2 3 4 5; do
    # bootout is asynchronous; bootstrap can briefly fail with EIO right after it.
    if launchctl bootstrap system "$plist" 2>/dev/null; then ok=1; break; fi
    sleep 1
  done
  [[ $ok -eq 1 ]] || { launchctl bootstrap system "$plist" || die "launchctl bootstrap failed for $label"; }
done

echo
say "state:"
for job in $JOBS; do
  label="$LABEL_PREFIX.$job"
  echo "  $label"
  launchctl print "system/$label" 2>/dev/null \
    | grep -E '^[[:space:]]*(state|last exit code|runs|path|program) =' | sed 's/^[[:space:]]*/    /' || echo "    (not loaded?)"
done
echo
say "pmset: $(pmset -g | awk '/ (sleep|disksleep|autorestart) /{printf "%s=%s ", $1, $2}')"
cat <<EOF

Done. Next steps (as $USER_NAME):
  - optional secrets: $USER_HOME/tinygs.env with NOTIFY_URL=... / HC_PING_URL=...
  - run a cycle now:  sudo launchctl kickstart -k system/$LABEL_PREFIX.cycle
  - watch:            tail -f $LOG_DIR/cycle.log $DATA_ROOT/logs/cycle-\$(date -u +%Y-%m).log
EOF
