"""Offline tests for the guardrail hardening found by audit case L11b: a tool result that is not a dict must not crash the guardrails.

    python -m unittest tests/test_fix_guardrail_odd_results.py -v

Before the fix, an alert with a string, None or list as the market tool result raised AttributeError or TypeError inside
apply_guardrails and the run died. Now such a result counts as "no valid market data", so the alert is blocked with a reason.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

TITLES = [section["title"] for section in agent.load_playbook()]
DRAFT = {"alert": True, "coins": ["BTC"], "message": "BTC volatility rank 99.3, above the alert rank of 95.",
         "sources": ["Volatility rule"], "reasoning": "BTC volatility rank 99.3 is at or above 95."}
GOOD = {"tool": "get_market_summary", "args": {"coin": "BTC"},
        "result": {"coin": "BTC", "ranks": {"volatility": 99.3}, "breaches": ["volatility"]}}


def guard(evidence):
    with mock.patch.object(agent, "log", lambda text: None):
        return agent.apply_guardrails(dict(DRAFT), TITLES, evidence)


class OddToolResults(unittest.TestCase):
    def test_a_non_dict_market_result_is_blocked_without_a_crash(self):
        for odd in ("not json", None, ["a", "list"], 42):
            with self.subTest(result=odd):
                result = guard([{"tool": "get_market_summary", "args": {"coin": "BTC"}, "result": odd}])
                self.assertFalse(result["alert"])
                self.assertEqual(result["blocked_reason"], "no valid market data in this run")

    def test_an_odd_result_next_to_a_good_one_does_not_hide_the_good_one(self):
        odd = {"tool": "get_market_summary", "args": {"coin": "ETH"}, "result": None}
        result = guard([odd, GOOD])
        self.assertTrue(result["alert"], result["blocked_reason"])

    def test_a_good_result_still_gives_the_same_decision_as_before(self):
        evidence = [dict(GOOD, result=dict(GOOD["result"], volatility_30d_daily_pct=4.8))]
        self.assertTrue(guard(evidence)["alert"])

    def test_an_error_result_is_still_no_valid_data(self):
        result = guard([{"tool": "get_market_summary", "args": {"coin": "BTC"}, "result": {"error": "no data"}}])
        self.assertEqual(result["blocked_reason"], "no valid market data in this run")


if __name__ == "__main__":
    unittest.main()
