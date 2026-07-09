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
