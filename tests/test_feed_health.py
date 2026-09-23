import unittest

import helpers  # noqa: F401  (puts tinygs_tle on sys.path)
from logio import feed_health, packets_from_fetch

SAT = "https://api.tinygs.com/v3/satellite/PROVES_Electra"
PK = "https://api.tinygs.com/v4/packets?satellite=PROVES_Electra"
H = 3_600_000


def capture(last_ms, newest_ms, auth=None):
    d = {
        SAT: {"lastPacketTime": last_ms},
        PK: {"packets": [{"serverTime": newest_ms - H}, {"serverTime": newest_ms}]},
    }
    if auth is not None:
        d["_meta"] = {"packets_request_authenticated": auth}
    return d


class FeedHealthTest(unittest.TestCase):
    def health(self, d):
        return feed_health(d, packets_from_fetch(d))

    def test_frozen_list_reports_lag(self):
        d = capture(last_ms=100 * H, newest_ms=68 * H, auth=False)
        self.assertEqual(self.health(d), "feed_lag_h=32.00 auth=false")

    def test_silent_satellite_is_not_stale(self):
        d = capture(last_ms=50 * H, newest_ms=50 * H, auth=True)
        self.assertEqual(self.health(d), "feed_lag_h=0.00 auth=true")

    def test_old_capture_without_meta_or_satellite(self):
        d = capture(last_ms=None, newest_ms=50 * H)
        self.assertEqual(self.health(d), "feed_lag_h=na auth=na")

    def test_meta_key_does_not_break_packet_lookup(self):
        d = capture(last_ms=1, newest_ms=1, auth=True)
        self.assertEqual(len(packets_from_fetch(d)), 2)


if __name__ == "__main__":
    unittest.main()
