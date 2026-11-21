"""Offline test: the "Coins at the alert rank" tile counts only coins that have data, and names the ones that do not.

    python -m unittest tests/test_fix_partial_tiles.py -v

With BTC unavailable and ETH and XRP calm, the tile used to read "0 of 3", as if all three coins had been checked.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import insights  # noqa: E402
import report  # noqa: E402

NOW_DAY = 1_800_000_000 // agent.SECONDS_PER_DAY * agent.SECONDS_PER_DAY
STAMPS = [NOW_DAY - (300 - i) * agent.SECONDS_PER_DAY for i in range(300)]
CALM_CLOSES = [100.0 + (i % 7) * 0.5 for i in range(300)]
CRASH_CLOSES = [100.0 + (i % 7) * 0.5 for i in range(299)] + [60.0]
STALE = "stale data: the latest completed candle is 2026-11-17"


def make_data(coins):
    return {"coins": coins, "cross": report.demo_cross(), "context": None, "decisions": [], "decisions_note": "", "demo": True,
            "generated": "2026-11-21 08:30", "generated_long": "Saturday 21 November 2026", "events": insights.event_study(coins)}


def tile(data):
    return next(t for t in report.tile_list(data) if t[0] == "Coins at the alert rank")


class AlertRankTile(unittest.TestCase):
    def test_all_coins_with_data_is_unchanged(self):
        data = make_data([report.analyse(c, STAMPS, CALM_CLOSES) for c in agent.WATCHLIST])
        self.assertEqual(tile(data)[1], "0 of 3")
        self.assertNotIn("unavailable", tile(data)[2])

    def test_a_coin_without_data_is_not_counted_and_is_named(self):
        data = make_data([{"coin": "BTC", "error": STALE}, report.analyse("ETH", STAMPS, CALM_CLOSES), report.analyse("XRP", STAMPS, CALM_CLOSES)])
        self.assertEqual(tile(data)[1], "0 of 2")
        self.assertIn("BTC unavailable", tile(data)[2])

    def test_an_alert_with_a_missing_coin_counts_out_of_the_working_coins(self):
        data = make_data([{"coin": "BTC", "error": STALE}, report.analyse("ETH", STAMPS, CRASH_CLOSES), report.analyse("XRP", STAMPS, CALM_CLOSES)])
        self.assertEqual(tile(data)[1], "1 of 2")
        self.assertIn("BTC unavailable", tile(data)[2])

    def test_no_data_at_all_still_gives_no_tiles(self):
        data = make_data([{"coin": c, "error": "down"} for c in agent.WATCHLIST])
        self.assertEqual(report.tile_list(data), [])


if __name__ == "__main__":
    unittest.main()
