"""Offline tests for fix D-01: the numbers in an alert message are checked against the evidence on the default path.

    python -m pytest tests/test_fix_d01.py -q

Before the fix the number check (unsupported_numbers) ran only inside the optional reviewer, which the daily workflow
switches off (--reviewer off). An alert message with invented numbers was delivered. Now apply_guardrails blocks it.
The "AI" here is a script of canned replies, and the evidence comes from the code's own functions, so every run is the same.
"""

import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import scenarios  # noqa: E402


# The fake market data are dated 2026-11-19. The freshness check (R6) blocks an alert on data that are not the latest completed day, so these
# tests run on the scenarios' fake clock, as the scenarios do. The tests that call decide() with their own clock are not affected.
_CLOCK = mock.patch.object(agent, "utc_now", lambda: scenarios.FAKE_NOW)


def setUpModule():
    _CLOCK.start()


def tearDownModule():
    _CLOCK.stop()

TITLES = [section["title"] for section in agent.load_playbook()]
DAY = 86400
END = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
SOURCES = ["Volatility rule", "Alert rules"]
REASON = "numbers not found in the evidence"
BTC_CLOSES = [67000.0 + 900.0 * math.sin(i / 4.0) + (i % 7) * 13.37 for i in range(199)] + [52918.37]


def draft(**changes):
    base = {"alert": True, "coins": ["BTC"], "message": "BTC volatility rank 99.3, above the alert rank of 95.",
            "sources": SOURCES, "reasoning": "BTC volatility rank 99.3 is at or above 95."}
    base.update(changes)
    return base


MARKETS_ONLY = [("get_market_summary", {"coin": "BTC"}), ("get_market_summary", {"coin": "ETH"}), ("get_market_summary", {"coin": "XRP"})]
ALL_TOOLS = MARKETS_ONLY + [("get_cross_asset_summary", {}), ("get_market_context", {}), ("get_alert_history_stats", {}),
                            ("get_recent_decisions", {}), ("search_playbook", {"query": "alert rules volatility trend drawdown"})]


def tool_calls(calls=ALL_TOOLS):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(calls)]}


def evidence_for(tools):
    """The evidence a full run builds: every tool read once, through the real run_tool."""
    evidence = []
    for coin in agent.WATCHLIST:
        agent.run_tool(tools, "get_market_summary", {"coin": coin}, evidence)
    for name in ("get_cross_asset_summary", "get_market_context", "get_alert_history_stats", "get_recent_decisions"):
        agent.run_tool(tools, name, {}, evidence)
    agent.run_tool(tools, "search_playbook", {"query": "alert rules volatility trend drawdown"}, evidence)
    return evidence


def real_summary(coin, closes):
    """A market summary made by the code's own summarize() and add_context() from a price series that ends on 2026-11-20."""
    stamps = [END - (len(closes) - 1 - i) * DAY for i in range(len(closes))]
    volumes = [1000.0 + (i * 37) % 211 for i in range(len(closes))]
    volumes[-1] = 3300.0
    return agent.add_context(agent.summarize(coin, stamps, closes), closes, volumes)


class QuietCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent, "log", lambda message: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_default_path(self, tools, message, reasoning="BTC rank is at or above 95.", calls=ALL_TOOLS, revisions=0, **changes):
        """agent.decide with the AI review off. The code review still runs, and a code finding gets one revision first: `revisions` is how
        many times the scripted AI repeats the same answer when asked to revise."""
        final = json.dumps(draft(message=message, reasoning=reasoning, **changes))
        replies = [tool_calls(calls)] + [{"role": "assistant", "content": final}] * (1 + revisions)
        # The clock is the day after the data (the fixtures end on 2026-11-19 or on 2026-11-20), so the freshness check (R6) sees the latest day.
        day = datetime.strptime(tools["get_market_summary"]("BTC")["as_of"], "%Y-%m-%d").replace(hour=5, minute=17, tzinfo=timezone.utc)
        with mock.patch.object(agent, "call_llm", side_effect=replies):
            return agent.decide("agent", False, tools, now=day + timedelta(days=1))


class AuditCaseL4(QuietCase):
    """The audit's loop_harness case L4: the same fake message, the same kind of evidence (a real summarize() on a crash series)."""
    def tools(self):
        calm = [100.0 + (i % 5) * 0.3 for i in range(200)]
        crash = [100.0 + (i % 3) for i in range(199)] + [55.0]
        summaries = {"BTC": real_summary("BTC", crash), "ETH": real_summary("ETH", calm), "XRP": real_summary("XRP", calm)}
        self.assertTrue(summaries["BTC"]["breaches"])
        return scenarios.fake_tools(summaries, [])

    FAKE = "BTC volatility is 9.87% and its rank is 99.9, the highest in 700 days."

    def test_the_fake_numbers_are_not_in_the_evidence(self):
        _, evidence = self.run_default_path(self.tools(), "BTC moved.", calls=MARKETS_ONLY)
        found = agent.unsupported_numbers(self.FAKE + " Volatility 9.87%, 412 consecutive alert days.", agent.evidence_text(evidence))
        # 700 is in the evidence now: the code adds the alert history stats, and the fake stats say 700 days ranked.
        self.assertEqual(sorted(set(found)), ["412", "9.87", "99.9"])

    def test_an_alert_with_invented_numbers_is_blocked_with_the_reviewer_off(self):
        decision, _ = self.run_default_path(self.tools(), self.FAKE, reasoning="Volatility 9.87%, 412 consecutive alert days.", calls=MARKETS_ONLY, revisions=1)
        self.assertFalse(decision["alert"])
        self.assertIn(REASON, decision["blocked_reason"])
        for number in ("9.87", "99.9"):   # 700 is in the evidence (the fake alert history stats)
            self.assertIn(number, decision["blocked_reason"])
        self.assertNotIn("412", decision["blocked_reason"], "the reasoning is not checked, only the message")
        self.assertEqual((decision["message"], decision["coins"]), ("", []))

    def test_the_block_only_removes_the_alert_and_does_not_rewrite_anything(self):
        evidence = evidence_for(self.tools())
        before = draft(message=self.FAKE)
        final = agent.apply_guardrails(dict(before), TITLES, evidence)
        self.assertEqual(final["sources"], SOURCES)
        self.assertEqual(final["reasoning"], before["reasoning"])
        self.assertEqual(set(final), set(before) | {"blocked_reason"})


class HonestMessages(QuietCase):
    """Realistic alert messages made from real evidence. Every one must still go out."""

    @staticmethod
    def twenty_november():
        """The README's 20 November numbers: BTC 20.46% below its 30-day average, distance rank 97.0, drawdown rank 96.6; XRP drawdown rank 96.1."""
        markets = scenarios.calm()
        markets["BTC"] = scenarios.fake_market("BTC", 64315.8, -4.1, -20.46, 3.2, -22.8, 90.2, 97.0, 96.6, volume_ratio=1.8, volume_rank=88.0, trend="down")
        markets["XRP"] = scenarios.fake_market("XRP", 1.9876, -3.0, -11.3, 4.4, -16.9, 91.0, 93.1, 96.1)
        for summary in markets.values():
            summary["as_of"] = "2026-11-20"
        return scenarios.fake_tools(markets, [])

    @staticmethod
    def spike():
        markets = scenarios.btc_spike(scenarios.calm())
        markets["BTC"] = scenarios.fake_market("BTC", 59120.0, -12.4, -9.3, 4.8, -14.2, 99.9, 98.1, 99.0,
                                               volume_ratio=3.1, volume_rank=97.5, trend="down")
        return scenarios.fake_tools(markets, [])

    @staticmethod
    def two_coins():
        markets = scenarios.calm()
        markets["BTC"] = scenarios.fake_market("BTC", 60400.0, -4.1, -5.2, 2.6, -11.8, 70.0, 91.0, 96.6, trend="down")
        markets["XRP"] = scenarios.fake_market("XRP", 1.9120, -6.8, -7.4, 5.9, -13.0, 97.2, 93.0, 94.0)
        return scenarios.fake_tools(markets, [])

    @staticmethod
    def from_real_functions():
        """Prices with decimals, as Kraken gives them, run through the real summarize()."""
        btc = BTC_CLOSES
        calm_eth = [3100.0 + 40.0 * math.sin(i / 5.0) for i in range(200)]
        calm_xrp = [2.30 + 0.05 * math.sin(i / 6.0) for i in range(200)]
        markets = {"BTC": real_summary("BTC", btc), "ETH": real_summary("ETH", calm_eth), "XRP": real_summary("XRP", calm_xrp)}
        return scenarios.fake_tools(markets, [])

    def cases(self):
        real = self.from_real_functions()
        b = real_summary("BTC", BTC_CLOSES)
        formatted = [
            ("a price with a thousands separator and cents", real, f"BTC closed at ${b['last_close']:,.2f}, rank {b['ranks']['distance']} on distance."),
            ("a price rounded to whole dollars", real, f"BTC closed at ${b['last_close']:,.0f}, {abs(b['dist_from_30d_avg_pct']):.1f}% below its 30-day average."),
            ("a price in thousands", real, f"BTC closed near ${b['last_close'] / 1000:.1f}k, {abs(b['dist_from_30d_avg_pct']):.0f}% below its 30-day average."),
            ("the distance rounded to one place and to none", real, f"BTC is {abs(b['dist_from_30d_avg_pct']):.1f}% (about {abs(b['dist_from_30d_avg_pct']):.0f}%) below its 30-day average."),
            ("a negative drawdown with the minus sign", real, f"BTC 30-day drawdown {b['max_drawdown_30d_pct']}%, rank {b['ranks']['drawdown']}."),
            ("the 60-day change and the trend", real, f"BTC trend {b['context']['trend']}: 30-day change {b['context']['ret_30d_pct']}%, 60-day change {b['context']['ret_60d_pct']}%."),
        ]
        fixed = [
            ("the README subject wording (20 November)", self.twenty_november(),
             "BTC closes 20.46% below its 30-day average, distance rank 97.0, above the alert rank of 95."),
            ("the README number rounded to one place", self.twenty_november(),
             "BTC closes 20.5% below its 30-day average (distance rank 97.0)."),
            ("the README number rounded to a whole percent, with a date", self.twenty_november(),
             "On 20 November BTC closed about 20% below its 30-day average, drawdown rank 96.6."),
            ("two coins at once, ranks quoted as given", self.twenty_november(),
             "BTC drawdown rank 96.6 and XRP drawdown rank 96.1, both at or above the alert rank of 95."),
            ("the rank quoted as 99.9", self.spike(), "BTC 30-day daily volatility is 4.8%, rank 99.9 of its own history, above the alert rank of 95."),
            ("the rank rounded down to 99", self.spike(), "BTC volatility rank 99 and drawdown rank 99 of its own history."),
            ("the alert rank 95 and the ISO date", self.spike(), "On 2026-11-19 BTC fell 12.4%, volatility rank 99.9, alert rank 95."),
            ("a price with a thousands separator", self.spike(), "BTC closed at $59,120, 9.3% below its 30-day average."),
            ("volume as a multiple, with its rank", self.spike(), "BTC volume was 3.1x its 30-day average (volume rank 97.5) on a 12.4% daily fall."),
            ("a context fact: sentiment and funding", self.spike(), "BTC volatility rank 99.9. Context: fear and greed index 52, funding 0.004% per hour."),
            ("a context fact in billions", self.spike(), "BTC drawdown rank 99.0. Market-wide 24-hour volume was $31.5B."),
            ("a context fact in millions", self.spike(), "BTC volatility rank 99.9, open interest about $900M."),
            ("a second coin quoted with four decimals", self.two_coins(), "XRP volatility rank 97.2, close $1.9120 (-6.8%); BTC drawdown rank 96.6."),
            ("a percent figure from the cross-asset summary", self.two_coins(), "BTC drawdown rank 96.6; BTC-ETH 30-day correlation 0.82."),
        ]
        return formatted + fixed

    def test_at_least_twelve_honest_messages(self):
        self.assertGreaterEqual(len(self.cases()), 12)

    def test_every_honest_message_still_goes_out_on_the_default_path(self):
        for label, tools, message in self.cases():
            with self.subTest(label):
                decision, _ = self.run_default_path(tools, message)
                self.assertTrue(decision["alert"], f"blocked: {decision['blocked_reason']}  message: {message}")
                self.assertEqual(decision["blocked_reason"], "")
                self.assertEqual(decision["message"], message)


class NoAlert(QuietCase):
    def test_a_decision_with_no_alert_is_not_touched(self):
        tools = scenarios.fake_tools(scenarios.calm(), [])
        evidence = evidence_for(tools)
        quiet = {"alert": False, "coins": [], "message": "", "sources": ["Alert rules"],
                 "reasoning": "No rank at 95. The invented 412 and 9.87 here are not checked."}
        final = agent.apply_guardrails(dict(quiet), TITLES, evidence)
        self.assertEqual(final, dict(quiet, blocked_reason=""))

    def test_a_no_alert_run_on_the_default_path_keeps_its_empty_reason(self):
        tools = scenarios.fake_tools(scenarios.calm(), [])
        final = json.dumps({"alert": False, "coins": [], "message": "BTC 9.87% and 412", "sources": ["Alert rules"], "reasoning": "calm, 700 days"})
        with mock.patch.object(agent, "call_llm", side_effect=[tool_calls(), {"role": "assistant", "content": final}]):
            decision, _ = agent.decide("agent", False, tools)
        self.assertFalse(decision["alert"])
        self.assertEqual(decision["blocked_reason"], "")


class OrderOfTheChecks(QuietCase):
    """The number check comes last: every older check keeps its own reason."""
    def evidence(self):
        return evidence_for(scenarios.fake_tools(scenarios.btc_spike(scenarios.calm()), []))

    def test_advice_wording_keeps_its_reason(self):
        final = agent.apply_guardrails(draft(message="BTC fell 61.3%, a good time to buy."), TITLES, self.evidence())
        self.assertIn("advice", final["blocked_reason"])

    def test_a_bad_source_keeps_its_reason(self):
        final = agent.apply_guardrails(draft(message="BTC fell 61.3%.", sources=["Made up"]), TITLES, self.evidence())
        self.assertIn("no valid source", final["blocked_reason"])

    def test_a_coin_without_a_breach_keeps_its_reason(self):
        final = agent.apply_guardrails(draft(coins=["ETH"], message="ETH fell 61.3%."), TITLES, self.evidence())
        self.assertIn("context never alerts alone", final["blocked_reason"])

    def test_a_repeat_alert_keeps_its_reason(self):
        history = [{"time": "2026-11-18T05:17:02Z", "alert": True, "coins": ["BTC"], "message": "x", "sources": ["Alert rules"]}]
        evidence = evidence_for(scenarios.fake_tools(scenarios.btc_spike(scenarios.calm()), history))
        final = agent.apply_guardrails(draft(message="BTC fell 61.3%."), TITLES, evidence)
        self.assertIn("already alerted", final["blocked_reason"])

    def test_a_message_with_no_invented_number_and_no_other_problem_passes(self):
        final = agent.apply_guardrails(draft(message="BTC 30-day daily volatility is 4.8%, rank 99.3."), TITLES, self.evidence())
        self.assertTrue(final["alert"])


class TheNewHelper(unittest.TestCase):
    """unsupported_message_numbers is the message check. unsupported_numbers (the reviewer's) must behave exactly as before."""
    EVIDENCE = json.dumps([{"result": {"last_close": 59120.0, "ranks": {"volatility": 99.3}, "as_of": "2026-11-19", "daily_return_pct": -12.4,
                                       "dist_from_30d_avg_pct": -20.46, "open_interest_usd_approx": 900000000.0, "volume_24h_usd": 31500000000.0,
                                       "ret_60d_pct": 3.4, "history_days": 700}}])

    def check(self, text):
        return agent.unsupported_message_numbers(text, self.EVIDENCE)

    def test_same_answers_as_the_reviewer_check_for_plain_numbers(self):
        for text in ["BTC closed at 59,120 (-12.4%), rank 99.3 on 2026-11-19, 3 coins", "BTC is at 67000 and rank 99.9",
                     "rank 99, 20.5% and 20.46%", "rank 98.9", "20.4%", "BTC fell 9.87%"]:
            self.assertEqual(self.check(text), agent.unsupported_numbers(text, self.EVIDENCE), text)

    def test_rounding_tolerance_is_half_a_unit_in_the_last_written_digit(self):
        self.assertEqual(self.check("20.5% and 20% and 20.46%"), [])
        self.assertEqual(self.check("20.4%"), ["20.4"])
        self.assertEqual(self.check("20.55%"), ["20.55"])

    def test_scale_words_and_letters_are_checked_as_the_full_number(self):
        for text in ["$59.1k", "$59.12K", "$900M", "$0.9 billion", "900 million", "$31.5B", "31.5bn", "$59 thousand"]:
            self.assertEqual(self.check(text), [], text)
        for text in ["$59.9k", "$800M", "$5k", "$35B", "$9 million"]:
            self.assertEqual(len(self.check(text)), 1, text)

    def test_a_letter_after_a_number_is_a_scale_only_when_it_stands_alone(self):
        self.assertEqual(self.check("412 BTC, 777 months, 555 trades"), ["412", "777", "555"])

    def test_window_labels_pass_but_a_bare_60_or_24_does_not(self):
        self.assertEqual(self.check("the 60-day change and the 24-hour volume"), [])
        self.assertEqual(self.check("a 60% fall"), ["60"])
        self.assertEqual(self.check("rank 24"), ["24"])
        self.assertEqual(self.check("a 24-day and a 60-hour streak"), ["24", "60"])

    def test_the_reviewers_function_is_unchanged(self):
        self.assertEqual(agent.unsupported_numbers("close $59.1k, 60-day, $900M", self.EVIDENCE), ["59.1", "60", "900"])
        self.assertEqual(agent.ALLOWED_NUMBERS, {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 30, 31, 90, 95, 100, 280, 600})


if __name__ == "__main__":
    unittest.main()
