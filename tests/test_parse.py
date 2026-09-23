"""proves_parse: bit-slip (de-slip) recovery."""

import base64
import json
import unittest

import helpers as h
from proves_parse import crc16, deslip, parse_frame


class DeslipHelper(unittest.TestCase):
    def test_modes(self):
        b = bytes([0b10000000, 0b00000001])
        self.assertEqual(deslip(b, 1, None), b"\x00")
        self.assertEqual(deslip(b, 1), b"\x00\x02")
        self.assertEqual(deslip(b, 1, 1), b"\x00\x03")

    def test_roundtrip(self):
        f = h.make_frame()
        self.assertEqual(deslip(h.insert_bit(f, keep_len=False), 1, None), f)
        self.assertEqual(deslip(h.insert_bit(f, keep_len=True), 1, f[-1] & 1), f)


class ParseFrame(unittest.TestCase):
    def test_clean_frame_not_deslipped(self):
        r = parse_frame(h.HEADER + h.make_frame())
        self.assertTrue(r["crc_ok"])
        self.assertFalse(r["deslipped"])
        self.assertEqual(r["scid"], 3)
        self.assertEqual(r["beacon"]["BootCount"], 21)

    def test_slip_after_header_reads_scid1_until_repaired(self):
        f = h.make_frame()
        slipped = h.HEADER + h.insert_bit(f)  # fixed length: CRC LSB lost
        gv = int.from_bytes(slipped[4:6], "big")
        self.assertEqual((gv >> 4) & 0x3FF, 1)  # the misattribution
        r = parse_frame(slipped)
        self.assertTrue(r["crc_ok"])
        self.assertTrue(r["deslipped"])
        self.assertEqual(r["scid"], 3)
        self.assertEqual(r["beacon"]["BootCount"], 21)

    def test_lost_crc_bit_one(self):
        # find a frame whose CRC LSB is 1, so the fill=1 guess is needed
        for boot in range(1, 200):
            f = h.make_frame(boot=boot)
            if f[-1] & 1:
                break
        r = parse_frame(h.HEADER + h.insert_bit(f))
        self.assertTrue(r["deslipped"])
        self.assertEqual(r["beacon"]["BootCount"], boot)

    def test_slip_before_header(self):
        f = h.make_frame()
        for bit in (0, 1):
            r = parse_frame(h.insert_bit(h.HEADER + f, bit=bit))
            self.assertTrue(r["crc_ok"], bit)
            self.assertTrue(r["deslipped"])
            self.assertEqual(r["scid"], 3)

    def test_slip_with_extra_byte(self):
        r = parse_frame(h.HEADER + h.insert_bit(h.make_frame(), keep_len=False))
        self.assertTrue(r["deslipped"])
        self.assertEqual(r["scid"], 3)

    def test_garbage_stays_bad(self):
        f = bytearray(h.make_frame())
        f[100] ^= 0xFF
        r = parse_frame(h.HEADER + bytes(f))
        self.assertFalse(r["crc_ok"])
        self.assertFalse(r["deslipped"])
        self.assertEqual(r["scid"], 3)

    def test_short(self):
        r = parse_frame(b"\x00\x00\x00\x00\x01")
        self.assertIn("error", r)
        self.assertFalse(r["crc_ok"])
        self.assertFalse(r["deslipped"])

    def test_real_alcyone_frames_are_electra(self):
        # 2026-08-10: TinyGS filed these Electra frames under PROVES_Alcyone
        with open(h.fixture("alcyone_fetch.json")) as f:
            d = json.load(f)
        pk = next(v for k, v in d.items() if "packets?" in k)["packets"]
        self.assertEqual(len(pk), 2)
        for p in pk:
            r = parse_frame(base64.b64decode(p["raw"]))
            self.assertTrue(r["crc_ok"])
            self.assertTrue(r["deslipped"])
            self.assertEqual(r["scid"], 3)
            self.assertEqual(r["beacon"]["BootCount"], 21)

    def test_crc16_ccitt_false(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)


if __name__ == "__main__":
    unittest.main()
