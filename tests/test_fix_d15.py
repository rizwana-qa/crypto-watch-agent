"""Offline tests for D-15: numeric text from a web reply must be plain ASCII digits.

    python tests/test_fix_d15.py -v

Python's float() also accepts "1_000" and full-width digits. The agent (agent.as_number), the data validation (validate_data.num)
and the context sources (sources.number) now refuse them, and all three apply the same rule.
"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import sources  # noqa: E402
import validate_data as vd  # noqa: E402

# What Kraken, CoinGecko and alternative.me really send, plus ordinary JSON numbers.
REAL_FORMS = [("60452.1", 60452.1), ("0.00001234", 1.234e-05), ("12.34567890", 12.3456789), ("100", 100.0), ("-3.5", -3.5), ("+2", 2.0),
              ("1e-05", 1e-05), ("1E5", 100000.0), (".5", 0.5), ("5.", 5.0), ("0", 0.0), (7, 7.0), (7.5, 7.5), (0, 0.0),
              (" 5", 5.0), ("5 ", 5.0), (" 5.5 ", 5.5), ("12\n", 12.0), ("\t7", 7.0)]   # plain spaces around a number were always accepted
ODD_FORMS = ["1_000", "1_0.5", "\uff11\uff12\uff13", "\u0661\u0662\u0663", "1 000", "\u00a05", "5\u00a0", "\u20035", "1,000", "0x10", "0b1", "1e", "e5",
             "--1", "+-1", "1.2.3", "", " ", "nan", "NaN", "inf", "-inf", "Infinity", "abc", "1\u00a05", "\u06f1\u06f2", "\u2460", "5\u200b"]
NOT_NUMBERS = [None, True, False, [], {}, [1], "", object()]


class Parsers(unittest.TestCase):
    PARSERS = {"agent.as_number": agent.as_number, "validate_data.num": vd.num}

    def test_real_formats_still_parse_everywhere(self):
        for name, parse in self.PARSERS.items():
            for value, expected in REAL_FORMS:
                with self.subTest(parser=name, value=value):
                    self.assertEqual(parse(value), expected)
        for value, expected in REAL_FORMS:
            with self.subTest(parser="sources.number", value=value):
                self.assertEqual(sources.number(value, "x"), expected)

    def test_odd_text_is_refused_everywhere(self):
        for name, parse in self.PARSERS.items():
            for value in ODD_FORMS:
                with self.subTest(parser=name, value=value):
                    self.assertIsNone(parse(value))
        for value in ODD_FORMS:
            with self.subTest(parser="sources.number", value=value):
                with self.assertRaises(ValueError):
                    sources.number(value, "x")

    def test_non_numbers_are_refused_everywhere(self):
        for name, parse in self.PARSERS.items():
            for value in NOT_NUMBERS:
                with self.subTest(parser=name, value=repr(value)):
                    self.assertIsNone(parse(value))
        for value in NOT_NUMBERS:
            with self.subTest(parser="sources.number", value=repr(value)):
                with self.assertRaises(ValueError):
                    sources.number(value, "x")

    def test_the_gate_and_the_agent_agree_on_every_input(self):
        for value in [v for v, _ in REAL_FORMS] + ODD_FORMS + NOT_NUMBERS + [float("nan"), float("inf"), -float("inf"), 1e400, 10 ** 400]:
            with self.subTest(value=repr(value)):
                self.assertEqual(agent.as_number(value), vd.num(value))

    def test_infinite_and_nan_numbers_are_still_refused(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            self.assertIsNone(agent.as_number(value))
            self.assertIsNone(vd.num(value))
        self.assertTrue(math.isfinite(agent.as_number("1e300")))
        self.assertIsNone(agent.as_number("1e400"))   # parses to infinity, so it is refused as before

    def test_an_integer_too_large_for_a_float_is_refused_not_a_crash(self):
        for huge in (10 ** 400, -10 ** 400):
            self.assertIsNone(agent.as_number(huge))
            self.assertIsNone(vd.num(huge))
            with self.assertRaises(ValueError):
                sources.number(huge, "x")

    def test_one_regex_serves_all_three(self):
        self.assertIs(vd.agent.sources.NUMBER_TEXT_RE, sources.NUMBER_TEXT_RE)
        self.assertTrue(sources.NUMBER_TEXT_RE.pattern.endswith(r"\Z"))   # anchored at the end with \Z, not "$"


class OddCandles(unittest.TestCase):
    """A candle with such a value is refused as a whole, by the agent and by the validation."""

    def row(self, close):
        return [1_700_000_000, "100", "110", "90", close, "100", "5.5", 12]

    def test_the_agent_refuses_the_candle(self):
        for close in ("1_000", "\uff11\uff10\uff10", "10 0", "1e", "\u00a0100"):
            with self.subTest(close=close):
                with self.assertRaises(ValueError):
                    agent.parse_candles([self.row(close)], "XBTUSD")

    def test_a_normal_candle_is_still_accepted(self):
        candles = agent.parse_candles([self.row("100.5")], "XBTUSD")
        self.assertEqual(len(candles), 1)


if __name__ == "__main__":
    unittest.main()
