import datetime as dt
import unittest

import helpers  # noqa: F401  (puts tinygs_tle on sys.path)
from electra_ops import _switchover

SOLAR, BATT, MID = (10.22, 0.0), (8.372, 0.378), (9.336, 0.0)


T0 = dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc)


def beacons(*vs, step=16, boot=37, sc0=1000, t0=T0):
    return [{"sc_time": sc0 + k * step, "t": t0 + dt.timedelta(seconds=k * step), "crc_ok": True,
             "beacon": {"Voltage": v, "SysPower": p, "BootCount": boot}} for k, (v, p) in enumerate(vs)]


class Switchover(unittest.TestCase):
    def test_lone_sample_between_solar_and_battery(self):
        b = beacons(SOLAR, MID, BATT)
        self.assertTrue(_switchover(b[1], b))

    def test_battery_to_solar_direction(self):
        b = beacons(BATT, MID, SOLAR)
        self.assertTrue(_switchover(b[1], b))

    def test_two_in_band_samples_alert(self):
        b = beacons(SOLAR, MID, MID, BATT)
        self.assertFalse(_switchover(b[1], b))

    def test_same_state_on_both_sides_alerts(self):
        b = beacons(SOLAR, MID, SOLAR)
        self.assertFalse(_switchover(b[1], b))

    def test_neighbour_too_far_alerts(self):
        b = beacons(SOLAR, MID, BATT, step=60)
        self.assertFalse(_switchover(b[1], b))

    def test_latest_beacon_has_no_next_neighbour(self):
        b = beacons(SOLAR, MID)
        self.assertFalse(_switchover(b[1], b))

    def test_neighbours_from_another_boot_alert(self):
        old = beacons(SOLAR, BATT, step=40, sc0=5000)
        new = beacons(MID, MID, MID, boot=38, sc0=4900, t0=T0 + dt.timedelta(days=9), step=60)
        new[1]["sc_time"] = 5020
        b = sorted(old + new, key=lambda f: f["sc_time"])
        self.assertFalse(_switchover(new[1], b))

    def test_neighbour_far_in_receipt_time_alerts(self):
        b = beacons(SOLAR, MID, BATT)
        b[2]["t"] += dt.timedelta(days=9)
        self.assertFalse(_switchover(b[1], b))


if __name__ == "__main__":
    unittest.main()
