"""Parse TinyGS Surv-PROVES packets: LoRa hdr + CCSDS TM frame -> Beacon telemetry."""

import base64
import datetime
import json
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


def parse_frame(b):
    """b = raw LoRa payload. Returns dict or None."""
    if len(b) >= 4 and b[:4] == b"\x00\x00\x00\x00":
        b = b[4:]  # LoRaConfig HEADER[] = {0,0,0,0}
    if len(b) < 12:
        return {"error": f"too short ({len(b)})"}
    gv = int.from_bytes(b[0:2], "big")
    out = {
        "scid": (gv >> 4) & 0x3FF,
        "vcid": (gv >> 1) & 7,
        "mc": b[2],
        "vc": b[3],
        "frame_len": len(b),
        "crc_ok": len(b) >= 248 and crc16(b[:246]) == int.from_bytes(b[246:248], "big"),
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
    d = json.load(open(path))
    pk = next(v for k, v in d.items() if "packets?" in k)
    rows = []
    for p in pk["packets"]:
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
            f" sc_t={r.get('sc_time', '')[:19]}"
            + (
                f" boot={b['BootCount']} mode={b['CurrentMode']} V={b['Voltage']}"
                f" seqL={b['SeqNumLora']} seqU={b['SeqNumUart']} rxB={b['LoraBytesReceived']}"
                if b
                else f" apid={r.get('apid')}"
            )
        )


if __name__ == "__main__":
    main(sys.argv[1])
