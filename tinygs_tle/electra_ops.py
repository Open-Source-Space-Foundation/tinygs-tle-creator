#!/usr/bin/env python3
"""TinyGS scraper side of the LA station <-> scraper repo-as-bus protocol.

Runs in a Claude Code cloud session. Scrapes TinyGS for PROVES Electra only,
predicts LA passes, watches `ops/uplinks/` on proves-electra-ops `main`, and
writes results into a proves-electra-ops checkout:

  ops/scraper/STATUS.md           heartbeat, latest Beacon, ALERT block
  ops/scraper/la-passes.json      LA passes for the next 48 h (horizon 0 deg)
  (raw snapshots and detail files go to the tinygs-archive branch via scripts/cloud.sh save)
  passes/<pass>/tinygs/           summary.json, receptions.csv, frames.csv, README.md

Subcommands:
  run        the loop: poll every 10 min, scrape on schedule, analyze, commit, push
  predict    refresh la-passes.json
  scrape     one TinyGS fetch now (+ a background detail batch)
  analyze    rebuild STATUS.md and pass results from the data on disk
  tick       one loop iteration

Scrape schedule (conservative; every scrape is one TinyGS page load):
  - every BASE_CADENCE_MIN (45 min)
  - for every LA pass: at LOS+15 min and again one orbit later
  - during an armed uplink window (AOS-95 min .. latest until+2 h): every 20 min
  - 20 min after a scrape whose 50-packet window doesn't reach back to the
    previous scrape (frames may have been missed)
Per-station details (RSSI/SNR/station names): up to DETAILS_PER_RUN page loads
per scrape, 1/min, packets in pass/uplink windows first.
"""

import argparse
import csv
import datetime as dt
import glob
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
PDT = ZoneInfo("America/Los_Angeles")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.environ.get("TINYGS_CLOUD_ROOT", f"{REPO}/data/cloud/proves/tinygs")
SAT_DIR = f"{ROOT}/electra"
ARCHIVE_CLONE = os.path.expanduser(os.environ.get("TINYGS_ARCHIVE_CLONE", "~/.cache/tinygs-archive"))
OPS = os.environ.get("ELECTRA_OPS_REPO", os.path.join(os.path.dirname(REPO), "proves-electra-ops"))
OPS_BRANCH = os.environ.get("ELECTRA_OPS_BRANCH", "claude/sleepy-einstein-sm86vv")
UPLINK_BRANCH = os.environ.get("ELECTRA_UPLINK_BRANCH", "main")
STATE = f"{ROOT}/electra_ops_state.json"
AUTH = os.environ.get("TINYGS_AUTH_STATE", os.path.expanduser("~/.config/tinygs/auth.json"))
PY = f"{REPO}/.venv/bin/python"
FEED = "https://api.tinygs.com/v4/packets?satellite=PROVES_Electra"

NORAD = 69795
STATION = dict(lat=34.0047840, lon=-118.3376408, alt_m=200.0)
BASE_CADENCE_MIN = 45
UPLINK_CADENCE_MIN = 20
MARKER_CADENCE_MIN = 8  # inside fast-Beacon marker windows only
GAP_RETRY_MIN = 20
POLL_MIN = 10
ORBIT_MIN = 95
DETAILS_PER_RUN = 15
HEARTBEAT_H = 6

# Alert thresholds from the handoff
BOOT_BASE, MODE_OK, V_MIN = 38, 2, 9.5
V_BATT_MIN = 7.4  # 2S Li-ion, about 3.7 V per cell

FIELDS = {  # handoff short name -> our key
    "CurrentSequenceNumber": "CurrentSequenceNumber",
    "authenticatelora.CurrentSequenceNumber": "CurrentSequenceNumber",
    "BytesReceived": "BytesReceived",
    "lora.BytesReceived": "BytesReceived",
    "BootCount": "BootCount",
    "startupManager.BootCount": "BootCount",
    "CurrentMode": "CurrentMode",
    "modeManager.CurrentMode": "CurrentMode",
    "Voltage": "Voltage",
    "ina219SysManager.Voltage": "Voltage",
}
TRACKED = ["CurrentSequenceNumber", "BytesReceived", "BootCount"]


def now():
    return dt.datetime.now(UTC)


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def pdt(t):
    return t.astimezone(PDT).strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def log(*a):
    print(f"{iso(now())} [electra_ops]", *a, flush=True)


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"scrapes": [], "fired": []}


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


# --------------------------------------------------------------------------- decode


def crc16(d):
    crc = 0xFFFF
    for b in d:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def decode(raw):
    """CCSDS TM frame -> {fc, crc_ok, kinds, sc_time, beacon}. Beacon offsets follow
    proves_parse.py, except CurrentSequenceNumber, which is at channel bytes 65-69
    (TinyGS's web decoder calls it `reserved`; log.csv's SeqNumLora is the wrong offset)."""
    import struct

    b = raw[4:] if raw[:4] == b"\0\0\0\0" else raw
    out = {"fc": b[3] if len(b) > 3 else None, "crc_ok": len(b) > 8 and crc16(b[:-2]) == int.from_bytes(b[-2:], "big"),
           "kinds": [], "sc_time": None, "beacon": None}
    sp, i = b[6:-2], 0
    while i + 6 <= len(sp):
        apid = int.from_bytes(sp[i:i + 2], "big") & 0x7FF
        n = int.from_bytes(sp[i + 4:i + 6], "big") + 1
        if apid not in (2, 4):
            break
        pay = sp[i + 6:i + 6 + n]
        if apid == 2:
            out["kinds"].append("event")
        else:
            pid = int.from_bytes(pay[2:4], "big")
            secs = int.from_bytes(pay[7:11], "big")
            ch = pay[15:]
            if pid == 1 and len(ch) >= 81:
                out["kinds"].append("Beacon")
                out["sc_time"] = secs
                out["beacon"] = {
                    "BootCount": int.from_bytes(ch[0:8], "big"),
                    "CurrentMode": ch[8],
                    "Voltage": round(struct.unpack(">d", ch[33:41])[0], 3),
                    "SysPower": round(struct.unpack(">d", ch[41:49])[0], 4),
                    "SolPower": round(struct.unpack(">d", ch[49:57])[0], 4),
                    "CurrentSequenceNumber": int.from_bytes(ch[65:69], "big"),
                    "BytesReceived": int.from_bytes(ch[77:81], "big"),
                }
            else:
                out["kinds"].append(f"tlm{pid}")
        i += 6 + n
    return out


# --------------------------------------------------------------------------- data


def snapshot_files():
    files = glob.glob(f"{SAT_DIR}/raw/*/*/*/*.json.gz")
    files += glob.glob(f"{ARCHIVE_CLONE}/electra/raw/*/*/*/*.json.gz")
    seen, out = set(), []
    for f in sorted(files, key=os.path.basename):
        if os.path.basename(f) not in seen:
            seen.add(os.path.basename(f))
            out.append(f)
    return out


def load_frames():
    """All Electra frames held, deduped by TinyGS id, plus per-snapshot coverage."""
    import base64

    frames, coverage = {}, []
    for f in snapshot_files():
        try:
            v = json.load(gzip.open(f)).get(FEED)
        except (OSError, ValueError, EOFError):
            continue
        pk = v.get("packets", []) if isinstance(v, dict) else (v or [])
        ts = [p["serverTime"] for p in pk if "serverTime" in p]
        stamp = os.path.basename(f).split("_")[-1].split(".")[0]
        coverage.append({"snapshot": stamp, "oldest": min(ts) if ts else None, "newest": max(ts) if ts else None,
                         "n": len(pk)})
        for p in pk:
            if p.get("id") in frames or "raw" not in p:
                continue
            d = decode(base64.b64decode(p["raw"]))
            d.update(id=p["id"], t=dt.datetime.fromtimestamp(p["serverTime"] / 1000, UTC), raw=p["raw"],
                     n_stations=p.get("stationNumber"))
            frames[p["id"]] = d
    return sorted(frames.values(), key=lambda x: x["t"]), coverage


def load_details(fid):
    for d in (f"{SAT_DIR}/details", f"{ARCHIVE_CLONE}/electra/details"):
        p = f"{d}/{fid}.json"
        if os.path.exists(p):
            try:
                v = next(iter(json.load(open(p)).values()))
                return v.get("stations") or []
            except (OSError, ValueError, StopIteration, AttributeError):
                return None
    return None


def receptions(fr):
    """Per-station rows for one frame; None when details haven't been fetched."""
    st = load_details(fr["id"])
    if st is None:
        return None
    rows = []
    for s in st:
        rp = s.get("receptionParams") or {}
        t = dt.datetime.fromtimestamp(s["usec_time"] / 1e6, UTC) if s.get("usec_time") else fr["t"]
        loc = (s.get("location") or [None, None])[:2]
        rows.append({"t": t, "station": s.get("name"), "lat": loc[0], "lon": loc[1], "rssi": rp.get("rssi"),
                     "snr": rp.get("snr"), "freq_error": rp.get("frequency_error"), "crc_error": s.get("crc_error")})
    return rows


# --------------------------------------------------------------------------- passes


def fetch_tle():
    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={NORAD}&FORMAT=TLE"
    last = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tinygs-tle-creator/0.1"})
            txt = urllib.request.urlopen(req, timeout=30).read().decode()
            lines = [ln.strip() for ln in txt.strip().splitlines()]
            l1 = next(ln for ln in lines if ln.startswith(f"1 {NORAD}"))
            l2 = next(ln for ln in lines if ln.startswith(f"2 {NORAD}"))
            return l1, l2
        except Exception as e:  # noqa: BLE001 - CelesTrak through the proxy resets often
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"CelesTrak TLE fetch failed: {last}")


def predict(hours=48):
    from skyfield.api import EarthSatellite, load, wgs84

    l1, l2 = fetch_tle()
    ts = load.timescale()
    sat = EarthSatellite(l1, l2, "PROVES-ELECTRA", ts)
    gs = wgs84.latlon(STATION["lat"], STATION["lon"], elevation_m=STATION["alt_m"])
    t0 = now()
    times, events = sat.find_events(gs, ts.from_datetime(t0 - dt.timedelta(minutes=20)),
                                    ts.from_datetime(t0 + dt.timedelta(hours=hours)), altitude_degrees=0.0)
    passes, cur = [], {}
    for t, e in zip(times, events):
        alt, az, _ = (sat - gs).at(t).altaz()
        if e == 0:
            cur = {"aos": t.utc_datetime(), "aos_az": az.degrees}
        elif e == 1 and cur:
            if alt.degrees > cur.get("peak_el", -1):
                cur.update(peak=t.utc_datetime(), peak_el=alt.degrees, peak_az=az.degrees)
        elif e == 2 and "peak" in cur:
            cur.update(los=t.utc_datetime(), los_az=az.degrees)
            passes.append(cur)
            cur = {}
    out = []
    for p in passes:
        out.append({
            "id": p["aos"].astimezone(PDT).strftime("%Y-%m-%d_%H%M"),
            "aos_utc": iso(p["aos"]), "peak_utc": iso(p["peak"]), "los_utc": iso(p["los"]),
            "aos_pdt": pdt(p["aos"]), "peak_pdt": pdt(p["peak"]), "los_pdt": pdt(p["los"]),
            "peak_el_deg": round(p["peak_el"], 1), "peak_az_deg": round(p["peak_az"], 1),
            "aos_az_deg": round(p["aos_az"], 1), "los_az_deg": round(p["los_az"], 1),
            "duration_s": round((p["los"] - p["aos"]).total_seconds()),
        })
    doc = {
        "generated_utc": iso(t0),
        "window_utc": [iso(t0), iso(t0 + dt.timedelta(hours=hours))],
        "station": {**STATION, "horizon_deg": 0.0},
        "tle": {"source": "CelesTrak gp.php", "norad": NORAD, "line1": l1, "line2": l2},
        "method": "skyfield EarthSatellite.find_events (SGP4), WGS84 station, horizon 0 deg, refraction not applied",
        "id_rule": "id = AOS in America/Los_Angeles as YYYY-MM-DD_HHMM, matching passes/<id>/",
        "passes": [p for p in out if parse_t(p["los_utc"]) > t0],
    }
    return doc


def known_passes(state):
    """Every predicted pass still relevant (kept in state so past ones stay evaluable)."""
    return sorted(state.get("passes", {}).values(), key=lambda p: p["aos_utc"])


def merge_passes(state, doc):
    allp = state.setdefault("passes", {})
    for p in doc["passes"]:
        # replace a prior prediction of the same pass (AOS within 5 min)
        for k in [k for k, q in allp.items() if abs((parse_t(q["aos_utc"]) - parse_t(p["aos_utc"])).total_seconds()) < 300]:
            del allp[k]
        allp[p["aos_utc"]] = p
    cutoff = now() - dt.timedelta(days=3)
    for k in [k for k, q in allp.items() if parse_t(q["los_utc"]) < cutoff]:
        del allp[k]


# --------------------------------------------------------------------------- git


def git(*a, check=True, cwd=OPS):
    try:
        r = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git {' '.join(a)}: timed out") from e
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()}")
    return r.stdout


def git_retry(*a):
    for d in (0, 2, 4, 8, 16):
        time.sleep(d)
        try:
            return git(*a)
        except RuntimeError as e:
            err = e
    raise err


def sync_ops():
    """Fetch main and our branch; merge main in (station files never collide with ours)."""
    git_retry("fetch", "-q", "origin", UPLINK_BRANCH)
    git("fetch", "-q", "origin", OPS_BRANCH, check=False)  # absent until our first push
    if git("rev-parse", "--abbrev-ref", "HEAD").strip() != OPS_BRANCH:
        git("checkout", "-q", OPS_BRANCH)
    if git("ls-remote", "--heads", "origin", OPS_BRANCH).strip():
        git("merge", "-q", "--no-edit", f"origin/{OPS_BRANCH}", check=False)
    r = subprocess.run(["git", "-C", OPS, "merge", "-q", "--no-edit", f"origin/{UPLINK_BRANCH}"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        git("merge", "--abort", check=False)
        log("WARN merge of", UPLINK_BRANCH, "failed:", r.stderr.strip())


def uplinks():
    """ops/uplinks/*.json as committed on origin/main (read-only)."""
    out = {}
    names = git("ls-tree", "--name-only", f"origin/{UPLINK_BRANCH}", "ops/uplinks/", check=False).split()
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            out[os.path.basename(n)[:-5]] = json.loads(git("show", f"origin/{UPLINK_BRANCH}:{n}"))
        except (RuntimeError, ValueError) as e:
            log("WARN bad uplink file", n, e)
    return out


def commit_push(msg, paths):
    git("add", "--", *paths)
    if not git("diff", "--cached", "--name-only").strip():
        return False
    git("-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com", "commit", "-q", "-m", msg, "-m",
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
        "Claude-Session: https://claude.ai/code/session_013Bc5t1hD67bMMCUn8z4Yxn")
    for d in (2, 4, 8, 16, 0):
        try:
            if subprocess.run(["git", "-C", OPS, "push", "-q", "-u", "origin", OPS_BRANCH], timeout=120).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        git("pull", "-q", "--no-rebase", "--no-edit", "origin", OPS_BRANCH, check=False)
        time.sleep(d)
    log("WARN push failed; committed locally")
    return True


# --------------------------------------------------------------------------- scrape


def scrape(state):
    env = dict(os.environ, PATH=f"{REPO}/.venv/bin:" + os.environ.get("PATH", ""), VIRTUAL_ENV=f"{REPO}/.venv",
               TINYGS_AUTH_STATE=AUTH)
    try:
        r = subprocess.run([f"{REPO}/scripts/cloud.sh", "cycle"], env=env, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess([], 124, "", "cloud.sh cycle timed out\n")
    sys.stderr.write(r.stdout[-1500:] + r.stderr[-1500:])
    t = now()
    snaps = sorted(glob.glob(f"{SAT_DIR}/raw/*/*/*/*.json.gz"))
    newest_snap = snaps[-1] if snaps else None
    ok = r.returncode == 0 and newest_snap and (t.timestamp() - os.path.getmtime(newest_snap)) < 600
    state["scrapes"] = (state.get("scrapes", []) + [{"t": iso(t), "ok": bool(ok), "rc": r.returncode}])[-200:]
    if ok:
        state["last_ok_scrape"] = iso(t)
        # gap check: did this 50-packet window reach back to the previous snapshot's newest frame?
        _, cov = load_frames()
        cov = [c for c in cov if c["oldest"]]
        if len(cov) >= 2 and cov[-1]["oldest"] > cov[-2]["newest"]:
            state["gap_retry_at"] = iso(t + dt.timedelta(minutes=GAP_RETRY_MIN))
            log("feed window does not overlap the previous scrape; retrying in", GAP_RETRY_MIN, "min")
        else:
            state.pop("gap_retry_at", None)
        start_details(state)
    log("scrape", "ok" if ok else f"FAILED rc={r.returncode}")
    return ok


def windows(state, ups):
    """(start, end, label) windows whose packets get details first."""
    w = []
    for p in known_passes(state):
        a, l_ = parse_t(p["aos_utc"]), parse_t(p["los_utc"])
        w.append((a - dt.timedelta(minutes=5), l_ + dt.timedelta(minutes=15 + ORBIT_MIN), p["id"]))
    for name, u in ups.items():
        try:
            a = parse_t(u["pass"]["aos"])
            end = max(parse_t(e["until"]) for e in u.get("expect", [])) + dt.timedelta(hours=2)
        except (KeyError, ValueError):
            continue
        w.append((a - dt.timedelta(minutes=ORBIT_MIN), end, name))
    return w


def start_details(state):
    if state.get("details_pid"):
        try:
            os.kill(state["details_pid"], 0)
            return  # still running
        except OSError:
            pass
    logcsv = f"{SAT_DIR}/log.csv"
    if not os.path.exists(logcsv):
        return
    rows = list(csv.DictReader(open(logcsv)))
    ups = state.get("uplinks_cache", {})
    ws = windows(state, ups)
    pri = [r for r in rows if r.get("serverTime") and any(a <= parse_t(r["serverTime"]) <= b for a, b, _ in ws)]
    os.makedirs(f"{SAT_DIR}/details", exist_ok=True)
    wcsv = f"{ROOT}/window_log.csv"
    with open(wcsv, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["id", "serverTime"])
        wr.writeheader()
        wr.writerows(pri)
    cmd = [PY, f"{REPO}/tinygs_tle/tinygs_details_batch.py",
           "--source", f"{wcsv}:{SAT_DIR}/details", "--source", f"{logcsv}:{SAT_DIR}/details",
           "--max-per-run", str(DETAILS_PER_RUN), "--spacing-s", "60", "--lockfile", f"{ROOT}/.details_batch.lock"]
    if os.path.exists(AUTH):
        cmd += ["--auth-state", AUTH]
    os.makedirs(f"{ROOT}/logs", exist_ok=True)
    lf = open(f"{ROOT}/logs/details-{now():%Y-%m}.log", "a")
    state["details_pid"] = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True).pid


# --------------------------------------------------------------------------- evaluate

OPS_FN = {">=": lambda a, b, tol: a >= b, ">": lambda a, b, tol: a > b, "<=": lambda a, b, tol: a <= b,
          "<": lambda a, b, tol: a < b, "==": lambda a, b, tol: abs(a - b) <= (tol or 0),
          "!=": lambda a, b, tol: abs(a - b) > (tol or 0)}


def station_gaps(frames, start, end):
    """Beacon-to-Beacon gaps heard by one station, using only stretches where every
    Beacon in between has its station details (otherwise a gap is ambiguous)."""
    bea = [f for f in frames if f["beacon"] and start - dt.timedelta(minutes=2) <= f["t"] <= end]
    gaps, last, have_all = [], {}, True
    for f in bea:
        rx = receptions(f)
        if rx is None:
            last, have_all = {}, False  # unknown which stations heard this one
            continue
        for r in rx:
            if r["crc_error"]:
                continue
            p = last.get(r["station"])
            if p and start <= r["t"] <= end:
                gaps.append({"station": r["station"], "from": iso(p[0]), "to": iso(r["t"]),
                             "gap_s": round((r["t"] - p[0]).total_seconds(), 1),
                             "sc_gap_s": (f["sc_time"] - p[1]) if f["sc_time"] and p[1] else None})
            last[r["station"]] = (r["t"], f["sc_time"])
    return gaps, have_all


def sc_gaps(frames, start, end):
    """Gaps between consecutive Beacons by Electra's own clock (station-independent)."""
    sc = sorted({f["sc_time"] for f in frames if f["beacon"] and f["sc_time"] and start <= f["t"] <= end})
    return [{"from_sc": a, "to_sc": b, "gap_s": b - a} for a, b in zip(sc, sc[1:])]


def evaluate(e, frames, t_now, final):
    start, end = parse_t(e["from"]), parse_t(e["until"])
    op = OPS_FN.get(e.get("op"))
    val, tol = e.get("value"), e.get("tolerance")
    out = {"id": e.get("id"), "field": e.get("field"), "op": e.get("op"), "value": val, "tolerance": tol,
           "from": e.get("from"), "until": e.get("until")}
    if op is None:
        return {**out, "verdict": "no_data", "note": f"unsupported op {e.get('op')!r}"}
    closed = t_now > end and final
    if e.get("field") == "beacon_spacing_s":
        gaps, complete = station_gaps(frames, start, end)
        hits = [g for g in gaps if op(g["gap_s"], val, tol)]
        scg = sc_gaps(frames, start, end)
        sc_hits = [g for g in scg if op(g["gap_s"], val, tol)]
        out.update(single_station_gaps=len(gaps), supporting=hits[:50],
                   sc_clock_gaps_matching=len(sc_hits), sc_clock_gap_hist=_hist([g["gap_s"] for g in scg]),
                   details_complete=complete,
                   basis="gaps between consecutive Beacons heard by one station (TinyGS usec_time); "
                         "sc_clock_* is the same from Electra's own Beacon clock, as a cross-check")
        if hits:
            v = "met"
        elif sc_hits and not gaps:
            # no single-station pairs (details missing), but Electra's own clock is unambiguous
            v = "met"
            out["basis_used"] = "sc_clock"
        elif gaps or scg:
            v = "not_met" if closed else "pending"
            if not gaps:
                out["basis_used"] = "sc_clock"
        else:
            v = "no_data" if closed else "pending"
        if v != "met" and sc_hits and "basis_used" not in out:
            out["note"] = "no single-station gap matched, but Electra's clock shows matching Beacon spacing"
        return {**out, "verdict": v}
    key = FIELDS.get(e.get("field"), FIELDS.get(str(e.get("field")).rsplit(".", 1)[-1]))
    if key is None:
        return {**out, "verdict": "no_data", "note": f"unknown field {e.get('field')!r}"}
    quant = e.get("quantifier") or ("any" if e.get("op") in (">=", ">") else "all")
    pts = [f for f in frames if f["beacon"] and start <= f["t"] <= end]
    sup = [{"t": iso(f["t"]), "value": f["beacon"][key], "stations": _stations(f), "tinygs_id": f["id"]}
           for f in pts if op(f["beacon"][key], val, tol)]
    bad = [{"t": iso(f["t"]), "value": f["beacon"][key], "stations": _stations(f), "tinygs_id": f["id"]}
           for f in pts if not op(f["beacon"][key], val, tol)]
    out.update(quantifier=quant, beacons_in_window=len(pts),
               highest=max((f["beacon"][key] for f in pts), default=None),
               supporting=sup[:5] + (sup[-5:] if len(sup) > 10 else sup[5:]), contradicting=bad[-10:])
    if key == "CurrentSequenceNumber" and pts:
        top = max(pts, key=lambda f: (f["beacon"][key], f["t"]))
        out["highest_seen"] = {"value": top["beacon"][key], "t": iso(top["t"]), "stations": _stations(top),
                               "last_accepted_seq": top["beacon"][key] - 1}
    if not pts:
        v = "no_data" if closed else "pending"
    elif quant == "any":
        v = "met" if sup else ("not_met" if closed else "pending")
    else:
        v = "not_met" if bad else ("met" if closed else "pending")
    return {**out, "verdict": v}


def _hist(xs):
    h = {}
    for x in xs:
        h[str(x)] = h.get(str(x), 0) + 1
    return dict(sorted(h.items(), key=lambda kv: float(kv[0]))[:30])


def _stations(f):
    rx = receptions(f)
    if rx is None:
        return f"{f['n_stations']} station(s), details not fetched yet"
    return sorted({r["station"] for r in rx if r["station"]})


def field_history(frames, start, end, key):
    before = [f for f in frames if f["beacon"] and f["t"] < start]
    inwin = [f for f in frames if f["beacon"] and start <= f["t"] <= end]
    after = [f for f in frames if f["beacon"] and f["t"] > end]
    prev = before[-1]["beacon"][key] if before else None
    changes, cur = [], prev
    for f in inwin:
        v = f["beacon"][key]
        if cur is None:
            cur = v
        elif v != cur:
            changes.append({"t": iso(f["t"]), "from": cur, "to": v, "stations": _stations(f), "tinygs_id": f["id"]})
            cur = v
    return {"before": {"value": prev, "t": iso(before[-1]["t"])} if before else None,
            "changes": changes,
            "end_of_window": {"value": cur, "t": iso(inwin[-1]["t"])} if inwin else None,
            "after": {"value": after[0]["beacon"][key], "t": iso(after[0]["t"])} if after else None,
            "max_in_window": max((f["beacon"][key] for f in inwin), default=None)}


def coverage_gaps(coverage, start, end):
    """Stretches inside [start, end] no snapshot's 50-packet window covered."""
    iv = sorted((c["oldest"], c["newest"]) for c in coverage if c["oldest"])
    s, e = start.timestamp() * 1000, end.timestamp() * 1000
    gaps, cur = [], s
    for a, b in iv:
        if b < cur:
            continue
        if a > cur and a < e:
            gaps.append([iso(dt.datetime.fromtimestamp(cur / 1000, UTC)),
                         iso(dt.datetime.fromtimestamp(min(a, e) / 1000, UTC))])
        cur = max(cur, b)
        if cur >= e:
            break
    # trailing stretch only up to the latest scrape (not "now"), so outputs don't change between scrapes
    snaps = [dt.datetime.strptime(c["snapshot"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
             for c in coverage if c.get("snapshot", "").endswith("Z")]
    last = min(end, max(snaps)) if snaps else end
    if cur < last.timestamp() * 1000:
        gaps.append([iso(dt.datetime.fromtimestamp(cur / 1000, UTC)), iso(last)])
    return gaps


# --------------------------------------------------------------------------- pass results


def pass_folder(pid):
    """Reuse the station's own folder when its AOS minute differs from ours by <= 3 min."""
    base = dt.datetime.strptime(pid, "%Y-%m-%d_%H%M")
    for d in range(4):
        for s in (d, -d):
            c = (base + dt.timedelta(minutes=s)).strftime("%Y-%m-%d_%H%M")
            if os.path.isdir(f"{OPS}/passes/{c}"):
                return c
    return pid


def write_pass(folder, frames, coverage, start, end, expect, final, meta, la_pass=None):
    t_now = now()
    win = [f for f in frames if start <= f["t"] <= end]
    d = f"{OPS}/passes/{folder}/tinygs"
    os.makedirs(d, exist_ok=True)
    per_station, rx_rows = {}, []
    missing = 0
    for f in win:
        rx = receptions(f)
        b = f["beacon"] or {}
        dec = [b.get(k, "") for k in ("BootCount", "CurrentMode", "Voltage", "CurrentSequenceNumber", "BytesReceived")]
        kind = "+".join(f["kinds"]) or "?"
        if rx is None:
            missing += 1
            rx_rows.append([iso(f["t"]), "", "", "", "", "", "", f["fc"], kind, f["sc_time"] or "", *dec, f["id"],
                            f"{f['n_stations']} station(s), details pending"])
            continue
        for r in rx:
            s = per_station.setdefault(r["station"], {"receptions": 0, "rssi": [], "snr": [], "first": None,
                                                      "last": None, "location": [r["lat"], r["lon"]]})
            s["receptions"] += 1
            for k in ("rssi", "snr"):
                if r[k] is not None:
                    s[k].append(r[k])
            s["first"] = s["first"] or iso(r["t"])
            s["last"] = iso(r["t"])
            rx_rows.append([iso(r["t"]), r["station"], r["lat"], r["lon"], r["rssi"], r["snr"], r["freq_error"],
                            f["fc"], kind, f["sc_time"] or "", *dec, f["id"], "crc_error" if r["crc_error"] else ""])
    stations = {k: {"receptions": v["receptions"], "first": v["first"], "last": v["last"], "location": v["location"],
                    "rssi_dbm": _stats(v["rssi"]), "snr_db": _stats(v["snr"])}
                for k, v in sorted(per_station.items(), key=lambda kv: -kv[1]["receptions"])}
    verdicts = [evaluate(e, frames, t_now, final) for e in expect]
    gaps, _ = station_gaps(frames, start, end)
    summary = {
        "final": final,
        "data_through_utc": iso(max((f["t"] for f in frames), default=None)),
        "window_utc": [iso(start), iso(end)],
        **meta,
        "frames_in_window": len(win),
        "beacons_in_window": sum(1 for f in win if f["beacon"]),
        "first_reception": {"t": iso(win[0]["t"]), "stations": _stations(win[0])} if win else None,
        "last_reception": {"t": iso(win[-1]["t"]), "stations": _stations(win[-1])} if win else None,
        "station_details_missing_for_frames": missing,
        "receptions_per_station": stations,
        "fields": {k: field_history(frames, start, end, k) for k in TRACKED},
        "beacon_spacing": {
            "single_station_gaps": gaps,
            "sc_clock_gap_hist_s": _hist([g["gap_s"] for g in sc_gaps(frames, start, end)]),
        },
        "feed_coverage_gaps_utc": coverage_gaps(coverage, start, min(end, t_now)),
        "verdicts": verdicts,
        "notes": [
            "TinyGS serves the newest 50 Electra frames per page load; feed_coverage_gaps_utc lists stretches "
            "no scrape covered (frames there may exist but were not seen).",
            "CurrentSequenceNumber is Electra's next expected uplink seq (last accepted + 1).",
            "Voltage is ina219SysManager.Voltage (Beacon channel bytes 33-41).",
        ],
    }
    if la_pass:
        summary["la_pass"] = la_pass
    json.dump(summary, open(f"{d}/summary.json", "w"), indent=1, default=str)
    with open(f"{d}/receptions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["utc", "station", "lat", "lon", "rssi_dbm", "snr_db", "freq_error_hz", "frame_counter", "kind",
                    "sc_time", "BootCount", "CurrentMode", "Voltage", "CurrentSequenceNumber", "BytesReceived",
                    "tinygs_id", "note"])
        w.writerows(rx_rows)
    with open(f"{d}/frames.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["server_utc", "tinygs_id", "stations", "crc_ok", "kind", "raw_b64"])
        for fr in win:
            w.writerow([iso(fr["t"]), fr["id"], fr["n_stations"], fr["crc_ok"], "+".join(fr["kinds"]), fr["raw"]])
    vs = verdict_summary(verdicts)
    seq = summary["fields"]["CurrentSequenceNumber"]
    lines = [
        f"# TinyGS: {folder}" + ("" if final else " (interim)"),
        f"- **Verdicts:** {vs or 'no expectations (not an uplink pass)'}",
        f"- Window {iso(start)} to {iso(end)}: {len(win)} frames, {summary['beacons_in_window']} Beacons, "
        f"{len(stations)} station(s) with details" + (f", details pending for {missing}" if missing else ""),
        f"- CurrentSequenceNumber: before {(seq['before'] or {}).get('value')}, max in window {seq['max_in_window']}, "
        f"{len(seq['changes'])} change(s)",
        f"- Feed coverage gaps: {len(summary['feed_coverage_gaps_utc'])}. Data through "
        f"{summary['data_through_utc']}; details in summary.json",
    ]
    open(f"{d}/README.md", "w").write("\n".join(lines) + "\n")
    return f"passes/{folder}/tinygs", vs


def verdict_summary(vs):
    parts = []
    for v in vs:
        s = f"{v['id']} {v['verdict']}"
        if v.get("highest_seen"):
            s += f" ({v['highest_seen']['value']})"
        parts.append(s)
    return ", ".join(parts)


def _stats(xs):
    if not xs:
        return None
    return {"n": len(xs), "best": max(xs), "median": sorted(xs)[len(xs) // 2], "worst": min(xs)}


# --------------------------------------------------------------------------- status


def write_status(state, frames):
    t_now = now()
    bea = [f for f in frames if f["beacon"]]
    alerts, vnotes = [], []
    for f in bea:
        if f["t"] < t_now - dt.timedelta(days=2):
            continue
        b = f["beacon"]
        why = []
        if b["BootCount"] > BOOT_BASE:
            why.append(f"BootCount {b['BootCount']} > {BOOT_BASE}")
        if b["CurrentMode"] != MODE_OK:
            why.append(f"mode {b['CurrentMode']} != {MODE_OK}")
        if b["Voltage"] < V_MIN:
            # ~8.4 V with SysPower > 0 is the battery-connected reading (ops README); only
            # a low reading with the battery disconnected, or below V_BATT_MIN, alerts.
            if b["SysPower"] > 0 and b["Voltage"] >= V_BATT_MIN:
                if f["crc_ok"]:
                    vnotes.append(f)
            else:
                why.append(f"Voltage {b['Voltage']} V < {V_MIN} V (SysPower {b['SysPower']} W)")
        if why and f["crc_ok"]:
            alerts.append((f, why))
    last = bea[-1] if bea else None
    lines = []
    if alerts:
        lines += ["# ALERT", ""]
        lines += [f"- {iso(f['t'])} ({_fmt_st(f)}): {'; '.join(w)} (TinyGS {f['id']})" for f, w in alerts[-15:]]
        lines += [f"- {len(alerts)} alerting Beacon(s) in the last 48 h", ""]
    else:
        lines += ["ALERT: none in the last 48 h (BootCount > 38, mode != 2, or an abnormal Voltage < 9.5 V)", ""]
    if vnotes:
        lines += [f"Voltage note (not an alert): {len(vnotes)} Beacon(s) in the last 48 h read "
                  f"{min(f['beacon']['Voltage'] for f in vnotes)}..{max(f['beacon']['Voltage'] for f in vnotes)} V "
                  f"with SysPower > 0, latest {iso(vnotes[-1]['t'])}. That is the battery-connected reading "
                  "(ops README: real battery about 8.4 V; 10-11 V with Power 0 is the solar reading with the "
                  f"protection circuit open), so it is not treated as < {V_MIN} V. Readings below {V_BATT_MIN} V, "
                  "or below 9.5 V with SysPower 0, do alert.", ""]
    lines += [
        "# TinyGS scraper status (PROVES Electra, NORAD 69795)",
        "",
        f"- **Last successful scrape:** {state.get('last_ok_scrape')}",
        f"- **Cadence:** baseline every {BASE_CADENCE_MIN} min; extra scrapes at LOS+15 min and one orbit later for "
        f"every LA pass; every {UPLINK_CADENCE_MIN} min inside armed uplink windows; `ops/uplinks/` on "
        f"`{UPLINK_BRANCH}` polled every {POLL_MIN} min. Up to {DETAILS_PER_RUN} per-station detail fetches per "
        "scrape, 1/min, pass/uplink windows first.",
        f"- **Results branch:** `{OPS_BRANCH}` (the operator chose not to have this session push to `main`; "
        "read this file, `la-passes.json` and `passes/*/tinygs/` from that branch).",
        "- **Source:** TinyGS `/v4/packets?satellite=PROVES_Electra`, all stations worldwide, newest 50 frames per load.",
        "",
        "## Latest Electra Beacon held",
        "",
    ]
    if last:
        b = last["beacon"]
        lines += [
            "| Field | Value |", "|---|---|",
            f"| Time (TinyGS server) | {iso(last['t'])} ({pdt(last['t'])}) |",
            f"| Electra clock | {iso(dt.datetime.fromtimestamp(last['sc_time'], UTC)) if last['sc_time'] else ''} |",
            f"| Receiving station(s) | {_fmt_st(last)} |",
            f"| CurrentSequenceNumber | {b['CurrentSequenceNumber']} |",
            f"| lora.BytesReceived | {b['BytesReceived']} |",
            f"| BootCount | {b['BootCount']} |",
            f"| CurrentMode | {b['CurrentMode']} |",
            f"| ina219SysManager.Voltage | {b['Voltage']} V |",
            f"| TinyGS id | {last['id']} |",
            "",
        ]
        day = [f for f in bea if f["t"] > t_now - dt.timedelta(hours=24)]
        if day:
            seqs = [f["beacon"]["CurrentSequenceNumber"] for f in day]
            rxb = [f["beacon"]["BytesReceived"] for f in day]
            vs = [f["beacon"]["Voltage"] for f in day]
            lines += [f"Last 24 h: {len(day)} Beacons; CurrentSequenceNumber {min(seqs)}..{max(seqs)}; "
                      f"BytesReceived {min(rxb)}..{max(rxb)}; Voltage {min(vs)}..{max(vs)} V.", ""]
    else:
        lines += ["No Beacon held yet.", ""]
    ups = state.get("uplinks_cache", {})
    lines += ["## Uplink files seen", ""]
    lines += [f"- `{k}`: {state.get('uplink_verdicts', {}).get(k, 'pending')}" for k in sorted(ups)] or ["- none yet"]
    nxt = [p for p in known_passes(state) if parse_t(p["los_utc"]) > t_now][:3]
    lines += ["", "## Next LA passes", ""]
    lines += [f"- {p['id']}: peak {p['peak_el_deg']} deg at {p['peak_utc']} ({p['peak_pdt']})" for p in nxt]
    lines += ["", "## Verdict rules", "",
              "- Telemetry fields: ops `>=`/`>` are met by **any** Beacon in [from, until]; other ops need **all** "
              "Beacons to satisfy them. Add `\"quantifier\": \"any\"|\"all\"` to an expect entry to override.",
              "- `beacon_spacing_s`: gaps between consecutive Beacons heard by one station, counted only where "
              "every Beacon in between has station details; Electra's own clock is reported as a cross-check.",
              "- Verdicts stay `pending` until `until` has passed and a scrape after `until + 2 h` has run.", ""]
    os.makedirs(f"{OPS}/ops/scraper", exist_ok=True)
    open(f"{OPS}/ops/scraper/STATUS.md", "w").write("\n".join(lines))
    return bool(alerts)


def _fmt_st(f):
    s = _stations(f)
    return ", ".join(s) if isinstance(s, list) else s


# --------------------------------------------------------------------------- analyze


def analyze(state):
    frames, coverage = load_frames()
    t_now = now()
    out_paths, msgs = [], []
    ups = state.get("uplinks_cache", {})
    last_ok = parse_t(state["last_ok_scrape"]) if state.get("last_ok_scrape") else None
    handled = set()
    for name, u in sorted(ups.items()):
        try:
            aos = parse_t(u["pass"]["aos"])
            exp = u.get("expect", [])
            until = max((parse_t(e["until"]) for e in exp), default=parse_t(u["pass"]["los"]))
        except (KeyError, ValueError) as e:
            log("WARN uplink", name, "unreadable:", e)
            continue
        start, end = aos - dt.timedelta(minutes=ORBIT_MIN), until + dt.timedelta(hours=2)
        if t_now < start:
            continue
        final = bool(last_ok and last_ok > end)
        meta = {"uplink_file": f"ops/uplinks/{name}.json", "seq_sent": u.get("seq_sent"), "mode": u.get("mode")}
        path, vs = write_pass(name, frames, coverage, start, end, exp, final, meta)
        handled.add(name)
        state.setdefault("uplink_verdicts", {})[name] = vs + (" (final)" if final else " (interim)")
        out_paths.append(path)
        msgs.append((name, vs, final))
    for p in known_passes(state):
        aos, los = parse_t(p["aos_utc"]), parse_t(p["los_utc"])
        folder = pass_folder(p["id"])
        if folder in handled or t_now < los + dt.timedelta(minutes=15):
            continue
        start, end = aos - dt.timedelta(minutes=5), los + dt.timedelta(minutes=15 + ORBIT_MIN)
        final = bool(last_ok and last_ok > end)
        path, vs = write_pass(folder, frames, coverage, start, end, [], final,
                              {"pass_window_utc": [iso(start), iso(los + dt.timedelta(minutes=15))],
                               "after_window_utc": [iso(los + dt.timedelta(minutes=15)), iso(end)]}, la_pass=p)
        out_paths.append(path)
        n = sum(1 for f in frames if start <= f["t"] <= los + dt.timedelta(minutes=15))
        msgs.append((folder, f"{n} frame(s) AOS-5..LOS+15", final))
    alert = write_status(state, frames)
    return out_paths, msgs, alert


def commit_all(state, out_paths, msgs, alert):
    paths = ["ops/scraper"] + out_paths
    changed = [m for m in msgs if git("status", "--porcelain", "--", f"passes/{m[0]}/tinygs").strip()]
    if changed:
        head = "; ".join(f"{n} {v}" + ("" if fin else " (interim)") for n, v, fin in changed[:3])
        msg = f"tinygs: {head}"
    else:
        msg = f"tinygs: status {state.get('last_ok_scrape')}" + (" ALERT" if alert else "")
    if commit_push(msg[:200], paths):
        log("committed:", msg)


# --------------------------------------------------------------------------- loop


def due_scrape(state, t_now):
    last = parse_t(state["last_ok_scrape"]) if state.get("last_ok_scrape") else None
    last_try = parse_t(state["scrapes"][-1]["t"]) if state.get("scrapes") else None
    if last_try and t_now - last_try < dt.timedelta(minutes=8):
        return None  # never more often than this, even after a failure
    if last is None or t_now - last >= dt.timedelta(minutes=BASE_CADENCE_MIN):
        return "baseline"
    if state.get("gap_retry_at") and t_now >= parse_t(state["gap_retry_at"]):
        return "gap-retry"
    fired = set(state.get("fired", []))
    for p in known_passes(state):
        los = parse_t(p["los_utc"])
        for tag, at in (("los15", los + dt.timedelta(minutes=15)),
                        ("orbit", los + dt.timedelta(minutes=15 + ORBIT_MIN))):
            key = f"{p['aos_utc']}:{tag}"
            if key not in fired and at <= t_now < at + dt.timedelta(hours=3):
                state.setdefault("fired", []).append(key)
                return f"pass {p['id']} {tag}"
    for name, u in state.get("uplinks_cache", {}).items():
        try:
            a = parse_t(u["pass"]["aos"]) - dt.timedelta(minutes=5)
            end = max(parse_t(e["until"]) for e in u["expect"]) + dt.timedelta(hours=2, minutes=10)
        except (KeyError, ValueError):
            continue
        if a <= t_now <= end and t_now - last >= dt.timedelta(minutes=UPLINK_CADENCE_MIN):
            return f"uplink {name}"
        # fast-Beacon marker windows: one 50-frame page covers only ~8 min of 10 s Beacons
        for e in u.get("expect", []):
            try:
                if e.get("field") != "beacon_spacing_s" or float(e["value"]) > 15:
                    continue
                f0 = parse_t(e["from"]) - dt.timedelta(minutes=2)
                f1 = parse_t(e["until"]) + dt.timedelta(minutes=10)
            except (KeyError, ValueError, TypeError):
                continue
            if f0 <= t_now <= f1 and t_now - last >= dt.timedelta(minutes=MARKER_CADENCE_MIN):
                return f"uplink {name} marker {e.get('id')}"
    return None


def in_marker_window(state, t_now):
    for u in state.get("uplinks_cache", {}).values():
        for e in u.get("expect", []):
            try:
                if e.get("field") == "beacon_spacing_s" and float(e["value"]) <= 15 and \
                        parse_t(e["from"]) - dt.timedelta(minutes=6) <= t_now <= parse_t(e["until"]) + dt.timedelta(minutes=10):
                    return True
            except (KeyError, ValueError, TypeError):
                continue
    return False


def tick(state):
    t_now = now()
    try:
        sync_ops()
        ups = uplinks()
        new = sorted(set(ups) - set(state.get("uplinks_cache", {})))
        if new:
            log("new uplink file(s):", new)
        state["uplinks_cache"] = ups
    except RuntimeError as e:
        log("WARN repo sync failed:", e)
    lp = state.get("last_predict")
    if not lp or t_now - parse_t(lp) > dt.timedelta(hours=24) or not os.path.exists(f"{OPS}/ops/scraper/la-passes.json"):
        try:
            doc = predict()
            merge_passes(state, doc)
            os.makedirs(f"{OPS}/ops/scraper", exist_ok=True)
            json.dump(doc, open(f"{OPS}/ops/scraper/la-passes.json", "w"), indent=1)
            state["last_predict"] = iso(t_now)
            log("predicted", len(doc["passes"]), "passes")
        except Exception as e:  # noqa: BLE001
            log("WARN prediction failed:", e)
    why = due_scrape(state, t_now)
    if why:
        log("scrape:", why)
        scrape(state)
    save_state(state)
    # commit on a scrape, on details arriving (pass files change), or as a 6 h heartbeat
    out_paths, msgs, alert = analyze(state)
    save_state(state)
    commit_all(state, out_paths, msgs, alert)
    archive_save()


def archive_save():
    """Push new raw snapshots and detail files to the tinygs-archive branch (scripts/cloud.sh save;
    write-once files, a no-op when nothing is new)."""
    try:
        r = subprocess.run([f"{REPO}/scripts/cloud.sh", "save"], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log("WARN archive save timed out")
        return
    msg = (r.stderr or r.stdout).strip().splitlines()
    if r.returncode:
        log("WARN archive save failed:", " | ".join(msg[-3:]))
    elif msg and "saved" in msg[-1]:
        log(msg[-1])


def run():
    log("loop start; pid", os.getpid())
    open(f"{ROOT}/electra_ops.pid", "w").write(str(os.getpid()))
    while True:
        state = load_state()
        try:
            tick(state)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            log("ERROR tick:", repr(e))
        save_state(state)
        time.sleep(60 * (4 if in_marker_window(state, now()) else POLL_MIN))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "tick", "predict", "scrape", "analyze"])
    a = ap.parse_args()
    os.makedirs(ROOT, exist_ok=True)
    state = load_state()
    if a.cmd == "run":
        run()
    elif a.cmd == "tick":
        tick(state)
    elif a.cmd == "predict":
        doc = predict()
        merge_passes(state, doc)
        print(json.dumps(doc["passes"], indent=1))
    elif a.cmd == "scrape":
        scrape(state)
    elif a.cmd == "analyze":
        out_paths, msgs, alert = analyze(state)
        print(out_paths, msgs, alert)
    save_state(state)


if __name__ == "__main__":
    main()
