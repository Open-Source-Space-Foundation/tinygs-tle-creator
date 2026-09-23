#!/usr/bin/env python3
"""Generic incremental tracker for non-F-Prime satellites (e.g. HUCSat-1): append new raw packets to a CSV log.

No frame decoding: each new TinyGS packet is logged with its raw bytes (hex)
and TinyGS's own decode (`parsed`, compact JSON) so it can be analysed later.
Dedupes by packet id against the existing log, so it's safe to run this
repeatedly against overlapping fetch windows. Older logs missing a column
are migrated in place before appending.

If the capture holds no `/packets?` response (e.g. a Cloudflare block), exits
2 with `FETCH-FAILED: ...`.

Usage:
    raw_track.py FETCH_JSON LOG_CSV [--source-sat SLUG]
"""

import argparse
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logio import (  # noqa: E402
    append_rows,
    compact_json,
    iso_from_ms,
    load_packets_or_exit,
    read_log,
)

FIELDS = [
    "id",
    "serverTime",
    "source_sat",
    "frequency",
    "len",
    "raw_hex",
    "parsed_json",
    "n_stations",
]


def _n_stations(p: dict):
    if isinstance(p.get("stations"), list):
        return len(p["stations"])
    for k in ("stationNumber", "n_stations", "nStations", "stationCount"):
        if isinstance(p.get(k), int):
            return p[k]
    return ""


def packet_row(p: dict, source_sat: str) -> dict:
    raw = base64.b64decode(p.get("raw") or "")
    parsed = p.get("parsed")
    return {
        "id": str(p["id"]),
        "serverTime": iso_from_ms(p["serverTime"]),
        "source_sat": source_sat,
        "frequency": p.get("frequency", p.get("freq", "")),
        "len": len(raw),
        "raw_hex": raw.hex(),
        "parsed_json": compact_json(parsed) if parsed is not None else "",
        "n_stations": _n_stations(p),
    }


def track(fetch_json: str, log_csv: str, source_sat: str = "") -> int:
    _, packets = load_packets_or_exit(fetch_json)
    _, rows = read_log(log_csv)
    known = {r.get("id") for r in rows}
    new_rows = []
    for p in packets:
        if str(p["id"]) in known:
            continue
        known.add(str(p["id"]))
        new_rows.append(packet_row(p, source_sat))
    new_rows.sort(key=lambda r: r["serverTime"])
    append_rows(log_csv, FIELDS, new_rows)

    print(f"new_frames={len(new_rows)}")
    if new_rows:
        print(
            f"span {new_rows[0]['serverTime'][:19]} .. {new_rows[-1]['serverTime'][:19]}"
        )
        lens = sorted({r["len"] for r in new_rows})
        print(f"lengths: {lens}")
    return len(new_rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fetch_json", help="path to a tinygs_fetch.py output JSON")
    ap.add_argument(
        "log_csv", help="CSV log to append new packets to (created if missing)"
    )
    ap.add_argument(
        "--source-sat",
        default="",
        help="TinyGS satellite slug/key the fetch came from (recorded per row)",
    )
    args = ap.parse_args()
    track(args.fetch_json, args.log_csv, source_sat=args.source_sat)


if __name__ == "__main__":
    main()
