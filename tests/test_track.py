"""proves_track / raw_track / logio: routing, schema migration, lastTlm, FETCH-FAILED."""

import csv
import json
import os
import tempfile
import unittest

import helpers as h
from proves_track import FIELDS


def read_csv(path):
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


OLD_HEADER = FIELDS[:12]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def p(self, *parts):
        return os.path.join(self.d, *parts)


class Routing(Base):
    def test_alcyone_fetch_routes_scid3_to_electra_log(self):
        electra, alcyone = self.p("electra", "log.csv"), self.p("alcyone", "log.csv")
        r = h.run_cli(
            "proves_track.py",
            h.fixture("alcyone_fetch.json"),
            alcyone,
            "--source-sat",
            "alcyone",
            "--route",
            f"3={electra}",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new_frames=2", r.stdout)
        hdr, rows = read_csv(electra)
        self.assertEqual(hdr, FIELDS)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["scid"], "3")
            self.assertEqual(row["crc_ok"], "True")
            self.assertEqual(row["deslipped"], "True")
            self.assertEqual(row["source_sat"], "alcyone")
            self.assertEqual(row["BootCount"], "21")
        hdr, rows = read_csv(alcyone)  # created (header only), nothing native
        self.assertEqual((hdr, rows), (FIELDS, []))
        # dedupe across the target log on re-run
        r = h.run_cli(
            "proves_track.py",
            h.fixture("alcyone_fetch.json"),
            alcyone,
            "--route",
            f"3={electra}",
        )
        self.assertIn("new_frames=0", r.stdout)
        self.assertEqual(len(read_csv(electra)[1]), 2)

    def test_crc_bad_scid3_is_not_routed(self):
        f = bytearray(h.make_frame(scid=3))
        f[100] ^= 0xFF  # unrecoverable: reads SCID 3 but CRC fails
        good1 = h.make_frame(scid=1, boot=4)
        fetch = h.write_fetch(
            self.p("f.json"),
            [
                h.packet("bad3", h.HEADER + bytes(f), ms=1786371482000),
                h.packet("good1", h.HEADER + good1, ms=1786371483000),
            ],
            slug="PROVES_Alcyone",
        )
        electra, alcyone = self.p("e.csv"), self.p("a.csv")
        r = h.run_cli("proves_track.py", fetch, alcyone, "--route", f"3={electra}")
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = [row["id"] for row in read_csv(alcyone)[1]]
        self.assertEqual(sorted(ids), ["bad3", "good1"])
        self.assertFalse(os.path.exists(electra) and read_csv(electra)[1])
        self.assertIn("failed CRC", r.stdout)

    def test_alerts_per_target_log(self):
        electra, alcyone = self.p("e.csv"), self.p("a.csv")
        alerts = self.p("alerts.log")
        seed = {k: "" for k in FIELDS}
        with open(electra, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerow(
                seed
                | {
                    "id": "old-e",
                    "serverTime": "2026-08-01T00:00:00+00:00",
                    "scid": 3,
                    "crc_ok": "True",
                    "BootCount": 20,
                    "LoraBytesReceived": 198,
                }
            )
        with open(alcyone, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerow(
                seed
                | {
                    "id": "old-a",
                    "serverTime": "2026-08-09T00:00:00+00:00",
                    "scid": 1,
                    "crc_ok": "True",
                    "BootCount": 99,
                }
            )
        r = h.run_cli(
            "proves_track.py",
            h.fixture("alcyone_fetch.json"),
            alcyone,
            "--source-sat",
            "alcyone",
            "--route",
            f"3={electra}",
            "--alerts-file",
            alerts,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        alert_lines = [ln for ln in r.stdout.splitlines() if ln.startswith("ALERT")]
        self.assertEqual(len(alert_lines), 1, r.stdout)
        self.assertIn("REBOOT: BootCount changed 20 -> [21]", alert_lines[0])
        with open(alerts) as f:
            logged = f.read().splitlines()
        self.assertEqual(len(logged), 1)
        self.assertRegex(logged[0], r"^\d{4}-\d\d-\d\dT\S+ \[alcyone\] ALERT REBOOT")
        self.assertIn(f"(log={electra})", logged[0])


class Migration(Base):
    def test_old_header_migrated_then_appended(self):
        log = self.p("log.csv")
        old_row = ["x-old", "2026-07-08T00:00:00+00:00", "3", "1", "True", ""]
        old_row += ["20", "2", "8.3", "0", "0", "198"]
        with open(log, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(OLD_HEADER)
            w.writerow(old_row)
        r = h.run_cli("proves_track.py", h.fixture("electra_fetch.json"), log)
        self.assertEqual(r.returncode, 0, r.stderr)
        hdr, rows = read_csv(log)
        self.assertEqual(hdr, FIELDS)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["id"], "x-old")
        self.assertEqual(rows[0]["BootCount"], "20")
        self.assertEqual((rows[0]["source_sat"], rows[0]["deslipped"]), ("", ""))
        self.assertTrue(all(r_["deslipped"] == "False" for r_ in rows[1:]))
        self.assertEqual(os.listdir(self.d), ["log.csv"])  # no temp leftovers

    def test_migrate_is_noop_when_current(self):
        from logio import ensure_header

        log = self.p("log.csv")
        ensure_header(log, FIELDS)
        m = os.stat(log).st_mtime_ns
        self.assertEqual(ensure_header(log, FIELDS), FIELDS)
        self.assertEqual(os.stat(log).st_mtime_ns, m)


class FetchFailed(Base):
    def test_no_packets_key_exit_2(self):
        fetch = h.write_fetch(self.p("f.json"), packets=None, sat_resp={"name": "x"})
        for script in ("proves_track.py", "raw_track.py"):
            r = h.run_cli(script, fetch, self.p("log.csv"))
            self.assertEqual(r.returncode, 2, script)
            self.assertIn("FETCH-FAILED: no packets response captured", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_empty_packets_is_not_a_failure(self):
        fetch = h.write_fetch(self.p("f.json"), packets=[])
        r = h.run_cli("proves_track.py", fetch, self.p("log.csv"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new_frames=0", r.stdout)


class LastTlm(Base):
    def test_dedupe_and_time(self):
        out = self.p("lasttlm.csv")
        for _ in range(2):
            r = h.run_cli(
                "proves_track.py",
                h.fixture("electra_fetch.json"),
                self.p("log.csv"),
                "--source-sat",
                "electra",
                "--lasttlm-csv",
                out,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
        hdr, rows = read_csv(out)
        self.assertEqual(hdr, ["fetched_at", "source_sat", "tlm_time", "tlm_json"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_sat"], "electra")
        self.assertEqual(rows[0]["tlm_time"], "2026-09-23T05:32:51.968000+00:00")
        tlm = json.loads(rows[0]["tlm_json"])
        self.assertEqual(tlm["spacePacket"]["payload"]["beacon"]["bootCount"], 37)

        # a changed telemetry (legacy `lastTlm` key) adds exactly one row
        fetch = h.write_fetch(
            self.p("f.json"),
            packets=[],
            sat_resp={"lastTlm": {"timestamp": 1790000000000, "v": 8.1}},
        )
        for _ in range(2):
            h.run_cli("proves_track.py", fetch, self.p("log.csv"), "--lasttlm-csv", out)
        rows = read_csv(out)[1]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["tlm_time"], "2026-09-21T14:13:20+00:00")

    def test_absent_is_silent(self):
        fetch = h.write_fetch(self.p("f.json"), packets=[], sat_resp={"name": "x"})
        out = self.p("lasttlm.csv")
        r = h.run_cli("proves_track.py", fetch, self.p("log.csv"), "--lasttlm-csv", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(out))


class RawTrack(Base):
    def test_hucsat(self):
        log = self.p("hucsat-1", "log.csv")
        r = h.run_cli(
            "raw_track.py",
            h.fixture("hucsat_fetch.json"),
            log,
            "--source-sat",
            "hucsat-1",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new_frames=3", r.stdout)
        hdr, rows = read_csv(log)
        self.assertEqual(
            hdr,
            [
                "id",
                "serverTime",
                "source_sat",
                "frequency",
                "len",
                "raw_hex",
                "parsed_json",
                "n_stations",
            ],
        )
        self.assertEqual(sorted(int(r_["len"]) for r_ in rows), [16, 225, 227])
        for row in rows:
            self.assertEqual(row["source_sat"], "hucsat-1")
            self.assertEqual(len(row["raw_hex"]), 2 * int(row["len"]))
            self.assertIn("radioheadHeader", json.loads(row["parsed_json"]))
            self.assertTrue(row["n_stations"].isdigit())
            self.assertEqual(row["frequency"], "437.4")
        r = h.run_cli("raw_track.py", h.fixture("hucsat_fetch.json"), log)
        self.assertIn("new_frames=0", r.stdout)


class UplinkAlertTest(unittest.TestCase):
    """LoraBytesReceived resets on reboot; only same-boot increases are uplinks."""

    def state(self, boot, rx):
        from proves_track import LogState

        st = LogState(os.devnull)
        st.last_boot, st.last_rx = boot, rx
        return st

    def row(self, t, boot, rx):
        return {
            "serverTime": t,
            "crc_ok": True,
            "BootCount": boot,
            "LoraBytesReceived": rx,
            "SeqNumLora": 0,
        }

    def alerts(self, st, rows):
        from proves_track import alerts_for

        return [a for a in alerts_for(st, rows) if a.startswith("uplink")]

    def test_reset_after_reboot_is_not_uplink(self):
        st = self.state(11, 2392)
        self.assertEqual(self.alerts(st, [self.row("2026-09-21T00", 36, 382)]), [])

    def test_increase_within_boot_alerts(self):
        st = self.state(36, 382)
        rows = [self.row("2026-09-21T00", 36, 400), self.row("2026-09-21T01", 36, 450)]
        self.assertEqual(
            self.alerts(st, rows),
            ["uplink-activity: LoraBytesReceived 382 -> 450 (+68 within boot 36)"],
        )

    def test_increase_after_reboot_within_new_boot(self):
        st = self.state(11, 2392)
        rows = [self.row("2026-09-21T00", 12, 10), self.row("2026-09-21T01", 12, 30)]
        self.assertEqual(len(self.alerts(st, rows)), 1)


if __name__ == "__main__":
    unittest.main()
