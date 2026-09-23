# tinygs-tle-creator

Fits a TLE for **PROVES Electra** (SCID 3) from Doppler data reported by
[TinyGS](https://tinygs.com) ground stations.

PROVES Electra was recently deployed from the ISS. TinyGS currently
propagates it using the **ISS TLE**, which is fine for pass prediction on
day one but drifts steadily as the satellite separates. This repo pulls the
per-station reception data TinyGS records for every packet — most usefully
each station's measured `frequency_error` — and fits small corrections to
the ISS reference orbit so the predicted state actually tracks the real
spacecraft.

## Quickstart

```sh
make all
```

This bootstraps a venv (with Playwright's Chromium), does one fetch pass
against TinyGS, does one rate-limited batch of packet-detail fetches, and
fits a TLE from whatever detail data is on disk. See `make help` for the
individual steps.

```sh
make help
```

## Why Playwright?

TinyGS's public API (`api.tinygs.com`) is behind Cloudflare and effectively
does not respond to plain HTTP clients (curl, requests, etc. — the request
just hangs or gets challenged). The only reliable way found to pull data is
to drive a real (headless) browser to the TinyGS web app and intercept the
JSON responses the page's own JavaScript receives. That's what
`tinygs_fetch.py` and `tinygs_packet_detail.py` do, via Playwright +
headless Chromium.

## Pipeline

```
tinygs_fetch.py  ──► tinygs_packets_latest.json ──► proves_track.py ──► data/log.csv
                                                                              │
                                                            (scans for missing detail files)
                                                                              ▼
                                              tinygs_details_batch.py ──► data/details/<id>.json
                                                                              │
                                                                              ▼
                                                                        fit_tle.py
                                                                              │
                                                                              ▼
                                                         out/surv_proves.tle, fit_report.{txt,json}
```

| script | what |
|---|---|
| `tinygs_tle/tinygs_fetch.py` | loads the TinyGS satellite page and captures the packet list the SPA fetches (`/v4/packets`) |
| `tinygs_tle/proves_parse.py` | decodes the raw LoRa/CCSDS TM frame in each packet (header strip, CRC16, Beacon channel fields) |
| `tinygs_tle/proves_track.py` | dedupes newly-fetched packets into a CSV log (`data/log.csv`), with basic health alerts (reboot, uplink activity, non-beacon frames) |
| `tinygs_tle/tinygs_details_batch.py` | rate-limited (1 page load/min, 25/run) fetch of per-packet detail JSON for log entries that don't have one yet |
| `tinygs_tle/tinygs_packet_detail.py` | fetches one packet's detail JSON (per-station doppler, `frequency_error`, rssi, snr, `usec_time`) |
| `tinygs_tle/fit_tle.py` | the actual orbit-determination fitter — turns `data/details/*.json` into a TLE |

## The Doppler observable

Each TinyGS ground station reports, per received packet, a
`receptionParams.frequency_error` — how far off the received carrier was
from where the station's radio expected it (based on TinyGS's own,
currently-wrong, ISS-based Doppler prediction plus its receiver's own
oscillator offset). `fit_tle.py` treats
`sign × frequency_error = true_doppler(orbit) + station_bias` as the
observable, where:

- `true_doppler(orbit)` comes from propagating a candidate orbit with SGP4
  (TEME) and differencing range to the station (ECEF→TEME via GMST,
  central difference for range-rate — this naturally folds in the
  station's own Earth-rotation velocity).
- `station_bias` is a constant per station. It absorbs that station's
  receiver crystal offset **and** the satellite's own oscillator offset —
  those two are not separable from single-frequency Doppler alone, so no
  separate satellite bias term is carried.
- `sign` (the TinyGS `frequency_error` sign convention) is resolved
  empirically each run by trying both and keeping whichever fits better.

Per-station biases are eliminated analytically (variable projection: for a
given orbit, a station's optimal bias is just the mean residual over its
observations), so the actual nonlinear solve only runs over the orbital
deltas — far more robust than a joint solve over orbit + N station biases.

## Fit parameters and when they become observable

The fitter solves for a small vector of deltas from the ISS reference
orbit — pass `--fit` as a comma list:

| param | meaning | becomes observable when |
|---|---|---|
| `dM` | delta mean anomaly (along-track position) | almost immediately — even a single pass constrains this |
| `dn` | delta mean motion (energy / semi-major axis) | once observations span roughly a day or more; before that it's ~100% degenerate with `dM` (a deployed CubeSat drifting ahead looks identical to "faster mean motion" over a short arc) |
| `dRAAN`, `dinc` | out-of-plane elements | needs several days of data **and** geographic spread across stations (Doppler-curve asymmetry at different latitudes is the only cross-track signal); expect large uncertainties at first |
| `decc`, `dargp` | eccentricity / argument of perigee | needs weeks of arc — leave held at the ISS reference until then |

`fit_tle.py` is honest about this: after fitting, each parameter's
significance is checked against its own 1σ uncertainty (z > 2 required).
Non-significant parameters are refit away entirely — the **published** TLE
only ever carries corrections the data can actually support, and everything
else stays pinned to the ISS reference. The fit report lists fit vs. held
vs. published parameters, their uncertainties, and pairwise correlations
(e.g. `dM`/`dn` correlation, which is large on short arcs).

Outliers are rejected iteratively (3σ, MAD-based, 50 Hz floor), and
under-observed stations (fewer than `--min-station-obs` receptions, default
2) are dropped before fitting — with too few observations a station's bias
term would just absorb all the signal.

## Usage

### `make all`

Runs `setup` → `fetch` → `details` → `tle` in one shot. Useful for a first
run or a fully-hands-off refresh.

### Individual targets

```sh
make setup                        # venv + deps + Playwright Chromium
make fetch                        # one packet-window fetch, appended into data/log.csv
make details                      # one rate-limited batch of per-packet detail JSON
make tle                          # fit out/surv_proves.tle from data/details/*.json
make tle DETAILS_DIR=/path/to/details   # fit from a different details directory
make tle FIT=dM,dn,dRAAN,dinc     # fit more parameters once the data supports it
make fmt                          # pre-commit (ruff, codespell, etc.)
make clean                        # remove venv, data/, out/
```

`SAT` (default `PROVES_Electra`) and `OUT_DIR` (default `out`) are also
overridable the same way.

### Running the scripts directly

Each script is a standalone CLI (`--help` works on all of them):

```sh
python3 tinygs_tle/tinygs_fetch.py --sat PROVES_Electra --out data/tinygs_packets_latest.json
python3 tinygs_tle/proves_track.py data/tinygs_packets_latest.json data/log.csv
python3 tinygs_tle/tinygs_details_batch.py --log data/log.csv --details-dir data/details
python3 tinygs_tle/fit_tle.py --details-dir data/details --out out --fit dM,dn
```

## Data layout

```
data/
  log.csv                  # deduped, decoded packet log (from proves_track.py)
  details/<packet_id>.json # per-packet per-station reception detail
out/
  surv_proves.tle          # latest fitted TLE
  fit_report.txt           # human-readable fit report
  fit_report.json          # machine-readable fit report
```

`data/` and `out/` are gitignored — they're pipeline state, not source.
`examples/` holds a checked-in reference fit (see below) so you can see
what good output looks like without running the pipeline first.

## Continuous operation

On the always-on Mac mini the pipeline runs unattended from three launchd
jobs. They're LaunchDaemons with `UserName`, so they run after a reboot
without anyone logging in. Each job is a thin bash wrapper in `scripts/`,
and you can also run each one by hand through `make`:

| job (label `space.proves.tinygs.*`) | when | wrapper | make |
|---|---|---|---|
| `cycle` | :00 and :30 | `scripts/cycle.sh`: fetch, archive the raw snapshot, then track, for each satellite in turn (20 s apart) | `make cycle` |
| `details` | :15 and :45 | `scripts/details.sh`: per-packet detail batch, at most 25 pages at 1/min | `make details-all` |
| `daily` | 03:00 local | `scripts/daily.sh`: CelesTrak TLE, then a Doppler fit once ≥ 20 new detail files have arrived | `make daily` |

`scripts/merge_import.sh` is a one-off. It merges the July 2026 laptop
archive (rsynced to `<root>/import/laptop-2026-07/`) into `electra/` and
leaves the import directory untouched.

### Satellites

`deploy/satellites.tsv` is tab-separated, with the columns `key slug kind norad f0_hz`:

- `kind=proves` decodes with `proves_track.py`.
  - Electra writes its own log.
  - Any other PROVES satellite routes SCID 3 frames into `electra/log.csv` via `--route`, which catches Electra frames that TinyGS misfiles.
- `kind=raw` logs with `raw_track.py`.
- The wrappers skip a row, with a warning, if its slug starts with `TODO`.
- `norad` and `f0_hz` can be `-`. Such a satellite gets no CelesTrak fetch or TLE fit.

### Data root and the drive guard

All data lives on the external USB NVMe:

```
/Volumes/nvme-1tb-m4/proves/
  .tinygs-data-root                         sentinel (created by install.sh)
  tinygs/                                   DATA_ROOT
    <key>/latest.json                       last fetch (overwritten)
    <key>/raw/YYYY/MM/DD/<key>_<ts>.json.gz permanent raw archive (source of truth)
    <key>/log.csv  lasttlm.csv  details/    decoded log, lastTlm snapshots, per-packet detail
    <key>/tle/celestrak/YYYYMMDD.tle latest.tle   <key>/tle/fit/<ts>/
    alerts.log                              ALERT lines only
    status/{cycle,details,daily}.json       last-run summaries (written atomically)
    logs/{cycle,details,daily}-YYYY-MM.log  full job output
```

`scripts/lib.sh` holds the guard, and every wrapper sources it. Before a
wrapper creates anything, the guard checks three things:

1. `/Volumes/nvme-1tb-m4` is a real mount point.
2. Its APFS Volume UUID is `53A32C48-941F-4BE3-BEA9-9640199F5D52`.
3. The sentinel exists.

If any check fails, the wrapper prints `DRIVE-MISSING: <reason>` plus a USB
diagnostic line to stderr and exits **3**. This stops the archive from
silently landing on the internal disk under a stale `/Volumes/...`
directory. launchd's own stdout and stderr go to the **internal** disk
(`~/Library/Logs/tinygs/<job>.log`). Look there first for `DRIVE-MISSING`
lines and for a one-line summary of each run.

These environment variables override the defaults: `TINYGS_DATA_ROOT`,
`TINYGS_VOLUME`, `TINYGS_VOLUME_UUID`, `TINYGS_SENTINEL`,
`TINYGS_SATS_TSV`, `TINYGS_PYTHON` and `TINYGS_PY_DIR`. For tests only,
`TINYGS_SKIP_UUID_CHECK=1` skips the mount-point and UUID checks, but the
volume directory and the sentinel must still exist.

### Exit codes and status

- **`cycle`** exits 0 when every enabled satellite fetched and tracked. It exits 1 when any of them failed.
  - A failed satellite doesn't stop the others.
  - A tracker exit of 2 means `FETCH-FAILED`: no packets response was captured, for example because of a Cloudflare challenge. `status/cycle.json` records this as `fetch_ok: false`.
  - When the satellite is silent, the run still counts as success, with `new_frames=0`.
- **`daily`** exits 1 if a CelesTrak fetch or a fit failed. A `SKIP:` from `fit_tle.py` (too few observations) is not a failure, and the wrapper removes the empty fit dir.

### Alerts and dead-man switch

`ALERT` lines go only to `<root>/alerts.log`. Alert sinks are deferred.
`notify()` in `scripts/lib.sh` is the single hook point, and it does nothing
unless `NOTIFY_URL` is set. When it is set, `notify()` POSTs the line as
plain text, which is the format ntfy.sh expects. `hc_ping()` pings
`HC_PING_URL`, for example healthchecks.io with a 2 h grace period, after
each cycle in which every fetch succeeded. Put both URLs in `~/tinygs.env`,
which the wrappers source and which is never committed:

```sh
NOTIFY_URL=https://ntfy.sh/<topic>
HC_PING_URL=https://hc-ping.com/<uuid>
```

### Install / uninstall

Prerequisites:

- Run `make setup` as the pipeline user, so the venv and Playwright Chromium live in *that* user's home.
- Run `make launcher` as the pipeline user. It builds `deploy/bin/tinygs-launch`, the small ad-hoc-signed program the daemons run; it runs only `scripts/{cycle,details,daily}.sh`. Grant it Full Disk Access: System Settings → Privacy & Security → Full Disk Access → **+**, press ⌘⇧G, and paste the binary's full path. Without this, TCC blocks writes to the external drive.
- The NVMe is plugged in and mounted.

```sh
sudo deploy/install.sh      # idempotent; re-run after editing deploy/launchd/ or moving the repo
sudo deploy/uninstall.sh    # boot out + remove the daemons; leaves data, logs, pmset alone
```

`install.sh` runs these steps in order:

1. `pmset -a disksleep 0`, **first**. It then verifies the setting and aborts if it didn't take (see Troubleshooting).
2. `pmset -a autorestart 1 sleep 0`.
3. Sets `AutomountDisksWithoutUserLogin`.
4. Creates `~/Library/Logs/tinygs`.
5. If the right volume is mounted, creates the data root and sentinel.
6. Renders `deploy/launchd/*.plist` (the `__USER__`/`__REPO__` placeholders) into `/Library/LaunchDaemons` (root:wheel 644).
7. Boots out and bootstraps each job into `system`, then prints its state.

`deploy/install.sh --render-only DIR` renders the plists without root, for review.

To check on the jobs:

```sh
launchctl print system/space.proves.tinygs.cycle | grep -E 'state|last exit code|runs'
sudo launchctl kickstart -k system/space.proves.tinygs.cycle      # run a cycle now
cat /Volumes/nvme-1tb-m4/proves/tinygs/status/cycle.json
tail -f ~/Library/Logs/tinygs/cycle.log /Volumes/nvme-1tb-m4/proves/tinygs/logs/cycle-$(date -u +%Y-%m).log
pmset -g | grep -E ' (sleep|disksleep|autorestart) '
```

To seed the July archive: rsync the laptop's `proves-pass-data/tinygs/` to
`<root>/import/laptop-2026-07/` (never copy `tgs_auth.json`), then run
`scripts/merge_import.sh`. It is idempotent.

### Troubleshooting

- **`DRIVE-MISSING ... bridge present, media detached - replug required`.**
  - The SABRENT enclosure's Realtek RTL9210 USB-NVMe bridge hangs and drops its disk when macOS sends SCSI START STOP UNIT on idle (disk sleep).
  - The bridge stays enumerated on USB (vendor 0x0bda, product 0x9210) but exposes no media. Only a physical unplug and replug recovers it.
  - Prevention is `disksleep 0`, which `install.sh` sets first and verifies. Check it with `pmset -g | awk '/ disksleep /{print $2}'`, which should print `0`.
  - A macOS update or an Energy settings change can reset it. After replugging, re-run `sudo deploy/install.sh`.
- **`... no Realtek 0x9210 USB-NVMe bridge on the USB bus`.** The enclosure is unplugged or unpowered.
- **`... bridge present with media attached - volume not mounted?`.** Run `diskutil list external`, then `diskutil mountDisk <disk>`.
- **`DRIVE-MISSING: ... Volume UUID ... != expected`.** A different disk is mounted at that path. Don't point the jobs at it. Fix the mount instead.
- **Jobs run but get `Operation not permitted` on `/Volumes/...`.** macOS privacy controls (TCC) block LaunchDaemons from writing to external volumes. Access is attributed to `deploy/bin/tinygs-launch`, so grant Full Disk Access to that binary only, not to `/bin/bash`, then kickstart the job. Rebuilding the launcher (`make launcher`) changes its code hash, so the grant has to be redone afterwards.
- **Every cycle is `FETCH-FAILED`.** Cloudflare is probably challenging headless Chromium. Look at the archived raw snapshot for that run, and run `.venv/bin/python tinygs_tle/tinygs_fetch.py --sat PROVES_Electra --out /tmp/t.json` by hand as the pipeline user.
- **Jobs don't run after a reboot.** Check that the plists are in `/Library/LaunchDaemons` (not `~/Library/LaunchAgents`), and that `launchctl print system/<label>` shows them.

## Current known results

See `examples/fit_report.txt` / `examples/fit_report.json` /
`examples/surv_proves.tle` for a full snapshot. As of that fit (775
observations, 238 stations, 20 passes, ~19.5 h arc):

- **Along-track**: PROVES Electra is running about **4.5 minutes ahead**
  of the ISS TLE's prediction (`dM` significant, along-track offset
  ≈ −272 s).
- **Mean motion**: about **+0.012 rev/day** faster than ISS (`dn`
  significant at the given arc length) — consistent with a deployed
  CubeSat sitting in a very slightly lower/faster orbit than its parent
  station.
- Residual RMS ≈ 91.5 Hz with both `dM` and `dn` fit (vs. 261 Hz for a
  `dM`-only fit), with `dM`/`dn` correlation r ≈ −0.96 (expected — they're
  not yet fully decorrelated at this arc length).
- Out-of-plane elements (`dRAAN`, `dinc`) and eccentricity/argp are still
  held at the ISS reference; not enough arc/geographic spread yet to
  observe them.

Rerun `make tle` periodically as more detail data accumulates — the fit
should keep improving and eventually support fitting `dRAAN`/`dinc` too.

## Caveats

- **NORAD ID 99999** / international designator **26999A** in the output
  TLE are placeholders — PROVES Electra doesn't have official ones yet.
  Epoch, B\*, and `ndot` are copied verbatim from the ISS reference TLE.
- Station positions come from TinyGS's reported lat/lon, altitude assumed
  100 m, WGS84.
- Timing is only as good as station clocks (NTP-quality); sub-second
  errors map to tens of Hz of Doppler error near closest approach.
  Light-time delay (~7 ms) is neglected (<1 Hz effect).
- Station biases are modeled as **constants** over the whole data set;
  crystal drift over many hours will start inflating residual RMS as the
  arc grows (a per-pass bias would be the natural next upgrade if RMS
  climbs past ~100 Hz).
- The TinyGS-reported `doppler`/`distance`/`elevation` fields per station
  are predictions from the **wrong (ISS) TLE** — the fitter never uses
  them, only `frequency_error`; they're fine for sanity-checking by eye.
- **Be gentle with TinyGS.** It's a free community service. `make fetch`
  does a single page load; `make details` is hard-capped at one packet
  detail page per minute, 25 per invocation. Don't loop these tighter than
  that, and don't run `make fetch`/`make details` speculatively — only
  when you actually want fresh data.

## Attribution

Reception data is provided by the [TinyGS](https://tinygs.com) community
ground station network — thank you to every station operator whose receiver
shows up in `data/details/*.json` and the per-station bias table in the fit
reports.
