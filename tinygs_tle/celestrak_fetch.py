#!/usr/bin/env python3
"""Fetch the current CelesTrak GP element set (TLE) for one NORAD catalog number.

Validates that the response holds a proper two-line element set (line
numbers, length, checksums, matching catalog numbers) before writing it,
atomically, to --out (name line kept when present). Exits 1 on any failure,
including CelesTrak's "No GP data found", leaving an existing --out intact.

Usage:
    celestrak_fetch.py --catnr 69799 --out data/electra/norad69799.tle
"""

import argparse
import os
import sys
import tempfile
import urllib.error
import urllib.request

URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=TLE"
USER_AGENT = "tinygs-tle-creator/0.1 (+https://github.com/Open-Source-Space-Foundation/tinygs-tle-creator)"
TIMEOUT_S = 30


def tle_checksum(line: str) -> int:
    return (
        sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68]) % 10
    )


def parse_tle(text: str) -> tuple[str, str, str]:
    """(name, line1, line2) from a CelesTrak TLE response; raises ValueError."""
    if "No GP data found" in text:
        raise ValueError("CelesTrak: No GP data found")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    i1 = next((i for i, ln in enumerate(lines) if ln.startswith("1 ")), None)
    if i1 is None or i1 + 1 >= len(lines):
        raise ValueError("no TLE line 1/2 in response")
    l1, l2 = lines[i1], lines[i1 + 1]
    name = lines[i1 - 1].strip() if i1 > 0 else ""
    for n, ln in ((1, l1), (2, l2)):
        if not ln.startswith(f"{n} ") or len(ln) != 69:
            raise ValueError(f"malformed TLE line {n}: {ln!r}")
        if not ln[68].isdigit() or tle_checksum(ln) != int(ln[68]):
            raise ValueError(f"bad checksum on TLE line {n}: {ln!r}")
    if l1[2:7] != l2[2:7]:
        raise ValueError(f"catalog number mismatch: {l1[2:7]!r} vs {l2[2:7]!r}")
    return name, l1, l2


def fetch(catnr: str) -> str:
    req = urllib.request.Request(
        URL.format(catnr=catnr), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", "replace")


def write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".tle", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catnr", required=True, help="NORAD catalog number")
    ap.add_argument("--out", required=True, help="output TLE file")
    args = ap.parse_args()
    try:
        name, l1, l2 = parse_tle(fetch(args.catnr))
        got = l1[2:7].strip()
        if got.isdigit() and args.catnr.isdigit() and int(got) != int(args.catnr):
            raise ValueError(f"asked for {args.catnr}, got {l1[2:7]}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"CELESTRAK-FAILED: catnr {args.catnr}: {e}", file=sys.stderr)
        sys.exit(1)
    write_atomic(args.out, "".join(f"{ln}\n" for ln in (name, l1, l2) if ln))
    print(f"{args.out}: {l1[2:7]} epoch {l1[18:32].strip()}")


if __name__ == "__main__":
    main()
