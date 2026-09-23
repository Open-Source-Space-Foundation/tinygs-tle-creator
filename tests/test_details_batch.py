"""tinygs_details_batch: shared budget across sources, eligibility, block detection."""

import contextlib
import csv
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (sets sys.path)
import tinygs_details_batch as tdb


def write_log(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


class FakeRun:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, cmd, **kw):
        pid, out = cmd[2], cmd[4]
        self.calls.append((pid, os.path.dirname(out)))
        if self.ok:
            with open(out, "w") as f:
                f.write("{" + '"x": 1, ' * 30 + '"id": "%s"}' % pid)
        return subprocess.CompletedProcess(cmd, 0 if self.ok else 1, "", "")


class Budget(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self._tmp = tempfile.TemporaryDirectory()
        d = self.d = self._tmp.name
        self.e_log, self.e_dir = f"{d}/electra.csv", f"{d}/electra_details"
        self.h_log, self.h_dir = f"{d}/hucsat.csv", f"{d}/hucsat_details"
        write_log(
            self.e_log,
            ["id", "serverTime", "crc_ok"],
            [
                {"id": "e1", "serverTime": "2026-09-01T00:00:01", "crc_ok": "True"},
                {"id": "e2", "serverTime": "2026-09-01T00:00:02", "crc_ok": "False"},
                {"id": "e3", "serverTime": "2026-09-01T00:00:03", "crc_ok": "True"},
                {"id": "e4", "serverTime": "2026-09-01T00:00:04", "crc_ok": "True"},
                {"id": "lasttlm-1", "serverTime": "2026-09-01T00:00:05", "crc_ok": ""},
            ],
        )
        os.makedirs(self.e_dir)
        open(f"{self.e_dir}/e4.json", "w").close()  # already archived
        write_log(
            self.h_log,
            ["id", "serverTime", "raw_hex"],  # raw_track log: no crc_ok column
            [
                {"id": f"h{i}", "serverTime": f"2026-09-02T00:00:0{i}", "raw_hex": ""}
                for i in range(1, 5)
            ],
        )
        self.lock = f"{d}/.lock"
        self.sources = [(self.e_log, self.e_dir), (self.h_log, self.h_dir)]

    def tearDown(self):
        self._tmp.cleanup()

    def test_budget_filled_in_priority_order(self):
        fake = FakeRun()
        with mock.patch.object(tdb.subprocess, "run", fake):
            rc = tdb.run(self.sources, self.lock, max_per_run=4, spacing_s=0)
        self.assertEqual(rc, 0)
        self.assertEqual(
            fake.calls,
            [
                ("e3", self.e_dir),
                ("e1", self.e_dir),
                ("h4", self.h_dir),
                ("h3", self.h_dir),
            ],
        )
        self.assertFalse(os.path.exists(self.lock))
        self.assertTrue(os.path.exists(f"{self.h_dir}/h4.json"))
        # next run picks up the rest
        fake2 = FakeRun()
        with mock.patch.object(tdb.subprocess, "run", fake2):
            tdb.run(self.sources, self.lock, max_per_run=4, spacing_s=0)
        self.assertEqual([c[0] for c in fake2.calls], ["h2", "h1"])

    def test_all_failed_exits_nonzero(self):
        fake = FakeRun(ok=False)
        with mock.patch.object(tdb.subprocess, "run", fake):
            rc = tdb.run(self.sources, self.lock, max_per_run=3, spacing_s=0)
        self.assertEqual((rc, len(fake.calls)), (1, 3))
        self.assertEqual(os.listdir(self.e_dir), ["e4.json"])  # failures cleaned

    def test_few_failures_are_not_a_block(self):
        with mock.patch.object(tdb.subprocess, "run", FakeRun(ok=False)):
            rc = tdb.run(self.sources, self.lock, max_per_run=2, spacing_s=0)
        self.assertEqual(rc, 0)

    def test_fresh_lock_skips(self):
        open(self.lock, "w").close()
        fake = FakeRun()
        with mock.patch.object(tdb.subprocess, "run", fake):
            rc = tdb.run(self.sources, self.lock, max_per_run=4, spacing_s=0)
        self.assertEqual((rc, fake.calls), (0, []))
        self.assertTrue(os.path.exists(self.lock))

    def test_parse_source(self):
        self.assertEqual(
            tdb.parse_source("/a/log.csv:/a/det"), ("/a/log.csv", "/a/det")
        )


if __name__ == "__main__":
    unittest.main()
