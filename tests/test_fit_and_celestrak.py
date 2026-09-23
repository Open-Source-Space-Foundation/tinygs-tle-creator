"""fit_tle (--min-obs / --since-days / identity flags) and celestrak_fetch validation."""

import json
import math
import os
import tempfile
import unittest

import helpers as h
from celestrak_fetch import parse_tle, tle_checksum

ISS_L1 = "1 25544U 98067A   26188.50835634  .00005806  00000-0  11369-3 0  9991"
ISS_L2 = "2 25544  51.6304 199.5144 0006687 267.6545  92.3678 15.48933372574901"


def with_checksum(line):
    return line[:68] + str(tle_checksum(line))


def write_details(details_dir, n_packets, true_dM=0.0, t0=None):
    """Synthetic detail JSONs: Doppler from the ISS orbit shifted by true_dM."""
    import fit_tle as ft

    ref = ft.parse_ref(ISS_L1, ISS_L2)
    sat = ft.build_satrec(ref, {"dM": true_dM})
    stations = {
        "st_a": (40.0, -3.0, 120.0),
        "st_b": (48.0, 2.0, -300.0),
        "st_c": (35.0, 10.0, 50.0),
        "st_d": (52.0, 13.0, 0.0),
    }
    t0 = t0 or (ref.jdsatepoch + ref.jdsatepochF - 2440587.5) * 86400.0 + 3600
    os.makedirs(details_dir, exist_ok=True)
    for i in range(n_packets):
        t = t0 + (i % 12) * 40 + (i // 12) * 5580  # passes ~1 orbit apart
        st_list = []
        for name, (lat, lon, bias) in stations.items():
            jd, fr = divmod(t / 86400.0 + 2440587.5, 1.0)
            o = {
                "jd": jd,
                "fr": fr,
                "r_ecef": ft.geodetic_to_ecef(lat, lon, 0.1),
            }
            dop = float(ft.predicted_doppler_hz(sat, [o])[0])
            st_list.append(
                {
                    "name": name,
                    "location": [lat, lon],
                    "receptionParams": {"frequency_error": dop + bias},
                    "usec_time": int(t * 1e6),
                }
            )
        doc = {
            "https://api/packet/p%d" % i: {
                "id": f"p{i}",
                "serverTime": t * 1000,
                "stations": st_list,
            }
        }
        with open(os.path.join(details_dir, f"p{i}.json"), "w") as f:
            json.dump(doc, f)


class FitCLI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_min_obs_skip(self):
        det, out = f"{self.d}/det", f"{self.d}/out"
        write_details(det, 2)  # 8 observations
        r = h.run_cli("fit_tle.py", "--details-dir", det, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKIP: only 8 observations", r.stdout)
        self.assertFalse(os.path.exists(out))

    def test_since_days_filters_everything_old(self):
        det, out = f"{self.d}/det", f"{self.d}/out"
        write_details(det, 12)  # July 2026 epoch: older than 1 day
        r = h.run_cli(
            "fit_tle.py", "--details-dir", det, "--out", out, "--since-days", "1"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKIP: only 0 observations", r.stdout)
        self.assertFalse(os.path.exists(out))

    def test_fit_uses_ref_identity_and_stem(self):
        det, out = f"{self.d}/det", f"{self.d}/out"
        true_dM = math.radians(3.0)
        write_details(det, 36, true_dM=true_dM)
        ref = f"{self.d}/ref.tle"
        l1 = with_checksum("1 69799U 98067YP " + ISS_L1[17:])
        l2 = with_checksum("2 69799" + ISS_L2[7:])
        with open(ref, "w") as f:
            f.write(f"ISS OBJECT YP\n{l1}\n{l2}\n")
        r = h.run_cli(
            "fit_tle.py",
            "--details-dir", det,
            "--out", out,
            "--fit", "dM",
            "--ref-tle", ref,
            "--output-stem", "electra",
            "--f0", "437.4e6",
        )  # fmt: skip
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        with open(f"{out}/electra.tle") as f:
            name, t1, t2 = f.read().splitlines()
        self.assertEqual(t1[2:7], "69799")
        self.assertEqual(t1[9:17].strip(), "98067YP")
        self.assertEqual(t2[2:7], "69799")
        self.assertEqual(tle_checksum(t1), int(t1[68]))
        with open(f"{out}/fit_report.json") as f:
            rep = json.load(f)
        self.assertAlmostEqual(rep["published_deltas"]["dM"], true_dM, delta=0.005)
        self.assertFalse(os.path.exists(f"{out}/surv_proves.tle"))

    def test_builtin_ref_keeps_placeholder_identity(self):
        import fit_tle as ft

        ref = ft.parse_ref(ISS_L1, ISS_L2)
        t1, t2 = ft.make_tle(ISS_L1, ISS_L2, ref, {"dM": 0.1})
        self.assertEqual((t1[2:7], t1[9:17]), ("99999", "26999A  "))
        t1, _ = ft.make_tle(ISS_L1, ISS_L2, ref, {}, satnum="69799", intldes="98067YP")
        self.assertEqual((t1[2:7], t1[9:17]), ("69799", "98067YP "))
        self.assertEqual(len(t1), 69)


class Celestrak(unittest.TestCase):
    def test_parse_valid(self):
        name, l1, l2 = parse_tle(f"ISS (ZARYA)\r\n{ISS_L1}\r\n{ISS_L2}\r\n")
        self.assertEqual((name, l1, l2), ("ISS (ZARYA)", ISS_L1, ISS_L2))
        self.assertEqual(parse_tle(f"{ISS_L1}\n{ISS_L2}\n")[0], "")

    def test_rejects(self):
        bad_ck = ISS_L1[:68] + str((int(ISS_L1[68]) + 1) % 10)
        for text in (
            "No GP data found",
            "",
            f"{bad_ck}\n{ISS_L2}\n",
            f"{ISS_L1}\n",
            f"{ISS_L1}\n{with_checksum('2 11111' + ISS_L2[7:])}\n",
            "<html>Cloudflare</html>",
        ):
            with self.assertRaises(ValueError, msg=text):
                parse_tle(text)


if __name__ == "__main__":
    unittest.main()
