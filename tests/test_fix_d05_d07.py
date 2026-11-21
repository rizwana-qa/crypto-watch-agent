"""Offline tests for the advice filter: D-07 (ordinary market terms were blocked) and D-05 (known gaps of the word list).

    python tests/test_fix_d05_d07.py -v

D-07: a few exact market phrases and the disclaimer are cut out before the word check, so they no longer block an alert.
D-05: the word list alone missed forecasts and advice written without a banned word. Seven such sentences were kept here as expected
failures; a second, block-only layer now catches them and they are normal tests (FormerGapsD05). The layer itself and its set of
honest messages that must still pass are in tests/test_fix_d05_layer2.py.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

TITLES = [section["title"] for section in agent.load_playbook()]
# The evidence holds every number the test messages quote (the D-01 number check blocks any number that is not in it).
GOOD_MARKET = [{"tool": "get_market_summary", "args": {"coin": "BTC"},
                "result": {"coin": "BTC", "volatility_30d_daily_pct": 4.8, "ranks": {"volatility": 99.3, "trend": 97.0}, "breaches": ["volatility"]}}]


def guard(message):
    """The guardrail result for an otherwise valid alert with this message."""
    draft = {"alert": True, "coins": ["BTC"], "message": message, "sources": ["Volatility rule"],
             "reasoning": "BTC volatility rank 99.3 is at or above 95."}
    with mock.patch.object(agent, "log", lambda text: None):
        return agent.apply_guardrails(draft, TITLES, GOOD_MARKET)


class QuietCase(unittest.TestCase):
    def assertDelivered(self, message):
        result = guard(message)
        self.assertTrue(result["alert"], f"{message!r} was blocked: {result['blocked_reason']}")
        self.assertEqual(result["blocked_reason"], "")

    def assertBlocked(self, message):
        result = guard(message)
        self.assertFalse(result["alert"], f"{message!r} was not blocked")
        self.assertEqual(result["blocked_reason"], "advice language in the message")


class MarketTermsAreAllowed(QuietCase):
    def test_the_audit_sentences_are_delivered(self):
        for message in ["BTC is in a sell-off, with volatility at its 99th percentile.",
                        "The long-term average sits well above the close.",
                        "Short-term volatility is at the 97th percentile.",
                        "ETH is holding steady near its 30-day average.",
                        "Funding turned negative during the short squeeze last week."]:
            with self.subTest(message=message):
                self.assertDelivered(message)

    def test_the_disclaimer_is_delivered(self):
        for message in ["BTC volatility rank is 99.3. This is not financial advice.",
                        "BTC volatility rank is 99.3. This is not investment advice."]:
            with self.subTest(message=message):
                self.assertDelivered(message)

    def test_every_allowed_phrase_works_in_any_case(self):
        for phrase in agent.ADVICE_SAFE_PHRASES:
            for text in (phrase, phrase.upper(), phrase.title()):
                with self.subTest(text=text):
                    self.assertDelivered(f"BTC volatility rank is 99.3 after the {text} in the data.")

    def test_the_allowed_phrases_are_exactly_the_agreed_list(self):
        self.assertEqual(sorted(agent.ADVICE_SAFE_PHRASES),
                         sorted(["sell-off", "selloff", "long-term", "short-term", "mid-term", "short squeeze", "holding steady",
                                 "not financial advice", "not investment advice", "not trading advice"]))

    def test_the_phrase_check_is_the_one_the_guardrail_uses(self):
        self.assertFalse(agent.has_advice_language("A sell-off and a short squeeze."))
        self.assertTrue(agent.has_advice_language("Sell now."))


class AdviceIsStillBlocked(QuietCase):
    def test_direct_advice_is_blocked(self):
        for message in ["BTC is a good buy after this drop.", "Go long on BTC.", "Hold ETH.", "Short BTC.", "Sell now.", "Long BTC.",
                        "Set a stop-loss at 60000.", "Set a stop loss at 60000.", "Time to take profit.", "You should wait.",
                        "A good entry point.", "We recommend waiting.", "This is advice.", "We advise caution."]:
            with self.subTest(message=message):
                self.assertBlocked(message)

    def test_a_removed_phrase_does_not_hide_advice_next_to_it(self):
        for message in ["BTC is in a sell-off, then sell now.", "Sell off your BTC.", "SELL-OFF, SELL NOW.",
                        "BTC is in a sell-off, so buy.", "Short-term traders should hold.", "Short-term: short BTC.",
                        "Long-term hold ETH.", "ETH is holding steady, so hold it.", "This is not financial advice, but buy BTC.",
                        "Not investment advice: go long.", "A short squeeze is coming, so sell.", "Mid-term: accumulate BTC.",
                        "The selloff is over, buy the dip.", "A sell-off\nsell now"]:
            with self.subTest(message=message):
                self.assertBlocked(message)

    def test_a_phrase_is_only_removed_as_a_whole_phrase(self):
        for message in ["BTC is in a sell-offer.", "Short-termism is growing.", "The long-terms are growing.",
                        "A short-term-hold.", "A pre-sell-off dip, sell.", "Selloff-style sell."]:
            with self.subTest(message=message):
                self.assertBlocked(message)

    def test_a_phrase_placed_inside_a_banned_phrase_does_not_hide_it(self):
        for message in ["Take sell-off profit.", "Take short-term profit.", "Stop long-term loss.", "take  short-term  profit",
                        "Sell-short-term-off now."]:
            with self.subTest(message=message):
                self.assertBlocked(message)

    def test_the_word_list_and_the_raw_regex_are_unchanged(self):
        self.assertEqual(agent.BANNED_WORDS, [
            "buy", "buying", "bought", "sell", "selling", "sold", "long", "short", "hold", "holding",
            "entry", "exit", "accumulate", "should", "recommend", "recommends", "recommended",
            "recommendation", "advise", "advice", "take profit", "stop loss", "stop-loss"])
        for text in ["A sell-off", "long-term", "Short-term", "holding steady", "short squeeze", "not financial advice"]:
            with self.subTest(text=text):
                self.assertTrue(agent.ADVICE_RE.search(text))      # the report and Ask tab checks use this one and stay strict

    def test_text_without_a_banned_word_is_still_allowed(self):
        self.assertDelivered("BTC 30-day daily volatility is 4.8%, rank 99.3.")


class FormerGapsD05(QuietCase):
    """These sentences got past the word list alone. The second layer (agent.ADVICE_PATTERNS, tests/test_fix_d05_layer2.py) now blocks them."""

    def test_add_to_positions(self):
        self.assertBlocked("Traders may want to add to their positions in BTC.")

    def test_load_up(self):
        self.assertBlocked("This is a good moment to load up on BTC.")

    def test_prediction_rebound(self):
        self.assertBlocked("BTC will likely rebound within days.")

    def test_prediction_upside(self):
        self.assertBlocked("Expect further upside for BTC after this move.")

    def test_price_target(self):
        self.assertBlocked("BTC has a price target well above its current close.")

    def test_guarantee(self):
        self.assertBlocked("BTC is guaranteed to recover.")

    def test_spaced_letters(self):
        self.assertBlocked("B U Y BTC now.")

    def test_a_disclaimer_does_not_hide_advice(self):
        self.assertBlocked("Not trading advice: buy BTC.")


if __name__ == "__main__":
    unittest.main()
