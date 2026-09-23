#!/usr/bin/env python3
"""Incremental tracker: parse a fetched TinyGS packets JSON, append new frames to a CSV log, print deltas.

Dedupes by packet id against the existing log, so it's safe to run this
repeatedly against overlapping fetch windows. Also raises simple text
alerts for events worth a human's attention (reboots, uplink activity,
non-beacon frames).

Multi-source: frames are parsed with bit-slip recovery (proves_parse), and a
frame whose CRC-valid SCID matches a --route goes to that route's log
instead of LOG_CSV (e.g. Electra frames that TinyGS filed under Alcyone).
Alerts are computed per target log. Logs written by older versions (no
`source_sat`/`deslipped` columns) are migrated in place before appending.

If the capture holds no `/packets?` response (e.g. a Cloudflare block), exits
2 with `FETCH-FAILED: ...`.

Usage:
    proves_track.py FETCH_JSON LOG_CSV [--source-sat SLUG]
                    [--route SCID=PATH ...] [--lasttlm-csv PATH]
                    [--alerts-file PATH]
"""

import argparse
import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logio import (  # noqa: E402
    append_rows,
    compact_json,
    feed_health,
    iso_from_ms,
    load_fetch,
    load_packets_or_exit,
    read_log,
    utc_now_iso,
)
from proves_parse import parse_frame  # noqa: E402

FIELDS = [
    "id",
    "serverTime",
    "scid",
    "vc",
    "crc_ok",
    "sc_time",
    "BootCount",
    "CurrentMode",
    "Voltage",
    "SeqNumLora",
    "SeqNumUart",
    "LoraBytesReceived",
    "source_sat",
    "deslipped",
]

LASTTLM_FIELDS = ["fetched_at", "source_sat", "tlm_time", "tlm_json"]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _trusted(row) -> bool:
    """Prior log rows are trusted unless explicitly logged as CRC-bad."""
    return str(row.get("crc_ok", "")).strip() != "False"


# ------------------------------------------------------------------ lastTlm
#
# /v3/satellite/<slug> carries TinyGS's own decode of the latest telemetry
# frame, as `lastTelemetry` (observed Sept 2026) or `lastTlm` (older name).
# For PROVES it looks like {loraHeader, primaryHeader{spacecraftId, ...},
# spacePacket{payload{fwTime{unixSecondsFloat}, beacon{...}}}, ...}.

LASTTLM_KEYS = ("lastTelemetry", "lastTlm")


def _epoch_to_iso(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return ""
    if v > 1e11:  # epoch ms
        return iso_from_ms(v)
    if v > 1e8:  # epoch s
        return iso_from_ms(v * 1000)
    return ""


def _time_like(obj, depth=0):
    """Best-effort timestamp: first time/date-ish key (breadth-first, nested)."""
    if not isinstance(obj, dict) or depth > 4:
        return ""
    for k, v in obj.items():
        kl = k.lower()
        if "time" in kl or "date" in kl or kl in ("ts", "epoch", "seconds"):
            if isinstance(v, str) and v:
                return v
            iso = _epoch_to_iso(v)
            if iso:
                return iso
    for v in obj.values():
        if isinstance(v, dict):
            t = _time_like(v, depth + 1)
            if t:
                return t
    return ""


def tlm_time(tlm) -> str:
    """Spacecraft time of a lastTelemetry dict, ISO; '' if none found."""
    try:
        t = _epoch_to_iso(tlm["spacePacket"]["payload"]["fwTime"]["unixSecondsFloat"])
        if t:
            return t
    except (KeyError, TypeError):
        pass
    return _time_like(tlm)


def ingest_lasttlm(capture: dict, lasttlm_csv: str, source_sat: str) -> int:
    """Append each distinct /v3/satellite/<slug> lastTelemetry to lasttlm_csv."""
    _, rows = read_log(lasttlm_csv)
    seen = {hashlib.sha1(r.get("tlm_json", "").encode()).hexdigest() for r in rows}
    new = []
    for k, v in capture.items():
        if "/satellite/" not in k or "stats" in k or "packets" in k:
            continue
        if not isinstance(v, dict):
            continue
        tlm = next((v[n] for n in LASTTLM_KEYS if v.get(n)), None)
        if tlm is None:
            continue
        tj = compact_json(tlm)
        h = hashlib.sha1(tj.encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        new.append(
            {
                "fetched_at": utc_now_iso(),
                "source_sat": source_sat,
                "tlm_time": tlm_time(tlm),
                "tlm_json": tj,
            }
        )
    if new:
        append_rows(lasttlm_csv, LASTTLM_FIELDS, new)
    print(f"lasttlm: {len(new)} new -> {lasttlm_csv}" if new else "lasttlm: none new")
    return len(new)


# ------------------------------------------------------------------- frames


def frame_row(p: dict, source_sat: str) -> tuple[dict, dict]:
    r = parse_frame(base64.b64decode(p["raw"]))
    b = r.get("beacon", {})
    row = {
        "id": str(p["id"]),
        "serverTime": iso_from_ms(p["serverTime"]),
        "scid": r.get("scid"),
        "vc": r.get("vc"),
        "crc_ok": bool(r.get("crc_ok")),
        "sc_time": r.get("sc_time", ""),
        "BootCount": b.get("BootCount"),
        "CurrentMode": b.get("CurrentMode"),
        "Voltage": b.get("Voltage"),
        "SeqNumLora": b.get("SeqNumLora"),
        "SeqNumUart": b.get("SeqNumUart"),
        "LoraBytesReceived": b.get("LoraBytesReceived"),
        "source_sat": source_sat,
        "deslipped": bool(r.get("deslipped")),
    }
    return row, r


class LogState:
    """Prior state of one target log (for dedupe and alert baselines)."""

    def __init__(self, path: str):
        self.path = path
        _, rows = read_log(path)
        self.known = {row.get("id") for row in rows}
        chrono = sorted(rows, key=lambda r: r.get("serverTime") or "")
        self.prev_latest_ts = chrono[-1].get("serverTime") or "" if chrono else ""
        self.last_boot = self.last_rx = None
        for row in reversed(chrono):
            if _trusted(row) and _int(row.get("BootCount")) is not None:
                self.last_boot = _int(row["BootCount"])
                break
        for row in reversed(chrono):
            if _trusted(row) and _int(row.get("LoraBytesReceived")) is not None:
                self.last_rx = _int(row["LoraBytesReceived"])
                break


def alerts_for(state: LogState, new_rows: list) -> list:
    """Alert messages (without the `ALERT ` prefix) for one target log."""
    out = []
    fresh = [
        r for r in new_rows if r["serverTime"] > state.prev_latest_ts and r["crc_ok"]
    ]
    # LoraBytesReceived resets on every reboot, so only count increases
    # between consecutive frames of the same boot (a reset is not an uplink).
    gain, prev_boot, prev_rx, first_rx = 0, state.last_boot, state.last_rx, None
    for r in sorted(fresh, key=lambda r: r["serverTime"]):
        rx, boot = r["LoraBytesReceived"], r["BootCount"]
        if rx is None:
            continue
        if boot == prev_boot and prev_rx is not None and rx > prev_rx:
            gain += rx - prev_rx
            if first_rx is None:
                first_rx = prev_rx
        prev_boot, prev_rx = boot, rx
    if gain:
        out.append(
            f"uplink-activity: LoraBytesReceived {first_rx} -> {prev_rx} "
            f"(+{gain} within boot {prev_boot})"
        )
    sq = [r["SeqNumLora"] for r in fresh if r["SeqNumLora"] is not None]
    if sq and max(sq) > 0:
        out.append(f"AUTH-SUCCESS: SeqNumLora reached {max(sq)}")
    bc = {r["BootCount"] for r in fresh if r["BootCount"] is not None}
    if len(bc) > 1 or (
        state.last_boot is not None and bc and max(bc) != state.last_boot
    ):
        out.append(
            f"REBOOT: BootCount changed {state.last_boot} -> {sorted(bc)} "
            "(command-loss timer?)"
        )
    nonbeacon = [r for r in new_rows if r["crc_ok"] and r["BootCount"] is None]
    if nonbeacon:
        out.append(
            f"non-beacon packets: {len(nonbeacon)} frame(s), "
            "see JSON for payload (possible EVR/event downlink)"
        )
    return out


def parse_routes(specs: list) -> dict:
    routes = {}
    for s in specs or []:
        scid, sep, path = s.partition("=")
        if not sep or not path or _int(scid) is None:
            raise SystemExit(f"bad --route {s!r}; expected SCID=PATH")
        routes[int(scid)] = path
    return routes


def track(
    fetch_json: str,
    log_csv: str,
    source_sat: str = "",
    routes: dict | None = None,
    lasttlm_csv: str | None = None,
    alerts_file: str | None = None,
) -> None:
    routes = routes or {}
    if lasttlm_csv:  # independent of the packets response, so ingest first
        ingest_lasttlm(load_fetch(fetch_json), lasttlm_csv, source_sat)
    capture, packets = load_packets_or_exit(fetch_json)

    states = {}

    def state(path):
        if path not in states:
            states[path] = LogState(path)
        return states[path]

    state(log_csv)
    by_target = {}
    for p in packets:
        row, _ = frame_row(p, source_sat)
        target = log_csv
        if row["crc_ok"] and row["scid"] in routes:
            target = routes[row["scid"]]
        st = state(target)
        if row["id"] in st.known:
            continue
        st.known.add(row["id"])
        by_target.setdefault(target, []).append(row)

    all_new = sorted(
        (r for rows in by_target.values() for r in rows), key=lambda r: r["serverTime"]
    )
    for path, rows in by_target.items():
        rows.sort(key=lambda r: r["serverTime"])
        append_rows(path, FIELDS, rows)
    if log_csv not in by_target:
        append_rows(log_csv, FIELDS, [])  # create / migrate header anyway

    print(f"new_frames={len(all_new)}")
    print(feed_health(capture, packets))
    if routes:
        for path in states:
            print(f"  {len(by_target.get(path, []))} -> {path}")
    ndeslip = sum(1 for r in all_new if r["deslipped"])
    nbad = sum(1 for r in all_new if not r["crc_ok"])
    if ndeslip:
        print(f"note: {ndeslip} frame(s) recovered by bit-slip repair")
    if nbad:
        print(f"note: {nbad} frame(s) failed CRC (logged, excluded from alerts)")

    alert_lines = []
    for path, rows in by_target.items():
        st = states[path]
        fresh = [r for r in rows if r["serverTime"] > st.prev_latest_ts]
        backfill = len(rows) - len(fresh)
        tag = f" [{path}]" if routes else ""
        if backfill:
            print(
                f"note: {backfill} historical/backfill frame(s) ingested{tag} "
                "(older than log tip; excluded from alerts)"
            )
        first, last = rows[0], rows[-1]
        print(f"span{tag} {first['serverTime'][:19]} .. {last['serverTime'][:19]}")
        print(
            f"latest{tag}: boot={last['BootCount']} mode={last['CurrentMode']} "
            f"V={last['Voltage']} seqL={last['SeqNumLora']} "
            f"seqU={last['SeqNumUart']} rxB={last['LoraBytesReceived']}"
        )
        for msg in alerts_for(st, rows):
            print(f"ALERT {msg}{tag}")
            alert_lines.append(
                f"{utc_now_iso()} [{source_sat or '-'}] ALERT {msg} (log={path})"
            )

    if alert_lines and alerts_file:
        os.makedirs(os.path.dirname(os.path.abspath(alerts_file)), exist_ok=True)
        with open(alerts_file, "a") as f:
            f.write("\n".join(alert_lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fetch_json", help="path to a tinygs_fetch.py output JSON")
    ap.add_argument(
        "log_csv",
        help="default CSV log to append new frames to (created if missing)",
    )
    ap.add_argument(
        "--source-sat",
        default="",
        help="TinyGS satellite slug/key the fetch came from (recorded per row)",
    )
    ap.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="SCID=PATH",
        help="send CRC-valid frames with this SCID to PATH instead of LOG_CSV "
        "(repeatable)",
    )
    ap.add_argument(
        "--lasttlm-csv",
        default=None,
        help="append distinct /v3/satellite lastTlm snapshots to this CSV",
    )
    ap.add_argument(
        "--alerts-file",
        default=None,
        help="also append ALERT lines (UTC timestamp + source prefix) here",
    )
    args = ap.parse_args()
    track(
        args.fetch_json,
        args.log_csv,
        source_sat=args.source_sat,
        routes=parse_routes(args.route),
        lasttlm_csv=args.lasttlm_csv,
        alerts_file=args.alerts_file,
    )


if __name__ == "__main__":
    main()
