"""Shared I/O helpers for the TinyGS trackers: fetch-JSON loading and CSV log append/migrate.

Used by proves_track.py (F-Prime/PROVES frames) and raw_track.py (any other
satellite). Not a CLI.

    load_fetch(path)            -> whole tinygs_fetch.py capture (dict)
    packets_from_fetch(d)       -> list of packets, or raises FetchFailed
    feed_health(d, packets)     -> "feed_lag_h=... auth=..." status line
    read_log(path)              -> (header, rows)
    append_rows(path, fields, rows)
                                -> append, creating the file or migrating an
                                   older header (atomic rewrite) first
"""

import csv
import datetime
import json
import os
import sys
import tempfile

FETCH_FAILED_EXIT = 2


class FetchFailed(Exception):
    """The fetch capture holds no usable /packets response (e.g. Cloudflare block)."""


def load_fetch(fetch_json: str) -> dict:
    with open(fetch_json) as f:
        d = json.load(f)
    return d if isinstance(d, dict) else {}


def packets_from_fetch(d: dict) -> list:
    """Return the packet list from a fetch capture, or raise FetchFailed."""
    keys = [k for k in d if "packets?" in k]
    if not keys:
        raise FetchFailed("FETCH-FAILED: no packets response captured")
    pk = d[keys[0]]
    if isinstance(pk, list):
        return pk
    if isinstance(pk, dict) and "_error" in pk:
        raise FetchFailed(
            f"FETCH-FAILED: packets response unreadable "
            f"(status {pk.get('_status')}: {pk['_error']})"
        )
    if isinstance(pk, dict) and isinstance(pk.get("packets"), list):
        return pk["packets"]
    return []


def load_packets_or_exit(fetch_json: str) -> tuple[dict, list]:
    """CLI helper: (capture, packets); on FetchFailed print to stderr and exit 2."""
    d = load_fetch(fetch_json)
    try:
        return d, packets_from_fetch(d)
    except FetchFailed as e:
        print(str(e), file=sys.stderr)
        sys.exit(FETCH_FAILED_EXIT)


def feed_health(d: dict, packets: list) -> str:
    """One status line for the wrappers: how stale the packet list is, and auth.

    feed_lag_h = hours between the satellite's lastPacketTime (from
    /v3/satellite/<slug>) and the newest packet in the list. A silent satellite
    gives ~0; a large value means TinyGS is serving a frozen list while the
    satellite is still being heard. `na` if either time is unavailable.
    auth = whether the packets request carried a session token (`na` for
    captures made before tinygs_fetch.py recorded it).
    """
    sat = next(
        (
            v
            for k, v in d.items()
            if "/satellite/" in k
            and "stats" not in k
            and "packets" not in k
            and isinstance(v, dict)
        ),
        {},
    )
    last = sat.get("lastPacketTime")
    times = [p.get("serverTime") for p in packets if p.get("serverTime")]
    lag = (
        f"{max(0.0, (last - max(times)) / 3.6e6):.2f}"
        if isinstance(last, (int, float)) and times
        else "na"
    )
    auth = (d.get("_meta") or {}).get("packets_request_authenticated")
    return f"feed_lag_h={lag} auth={'na' if auth is None else str(auth).lower()}"


def iso_from_ms(ms) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def compact_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------ CSV logs


def read_log(path: str) -> tuple[list, list]:
    """(header, rows) of a CSV log; ([], []) if missing or empty."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return [], []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        return list(r.fieldnames or []), rows


def _atomic_write_rows(path: str, header: list, rows: list) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".csv", dir=d)
    try:
        with os.fdopen(fd, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def ensure_header(path: str, fields: list) -> list:
    """Make sure `path` has every column in `fields`; return its (new) header.

    Missing columns are appended after the existing ones and the file is
    rewritten atomically (old rows get blanks). A missing/empty file gets
    `fields` as its header.
    """
    header, rows = read_log(path)
    if not header:
        _atomic_write_rows(path, list(fields), [])
        return list(fields)
    missing = [c for c in fields if c not in header]
    if missing:
        header = header + missing
        _atomic_write_rows(path, header, rows)
    return header


def append_rows(path: str, fields: list, rows: list) -> None:
    """Append rows (dicts) to a CSV log, creating/migrating its header first."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    header = ensure_header(path, fields)
    if not rows:
        return
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writerows(rows)
