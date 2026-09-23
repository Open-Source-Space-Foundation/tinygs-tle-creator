# Handoff: run the TinyGS → PROVES Electra pipeline continuously on a dedicated Mac Mini

Written 2026-09-22 from a survey of the July 2026 work on Michael's laptop. Audience: the agent (or human) setting this up on the always-on **Mac Mini**. Paths below that point at `~/GitHut/proves-pass-data` refer to the *laptop*; that data has to be copied over (§4 step 2).

> **Status (2026-09-22): implemented.** The deployed design differs from the plan below: LaunchDaemons `space.proves.tinygs.{cycle,details,daily}`, data on `/Volumes/nvme-1tb-m4/proves/tinygs/`, three satellites, and Electra is NORAD 69795. See the README section "Continuous operation" for the current setup; this document is kept as history.

## 1. Goal

Poll TinyGS on a schedule for **PROVES Electra** (SCID 3) downlink packets, keep a permanent local archive (raw JSON plus decoded CSV plus per-station reception details), and raise alerts on interesting spacecraft events. Today this only runs when someone runs `make` by hand. It should run unattended.

## 2. What exists today (inventory)

### Code: this repo, `Open-Source-Space-Foundation/tinygs-tle-creator`

Two commits, both dated 2026-07-08. Local `main` matches `origin/main`, and nothing is uncommitted.

| file | role | status |
|---|---|---|
| `tinygs_tle/tinygs_fetch.py` | Headless Playwright Chromium loads `app.tinygs.com/satellite/<SAT>` and intercepts the SPA's XHR JSON (`/v4/packets`, `/v3/satellite/<SAT>`, `/v3/satellite/stats/<SAT>`) | works (as of July) |
| `tinygs_tle/proves_parse.py` | Strips the 4-byte LoRa header, then decodes the CCSDS TM frame, checks CRC16, and decodes the Beacon (pktid 1) channels | works, no bit-slip handling (see §5) |
| `tinygs_tle/proves_track.py` | Dedupes by TinyGS packet `id`, appends to a CSV, and prints `ALERT ...` lines for reboot, uplink activity, auth sequence number > 0, and non-beacon frames | works, alerts go to stdout only |
| `tinygs_tle/tinygs_details_batch.py` | Fetches per-packet detail JSON for log rows that don't have one yet, at 1 page/min and at most 25 per run, newest first. A lockfile (stale after 40 min) prevents overlapping runs | works |
| `tinygs_tle/tinygs_packet_detail.py` | Fetches one packet's detail (per-station `frequency_error`, rssi, snr, `usec_time`) | works |
| `tinygs_tle/fit_tle.py` | Doppler orbit fit against the ISS reference TLE | see §5, probably obsolete |
| `Makefile` | `setup` (uv venv + deps + Playwright Chromium), `fetch`, `details`, `tle`, `all` | works |

**Why Playwright:** every dynamic `api.tinygs.com` endpoint silently stalls for curl, requests, WebFetch, and even curl_cffi with Chrome impersonation, because of Cloudflare gating. Only a real browser works. Use `wait_until="domcontentloaded"` and never `networkidle`: the SSE stream `api.tinygs.com/sse/events` keeps the page from ever going idle.

### Data: `~/GitHut/proves-pass-data/tinygs/` (not a git repo, 31 MB)

| path | content |
|---|---|
| `tinygs_packets_<UTC ts>.json` ×177 | Raw fetch snapshots from 2026-07-08T05:19Z to 2026-07-11T19:24Z, taken about every 30 min. Each holds the `/v4/packets` window, which is **only the latest 50 packets**, plus the `/v3/satellite` and `/stats` responses |
| `tinygs_log.csv` | 380 decoded rows. 335 are packet rows (July 8–11). 45 are `lasttlm-<epoch>` rows written by a **variant of `proves_track.py` that was never committed**; it also ingested `/v3/satellite/<SAT>` `lastTlm` |
| `details/*.json` ×338 | Per-packet, per-station reception detail |
| `tle/` | Interim TLE fits, plus `norad69799.tle`. **Correction (2026-09-22):** 69799 is the wrong object. The team identified Electra as **NORAD 69795** by surveying candidate TLEs with directional ground stations, and TinyGS tracks 69795. As of epoch 26265, CelesTrak also names 69795 "PROVES-ELECTRA" (98067YK) |
| `proves_electra.ksy`, `surv_proves.ksy`, `tinygs_ksy_README.md`, `ksc_out2/` | Kaitai decoder submitted to TinyGS |
| `diagnosis.md` | July commanding-failure analysis. Not pipeline-related |
| **`tgs_auth.json`** | **Playwright `storage_state` (TinyGS login cookies). Secret. Nothing in the committed code uses it.** Don't copy it to the new machine or commit it unless an authenticated fetch turns out to be needed |

### Lost pieces (lived only in the Claude session scratchpad `…/1aa1463a-…/scratchpad/`, now gone)

- `tinygs_cycle.sh`: the wrapper that ran fetch → timestamped snapshot → track → details.
- The scheduler: a **Claude-session cron** every 30 min. It died when that session ended (last snapshot 2026-07-11T19:24Z). **This is why the pipeline stopped.** There is no system crontab or launchd job.
- The `lasttlm` variant of `proves_track.py`.
- `deslip.py` (from the Aug 30 session): the bit-slip repair described in §5.

## 3. Gaps between "make targets" and "unattended service"

1. **No raw archive in the committed flow.** `make fetch` overwrites `data/tinygs_packets_latest.json` on every run. The July wrapper kept timestamped copies, and those copies are the real source of truth, because the CSV is lossy (it keeps only 12 beacon fields). The wrapper must save them.
2. **No scheduler.** Needs launchd jobs (below).
3. **Silent failure modes.**
   - If Cloudflare starts challenging headless Chromium, `tinygs_fetch.py` still exits 0 with no `packets?` key. `proves_track.py` then raises `StopIteration`, and that exception is the only signal.
   - When the satellite is silent, the run looks exactly like a working one (`new_frames=0`).
   - Add a dead-man check (see §4, step 6) that fires only on *fetch* failure, not on "no new packets".
4. **Alerts only go to stdout.** They need a sink: email, a Discord/Slack webhook, or ntfy.
5. **The 50-packet window.** At a 30 s beacon cadence, 50 packets cover more than one pass, so polling every 30 min loses nothing. Don't poll less often than about every 2 h.
6. **Parse robustness.** Rows with `crc_ok=False` are still logged with garbage beacon fields. Downstream analysis must filter on `crc_ok`.

## 4. Deployment plan (macOS / Mac Mini)

1. **Host prep**
   - The Mac must never sleep. Go to System Settings → Energy, turn on "Prevent automatic sleeping when the display is off" and "Start up automatically after a power failure". Or run `sudo pmset -a sleep 0 disksleep 0 autorestart 1`, then check with `pmset -g`.
   - LaunchAgents only run while their user is logged in. Either enable automatic login for the account that runs the pipeline, or use a LaunchDaemon with `UserName` (step 4, alternative). The daemon is the more robust choice for a headless box.
   - Install Xcode CLT (`xcode-select --install`, which provides `make` and `git`) and uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or `brew install uv`).

2. **Code and historical data**
   ```sh
   git clone https://github.com/Open-Source-Space-Foundation/tinygs-tle-creator.git ~/tinygs-tle-creator
   cd ~/tinygs-tle-creator && make setup      # venv + deps + Playwright Chromium (no install-deps needed on macOS)
   make fetch                                 # smoke test: expect "captured 3 responses", "new_frames=N"
   ```
   Seed the data, from the laptop or via AirDrop/USB:
   ```sh
   SRC=~/GitHut/proves-pass-data/tinygs
   DST=<user>@<mac-mini>.local:tinygs-tle-creator/data
   rsync -av $SRC/tinygs_packets_2026*.json $DST/raw/
   rsync -av $SRC/details/                  $DST/details/
   rsync -av $SRC/tinygs_log.csv            $DST/log.csv
   ```
   The 45 `lasttlm-*` rows are harmless to `proves_track.py`, which dedupes by id and takes the latest BootCount by time. Strip them first (`grep -v '^lasttlm-'`) if you want the log to be packets-only. Do **not** copy `tgs_auth.json`. If you run `make fetch` before seeding, it's fine: the log dedupes by packet id.

3. **Wrapper script**: commit it as `scripts/cycle.sh` and run `chmod +x`. Draft, not yet run:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   cd "$(dirname "$0")/.."
   ts=$(date -u +%Y%m%dT%H%M%SZ)
   mkdir -p data/raw
   make fetch 2>&1 | tee -a data/track.log > data/last_fetch.out   # fetch + track -> data/log.csv
   snap=data/raw/tinygs_packets_${ts}.json
   cp data/tinygs_packets_latest.json "$snap" && gzip "$snap"
   # healthy = the packets response was actually captured
   python3 -c 'import json,sys; d=json.load(open("data/tinygs_packets_latest.json")); sys.exit(0 if any("packets?" in k for k in d) else 1)'
   if grep -q '^ALERT' data/last_fetch.out && [ -n "${NOTIFY_URL:-}" ]; then
     grep '^ALERT' data/last_fetch.out | curl -fsS -m 10 --data-binary @- "$NOTIFY_URL" >/dev/null || true   # e.g. https://ntfy.sh/<topic>
   fi
   if [ -n "${HC_PING_URL:-}" ]; then curl -fsS -m 10 "$HC_PING_URL" >/dev/null || true; fi
   ```
   Secrets (`HC_PING_URL`, `NOTIFY_URL`) go in `~/tinygs.env`, which is never committed. The plist loads them through a `bash -c 'set -a; . ~/tinygs.env; exec scripts/cycle.sh'` wrapper, or through `EnvironmentVariables`.

4. **launchd jobs**: commit templates under `deploy/launchd/`, then install them to `~/Library/LaunchAgents/`.
   ```xml
   <!-- space.proves.tinygs.fetch.plist -->
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0"><dict>
     <key>Label</key><string>space.proves.tinygs.fetch</string>
     <key>WorkingDirectory</key><string>/Users/USER/tinygs-tle-creator</string>
     <key>ProgramArguments</key><array>
       <string>/bin/bash</string><string>-c</string>
       <string>set -a; [ -f ~/tinygs.env ] &amp;&amp; . ~/tinygs.env; set +a; exec scripts/cycle.sh</string>
     </array>
     <key>EnvironmentVariables</key><dict>
       <key>PATH</key><string>/Users/USER/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
     </dict>
     <key>StartCalendarInterval</key><array>
       <dict><key>Minute</key><integer>0</integer></dict>
       <dict><key>Minute</key><integer>30</integer></dict>
     </array>
     <key>StandardOutPath</key><string>/Users/USER/tinygs-tle-creator/data/launchd-fetch.log</string>
     <key>StandardErrorPath</key><string>/Users/USER/tinygs-tle-creator/data/launchd-fetch.log</string>
   </dict></plist>
   ```
   `space.proves.tinygs.details.plist` is identical except for three things: the label, `ProgramArguments` set to `/usr/bin/make details`, and `Minute` values **15 and 45**. The details job takes about 25 min at its cap. launchd never starts a second copy of a job that is still running, and the script's lockfile also guards against overlap.
   ```sh
   cp deploy/launchd/*.plist ~/Library/LaunchAgents/     # after substituting USER
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/space.proves.tinygs.fetch.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/space.proves.tinygs.details.plist
   launchctl kickstart -k gui/$(id -u)/space.proves.tinygs.fetch   # run once now
   launchctl print gui/$(id -u)/space.proves.tinygs.fetch | grep -E 'state|last exit'
   ```
   **Alternative (no auto-login):** put the same plists in `/Library/LaunchDaemons/`, owned by `root:wheel` with mode 644. Add `<key>UserName</key><string>USER</string>`, and use `sudo launchctl bootstrap system <plist>`. In that case `~` in the bash string resolves via `HOME`, so set `HOME` in `EnvironmentVariables`.

   Gotchas:
   - launchd doesn't read shell profiles, so `PATH` must include wherever `uv` lives (`which uv`).
   - If the Mac was asleep at a scheduled minute, launchd runs the job once on wake. Missed runs are coalesced, not replayed.
   - Playwright's Chromium lives in `~/Library/Caches/ms-playwright`. It must be installed by the **same user** the job runs as.

5. **Be gentle with TinyGS.** Fetch uses 1 page load per 30 min. Details uses at most 25 page loads per 30 min at 1/min. That is the July load, and nobody complained. Don't tighten either.

6. **Monitoring and backup**
   - Dead-man switch: put `HC_PING_URL` (healthchecks.io) in `~/tinygs.env` with a 2 h grace period. It pings only after a run with a real `packets?` capture.
   - Backup: Time Machine to an external disk or NAS covers `~/tinygs-tle-creator/data/`. It grows about 10 MB/day gzipped while the bird is active, and far less when it's silent. Add an off-box copy (restic or rclone to cloud) if the data matters.

## 5. Things to decide or know before calling it done

- **TLE fitting is probably obsolete.** It exists because TinyGS propagated Electra with the ISS TLE (NORAD 99999). Electra is **NORAD 69795** (see the correction in §2; 69799 was a misidentification), and the committed repo's `fit_tle.py` still defaults to the ISS reference. My recommendation: don't schedule `make tle`; keep details collection because it's cheap and scientifically useful (per-station rssi/snr/frequency error). Check whether the TinyGS packet `norad` field has changed from 99999.
- **Misattribution and bit-slip.** On 2026-08-10, TinyGS filed Electra frames under **Alcyone**. They carried one spurious leading bit, and after de-slipping them (drop the first bit, re-pack, CRC16-CCITT over `frame[:246]`) they decoded as SCID 3, BootCount 21. The pipeline only polls `SAT=PROVES_Electra` and doesn't de-slip, so it would miss these. Options: add a second timer with `SAT=<Alcyone slug>`, and add de-slip fallback to `proves_parse.parse_frame` (try as-is, and on CRC failure retry shifted by one bit). Before storing anything, attribute frames by the **de-slipped** SCID. A slipped frame falsely reads "SCID 1".
- **Is Electra still transmitting?** The last decoded frames are from 2026-08-10. Its 72 h command-loss reboot cadence was predicting continued beacons. The first week of the service will answer this. If it stays silent, the useful part is the dead-man alert, not the data.
- **Datastore format.** CSV plus gzipped raw JSON is enough for now. If analysis grows, load `data/raw/*.json.gz` into SQLite or DuckDB instead of widening the CSV: the raw snapshots have every field, the CSV doesn't.
- **Commit, don't scratchpad.** The July pipeline died because the wrapper and scheduler lived in an ephemeral session. `scripts/cycle.sh`, the launchd plists (`deploy/launchd/`), and any de-slip or `lastTlm` changes belong in this repo.

## 6. Acceptance checklist

- [ ] `launchctl print gui/$(id -u)/space.proves.tinygs.fetch` (or `system/...`) shows the job loaded with `last exit code = 0`
- [ ] After 1 h: at least 2 new `data/raw/*.json.gz` files, and `data/launchd-fetch.log` shows `captured 3 responses`
- [ ] `data/log.csv` row count doesn't shrink across runs (append-only)
- [ ] `data/details/` count grows while new packet ids are arriving
- [ ] `pmset -g` shows `sleep 0`, and `autorestart 1` is set
- [ ] Dead-man check tested: `launchctl bootout` the fetch job, confirm the alert fires after the grace period, then re-bootstrap
- [ ] Alert path tested: append a fake higher-BootCount row, then check that `ALERT REBOOT` reaches `NOTIFY_URL` (then remove the row)
- [ ] Reboot the Mac Mini, and confirm the jobs come back without manual login (or with auto-login, if using LaunchAgents)
- [ ] Time Machine (or other backup) has captured `data/` at least once
