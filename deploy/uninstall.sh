#!/usr/bin/env bash
# Stop and remove the TinyGS LaunchDaemons installed by deploy/install.sh.
#   sudo deploy/uninstall.sh
# Leaves data (NVMe), logs (~/Library/Logs/tinygs), the sentinel, and the
# pmset / autodiskmount settings untouched; the script prints how to revert them.
set -euo pipefail

JOBS="cycle details daily"
LABEL_PREFIX="space.proves.tinygs"
DAEMON_DIR=/Library/LaunchDaemons

die() { printf 'uninstall.sh: ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"
[[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]] || die "run via sudo from the owning account (SUDO_USER unset/root)"

for job in $JOBS; do
  label="$LABEL_PREFIX.$job"
  if launchctl print "system/$label" >/dev/null 2>&1; then
    launchctl bootout "system/$label" && echo "==> booted out $label"
  else
    echo "==> $label not loaded"
  fi
  if [[ -f "$DAEMON_DIR/$label.plist" ]]; then
    rm -f "$DAEMON_DIR/$label.plist" && echo "==> removed $DAEMON_DIR/$label.plist"
  fi
done

cat <<'MSG'

Left in place (remove/revert by hand if wanted):
  data:      /Volumes/nvme-1tb-m4/proves/tinygs  and sentinel /Volumes/nvme-1tb-m4/proves/.tinygs-data-root
  logs:      ~/Library/Logs/tinygs
  power:     sudo pmset -a sleep <min> disksleep <min> autorestart 0
             (keep disksleep 0 while the SABRENT/RTL9210 NVMe enclosure is attached)
  automount: sudo defaults delete /Library/Preferences/SystemConfiguration/autodiskmount AutomountDisksWithoutUserLogin
MSG
