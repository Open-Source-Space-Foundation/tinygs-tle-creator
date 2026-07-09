#!/usr/bin/env python3
"""Incremental tracker: parse a fetched TinyGS packets JSON, append new frames to a CSV log, print deltas.

Dedupes by packet id against the existing log, so it's safe to run this
repeatedly against overlapping fetch windows. Also raises simple text
alerts for events worth a human's attention (reboots, uplink activity,
non-beacon frames).

Usage:
    proves_track.py FETCH_JSON LOG_CSV
"""

import argparse
import base64
import csv
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
]


def load_packets(fetch_json: str) -> dict:
    import json

    d = json.load(open(fetch_json))
    pk = next(v for k, v in d.items() if "packets?" in k)
    if isinstance(pk, list):
        pk = {"packets": pk}
    if not isinstance(pk.get("packets"), list):
        pk = {"packets": []}
    return pk


def track(fetch_json: str, log_csv: str) -> None:
    pk = load_packets(fetch_json)

    known = set()
    last_boot = None
    last_rx = None
    prev_latest_ts = ""
    if os.path.exists(log_csv):
        with open(log_csv) as f:
            rows_prev = list(csv.DictReader(f))
        known = {row["id"] for row in rows_prev}
        rows_chrono = sorted(rows_prev, key=lambda r: r["serverTime"])
        prev_latest_ts = rows_chrono[-1]["serverTime"] if rows_chrono else ""
        for row in reversed(rows_chrono):
            if row.get("BootCount"):
                last_boot = int(row["BootCount"])
                break
        for row in reversed(rows_chrono):
            if row.get("LoraBytesReceived"):
                last_rx = int(row["LoraBytesReceived"])
                break

    new_rows = []
    for p in pk["packets"]:
        if p["id"] in known:
            continue
        r = parse_frame(base64.b64decode(p["raw"]))
        b = r.get("beacon", {})
        new_rows.append(
            {
                "id": p["id"],
                "serverTime": datetime.datetime.fromtimestamp(
                    p["serverTime"] / 1000, datetime.timezone.utc
                ).isoformat(),
                "scid": r.get("scid"),
                "vc": r.get("vc"),
                "crc_ok": r.get("crc_ok"),
                "sc_time": r.get("sc_time", ""),
                "BootCount": b.get("BootCount"),
                "CurrentMode": b.get("CurrentMode"),
                "Voltage": b.get("Voltage"),
                "SeqNumLora": b.get("SeqNumLora"),
                "SeqNumUart": b.get("SeqNumUart"),
                "LoraBytesReceived": b.get("LoraBytesReceived"),
            }
        )

    new_rows.sort(key=lambda r: r["serverTime"])
    os.makedirs(os.path.dirname(os.path.abspath(log_csv)), exist_ok=True)
    write_header = not os.path.exists(log_csv)
    with open(log_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(new_rows)

    print(f"new_frames={len(new_rows)}")
    fresh = [r for r in new_rows if r["serverTime"] > prev_latest_ts]
    backfill = len(new_rows) - len(fresh)
    if backfill:
        print(
            f"note: {backfill} historical/backfill frame(s) ingested "
            "(older than log tip; excluded from alerts)"
        )
    if new_rows:
        first, last = new_rows[0], new_rows[-1]
        print(f"span {first['serverTime'][:19]} .. {last['serverTime'][:19]}")
        print(
            f"latest: boot={last['BootCount']} mode={last['CurrentMode']} "
            f"V={last['Voltage']} seqL={last['SeqNumLora']} "
            f"seqU={last['SeqNumUart']} rxB={last['LoraBytesReceived']}"
        )
        rx = [
            r["LoraBytesReceived"] for r in fresh if r["LoraBytesReceived"] is not None
        ]
        base = last_rx if last_rx is not None else (min(rx) if rx else None)
        if rx and max(rx) != base:
            print(
                f"ALERT uplink-activity: LoraBytesReceived {base} -> {max(rx)} (+{max(rx) - base})"
            )
        sq = [r["SeqNumLora"] for r in fresh if r["SeqNumLora"] is not None]
        if sq and max(sq) > 0:
            print(f"ALERT AUTH-SUCCESS: SeqNumLora reached {max(sq)}")
        bc = {r["BootCount"] for r in fresh if r["BootCount"] is not None}
        if len(bc) > 1 or (last_boot is not None and bc and max(bc) != last_boot):
            print(
                f"ALERT REBOOT: BootCount changed {last_boot} -> {sorted(bc)} (command-loss timer?)"
            )
        nonbeacon = [r for r in new_rows if r["BootCount"] is None]
        if nonbeacon:
            print(
                f"ALERT non-beacon packets: {len(nonbeacon)} frame(s), "
                "see JSON for payload (possible EVR/event downlink)"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fetch_json", help="path to a tinygs_fetch.py output JSON")
    ap.add_argument(
        "log_csv", help="CSV log to append new frames to (created if missing)"
    )
    args = ap.parse_args()
    track(args.fetch_json, args.log_csv)


if __name__ == "__main__":
    main()
