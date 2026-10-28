import unittest

import helpers  # noqa: F401  (puts tinygs_tle on sys.path)
from electra_ops import _switchover

SOLAR, BATT, MID = (10.22, 0.0), (8.372, 0.378), (9.336, 0.0)


def beacons(*vs, step=16):
    return [{"sc_time": 1000 + k * step, "beacon": {"Voltage": v, "SysPower": p}} for k, (v, p) in enumerate(vs)]


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


if __name__ == "__main__":
    unittest.main()
