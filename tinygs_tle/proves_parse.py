"""Parse TinyGS Surv-PROVES packets: LoRa hdr + CCSDS TM frame -> Beacon telemetry.

Frames that fail CRC are retried with one spurious leading bit removed
(see `deslip`); a recovered frame is reported with `deslipped: True`.

Usage:
    proves_parse.py FETCH_JSON
"""

import base64
import datetime
import os
import struct
import sys


def crc16(d):
    crc = 0xFFFF
    for byte in d:
        crc ^= byte << 8
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
            )
    return crc


LORA_HEADER = b"\x00\x00\x00\x00"  # LoRaConfig HEADER[] = {0,0,0,0}
FRAME_CRC_LEN = 248  # CRC16 over frame[:246], stored big-endian in frame[246:248]


def deslip(b: bytes, nbits: int = 1, fill: int | None = 0) -> bytes:
    """Drop the first `nbits` bits of `b` and re-pack into bytes.

    fill=0 or 1: keep the input length, shifting in `nbits` copies of that
    bit at the end. This is the normal case: the radio delivers a fixed-length
    payload, so a spurious leading bit pushes the frame's last bit (the CRC
    LSB) off the end and it has to be guessed.
    fill=None: drop the trailing partial byte instead (result one byte
    shorter), for payloads that carried an extra padding byte.
    """
    total = len(b) * 8 - nbits
    if total < 8:
        return b""
    v = int.from_bytes(b, "big") & ((1 << total) - 1)
    if fill is None:
        n = total // 8
        return (v >> (total - n * 8)).to_bytes(n, "big")
    pad = ((1 << nbits) - 1) if fill else 0
    return ((v << nbits) | pad).to_bytes(len(b), "big")


def _deslip_candidates(b: bytes, nbits: int = 1):
    for fill in (0, 1, None):
        yield deslip(b, nbits, fill)


def _crc_ok(frame: bytes) -> bool:
    return len(frame) >= FRAME_CRC_LEN and crc16(frame[:246]) == int.from_bytes(
        frame[246:248], "big"
    )


def _strip_header(b: bytes) -> tuple[bytes, bool]:
    if len(b) >= 4 and b[:4] == LORA_HEADER:
        return b[4:], True
    return b, False


def parse_frame(b):
    """b = raw LoRa payload. Returns dict (with `crc_ok` and `deslipped`).

    Parses as-is first. If the CRC fails, retries assuming one spurious
    leading bit (seen when TinyGS misfiled Electra frames under Alcyone):
    first on the frame after LoRa-header stripping (bit inserted after the
    header), then on the whole payload (bit inserted before the header,
    which then no longer reads as four 0x00 bytes). The first candidate
    whose CRC passes is returned with `deslipped: True` (each position is
    tried with the lost final bit guessed as 0 and 1, and truncated);
    otherwise the
    as-is parse is returned with `deslipped: False`.
    """
    frame, had_hdr = _strip_header(b)
    out = _parse_body(frame)
    if out.get("crc_ok"):
        out["deslipped"] = False
        return out
    candidates = []
    if had_hdr:
        candidates += list(_deslip_candidates(frame))
    candidates += [_strip_header(c)[0] for c in _deslip_candidates(b)]
    for cand in candidates:
        if _crc_ok(cand):
            fixed = _parse_body(cand)
            fixed["deslipped"] = True
            return fixed
    out["deslipped"] = False
    return out


def _parse_body(b):
    """Decode a CCSDS TM frame (LoRa header already stripped)."""
    if len(b) < 12:
        return {"error": f"too short ({len(b)})", "crc_ok": False}
    gv = int.from_bytes(b[0:2], "big")
    out = {
        "scid": (gv >> 4) & 0x3FF,
        "vcid": (gv >> 1) & 7,
        "mc": b[2],
        "vc": b[3],
        "frame_len": len(b),
        "crc_ok": _crc_ok(b),
    }
    # first space packet
    sp = b[6:]
    apid = int.from_bytes(sp[0:2], "big") & 0x7FF
    plen = int.from_bytes(sp[4:6], "big") + 1
    out["apid"] = apid
    payload = sp[6 : 6 + plen]
    if apid == 4 and len(payload) >= 15:
        pktid = int.from_bytes(payload[2:4], "big")
        secs = int.from_bytes(payload[7:11], "big")
        out["pktid"] = pktid
        out["sc_time"] = datetime.datetime.fromtimestamp(
            secs, datetime.timezone.utc
        ).isoformat()
        ch = payload[15:]
        if pktid == 1 and len(ch) >= 81:  # Beacon
            out["beacon"] = {
                "BootCount": int.from_bytes(ch[0:8], "big"),
                "CurrentMode": ch[8],
                "AngVel": [
                    round(struct.unpack(">d", ch[9 + i * 8 : 17 + i * 8])[0], 4)
                    for i in range(3)
                ],
                "Voltage": round(struct.unpack(">d", ch[33:41])[0], 2),
                "SysPower": round(struct.unpack(">d", ch[41:49])[0], 3),
                "SolPower": round(struct.unpack(">d", ch[49:57])[0], 3),
                "TotPowerCons": round(struct.unpack(">f", ch[57:61])[0], 1),
                "TotPowerGen": round(struct.unpack(">f", ch[61:65])[0], 1),
                "SeqNumLora": int.from_bytes(ch[69:73], "big"),
                "SeqNumUart": int.from_bytes(ch[73:77], "big"),
                "LoraBytesReceived": int.from_bytes(ch[77:81], "big"),
            }
    return out


def main(path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from logio import load_packets_or_exit

    _, packets = load_packets_or_exit(path)
    rows = []
    for p in packets:
        raw = base64.b64decode(p["raw"])
        r = parse_frame(raw)
        r["serverTime"] = datetime.datetime.fromtimestamp(
            p["serverTime"] / 1000, datetime.timezone.utc
        ).isoformat()
        r["id"] = p.get("id")
        rows.append(r)
    # dedupe by (mc, vc, sc_time)
    seen, uniq = set(), []
    for r in rows:
        k = (r.get("mc"), r.get("vc"), r.get("sc_time"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    print(f"{len(rows)} packets, {len(uniq)} unique frames")
    for r in sorted(uniq, key=lambda x: x["serverTime"]):
        b = r.get("beacon", {})
        print(
            f"{r['serverTime'][:19]} scid={r.get('scid')} vc={r.get('vc')} crc={'OK' if r.get('crc_ok') else 'BAD'}"
            + (" deslipped" if r.get("deslipped") else "")
            + f" sc_t={r.get('sc_time', '')[:19]}"
            + (
                f" boot={b['BootCount']} mode={b['CurrentMode']} V={b['Voltage']}"
                f" seqL={b['SeqNumLora']} seqU={b['SeqNumUart']} rxB={b['LoraBytesReceived']}"
                if b
                else f" apid={r.get('apid')}"
            )
        )


if __name__ == "__main__":
    main(sys.argv[1])
