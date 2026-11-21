"""Offline tests for the second advice layer (D-05): forecasts and advice written without a banned word.

    python tests/test_fix_d05_layer2.py -v

The word list (BANNED_WORDS) is unchanged. A second, narrow layer blocks sentences such as "will likely rebound", "price target",
"add to their positions" and letters spaced out ("B U Y"). It only blocks. The main risk of any filter like this is blocking an honest
alert, so most of this file is a set of honest messages that must still pass, built from every metric the agent reports.
"""

import itertools
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

TITLES = [section["title"] for section in agent.load_playbook()]
GOOD_MARKET = [{"tool": "get_market_summary", "args": {"coin": "BTC"},
                "result": {"coin": "BTC", "volatility_30d_daily_pct": 4.8, "ranks": {"volatility": 99.3, "trend": 97.0}, "breaches": ["volatility"]}}]


def guard(message):
    draft = {"alert": True, "coins": ["BTC"], "message": message, "sources": ["Volatility rule"],
             "reasoning": "BTC volatility rank 99.3 is at or above 95."}
    with mock.patch.object(agent, "log", lambda text: None):
        return agent.apply_guardrails(draft, TITLES, GOOD_MARKET)


class SecondLayerBlocks(unittest.TestCase):
    """Each family has several wordings. Every one must be blocked, in any case."""

    FAMILIES = {
        "forecast": ["BTC will rise.", "ETH is going to crash soon.", "XRP is expected to rally.", "BTC is likely to drop further.",
                     "BTC is poised to break out.", "BTC is bound to recover.", "BTC is certain to rebound.", "The price will double.",
                     "BTC will likely rebound within days.", "ETH will continue to fall.", "BTC will keep rising.",
                     "BTC is about to reach 70000.", "BTC will probably go higher.", "BTC will then drop.", "BTC won't recover soon."],
        "expectation": ["Expect further upside for BTC after this move.", "We expect a rebound.", "Expecting more downside.",
                        "Expect a sharp pullback in ETH.", "Traders expect continuation."],
        "price target": ["BTC has a price target of 70000.", "Target price 70000.", "A price objective near 70000.", "Price targets are higher."],
        "promise": ["BTC is guaranteed to recover.", "A risk-free move.", "Sure thing.", "You can't lose.", "You cannot lose here.",
                    "No risk here.", "Easy money in ETH.", "This guarantees gains."],
        "advice paraphrase": ["Traders may want to add to their positions in BTC.", "Add to your BTC position.", "Adding to positions now.",
                              "Load up on BTC.", "Loading up on ETH.", "Get in now.", "Get out before it falls.", "Jump in now.",
                              "Go all in.", "Double down.", "Catch the dip.", "Do not miss this.", "Don't miss this.", "FOMO is high.",
                              "Diversify now.", "Rebalance your holdings.", "Cut your losses.",
                              "Investors may consider fresh exposure.", "You might want to wait.", "You could consider waiting.",
                              "Consider reducing exposure.", "Think about trimming ETH.", "It would be wise to wait.",
                              "A great opportunity to add.", "Right time to enter.", "Time to act.", "Chance to trim.",
                              "Wait for a pullback.", "Waiting for the dip.", "Use caution.", "Exercise caution.", "Proceed with caution.",
                              "Be careful.", "Stay cautious.", "Watch out.", "Do your own research.", "DYOR."],
    }

    def test_every_family_is_blocked_in_any_case(self):
        for family, messages in self.FAMILIES.items():
            for message in messages:
                for variant in (message, message.upper(), message.lower()):
                    with self.subTest(family=family, message=variant):
                        self.assertTrue(agent.has_advice_language(variant))

    def test_the_guardrail_blocks_with_the_advice_reason(self):
        for family, messages in self.FAMILIES.items():
            for message in messages:
                with self.subTest(family=family, message=message):
                    result = guard(message)
                    self.assertFalse(result["alert"])
                    self.assertEqual(result["blocked_reason"], "advice language in the message")

    def test_a_forecast_is_blocked_inside_a_normal_alert(self):
        for tail in ["BTC will likely rebound within days.", "A price target well above its close.", "Traders may want to add to positions."]:
            with self.subTest(tail=tail):
                self.assertTrue(agent.has_advice_language(f"BTC 30-day daily volatility is 4.8%, rank 99.3. {tail}"))


class SpellingTricks(unittest.TestCase):
    def test_letters_spaced_out_or_separated_are_joined(self):
        for message in ["B U Y BTC now.", "b.u.y BTC now.", "S-E-L-L now.", "s e l l now", "H*O*L*D ETH", "B_U_Y now"]:
            with self.subTest(message=message):
                self.assertTrue(agent.has_advice_language(message))

    def test_full_width_and_invisible_characters_do_not_hide_a_word(self):
        for message in ["ｂｕｙ BTC now.", "bu​y BTC now.", "s­ell now.", "se⁠ll now.", "﻿sell now"]:
            with self.subTest(message=message):
                self.assertTrue(agent.has_advice_language(message))

    def test_plain_text_leaves_normal_messages_alone(self):
        for message in ["BTC volatility rank is 99.3.", "ETH closed 2026-11-21 at 3,212.40.", "Volume was 2.4 times its 30-day average.",
                        "The U.S. session ended.", "BTC vs. ETH correlation is 0.92.", "e.g. a rank of 95."]:
            with self.subTest(message=message):
                self.assertEqual(agent.plain_text(message), message)
                self.assertFalse(agent.has_advice_language(message))

    def test_a_non_string_does_not_crash_the_check(self):
        self.assertFalse(agent.has_advice_language(""))
        self.assertFalse(agent.has_advice_language(None) and False)   # str(None) is "None": no banned word, no crash


class DisclaimersAndSafePhrases(unittest.TestCase):
    def test_plain_disclaimers_pass(self):
        for message in ["Past moves are not guaranteed to repeat.", "There is no guarantee this repeats.", "This is not a price target.",
                        "These are not price targets and not a forecast.", "That is not a guarantee.", "We cannot guarantee anything here.",
                        "The data does not guarantee a repeat.", "BTC volatility rank is 99.3. This is not financial advice."]:
            with self.subTest(message=message):
                self.assertFalse(agent.has_advice_language(message))

    def test_a_disclaimer_does_not_hide_a_promise_next_to_it(self):
        for message in ["Not guaranteed, but BTC will rebound.", "No guarantee, yet BTC is guaranteed to recover.",
                        "This is not a price target, but the price target is 70000.", "Not financial advice: BTC will likely rebound.",
                        "A sell-off is over and BTC will rise."]:
            with self.subTest(message=message):
                self.assertTrue(agent.has_advice_language(message))

    def test_the_word_list_is_still_checked_first(self):
        self.assertTrue(agent.has_advice_language("Sell now."))
        self.assertTrue(agent.has_advice_language("A sell-off, so sell."))
        self.assertFalse(agent.has_advice_language("A sell-off and a short squeeze."))


class HonestMessagesStillPass(unittest.TestCase):
    """The false-positive set. None of these gives advice or a forecast, and all of them must be delivered."""

    HAND_WRITTEN = [
        "BTC 30-day daily volatility is 4.8%, rank 99.3, above the alert rank of 95.",
        "BTC fell 8.2% in 24 hours and is 18.3% below its 30-day average.",
        "BTC dropped to 60,200 and recovered most of the loss within the day.",
        "ETH rose 6.1% on the day and bounced from its 30-day low.",
        "XRP surged 12% on the day, its largest daily move in 700 days.",
        "BTC hit its highest close in 60 days.",
        "BTC reached the alert rank for drawdown today.",
        "The 24-hour move reached 8.2%, volume was 3.1 times its average.",
        "A jump in volume of 3.4 times its 30-day average came with a 5% drop.",
        "Volatility continued to rise for a third day, rank 97.4.",
        "Volatility has kept rising since the 14th, rank 96.8.",
        "BTC has been falling for five days and is 11.2% below its 30-day average.",
        "BTC stayed above its 30-day average all week.",
        "BTC remains below its 60-day average, trend label downtrend.",
        "The next check will include XRP.",
        "The data will update at 05:00 UTC.",
        "The alert rank is set to 95 by design.",
        "This is not a forecast and not a price target.",
        "No data for ETH in this run, so it was not checked.",
        "Fear and greed is 9, extreme fear, and BTC volatility rank is 99.3.",
        "Funding for BTC is positive at 0.01%, the 91st percentile of its own history.",
        "Options imply 52% volatility for BTC, rank 96.",
        "BTC and ETH moved together, 30-day correlation 0.92.",
        "BTC is at the alert rank for volatility and distance, drawdown is near threshold at 91.2.",
        "Two coins are at the alert rank: BTC for volatility and XRP for drawdown.",
        "BTC was alerted 3 days ago, so it was not repeated.",
        "ETH 60-day trend is up, but no metric is at the alert rank.",
        "Short-term volatility is at the 97th percentile.",
        "The long-term average sits well above the close.",
        "BTC is in a sell-off, with volatility at its 99th percentile.",
        "Funding turned negative during the short squeeze last week.",
        "ETH is holding steady near its 30-day average.",
        "BTC closed at 60,452, which is 18.3% below its 30-day average of 73,990.",
        "The drawdown from the 60-day peak is 22.4%, rank 98.1.",
        "BTC volume was 2.4 times its 30-day average, rank 95.6.",
        "XRP moved 9.3% in one day, the 98th percentile of its own history.",
        "BTC price is within 3% of the level where volatility would reach the alert rank.",
        "ETH would reach the distance alert rank at a close of 3,050, a 4.1% move from here.",
        "Trigger levels are conditions to monitor and not forecasts.",
        "BTC reversed after a 7% drop and closed flat.",
        "The spike will stay inside the 30-day window until 2026-12-19.",
        "The rank stays high for days because the spike stays inside the window.",
        "BTC double bottom is not part of the playbook, so it is not used.",
        "The market summary tool returned no data for XRP.",
        "BTC volatility rank 99.3, ETH 71.2, XRP 64.0.",
        "Coins checked: BTC, ETH, XRP. At the alert rank: BTC.",
        "BTC 24-hour volume of 1.9B is 3.4 times its 30-day average.",
        "A daily close 9.8% below the 30-day average is unusual for ETH: rank 97.2.",
        "XRP implied volatility is not available, so no rank is given.",
        "On 2026-11-20 BTC fell 6.4% and volume rose 2.9 times.",
        "Volatility was 3.1% a month ago and is 4.8% now.",
        "BTC ranks in the top 1% of its own days for drawdown.",
        "The reader can check the calculation in the playbook.",
        "Correlation between BTC and XRP fell to 0.41 from 0.88.",
        "Market-wide volume is 1.6 times its average and the fear and greed index is 22.",
        "BTC test of the 30-day average ended with a close 2.2% above it.",
        "The close was the lowest in 60 days.",
        "BTC reached 99.3 on the volatility rank and 97.0 on the trend rank.",
        "XRP fell 4% while BTC rose 1%, so the relative strength gap is 5 points.",
        "Alert: BTC volatility rank 99.3. Sources: Volatility rule, Alert rules.",
        "BTC is up 3.4% this week and ETH is down 2.1%.",
        "Open interest is 5.2B, the 94th percentile.",
        "This alert is for information only.",
        "Educational project output, not trading advice.",
        "BTC: volatility 4.8%, distance -18.3%, drawdown -22.4%.",
        "Position data is not part of this agent, so none is used.",
        "BTC reached its highest volatility rank since 2026-09-14.",
    ]

    def test_hand_written_honest_messages_pass_the_check(self):
        for message in self.HAND_WRITTEN:
            with self.subTest(message=message):
                self.assertFalse(agent.has_advice_language(message), message)

    def test_hand_written_honest_messages_are_within_the_length_limit(self):
        for message in self.HAND_WRITTEN:
            self.assertLessEqual(len(message), agent.MAX_MESSAGE_CHARS, message)

    def test_generated_honest_messages_pass_the_check(self):
        templates = [
            "{c} 30-day daily volatility is {v}%, rank {r}, above the alert rank of 95.",
            "{c} closed {d}% below its 30-day average, distance rank {r}.",
            "{c} drawdown from its 60-day peak is {d}%, rank {r}.",
            "{c} volume was {v} times its 30-day average.",
            "{c} fell {d}% in 24 hours, the largest daily drop in its history.",
            "{c} rose {d}% on the day and recovered most of last week's loss.",
            "{c} bounced after a sell-off and the short-term trend label is downtrend.",
            "{c} reached rank {r} for volatility, and the close was {d}% under the 30-day average.",
            "{c} jumped {d}% on the day, with volume {v} times its average.",
            "{c} is near threshold at {r}, a day just under the alert rank.",
            "{c} was at the alert rank on {d}% of its days.",
            "No data for {c} in this run, so it was not checked.",
            "{c} moved {d}% in one day, the {r}th percentile of its own history.",
            "{c} trend: downtrend for {v} days, rank {r}.",
        ]
        count = 0
        for template, coin, value, rank, drop in itertools.product(
                templates, ("BTC", "ETH", "XRP"), ("2.4", "4.8", "12.0"), ("95.6", "99.3"), ("8.2", "18.3", "22.4")):
            message = template.format(c=coin, v=value, r=rank, d=drop)
            count += 1
            with self.subTest(message=message):
                self.assertFalse(agent.has_advice_language(message), message)
        self.assertEqual(count, 756)

    def test_honest_messages_pass_the_whole_guardrail(self):
        for message in ["BTC 30-day daily volatility is 4.8%, rank 99.3.", "BTC volatility rank is 99.3 and trend rank is 97.0.",
                        "BTC volatility rank 99.3. This is not guaranteed to repeat and is not a price target."]:
            with self.subTest(message=message):
                result = guard(message)
                self.assertTrue(result["alert"], result["blocked_reason"])
                self.assertEqual(result["message"], message)   # the message is never rewritten


class LayerDesign(unittest.TestCase):
    def test_the_layer_only_blocks_it_never_changes_the_message(self):
        message = "BTC volatility rank 99.3, above 95."
        self.assertEqual(guard(message)["message"], message)
        blocked = guard("BTC will likely rebound within days.")
        self.assertFalse(blocked["alert"])
        self.assertEqual(blocked["blocked_reason"], "advice language in the message")

    def test_the_word_list_is_unchanged(self):
        self.assertEqual(len(agent.BANNED_WORDS), 23)
        self.assertTrue(agent.ADVICE_RE.search("sell-off"))   # the raw regex used by the report and the Ask tab stays strict

    def test_no_pattern_matches_an_empty_or_plain_number_text(self):
        for text in ["", " ", "95", "99.3", "BTC", "rank 99.3 of 100", "4.8% 2.4x"]:
            self.assertFalse(agent.ADVICE_PATTERN_RE.search(text), text)

    def test_the_allowed_disclaimers_are_exactly_the_agreed_list(self):
        self.assertEqual(sorted(agent.ADVICE_DISCLAIMERS), sorted([
            "not guaranteed", "no guarantee", "no guarantees", "not a guarantee", "cannot guarantee", "can't guarantee",
            "does not guarantee", "doesn't guarantee", "do not guarantee", "not a price target", "not price targets", "no price target"]))

    def test_the_check_is_fast_on_hostile_text(self):
        hostile = ["a " * 140, "will " * 56, "expect " * 40, "B U Y " * 46, "is going to " * 25, "." * 280, "a b c d e f g h " * 17,
                   "Traders " * 35, "get in " * 40]
        started = time.perf_counter()
        for text in hostile:
            agent.has_advice_language(text)
        self.assertLess(time.perf_counter() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
