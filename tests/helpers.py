"""Shared test helpers: import path, synthetic PROVES frames, fetch-JSON fixtures."""

import base64
import json
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "tinygs_tle")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, PKG)

from proves_parse import crc16  # noqa: E402

HEADER = b"\x00\x00\x00\x00"
ELECTRA_PACKETS_KEY = "https://api.tinygs.com/v4/packets?satellite=PROVES_Electra"


def make_frame(scid=3, boot=21, vcid=1, mc=5, rx=198, seq_lora=0, secs=1786371495):
    """A 248-byte CCSDS TM frame carrying a PROVES Beacon, with a valid CRC16."""
    ch = bytearray(81)
    ch[0:8] = boot.to_bytes(8, "big")
    ch[8] = 2  # CurrentMode
    for i, w in enumerate((0.01, 0.02, 0.03)):
        ch[9 + i * 8 : 17 + i * 8] = struct.pack(">d", w)
    ch[33:41] = struct.pack(">d", 8.3)
    ch[41:49] = struct.pack(">d", 0.34)
    ch[49:57] = struct.pack(">d", 0.0)
    ch[57:61] = struct.pack(">f", 7716.2)
    ch[61:65] = struct.pack(">f", 8.7)
    ch[69:73] = seq_lora.to_bytes(4, "big")
    ch[77:81] = rx.to_bytes(4, "big")
    payload = bytearray(15)
    payload[2:4] = (1).to_bytes(2, "big")  # pktid 1 = Beacon
    payload[7:11] = secs.to_bytes(4, "big")
    payload += ch
    sp = (4).to_bytes(2, "big") + b"\xc0\x00" + (len(payload) - 1).to_bytes(2, "big")
    gv = (scid << 4) | (vcid << 1)
    f = bytearray(gv.to_bytes(2, "big") + bytes([mc, mc]) + b"\x18\x00" + sp + payload)
    f += b"\x00" * (246 - len(f))
    f += crc16(bytes(f)).to_bytes(2, "big")
    assert len(f) == 248
    return bytes(f)


def insert_bit(b: bytes, bit=0, keep_len=True) -> bytes:
    """Prepend one bit. keep_len drops the last bit (fixed-length radio payload)."""
    n = len(b) * 8
    v = (bit << n) | int.from_bytes(b, "big")  # n+1 bits
    if keep_len:
        return (v >> 1).to_bytes(len(b), "big")
    return (v << 7).to_bytes(len(b) + 1, "big")


def packet(pid, raw: bytes, ms=1786371482281, **extra):
    p = {
        "id": pid,
        "serverTime": ms,
        "raw": base64.b64encode(raw).decode(),
        "freq": 437.4,
    }
    p.update(extra)
    return p


def write_fetch(path, packets=None, sat_resp=None, slug="PROVES_Electra"):
    d = {}
    if sat_resp is not None:
        d[f"https://api.tinygs.com/v3/satellite/{slug}"] = sat_resp
    if packets is not None:
        d[f"https://api.tinygs.com/v4/packets?satellite={slug}"] = {"packets": packets}
    with open(path, "w") as f:
        json.dump(d, f)
    return path


def fixture(name):
    return os.path.join(FIXTURES, name)


def run_cli(script, *args):
    return subprocess.run(
        [sys.executable, os.path.join(PKG, script), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=300,
    )
