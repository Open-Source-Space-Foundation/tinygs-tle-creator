#!/usr/bin/env python3
"""Doppler-based orbit determination for Surv-PROVES from TinyGS reception data.

Fits corrections to a reference TLE (default: the ISS TLE that TinyGS is
currently using for this satellite) using per-station measured frequency
errors as Doppler observables. Solves per-station oscillator biases jointly
with a small set of orbital-element deltas, and honestly reports which
parameters the data can actually constrain.

Usage:
    fit_tle.py --details-dir /path/to/details --out /path/to/outdir
               [--fit dM,dn] [--ref-tle file] [--min-station-obs 2]

Outputs (in --out):
    surv_proves.tle   candidate TLE (NORAD 99999, intl designator 26999A)
    fit_report.txt    human-readable fit report
    fit_report.json   machine-readable fit report
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from sgp4.api import WGS72, Satrec, jday

C_LIGHT = 299792.458  # km/s
F0 = 437.4e6  # Hz nominal downlink

# Reference TLE: ISS, which TinyGS currently uses to propagate Surv-PROVES.
REF_TLE_NAME = "ISS (ZARYA)"
REF_TLE_L1 = "1 25544U 98067A   26188.50835634  .00005806  00000-0  11369-3 0  9991"
REF_TLE_L2 = "2 25544  51.6304 199.5144 0006687 267.6545  92.3678 15.48933372574901"

# Orbital delta parameters (units used internally):
#   dM    delta mean anomaly           [rad]
#   dn    delta mean motion            [rad/min]
#   dRAAN delta RAAN                   [rad]
#   dinc  delta inclination            [rad]
#   decc  delta eccentricity           [-]
#   dargp delta argument of perigee    [rad]
ORBIT_PARAMS = ["dM", "dn", "dRAAN", "dinc", "decc", "dargp"]
PARAM_SCALE = {
    "dM": 0.05,
    "dn": 1e-5,
    "dRAAN": 0.01,
    "dinc": 0.005,
    "decc": 1e-4,
    "dargp": 0.05,
}


# ----------------------------------------------------------------- geometry


def gmst_rad(jd_ut1):
    """Greenwich mean sidereal time (IAU-82), radians."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    g = (
        67310.54841
        + (876600.0 * 3600 + 8640184.812866) * t
        + 0.093104 * t * t
        - 6.2e-6 * t**3
    )  # seconds
    return math.radians((g % 86400.0) / 240.0) % (2 * math.pi)


def geodetic_to_ecef(lat_deg, lon_deg, alt_km):
    """WGS84 geodetic to ECEF, km."""
    a = 6378.137
    f = 1.0 / 298.257223563
    e2 = f * (2 - f)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x = (n + alt_km) * math.cos(lat) * math.cos(lon)
    y = (n + alt_km) * math.cos(lat) * math.sin(lon)
    z = (n * (1 - e2) + alt_km) * math.sin(lat)
    return np.array([x, y, z])


def ecef_to_teme(r_ecef, jd_ut1):
    """Rotate ECEF (≈PEF) into TEME: r_TEME = R3(-gmst) r_PEF."""
    th = gmst_rad(jd_ut1)
    c, s = math.cos(th), math.sin(th)
    x, y, z = r_ecef
    return np.array([c * x - s * y, s * x + c * y, z])


# ----------------------------------------------------------- SGP4 machinery


def parse_ref(l1, l2):
    sat = Satrec.twoline2rv(l1, l2)
    return sat


def build_satrec(ref, deltas):
    """Build a Satrec from the reference with orbital-element deltas applied."""
    sat = Satrec()
    epoch_days = ref.jdsatepoch + ref.jdsatepochF - 2433281.5
    sat.sgp4init(
        WGS72,
        "i",
        ref.satnum,
        epoch_days,
        ref.bstar,
        ref.ndot,
        ref.nddot,
        max(1e-8, ref.ecco + deltas.get("decc", 0.0)),
        (ref.argpo + deltas.get("dargp", 0.0)) % (2 * math.pi),
        ref.inclo + deltas.get("dinc", 0.0),
        (ref.mo + deltas.get("dM", 0.0)) % (2 * math.pi),
        ref.no_kozai + deltas.get("dn", 0.0),
        (ref.nodeo + deltas.get("dRAAN", 0.0)) % (2 * math.pi),
    )
    return sat


def predicted_doppler_hz(sat, obs, dt=0.5):
    """Predicted received frequency error (Hz) due to Doppler for each obs.

    obs: list of dicts with jd, fr, r_ecef. Central-difference range rate,
    including Earth-rotation velocity of the station via GMST at t±dt.
    """
    out = np.empty(len(obs))
    dfr = dt / 86400.0
    for i, o in enumerate(obs):
        rng = []
        for sgn in (-1.0, 1.0):
            jd, fr = o["jd"], o["fr"] + sgn * dfr
            err, r, v = sat.sgp4(jd, fr)
            if err != 0:
                out[i] = np.nan
                break
            r_sta = ecef_to_teme(o["r_ecef"], jd + fr)
            rng.append(np.linalg.norm(np.asarray(r) - r_sta))
        else:
            rdot = (rng[1] - rng[0]) / (2 * dt)  # km/s
            out[i] = -rdot / C_LIGHT * F0
    return out


# ------------------------------------------------------------- data loading


def load_observations(details_dir):
    obs = []
    seen = set()
    files = sorted(Path(details_dir).glob("*.json"))
    for f in files:
        try:
            doc = json.load(open(f))
        except (json.JSONDecodeError, OSError):
            continue
        # files wrap the packet under its URL key; tolerate both layouts
        pkt = (
            next(iter(doc.values()))
            if len(doc) == 1 and "serverTime" not in doc
            else doc
        )
        if "serverTime" not in pkt:
            continue
        pkt_id = pkt.get("id", f.stem)
        t_pkt = pkt["serverTime"] / 1000.0
        for s in pkt.get("stations", []):
            name = s.get("name")
            fe = (s.get("receptionParams") or {}).get("frequency_error")
            loc = s.get("location")
            if name is None or fe is None or not loc:
                continue
            key = (pkt_id, name)
            if key in seen:
                continue
            seen.add(key)
            # Prefer usec_time when it agrees with server time to ~5 s;
            # station clocks are NTP-ish, so distrust big disagreements.
            t = t_pkt
            ut = s.get("usec_time")
            if ut and abs(ut / 1e6 - t_pkt) < 5.0:
                t = ut / 1e6
            dtobj = datetime.fromtimestamp(t, tz=timezone.utc)
            jd, fr = jday(
                dtobj.year,
                dtobj.month,
                dtobj.day,
                dtobj.hour,
                dtobj.minute,
                dtobj.second + dtobj.microsecond / 1e6,
            )
            obs.append(
                {
                    "t": t,
                    "jd": jd,
                    "fr": fr,
                    "station": name,
                    "lat": loc[0],
                    "lon": loc[1],
                    "r_ecef": geodetic_to_ecef(loc[0], loc[1], 0.1),  # assume 100 m
                    "fe": float(fe),
                    "packet": pkt_id,
                    "tinygs_doppler": s.get("doppler"),
                }
            )
    return obs, len(files)


def count_passes(obs, gap_s=600):
    ts = sorted(o["t"] for o in obs)
    if not ts:
        return 0
    n = 1
    for a, b in zip(ts, ts[1:]):
        if b - a > gap_s:
            n += 1
    return n


# ------------------------------------------------------------------ solver
#
# Per-station biases are eliminated analytically (variable projection):
# for a given orbit, the optimal constant bias per station is the mean of
# (sign*fe - predicted doppler) over that station's observations. The
# nonlinear solve then runs over the orbital deltas only, which is far more
# robust than a joint solve.

# Physical bounds on deltas (the satellite is ISS-like by construction):
PARAM_BOUNDS = {
    "dM": (-math.pi, math.pi),  # along-track: anywhere in phase
    "dn": (-4.4e-4, 4.4e-4),  # +/- ~0.1 rev/day
    "dRAAN": (-0.09, 0.09),  # +/- ~5 deg
    "dinc": (-0.035, 0.035),  # +/- ~2 deg
    "decc": (-6e-4, 3e-3),
    "dargp": (-math.pi, math.pi),
}


def _demean_residual(pred, obs, sidx, n_sta, fe_signed):
    """fe - pred with the per-station mean removed (optimal constant biases)."""
    d = fe_signed - pred
    sums = np.zeros(n_sta)
    cnts = np.zeros(n_sta)
    np.add.at(sums, sidx, d)
    np.add.at(cnts, sidx, 1)
    return d - (sums / np.maximum(cnts, 1))[sidx]


def make_reduced_residual(ref, obs, fit_params, stations, sign):
    sta_index = {s: i for i, s in enumerate(stations)}
    sidx = np.array([sta_index[o["station"]] for o in obs])
    fe = np.array([o["fe"] for o in obs]) * sign

    def resid(x):
        deltas = dict(zip(fit_params, x))
        pred = predicted_doppler_hz(build_satrec(ref, deltas), obs)
        if np.isnan(pred).any():
            return np.full(len(obs), 1e6)
        return _demean_residual(pred, obs, sidx, len(stations), fe)

    return resid, sidx, fe


def grid_search_dM(ref, obs, stations, sign, n_grid=180):
    """Coarse 1-D scan of dM (dn=0) to find the along-track basin."""
    fn, _, _ = make_reduced_residual(ref, obs, ["dM"], stations, sign)
    best = (np.inf, 0.0)
    for dm in np.linspace(-math.pi, math.pi, n_grid, endpoint=False):
        c = float(np.sum(fn([dm]) ** 2))
        if c < best[0]:
            best = (c, dm)
    return best  # (cost, dM)


def fit(ref, obs, fit_params, sign, dm0=None):
    """Bounded least-squares fit of orbital deltas; biases eliminated."""
    stations = sorted({o["station"] for o in obs})
    if dm0 is None and "dM" in fit_params:
        _, dm0 = grid_search_dM(ref, obs, stations, sign)
    x0 = np.array([dm0 if p == "dM" and dm0 is not None else 0.0 for p in fit_params])
    lo = np.array([PARAM_BOUNDS[p][0] for p in fit_params])
    hi = np.array([PARAM_BOUNDS[p][1] for p in fit_params])
    if "dM" in fit_params and dm0 is not None:
        i = fit_params.index("dM")
        lo[i], hi[i] = dm0 - 0.35, dm0 + 0.35  # stay in the grid basin
    x0 = np.clip(x0, lo, hi)
    scale = np.array([PARAM_SCALE[p] for p in fit_params])
    fn, sidx, fe = make_reduced_residual(ref, obs, fit_params, stations, sign)
    res = least_squares(
        fn,
        x0,
        bounds=(lo, hi),
        x_scale=scale,
        method="trf",
        diff_step=1e-3,
        max_nfev=500,
    )
    # recover biases
    pred = predicted_doppler_hz(build_satrec(ref, dict(zip(fit_params, res.x))), obs)
    biases = {}
    for i, s in enumerate(stations):
        sel = sidx == i
        biases[s] = float(np.mean(fe[sel] - pred[sel]))
    return res, stations, biases


def sigma_clip_fit(
    ref, obs, fit_params, sign, nsigma=3.0, max_iter=4, min_station_obs=2
):
    """Iterative fit with MAD-based outlier rejection.

    After each rejection round, stations that drop below min_station_obs
    observations are removed entirely (their bias would absorb everything).
    """
    work = list(obs)
    rejected = []
    res = stations = biases = None
    dm0 = None
    for _ in range(max_iter):
        # drop under-observed stations
        while True:
            counts = {}
            for o in work:
                counts[o["station"]] = counts.get(o["station"], 0) + 1
            drop = {s for s, c in counts.items() if c < min_station_obs}
            if not drop:
                break
            rejected += [
                (o, "station has <%d obs" % min_station_obs)
                for o in work
                if o["station"] in drop
            ]
            work = [o for o in work if o["station"] not in drop]
        if len(work) < len(fit_params) + 2:
            raise SystemExit(
                "Not enough observations to fit after filtering "
                f"({len(work)} left). Need more data."
            )
        res, stations, biases = fit(ref, work, fit_params, sign, dm0=dm0)
        if "dM" in fit_params:
            dm0 = res.x[fit_params.index("dM")]  # reuse basin next round
        r = res.fun
        mad = np.median(np.abs(r - np.median(r)))
        sig = max(1.4826 * mad, 50.0)  # floor: don't clip below 50 Hz scatter
        bad = np.abs(r - np.median(r)) > nsigma * sig
        if not bad.any():
            break
        rejected += [
            (o, f"residual {r[i]:+.0f} Hz > {nsigma} sigma")
            for i, (o, b) in enumerate(zip(work, bad))
            if b
        ]
        work = [o for o, b in zip(work, bad) if not b]
    return res, stations, biases, work, rejected


def covariance(res, n_sta):
    """Orbit-parameter covariance from the reduced-problem Jacobian.

    DOF accounts for the analytically eliminated per-station biases.
    """
    J = res.jac
    dof = max(1, J.shape[0] - J.shape[1] - n_sta)
    s2 = 2 * res.cost / dof
    try:
        cov = np.linalg.inv(J.T @ J) * s2
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(J.T @ J) * s2
    return cov


# ---------------------------------------------------------------- TLE output


def tle_checksum(line):
    s = 0
    for ch in line:
        if ch.isdigit():
            s += int(ch)
        elif ch == "-":
            s += 1
    return s % 10


def make_tle(ref_l1, ref_l2, ref, deltas, satnum=99999, intldes="26999A"):
    """Emit a TLE at the reference epoch with deltas applied.

    ndot/nddot/bstar/epoch fields are copied verbatim from the reference line 1.
    """
    l1 = f"1 {satnum:05d}U {intldes:<8s} " + ref_l1[18:64] + " 999"
    l1 = l1[:68] + str(tle_checksum(l1[:68]))
    incl = math.degrees(ref.inclo + deltas.get("dinc", 0.0))
    raan = math.degrees(ref.nodeo + deltas.get("dRAAN", 0.0)) % 360.0
    ecc = max(0.0, ref.ecco + deltas.get("decc", 0.0))
    argp = math.degrees(ref.argpo + deltas.get("dargp", 0.0)) % 360.0
    mo = math.degrees(ref.mo + deltas.get("dM", 0.0)) % 360.0
    n_revday = (ref.no_kozai + deltas.get("dn", 0.0)) * 1440.0 / (2 * math.pi)
    revnum = int(ref_l2[63:68])
    l2 = (
        f"2 {satnum:05d} {incl:8.4f} {raan:8.4f} {int(round(ecc * 1e7)):07d} "
        f"{argp:8.4f} {mo:8.4f} {n_revday:11.8f}{revnum:5d}"
    )
    l2 = l2[:68] + str(tle_checksum(l2[:68]))
    assert len(l1) == 69 and len(l2) == 69, (len(l1), len(l2))
    return l1, l2


# -------------------------------------------------------------------- main

REF = None  # set in main; used by sigma_clip_fit


def main():
    global REF
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--details-dir", required=True)
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument(
        "--fit",
        default="dM,dn",
        help="comma list of orbital deltas to fit "
        f"(subset of {','.join(ORBIT_PARAMS)}); default dM,dn",
    )
    ap.add_argument(
        "--ref-tle",
        default=None,
        help="file with 2- or 3-line reference TLE (default: built-in ISS)",
    )
    ap.add_argument("--min-station-obs", type=int, default=2)
    ap.add_argument("--name", default="SURV-PROVES (EST)")
    args = ap.parse_args()

    if args.ref_tle:
        lines = [line.strip() for line in open(args.ref_tle) if line.strip()]
        l1 = next(line for line in lines if line.startswith("1 "))
        l2 = next(line for line in lines if line.startswith("2 "))
    else:
        l1, l2 = REF_TLE_L1, REF_TLE_L2
    REF = ref = parse_ref(l1, l2)

    fit_params = [p.strip() for p in args.fit.split(",") if p.strip()]
    for p in fit_params:
        if p not in ORBIT_PARAMS:
            sys.exit(f"unknown fit parameter {p!r}; choose from {ORBIT_PARAMS}")

    obs, nfiles = load_observations(args.details_dir)
    if not obs:
        sys.exit(f"no observations found in {args.details_dir}")
    n_pass_all = count_passes(obs)
    print(
        f"Loaded {len(obs)} observations from {nfiles} packet files, "
        f"{len({o['station'] for o in obs})} stations, {n_pass_all} passes"
    )

    # --- resolve the sign convention of frequency_error empirically:
    # fit obs = sign*fe; pick the sign whose along-track scan fits better.
    all_stations = sorted({o["station"] for o in obs})
    cost_pos, dm_pos = grid_search_dM(ref, obs, all_stations, +1)
    cost_neg, dm_neg = grid_search_dM(ref, obs, all_stations, -1)
    sign = +1 if cost_pos <= cost_neg else -1
    print(
        f"sign scan: +1 cost {cost_pos:.3e} (dM {math.degrees(dm_pos):+.1f} deg), "
        f"-1 cost {cost_neg:.3e} (dM {math.degrees(dm_neg):+.1f} deg) "
        f"-> using sign {sign:+d}"
    )

    # --- primary fit
    res, stations, biases, used, rejected = sigma_clip_fit(
        ref, obs, fit_params, sign, min_station_obs=args.min_station_obs
    )
    n_orb = len(fit_params)
    deltas = dict(zip(fit_params, res.x))
    rms = float(np.sqrt(np.mean(res.fun**2)))
    cov = covariance(res, len(stations))
    sig = np.sqrt(np.abs(np.diag(cov)))
    corr = cov / np.outer(
        np.sqrt(np.abs(np.diag(cov))) + 1e-300, np.sqrt(np.abs(np.diag(cov))) + 1e-300
    )

    # --- comparison fit: dM only (the first observable to emerge)
    rms_dM = None
    if fit_params != ["dM"]:
        try:
            res1, _, _, u1, _ = sigma_clip_fit(
                ref, obs, ["dM"], sign, min_station_obs=args.min_station_obs
            )
            rms_dM = float(np.sqrt(np.mean(res1.fun**2)))
        except SystemExit:
            pass

    # significance / observability per orbital parameter
    signif = {}
    for i, p in enumerate(fit_params):
        z = abs(res.x[i]) / sig[i] if sig[i] > 0 else 0.0
        signif[p] = {
            "value": res.x[i],
            "sigma": float(sig[i]),
            "z": float(z),
            "significant": bool(z > 2.0),
        }

    # Publish only what the data supports: refit with the significant
    # parameters alone so the TLE does not carry noise-level deltas.
    pub_params = [p for p in fit_params if signif[p]["significant"]]
    if not pub_params and "dM" in fit_params:
        pub_params = ["dM"]  # along-track is the primary observable
    if pub_params != fit_params:
        res_pub, sta_pub, bias_pub, used_pub, _ = sigma_clip_fit(
            ref, obs, pub_params, sign, min_station_obs=args.min_station_obs
        )
        pub_deltas = dict(zip(pub_params, res_pub.x))
        pub_rms = float(np.sqrt(np.mean(res_pub.fun**2)))
    else:
        pub_deltas = dict(deltas)
        pub_rms = rms

    # --- reference-frame numbers for the report (published solution)
    n0_revday = ref.no_kozai * 1440.0 / (2 * math.pi)
    period_min = 1440.0 / n0_revday
    dM_deg = math.degrees(pub_deltas.get("dM", 0.0))
    along_track_s = -dM_deg / 360.0 * period_min * 60.0  # + = sat arrives later
    dn_revday = pub_deltas.get("dn", 0.0) * 1440.0 / (2 * math.pi)

    tle1, tle2 = make_tle(l1, l2, ref, pub_deltas)

    # --- round-trip validation: parse output TLE, compare predicted doppler
    sat_out = Satrec.twoline2rv(tle1, tle2)
    sat_fit = build_satrec(ref, pub_deltas)
    pd_out = predicted_doppler_hz(sat_out, used)
    pd_fit = predicted_doppler_hz(sat_fit, used)
    roundtrip_max_hz = float(np.nanmax(np.abs(pd_out - pd_fit)))

    # --- write outputs
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "surv_proves.tle").write_text(f"{args.name}\n{tle1}\n{tle2}\n")

    per_station = {}
    for s in stations:
        n = sum(1 for o in used if o["station"] == s)
        per_station[s] = {"bias_hz": round(biases[s], 1), "n_obs": n}

    tmin = datetime.fromtimestamp(min(o["t"] for o in used), tz=timezone.utc)
    tmax = datetime.fromtimestamp(max(o["t"] for o in used), tz=timezone.utc)

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "n_packet_files": nfiles,
        "n_obs_loaded": len(obs),
        "n_obs_used": len(used),
        "n_obs_rejected": len(rejected),
        "n_stations_used": len(stations),
        "n_passes_used": count_passes(used),
        "data_span_utc": [tmin.isoformat(), tmax.isoformat()],
        "observable_sign": sign,
        "residual_rms_hz": round(rms, 1),
        "residual_rms_hz_dM_only": round(rms_dM, 1) if rms_dM else None,
        "parameters_fit": {
            p: {
                "value": signif[p]["value"],
                "sigma": signif[p]["sigma"],
                "significant_2sigma": signif[p]["significant"],
            }
            for p in fit_params
        },
        "parameters_held_at_reference": [
            p for p in ORBIT_PARAMS if p not in fit_params
        ],
        "published_parameters": pub_params,
        "published_deltas": {p: pub_deltas[p] for p in pub_params},
        "published_rms_hz": round(pub_rms, 1),
        "rejection_breakdown": {
            "under_observed_station": sum(
                1 for _, why in rejected if "station has" in why
            ),
            "residual_outlier": sum(1 for _, why in rejected if "residual" in why),
        },
        "derived": {
            "dM_deg": round(dM_deg, 4),
            "along_track_time_offset_s": round(along_track_s, 2),
            "dn_rev_per_day": round(dn_revday, 8),
            "mean_motion_rev_per_day": round(n0_revday + dn_revday, 8),
        },
        "station_biases_hz": per_station,
        "satellite_bias_note": (
            "Satellite oscillator bias is NOT separable from station biases "
            "with this data; each station bias includes the common satellite "
            "offset. Biases are modeled as constants per station."
        ),
        "correlation_orbit_params": corr[:n_orb, :n_orb].round(3).tolist(),
        "roundtrip_tle_max_doppler_diff_hz": round(roundtrip_max_hz, 2),
        "tle": [tle1, tle2],
        "reference_tle": [l1, l2],
        "caveats": [],
    }

    caveats = report["caveats"]
    if count_passes(used) < 3:
        caveats.append(
            "Fewer than 3 passes used; mean motion (dn) is weakly "
            "constrained and mostly reflects along-track drift over "
            "the short data arc, not an independent SMA measurement."
        )
    for p in fit_params:
        if not signif[p]["significant"]:
            caveats.append(
                f"Parameter {p} is NOT significant at 2 sigma "
                f"(value {signif[p]['value']:.3e} +/- "
                f"{signif[p]['sigma']:.3e}); treat as held-at-zero."
            )
    if abs(corr[0, min(1, n_orb - 1)]) > 0.95 and n_orb > 1:
        caveats.append(
            f"{fit_params[0]} and {fit_params[1]} are highly "
            f"correlated (r={corr[0, 1]:.3f}); only their "
            "combination is observed."
        )
    caveats.append(
        "Out-of-plane elements (inclination, RAAN) and "
        "eccentricity/argp held at ISS values; single-frequency "
        "Doppler from few passes cannot separate them yet."
    )
    caveats.append(
        "Station altitudes assumed 100 m; timing from station "
        "clocks (NTP quality). Sub-second timing errors map to "
        "tens of Hz of Doppler error near TCA."
    )

    (outdir / "fit_report.json").write_text(json.dumps(report, indent=2))

    # human-readable report
    lines_txt = []
    ap_ = lines_txt.append
    ap_("Surv-PROVES Doppler orbit fit")
    ap_("=" * 60)
    ap_(f"generated:      {report['generated_utc']}")
    ap_(f"data span:      {tmin.isoformat()}  ->  {tmax.isoformat()}")
    ap_(
        f"observations:   {len(used)} used / {len(obs)} loaded "
        f"({len(rejected)} rejected)"
    )
    ap_(f"stations:       {len(stations)}   passes: {count_passes(used)}")
    ap_(
        f"residual RMS:   {rms:.1f} Hz"
        + (f"   (dM-only fit: {rms_dM:.1f} Hz)" if rms_dM else "")
    )
    ap_("")
    ap_("Parameters FIT (deltas from ISS reference):")
    for p in fit_params:
        v, s_, z = signif[p]["value"], signif[p]["sigma"], signif[p]["z"]
        flag = "" if signif[p]["significant"] else "   ** NOT significant **"
        ap_(f"  {p:6s} = {v:+.6e} +/- {s_:.2e}  (z={z:.1f}){flag}")
    ap_(
        f"Parameters HELD at ISS values: "
        f"{', '.join(report['parameters_held_at_reference'])}"
    )
    ap_(
        f"Published TLE uses: {', '.join(pub_params)} "
        f"(non-significant params held at 0; RMS {pub_rms:.1f} Hz)"
    )
    ap_(
        f"Rejections: {report['rejection_breakdown']['under_observed_station']}"
        f" from under-observed stations, "
        f"{report['rejection_breakdown']['residual_outlier']} residual outliers"
    )
    ap_("")
    ap_(
        f"along-track offset: {along_track_s:+.1f} s "
        f"({dM_deg:+.3f} deg mean anomaly; positive = arrives later than ISS)"
    )
    ap_(
        f"mean motion:        {n0_revday + dn_revday:.8f} rev/day "
        f"(ISS {n0_revday:.8f}, delta {dn_revday:+.8f})"
    )
    ap_("")
    ap_("Per-station biases (Hz, includes common satellite oscillator offset):")
    for s in stations:
        ap_(
            f"  {s:28s} {per_station[s]['bias_hz']:+9.1f}  "
            f"(n={per_station[s]['n_obs']})"
        )
    ap_("")
    ap_("TLE (NORAD 99999 / 26999A placeholder):")
    ap_("  " + tle1)
    ap_("  " + tle2)
    ap_(f"round-trip TLE vs fitted state: max doppler diff {roundtrip_max_hz:.2f} Hz")
    ap_("")
    ap_("Caveats:")
    for c_ in caveats:
        ap_("  - " + c_)
    ap_("")
    (outdir / "fit_report.txt").write_text("\n".join(lines_txt))
    print("\n".join(lines_txt))
    print(f"Wrote {outdir}/surv_proves.tle, fit_report.txt, fit_report.json")


if __name__ == "__main__":
    main()
