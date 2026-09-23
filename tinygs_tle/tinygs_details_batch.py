#!/usr/bin/env python3
"""Fetch per-station detail JSON for logged packets missing one.

Rate-limited to be gentle on TinyGS's free service: one packet detail page
load per minute, capped at --max-per-run per invocation. Scans the CSV log
(written by proves_track.py or raw_track.py) for packet ids that don't yet
have a detail file under --details-dir, and fetches the newest ones first
(older ones are picked up on a later run, or dropped once too many are
missing).

Multiple sources share one --max-per-run budget: pass --source LOG:DIR once
per satellite, in priority order; the budget is filled from the first source
(newest first), then the next, and so on. Rows with a `crc_ok` column are
only eligible when it is True (CRC-bad frames aren't worth a page load);
logs without that column (raw_track.py) are always eligible.

Uses a lockfile so it's safe to invoke this repeatedly (e.g. from cron/make)
without overlapping runs; a stale lock (>40 min old) is treated as dead.

Exits 1 if every attempted fetch in the run failed and there were at least 3
attempts (likely a Cloudflare block), so wrappers can alert on it.

Usage:
    tinygs_details_batch.py [--log data/log.csv] [--details-dir data/details]
                             [--source LOG_CSV:DETAILS_DIR ...]
                             [--max-per-run 25] [--spacing-s 60]
                             [--lockfile data/.details_batch.lock]
                             [--auth-state FILE]
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
MIN_ATTEMPTS_FOR_BLOCK = 3


def _eligible(row: dict, has_crc_col: bool) -> bool:
    pid = row.get("id") or ""
    if not pid or pid.startswith("lasttlm-"):  # synthetic rows (old variant)
        return False
    if not has_crc_col:
        return True
    return str(row.get("crc_ok", "")).strip().lower() in ("true", "1")


def missing_ids(log: str, details_dir: str) -> list:
    """Packet ids in `log` without a detail file in `details_dir`, newest first."""
    with open(log, newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        has_crc_col = "crc_ok" in (r.fieldnames or [])
    have = (
        {fn[:-5] for fn in os.listdir(details_dir) if fn.endswith(".json")}
        if os.path.isdir(details_dir)
        else set()
    )
    out, seen = [], set()
    for row in sorted(rows, key=lambda r: r.get("serverTime") or "", reverse=True):
        pid = row.get("id")
        if pid in have or pid in seen or not _eligible(row, has_crc_col):
            continue
        seen.add(pid)
        out.append(pid)
    return out


def plan(sources: list, max_per_run: int) -> list:
    """[(pid, details_dir)] filling the shared budget from sources in order."""
    todo = []
    for log, details_dir in sources:
        if not os.path.exists(log):
            print(f"no log at {log}, skipping")
            continue
        missing = missing_ids(log, details_dir)
        room = max(0, max_per_run - len(todo))
        take = missing[:room]
        todo += [(pid, details_dir) for pid in take]
        print(
            f"details {log}: {len(missing)} missing, fetching {len(take)} "
            f"(deferring {len(missing) - len(take)}) -> {details_dir}"
        )
    return todo


def run(
    sources: list,
    lockfile: str,
    max_per_run: int,
    spacing_s: int,
    auth_state: str | None = None,
) -> int:
    """Returns a process exit code."""
    if not any(os.path.exists(log) for log, _ in sources):
        print("no logs found, nothing to do")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(lockfile)) or ".", exist_ok=True)
    if (
        os.path.exists(lockfile)
        and time.time() - os.path.getmtime(lockfile) < STALE_LOCK_S
    ):
        print("batch already running, skipping")
        return 0
    with open(lockfile, "w") as f:
        f.write(str(os.getpid()))
    attempts = failures = 0
    try:
        todo = plan(sources, max_per_run)
        detail_script = os.path.join(HERE, "tinygs_packet_detail.py")
        for i, (pid, details_dir) in enumerate(todo):
            if i > 0:
                time.sleep(spacing_s)
            os.makedirs(details_dir, exist_ok=True)
            out = os.path.join(details_dir, f"{pid}.json")
            attempts += 1
            try:
                r = subprocess.run(
                    [sys.executable, detail_script, pid, "--out", out]
                    + (["--auth-state", auth_state] if auth_state else []),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                rc = r.returncode
            except subprocess.TimeoutExpired:
                rc = -1
            ok = rc == 0 and os.path.exists(out) and os.path.getsize(out) > 100
            print(f"  {pid}: {'ok' if ok else 'FAIL'}")
            if not ok:
                failures += 1
                if os.path.exists(out):
                    os.remove(out)
            os.utime(lockfile, None)
    finally:
        os.remove(lockfile)

    print(f"details: {attempts - failures}/{attempts} fetched")
    if attempts >= MIN_ATTEMPTS_FOR_BLOCK and failures == attempts:
        print(
            f"DETAILS-FAILED: all {attempts} detail fetches failed (Cloudflare block?)",
            file=sys.stderr,
        )
        return 1
    return 0


def parse_source(spec: str) -> tuple:
    log, sep, details_dir = spec.rpartition(":")
    if not sep or not log or not details_dir:
        raise argparse.ArgumentTypeError(
            f"bad --source {spec!r}; expected LOG_CSV:DETAILS_DIR"
        )
    return log, details_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--log",
        default=None,
        help=f"CSV log path, single-source mode (default: {DEFAULT_LOG})",
    )
    ap.add_argument(
        "--details-dir",
        default=None,
        help="directory of per-packet detail JSON, single-source mode "
        f"(default: {DEFAULT_DETAILS_DIR})",
    )
    ap.add_argument(
        "--source",
        action="append",
        default=[],
        type=parse_source,
        metavar="LOG_CSV:DETAILS_DIR",
        help="log + details dir pair, repeatable, in priority order "
        "(shares --max-per-run); replaces --log/--details-dir",
    )
    ap.add_argument(
        "--lockfile",
        default=DEFAULT_LOCKFILE,
        help=f"single-instance lockfile (default: {DEFAULT_LOCKFILE})",
    )
    ap.add_argument(
        "--max-per-run",
        type=int,
        default=25,
        help="max detail fetches per invocation, across all sources",
    )
    ap.add_argument(
        "--spacing-s", type=int, default=60, help="seconds between detail page loads"
    )
    ap.add_argument(
        "--auth-state",
        default=None,
        help="Playwright storage_state JSON with the TinyGS login (optional)",
    )
    args = ap.parse_args()
    if args.source:
        if args.log or args.details_dir:
            ap.error("use either --source or --log/--details-dir, not both")
        sources = args.source
    else:
        sources = [(args.log or DEFAULT_LOG, args.details_dir or DEFAULT_DETAILS_DIR)]
    sys.exit(
        run(sources, args.lockfile, args.max_per_run, args.spacing_s, args.auth_state)
    )


if __name__ == "__main__":
    main()
