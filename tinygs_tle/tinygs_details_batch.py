#!/usr/bin/env python3
"""Fetch per-station detail JSON for logged packets missing one.

Rate-limited to be gentle on TinyGS's free service: one packet detail page
load per minute, capped at --max-per-run per invocation. Scans the CSV log
(written by proves_track.py) for packet ids that don't yet have a detail
file under --details-dir, and fetches the newest ones first (older ones are
picked up on a later run, or dropped once too many are missing).

Uses a lockfile so it's safe to invoke this repeatedly (e.g. from cron/make)
without overlapping runs; a stale lock (>40 min old) is treated as dead.

Usage:
    tinygs_details_batch.py [--log data/log.csv] [--details-dir data/details]
                             [--max-per-run 25] [--spacing-s 60]
                             [--lockfile data/.details_batch.lock]
"""

import argparse
import csv
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join("data", "log.csv")
DEFAULT_DETAILS_DIR = os.path.join("data", "details")
DEFAULT_LOCKFILE = os.path.join("data", ".details_batch.lock")
STALE_LOCK_S = 2400  # 40 min


def run(
    log: str, details_dir: str, lockfile: str, max_per_run: int, spacing_s: int
) -> None:
    if not os.path.exists(log):
        print(f"no log at {log}, nothing to do")
        return

    os.makedirs(os.path.dirname(os.path.abspath(lockfile)) or ".", exist_ok=True)
    if (
        os.path.exists(lockfile)
        and time.time() - os.path.getmtime(lockfile) < STALE_LOCK_S
    ):
        print("batch already running, skipping")
        return
    with open(lockfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        os.makedirs(details_dir, exist_ok=True)
        with open(log) as f:
            rows = list(csv.DictReader(f))
        have = {fn[:-5] for fn in os.listdir(details_dir) if fn.endswith(".json")}
        missing = [
            r["id"]
            for r in sorted(rows, key=lambda r: r["serverTime"], reverse=True)
            if r["id"] not in have
        ]
        dropped = max(0, len(missing) - max_per_run)
        todo = missing[
            :max_per_run
        ]  # newest first; older ones picked up next run (or dropped)
        print(
            f"details: {len(have)} archived, {len(missing)} missing, "
            f"fetching {len(todo)} (deferring {dropped})"
        )
        detail_script = os.path.join(HERE, "tinygs_packet_detail.py")
        for i, pid in enumerate(todo):
            if i > 0:
                time.sleep(spacing_s)
            out = os.path.join(details_dir, f"{pid}.json")
            r = subprocess.run(
                [sys.executable, detail_script, pid, "--out", out],
                capture_output=True,
                text=True,
                timeout=120,
            )
            ok = (
                r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 100
            )
            print(f"  {pid}: {'ok' if ok else 'FAIL'}")
            if not ok and os.path.exists(out):
                os.remove(out)
            os.utime(lockfile, None)
    finally:
        os.remove(lockfile)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--log", default=DEFAULT_LOG, help=f"CSV log path (default: {DEFAULT_LOG})"
    )
    ap.add_argument(
        "--details-dir",
        default=DEFAULT_DETAILS_DIR,
        help=f"directory of per-packet detail JSON (default: {DEFAULT_DETAILS_DIR})",
    )
    ap.add_argument(
        "--lockfile",
        default=DEFAULT_LOCKFILE,
        help=f"single-instance lockfile (default: {DEFAULT_LOCKFILE})",
    )
    ap.add_argument(
        "--max-per-run", type=int, default=25, help="max detail fetches per invocation"
    )
    ap.add_argument(
        "--spacing-s", type=int, default=60, help="seconds between detail page loads"
    )
    args = ap.parse_args()
    run(args.log, args.details_dir, args.lockfile, args.max_per_run, args.spacing_s)


if __name__ == "__main__":
    main()
