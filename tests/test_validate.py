"""Offline tests for validate_data.py. No test calls the internet: Kraken's and CoinGecko's replies are samples in the documented shape.

    python tests/test_validate.py -v

The main test injects about 40 different faults, one at a time, into a clean set of candles and checks two things for each:
  1. the validator reports the right check as FAIL or WARN (and nothing else goes wrong),
  2. whether the agent's own checks (parse_candles and completed_closes) would already have refused the data.
The second column is a fact about the code, written down so nobody has to guess: the agent only uses time, close and volume,
so a fault in open, high, low, vwap or the trade count does not stop it. The validator is what catches those.
"""

import copy
import csv
import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import validate_data as vd  # noqa: E402

DAY = 86400
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TODAY = int(NOW.timestamp()) // DAY * DAY
DONE = 200                                   # completed days; row DONE is today's unfinished candle
MID = 100                                    # the row the faults are put into (a completed day with a neighbour on each side)


def good_rows(seed=1):
    """Clean candles: open is the previous close, high and low enclose open and close, vwap sits between them, daily moves are about 1%."""
    rows, previous = [], 100.0
    for k in range(DONE + 1):
        stamp = TODAY - (DONE - k) * DAY
        close = 100.0 + 10 * math.sin(k / 9.0 + seed) + 1.0 * math.sin(k * 1.3)
        open_ = previous
        high = max(open_, close) * 1.01
        low = min(open_, close) * 0.99
        rows.append([stamp, f"{open_:.2f}", f"{high:.2f}", f"{low:.2f}", f"{close:.2f}", f"{(open_ + close) / 2:.2f}",
                     f"{1000 + 50 * math.cos(k / 3.0):.4f}", 100 + k])
        previous = float(f"{close:.2f}")
    return rows


def statuses(rows, coin="BTC"):
    return {r["check"]: r["status"] for r in vd.check_rows(coin, rows, NOW)}


def edit(row, column, value):
    def mutate(rows):
        rows[row][column] = value
        return rows
    return mutate


def edit_with(row, column, function):
    def mutate(rows):
        rows[row][column] = function(rows[row])
        return rows
    return mutate


def delete(index):
    def mutate(rows):
        del rows[index]
        return rows
    return mutate


def swap(a, b):
    def mutate(rows):
        rows[a], rows[b] = rows[b], rows[a]
        return rows
    return mutate


def agent_rejects(rows):
    try:
        agent.completed_closes(agent.parse_candles(rows, "XBTUSD"), NOW)
    except ValueError:
        return True
    return False


T, O, H, L, C, V, VOL, N = range(8)   # column positions in a Kraken candle: time, open, high, low, close, vwap, volume, count

# name -> (how to break clean data, {check: expected status}, does the agent's own code already refuse it)
FAULTS = {
    # ---- timestamps
    "a candle shifted by one hour": (edit_with(MID, T, lambda r: r[T] + 3600), {"timestamp_midnight": vd.FAIL, "no_missing_days": vd.FAIL}, True),
    "the same day twice": (edit_with(MID, T, lambda r: r[T] - DAY), {"timestamp_order": vd.FAIL, "no_missing_days": vd.FAIL}, True),
    "two candles in the wrong order": (swap(MID, MID + 1), {"timestamp_order": vd.FAIL, "no_missing_days": vd.PASS}, True),
    "a missing day": (delete(MID), {"no_missing_days": vd.FAIL, "timestamp_order": vd.PASS}, True),
    "a time in milliseconds": (edit_with(MID, T, lambda r: r[T] * 1000), {"timestamp_valid": vd.FAIL}, True),
    "a time as text": (edit_with(MID, T, lambda r: str(r[T])), {"timestamp_valid": vd.FAIL}, True),
    "a time as a decimal": (edit_with(MID, T, lambda r: float(r[T])), {"timestamp_valid": vd.FAIL}, True),
    "a time as true": (edit(MID, T, True), {"timestamp_valid": vd.FAIL}, True),
    "a time before 2013": (edit(MID, T, 0), {"timestamp_valid": vd.FAIL}, True),
    "a candle that starts tomorrow": (lambda rows: rows + [[TODAY + DAY] + rows[-1][1:]], {"timestamp_valid": vd.FAIL}, True),
    "today's unfinished candle twice": (lambda rows: rows + [list(rows[-1])], {"timestamp_order": vd.FAIL, "no_missing_days": vd.PASS}, True),
    "today's unfinished candle before yesterday's": (swap(-2, -1), {"timestamp_order": vd.FAIL, "no_missing_days": vd.PASS}, True),
    "stale data (newest completed day is two days old)": (lambda rows: rows[:-2], {"latest_day_is_yesterday": vd.FAIL}, True),
    "only today's unfinished candle": (lambda rows: rows[-1:], {"latest_day_is_yesterday": vd.FAIL, "enough_history": vd.FAIL}, True),
    "only 50 days of history": (lambda rows: rows[-51:], {"enough_history": vd.FAIL, "no_missing_days": vd.PASS}, True),
    # ---- shape
    "a candle with 7 values": (lambda rows: rows.__setitem__(MID, rows[MID][:7]) or rows, {"row_shape": vd.FAIL}, True),
    "a candle with 9 values": (lambda rows: rows.__setitem__(MID, rows[MID] + [1]) or rows, {"row_shape": vd.FAIL}, True),
    "a candle that is not a list": (lambda rows: rows.__setitem__(MID, "oops") or rows, {"row_shape": vd.FAIL}, True),
    "no candles": (lambda rows: [], {"row_shape": vd.FAIL}, True),
    # ---- prices
    "a negative close": (edit(MID, C, "-5"), {"price_values": vd.FAIL}, True),
    "a zero close": (edit(MID, C, "0"), {"price_values": vd.FAIL}, True),
    "a close of NaN": (edit(MID, C, "nan"), {"price_values": vd.FAIL}, True),
    "a close of infinity": (edit(MID, C, "inf"), {"price_values": vd.FAIL}, True),
    "a close that is a word": (edit(MID, C, "abc"), {"price_values": vd.FAIL}, True),
    "a close with an underscore": (edit(MID, C, "1_000"), {"price_values": vd.FAIL}, True),
    "a close in full-width digits": (edit(MID, C, "\uff16\uff10\uff14\uff15\uff12"), {"price_values": vd.FAIL}, True),
    "a close that is missing (null)": (edit(MID, C, None), {"price_values": vd.FAIL}, True),
    "a close of true": (edit(MID, C, True), {"price_values": vd.FAIL}, True),
    "a negative open": (edit(MID, O, "-1"), {"price_values": vd.FAIL}, True),
    "a zero high": (edit(MID, H, "0"), {"price_values": vd.FAIL}, True),
    "a vwap that is a word": (edit(MID, V, "n/a"), {"price_values": vd.FAIL}, False),
    "a negative vwap": (edit(MID, V, "-3"), {"price_values": vd.FAIL}, False),
    # ---- the day's range
    "high below low": (lambda rows: rows[MID].__setitem__(H, "50.00") or rows[MID].__setitem__(L, "60.00") or rows, {"high_low_consistent": vd.FAIL}, True),
    "close above high": (edit(MID, C, "999.00"), {"high_low_consistent": vd.FAIL}, True),
    "open below low": (edit(MID, O, "1.00"), {"high_low_consistent": vd.FAIL}, True),
    "vwap above high": (edit(MID, V, "999.00"), {"vwap_in_range": vd.FAIL, "high_low_consistent": vd.PASS}, False),
    "vwap below low": (edit(MID, V, "1.00"), {"vwap_in_range": vd.FAIL, "high_low_consistent": vd.PASS}, False),
    # ---- volume and trades
    "a negative volume": (edit(MID, VOL, "-1"), {"volume_trades_values": vd.FAIL}, True),
    "a volume that is a word": (edit(MID, VOL, "lots"), {"volume_trades_values": vd.FAIL}, True),
    "a trade count with a decimal": (edit(MID, N, 3.5), {"volume_trades_values": vd.FAIL}, True),
    "a negative trade count": (edit(MID, N, -4), {"volume_trades_values": vd.FAIL}, True),
    "a trade count of true": (edit(MID, N, True), {"volume_trades_values": vd.FAIL}, True),
    # ---- unusual but possible: WARN
    "a day with no trading": (lambda rows: rows[MID].__setitem__(VOL, "0.0") or rows[MID].__setitem__(V, "0.0") or rows,
                              {"zero_volume_days": vd.WARN, "price_values": vd.PASS, "vwap_in_range": vd.PASS, "volume_trades_values": vd.PASS}, False),
    "a 40% jump in one day": (lambda rows: rows[MID].__setitem__(C, f"{float(rows[MID][C]) * 1.4:.2f}") or rows[MID].__setitem__(H, f"{float(rows[MID][C]) * 1.01:.2f}") or rows,
                              {"extreme_daily_moves": vd.WARN, "high_low_consistent": vd.PASS}, False),
    "an open 10% away from the previous close": (lambda rows: rows[MID].__setitem__(O, f"{float(rows[MID - 1][C]) * 1.10:.2f}") or rows[MID].__setitem__(H, f"{float(rows[MID][O]) * 1.02:.2f}") or rows,
                                                 {"open_vs_previous_close": vd.WARN, "high_low_consistent": vd.PASS}, False),
}


class CleanData(unittest.TestCase):
    def test_clean_data_passes_every_check(self):
        report = vd.check_rows("BTC", good_rows(), NOW)
        self.assertEqual({r["status"] for r in report}, {vd.PASS}, [r for r in report if r["status"] != vd.PASS])

    def test_clean_data_is_accepted_by_the_agent_too(self):
        self.assertFalse(agent_rejects(good_rows()))

    def test_todays_unfinished_candle_with_partial_values_is_fine(self):
        rows = good_rows()
        rows[-1][VOL] = "0.5"
        self.assertEqual({v for v in statuses(rows).values()}, {vd.PASS})

    def test_the_numbers_are_text_as_kraken_sends_them(self):
        rows = good_rows()
        self.assertIsInstance(rows[5][C], str)                     # Kraken sends prices and volume as text
        self.assertEqual({v for v in statuses(rows).values()}, {vd.PASS})


class InjectedFaults(unittest.TestCase):
    def test_every_fault_is_reported_by_the_right_check(self):
        for name, (mutate, expected, _) in FAULTS.items():
            with self.subTest(fault=name):
                got = statuses(mutate(good_rows()))
                for check, status in expected.items():
                    self.assertEqual(got.get(check), status, f"{name}: {check} expected {status}, got {got.get(check)} (all: {got})")

    def test_a_fault_never_makes_the_validator_crash_or_hides_other_checks(self):
        for name, (mutate, _, _) in FAULTS.items():
            with self.subTest(fault=name):
                report = vd.check_rows("BTC", mutate(good_rows()), NOW)
                self.assertGreaterEqual(len(report), 1)
                for r in report:
                    self.assertIn(r["status"], (vd.PASS, vd.WARN, vd.FAIL, vd.SKIP))
                    self.assertTrue(r["detail"])

    def test_each_fault_is_reported_only_where_it_belongs(self):
        """A single-field fault must not turn unrelated checks red."""
        unrelated = {"a negative close": {"timestamp_valid", "timestamp_midnight", "timestamp_order", "no_missing_days", "volume_trades_values", "vwap_in_range"},
                     "a negative volume": {"price_values", "high_low_consistent", "timestamp_valid", "no_missing_days"},
                     "a time in milliseconds": {"price_values", "volume_trades_values", "high_low_consistent"}}
        for name, checks in unrelated.items():
            got = statuses(FAULTS[name][0](good_rows()))
            for check in checks:
                self.assertEqual(got[check], vd.PASS, f"{name} should not touch {check}")

    def test_what_the_agents_own_checks_already_refuse(self):
        for name, (mutate, _, refused) in FAULTS.items():
            with self.subTest(fault=name):
                self.assertEqual(agent_rejects(mutate(good_rows())), refused, name)

    def test_faults_in_fields_the_agent_ignores_are_only_caught_by_the_validator(self):
        for name in ("vwap above high", "vwap below low", "a negative vwap", "a vwap that is a word", "a day with no trading",
                     "a 40% jump in one day", "an open 10% away from the previous close"):
            mutate, expected, refused = FAULTS[name]
            rows = mutate(good_rows())
            self.assertFalse(agent_rejects(rows), name)                 # the agent would carry on ...
            self.assertTrue(any(s != vd.PASS for s in statuses(rows).values()), name)   # ... and the validator says so

    def test_several_faults_at_once_are_all_reported(self):
        rows = good_rows()
        rows[MID][C] = "nan"
        rows[MID + 5][VOL] = "-2"
        del rows[MID + 20]
        got = statuses(rows)
        self.assertEqual((got["price_values"], got["volume_trades_values"], got["no_missing_days"]), (vd.FAIL, vd.FAIL, vd.FAIL))

    def test_the_detail_names_the_day_of_the_fault(self):
        rows = FAULTS["a negative close"][0](good_rows())
        detail = [r["detail"] for r in vd.check_rows("BTC", rows, NOW) if r["check"] == "price_values"][0]
        self.assertIn(agent.format_date(rows[MID][T]), detail)

    def test_many_faults_are_summarised_not_listed_in_full(self):
        rows = good_rows()
        for i in range(10, 30):
            rows[i][C] = "-1"
        detail = [r["detail"] for r in vd.check_rows("BTC", rows, NOW) if r["check"] == "price_values"][0]
        self.assertIn("20 candle(s)", detail)
        self.assertIn("and 17 more", detail)


class IndependentSource(unittest.TestCase):
    def cg_for(self, rows, factor=1.0):
        """CoinGecko's daily points: the price at 00:00 UTC of day D, which is Kraken's close of day D-1."""
        return {agent.format_date(r[T] + DAY): float(r[C]) * factor for r in rows[:-1]}

    def test_prices_that_agree_pass(self):
        rows = good_rows()
        r = vd.check_against_coingecko("BTC", rows, self.cg_for(rows, 1.002), NOW)
        self.assertEqual(r["status"], vd.PASS)
        self.assertIn("14 latest days compared", r["detail"])

    def test_a_gap_of_one_and_a_half_percent_is_a_warning(self):
        rows = good_rows()
        self.assertEqual(vd.check_against_coingecko("BTC", rows, self.cg_for(rows, 1.015), NOW)["status"], vd.WARN)

    def test_a_gap_of_more_than_three_percent_fails(self):
        rows = good_rows()
        self.assertEqual(vd.check_against_coingecko("BTC", rows, self.cg_for(rows, 1.05), NOW)["status"], vd.FAIL)

    def test_one_wrong_day_among_good_ones_is_enough_to_fail(self):
        rows = good_rows()
        cg = self.cg_for(rows)
        cg[agent.format_date(rows[DONE - 3][T] + DAY)] *= 1.2
        r = vd.check_against_coingecko("BTC", rows, cg, NOW)
        self.assertEqual(r["status"], vd.FAIL)
        self.assertIn(agent.format_date(rows[DONE - 3][T]), r["detail"])

    def test_the_wrong_unit_or_pair_is_caught(self):
        rows = good_rows()
        self.assertEqual(vd.check_against_coingecko("BTC", rows, self.cg_for(rows, 100.0), NOW)["status"], vd.FAIL)   # cents instead of dollars
        self.assertEqual(vd.check_against_coingecko("BTC", rows, self.cg_for(rows, 0.5), NOW)["status"], vd.FAIL)     # another coin

    def test_the_comparison_pairs_the_right_days(self):
        """Kraken's close of 2026-11-18 is compared with CoinGecko's 00:00 UTC point of 2026-11-19, worked out by hand: 100 / 100.4 - 1 = -0.398%."""
        rows = [[TODAY - 2 * DAY, "1", "1", "1", "100.0", "1", "1", 1], [TODAY - DAY, "1", "1", "1", "101.0", "1", "1", 1], [TODAY, "1", "1", "1", "1", "1", "1", 1]]
        cg = {agent.format_date(TODAY - DAY): 100.4, agent.format_date(TODAY): 101.0}
        r = vd.check_against_coingecko("BTC", rows, cg, NOW, days=5)
        self.assertEqual(r["status"], vd.PASS)
        self.assertIn("2 latest days compared", r["detail"])
        self.assertIn("-0.40% on " + agent.format_date(TODAY - 2 * DAY), r["detail"])

    def test_no_data_from_coingecko_is_skip_not_fail(self):
        rows = good_rows()
        self.assertEqual(vd.check_against_coingecko("BTC", rows, None, NOW)["status"], vd.SKIP)
        self.assertEqual(vd.check_against_coingecko("BTC", rows, {}, NOW)["status"], vd.SKIP)
        self.assertEqual(vd.check_against_coingecko("BTC", rows, {"1999-01-01": 5.0}, NOW)["status"], vd.SKIP)

    def test_only_points_stamped_at_midnight_utc_are_used(self):
        day_ms = DAY * 1000
        body = {"prices": [[TODAY * 1000, 100.0], [TODAY * 1000 + 3_600_000, 101.0], [(TODAY - DAY) * 1000, 99.0], "junk", [1], [True, 5], [(TODAY - 2 * DAY) * 1000, -3],
                           [(TODAY - 3 * DAY) * 1000, float("nan")], [(TODAY - 4 * DAY) * 1000, "x"], [(TODAY - 5 * DAY) * 1000, "98.5"]]}
        got = vd.coingecko_prices(body)
        self.assertEqual(got, {agent.format_date(TODAY): 100.0, agent.format_date(TODAY - DAY): 99.0, agent.format_date(TODAY - 5 * DAY): 98.5})
        self.assertEqual(vd.coingecko_prices({"prices": "no"}), {})
        self.assertEqual(vd.coingecko_prices(None), {})
        self.assertEqual(vd.coingecko_prices([1, 2]), {})
        self.assertEqual(day_ms, 86_400_000)

    def test_no_key_means_no_request(self):
        with mock.patch.object(vd.sources, "get_json") as call:
            self.assertIsNone(vd.fetch_coingecko("BTC", ""))
        call.assert_not_called()

    def test_a_source_that_is_down_is_none_not_an_exception(self):
        with mock.patch.object(vd.sources, "get_json", side_effect=RuntimeError("HTTP 500")):
            self.assertIsNone(vd.fetch_coingecko("BTC", "KEY"))

    def test_a_working_source_gives_prices(self):
        with mock.patch.object(vd.sources, "get_json", return_value={"prices": [[TODAY * 1000, 100.0]]}) as call:
            self.assertEqual(vd.fetch_coingecko("ETH", "KEY"), {agent.format_date(TODAY): 100.0})
        self.assertIn("/coins/ethereum/market_chart", call.call_args.args[0])


class WholeReport(unittest.TestCase):
    def raw(self):
        return {"BTC": good_rows(1), "ETH": good_rows(2), "XRP": good_rows(3)}

    def test_every_check_has_a_plain_language_line(self):
        cg = {coin: {agent.format_date(r[T] + DAY): float(r[C]) for r in rows[:-1]} for coin, rows in self.raw().items()}
        report = vd.validate(self.raw(), NOW, cg)
        self.assertEqual({r["check"] for r in report}, set(vd.COVERS))
        for line in vd.COVERS.values():
            self.assertGreater(len(line), 20)

    def test_a_coin_kraken_did_not_answer_is_one_fail_and_the_others_are_still_checked(self):
        raw = self.raw()
        del raw["XRP"]
        report = vd.validate(raw, NOW, {}, {"XRP": "XRPUSD: request to Kraken failed after 3 attempts (ConnectionError)"})
        xrp = [r for r in report if r["coin"] == "XRP"]
        self.assertEqual([(r["check"], r["status"]) for r in xrp], [("kraken_reply", vd.FAIL)])
        self.assertTrue(all(r["status"] != vd.FAIL for r in report if r["coin"] == "BTC"))

    def test_the_summary_counts_every_status(self):
        report = vd.validate(self.raw(), NOW, {})
        counts, line = vd.summary(report)
        self.assertEqual(sum(counts.values()), len(report))
        self.assertEqual(counts[vd.SKIP], 3)                      # the CoinGecko check, once per coin
        self.assertIn(f"{len(report)} checks", line)

    def run_main(self, raw_by_pair, *args, errors=None):
        def fetch(pair):
            if errors and pair in errors:
                raise RuntimeError(errors[pair])
            return copy.deepcopy(raw_by_pair[pair])
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        with mock.patch.object(agent, "fetch_rows", side_effect=fetch), mock.patch("builtins.print"):
            code = vd.main(["--out", out.name, "--no-coingecko", *args], now=NOW)
        return code, Path(out.name) / "data_quality.csv"

    def pairs(self, **changes):
        base = {"XBTUSD": good_rows(1), "ETHUSD": good_rows(2), "XRPUSD": good_rows(3)}
        base.update(changes)
        return base

    def test_exit_code_is_0_for_clean_data_and_the_csv_is_written(self):
        code, path = self.run_main(self.pairs())
        self.assertEqual(code, 0)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(list(rows[0]), ["coin", "check", "status", "detail", "what_it_catches"])
        self.assertEqual({r["coin"] for r in rows}, {"BTC", "ETH", "XRP", "ALL"})   # ALL: the check across coins
        self.assertEqual(sum(1 for r in rows if r["coin"] == "ALL"), 1)
        self.assertEqual(len(rows), 3 * 16 + 1)                                      # 16 checks per coin, one across coins
        self.assertTrue(all(r["what_it_catches"] for r in rows))

    def test_exit_code_is_0_when_there_are_only_warnings(self):
        code, _ = self.run_main(self.pairs(XBTUSD=FAULTS["a day with no trading"][0](good_rows(1))))
        self.assertEqual(code, 0)

    def test_exit_code_is_1_when_any_check_fails(self):
        code, path = self.run_main(self.pairs(ETHUSD=FAULTS["a missing day"][0](good_rows(2))))
        self.assertEqual(code, 1)
        text = path.read_text(encoding="utf-8")
        self.assertIn("ETH,no_missing_days,FAIL", text)

    def test_exit_code_is_1_when_kraken_is_down(self):
        code, path = self.run_main(self.pairs(), errors={"XRPUSD": "XRPUSD: request to Kraken failed after 3 attempts (ConnectionError)"})
        self.assertEqual(code, 1)
        self.assertIn("XRP,kraken_reply,FAIL", path.read_text(encoding="utf-8"))

    def test_the_comparison_with_coingecko_is_skipped_without_a_key(self):
        code, path = self.run_main(self.pairs())
        self.assertIn("coingecko_agreement,SKIP", path.read_text(encoding="utf-8"))


class MatchesTheExport(unittest.TestCase):
    def test_the_data_the_export_saves_passes_the_validator(self):
        """The candles written to kraken_candles.csv are the rows that were validated (same fetch, same rows)."""
        import export_data
        rows = {pair: good_rows(i) for i, pair in enumerate(agent.WATCHLIST.values(), start=1)}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(agent, "fetch_rows", side_effect=lambda p: copy.deepcopy(rows[p])):
            export_data.export(Path(tmp) / "x", NOW)
            with open(Path(tmp) / "x" / "kraken_candles.csv", newline="", encoding="utf-8") as handle:
                saved = [r for r in csv.DictReader(handle) if r["coin"] == "BTC"]
        back = [[int(r["time_unix"]), r["open"], r["high"], r["low"], r["close"], r["vwap"], r["volume"], int(r["trades"])] for r in saved]
        self.assertEqual(back, rows["XBTUSD"])
        self.assertEqual({v for v in statuses(back).values()}, {vd.PASS})


if __name__ == "__main__":
    unittest.main()
