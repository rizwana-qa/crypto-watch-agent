"""Fix D-06: the Ask page answer guard must check that a cited point is supported by the section it cites.

Before the fix the guard proved only that the cited section exists, was retrieved and has no advice word. A wrong number
(audit case A4) or a claim from the model's own knowledge (A9) was shown as an answer with every check passed. The scripted
model below plays the audit cases A1 to A9 and a set of faithful paraphrases written from the playbook sections.

    python tests/test_fix_d06.py -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag  # noqa: E402

INDEX = rag.Index()
CHECK = "Numbers and key words match the cited sections"
SECTIONS = {s["title"]: s for s in INDEX.sections}


def run(question, bottom_line, points, related=()):
    """rag.ask with a scripted model that answers with exactly this JSON. points is a list of (text, section)."""
    reply = {"bottom_line": bottom_line, "why": [{"text": t, "section": s} for t, s in points], "related": list(related)}
    return rag.ask(question, INDEX, llm=lambda messages: {"role": "assistant", "content": json.dumps(reply)})


def check_result(answer):
    return {c["name"]: c["passed"] for c in answer["guardrails"]["checks"]}[CHECK]


# Faithful answers in the model's own words: (question, bottom line, [(point, cited section)]). Every one must be shown as an answer.
# No banned word appears in any of them, so only the new support check can reject them.
FAITHFUL = [
    ("How many characters can a message have?", "A message may have at most 280 characters.", [("At most 280 characters.", "Wording rules")]),
    ("How long can an alert message be?", "An alert message can have at most 280 characters.",
     [("The text is limited to 280 characters in plain words, which keeps the email readable at a glance.", "Wording rules")]),
    ("What does near threshold mean?", "Near threshold means a rank from 90 up to but below 95, and it does not raise an alert.",
     [("A rank in the band from 90 to just under 95 is called near threshold.", "Alert rules"),
      ("A coin that is near threshold gets no alert but may be mentioned in the reasoning.", "Alert rules")]),
    ("When does the agent raise an alert?", "It alerts on a coin when its volatility, trend or drawdown rank reaches 95 or higher.",
     [("Alert on a coin when at least one of the three price ranks is 95 or higher.", "Alert rules"),
      ("On a calm day, with every rank under 90, there is no alert.", "Alert rules"),
      ("Only one alert message goes out per day, and it covers all the coins that breached.", "Alert rules")]),
    ("When does the agent not repeat an alert?", "A coin is not alerted again if it was alerted within the last 7 decisions.",
     [("A coin that was alerted in any of the last 7 decisions is not alerted again.", "Repeat alerts"),
      ("A different coin that has not been alerted can still alert.", "Repeat alerts")]),
    ("What is the minimum number of candles?", "At least 90 completed daily candles are needed.",
     [("With fewer than 90 completed daily candles, there is too little history to rank against.", "Data quality"),
      ("Ninety days gives 4 or 5 days in the top 5 percent, the least that makes a rank meaningful.", "Data quality")]),
    ("At what values is fear and greed extreme?", "A Fear and Greed value of 24 or lower, or 76 or higher, is extreme.",
     [("Values at 24 or below, or 76 and above, are listed in the flags as extreme.", "Sentiment index"),
      ("It is context only and never raises an alert on its own.", "Sentiment index")]),
    ("Does volume raise an alert?", "No. Volume is context and never raises an alert on its own.",
     [("A volume rank of 95 or higher is unusual and appears in the flags of the market summary.", "Volume rule"),
      ("When a price rank breaches, say whether volume was also unusual.", "Volume rule")]),
    ("When do the two prices disagree?", "The Kraken and CoinGecko prices agree when the gap is 1% or less.",
     [("A gap above 1% is listed as price sources disagree.", "Price source check"),
      ("It never blocks an alert because the alert rules rest on Kraken's completed daily candles.", "Price source check")]),
    ("How is volatility measured?", "Volatility is the sample standard deviation of the last 30 daily returns.",
     [("It needs 31 closing prices to get 30 daily simple returns.", "Volatility rule"),
      ("A volatility rank of 95 or higher is unusual.", "Volatility rule")]),
    ("How is the spread calculated?", "The spread is the best ask minus the best bid, divided by the mid price, in percent.",
     [("Depth is the dollar value of resting bids and asks within a band around the mid price.", "Liquidity (order book)"),
      ("Both sides use the same band so bid and ask depth can be compared.", "Liquidity (order book)")]),
    ("How common is a breach?", "The history counts the days on which at least one price rank was 95 or higher.",
     [("share_pct is the number of alert days divided by the ranked days.", "Alert history base rate"),
      ("Several alert days in a row count as one episode.", "Alert history base rate"),
      ("It can say how common a breach is, for example 8.4% of days.", "Alert history base rate")]),
    ("What is DVOL?", "DVOL is Deribit's annualised 30-day implied volatility index, for BTC and ETH only.",
     [("XRP has no such index, so it is reported as unavailable.", "Implied volatility")]),
    ("How is drawdown defined?", "Drawdown is the largest fall from a running peak in the last 31 closes.",
     [("A drawdown depth rank of 95 or higher is unusual.", "Drawdown rule")]),
    ("What does the trend label mean?", "The label is up, down or mixed, and it describes the past.",
     [("The label is up when both the 30-day and the 60-day changes are positive and the last close is above its 30-day average.", "Trend label")]),
    ("How is the distance from the 30-day average worked out?", "It is the last close against the mean of the last 30 closes, in percent.",
     [("Distance is how far the last close sits above or below the mean of the last 30 closes, in percent.", "Trend against the 30-day average"),
      ("The sign shows whether the coin is above or below its average.", "Trend against the 30-day average")]),
    ("What is market-wide volume?", "It is the 24-hour traded volume across all the exchanges CoinGecko tracks.",
     [("This differs from the Volume rule because it uses volume across all the exchanges CoinGecko tracks.", "Market-wide volume")]),
]


class Faithful(unittest.TestCase):
    def test_every_faithful_answer_is_still_shown_with_all_four_checks_passed(self):
        self.assertGreaterEqual(len(FAITHFUL), 12)
        for question, bottom, points in FAITHFUL:
            with self.subTest(question):
                out = run(question, bottom, points)
                self.assertEqual(out["kind"], "answer", out["note"])
                self.assertTrue(out["used_ai"])
                self.assertEqual((out["guardrails"]["passed"], out["guardrails"]["total"]), (4, 4))
                self.assertEqual([w["section"] for w in out["why"]], [s for _, s in points])
                self.assertEqual(out["bottom_line"], bottom)

    def test_the_cited_section_is_among_the_retrieved_ones_for_every_case(self):
        for question, _, points in FAITHFUL:
            retrieved = [h["title"] for h in INDEX.search(question)]
            for _, section in points:
                self.assertIn(section, retrieved, question)


class AuditCases(unittest.TestCase):
    """A1 to A9 from the audit probe, with the same scripted model replies."""

    def test_a1_faithful_answer_is_kept(self):
        out = run("How many characters can a message have?", "A message may have at most 280 characters.", [("At most 280 characters.", "Wording rules")])
        self.assertEqual(out["kind"], "answer")
        self.assertEqual(out["guardrails"]["passed"], out["guardrails"]["total"])

    def test_a2_a_section_that_does_not_exist_is_dropped(self):
        out = run("How many characters can a message have?", "300 characters.", [("300 characters.", "Secret Rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertEqual([c["passed"] for c in out["guardrails"]["checks"]][:3], [False, True, True])
        self.assertTrue(check_result(out), "a missing section must not be blamed on the support check")

    def test_a3_a_real_section_that_was_not_retrieved_is_dropped(self):
        out = run("How many characters can a message have?", "300 characters.", [("300 characters.", "Sentiment index")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertEqual([c["passed"] for c in out["guardrails"]["checks"]][:3], [True, True, False])
        self.assertTrue(check_result(out))

    def test_a4_right_section_wrong_number_is_withheld(self):
        out = run("How many characters can a message have?", "A message may have at most 500 characters.",
                  [("The limit is 500 characters.", "Wording rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(out["used_ai"])
        self.assertFalse(check_result(out))
        self.assertLess(out["guardrails"]["passed"], out["guardrails"]["total"])
        self.assertNotIn("500", json.dumps(out["why"]) + out["bottom_line"])
        self.assertIn("280", " ".join(w["text"] for w in out["why"]))     # the playbook's own sentence is shown instead
        self.assertIn("not found", out["note"])

    def test_a4_a_wrong_number_only_in_the_bottom_line_falls_back_too(self):
        out = run("How many characters can a message have?", "A message may have at most 500 characters.", [("At most 280 characters.", "Wording rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(check_result(out))

    def test_a4_a_wrong_number_only_in_a_point_falls_back_too(self):
        out = run("How many characters can a message have?", "A message has a character limit.", [("The limit is 500 characters.", "Wording rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(check_result(out))

    def test_a5_an_invented_fact_is_withheld_with_or_without_the_word_buy(self):
        question = "At what values is fear and greed extreme?"
        invented = [
            ("A reading of 10 or lower is extreme fear and a strong buy signal for most traders.", "Extreme fear starts at 10."),
            ("A reading of 10 or lower is extreme fear and a strong signal for most traders.", "Extreme fear starts at 10."),
            ("Extreme fear is usually followed by a rebound within a week, so traders often return.", "Extreme fear is usually followed by a rebound."),
        ]
        for text, bottom in invented:
            with self.subTest(text):
                out = run(question, bottom, [(text, "Sentiment index")])
                self.assertEqual(out["kind"], "retrieval")
                self.assertFalse(out["used_ai"])
                self.assertFalse(check_result(out))
        without_buy = run(question, invented[1][1], [(invented[1][0], "Sentiment index")])
        self.assertNotIn("advice wording", without_buy["note"])         # held back by the new check, not by the word list
        self.assertTrue(without_buy["guardrails"]["checks"][1]["passed"])

    def test_a5_with_the_word_buy_fails_both_checks(self):
        out = run("At what values is fear and greed extreme?", "Extreme fear starts at 10.",
                  [("A reading of 10 or lower is extreme fear and a strong buy signal for most traders.", "Sentiment index")])
        names = {c["name"]: c["passed"] for c in out["guardrails"]["checks"]}
        self.assertFalse(names["No advice wording"])
        self.assertFalse(names[CHECK])

    def test_a6_advice_wording_is_still_withheld(self):
        out = run("At what values is fear and greed extreme?", "You should buy when fear is extreme.",
                  [("Extreme fear is a good time to buy.", "Sentiment index")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(out["guardrails"]["checks"][1]["passed"])
        self.assertIn("advice wording", out["note"])

    def test_a8_not_in_the_playbook_is_still_stated(self):
        out = run("What is the capital of France?", rag.NOT_IN_PLAYBOOK, [])
        self.assertIn(out["kind"], ("no_match", "not_in_playbook"))
        out = run("What does near threshold mean?", rag.NOT_IN_PLAYBOOK, [])
        self.assertEqual(out["kind"], "not_in_playbook")
        self.assertEqual(out["guardrails"]["passed"], out["guardrails"]["total"])

    def test_a9_an_answer_from_the_models_own_knowledge_is_withheld(self):
        question = "What is the Sharpe ratio?"
        self.assertIn("Volume rule", [h["title"] for h in INDEX.search(question)])       # the audit cited Volume rule
        for bottom, text in [
            ("The Sharpe ratio measures return per unit of risk.", "The Sharpe ratio measures the return of an investment per unit of risk taken."),
            ("The Sharpe ratio compares return with volatility.", "The Sharpe ratio is the average excess return divided by its standard deviation."),
            ("The Sharpe ratio is a ratio of excess return to volatility.", "A Sharpe ratio is the ratio of returns to risk."),
        ]:
            with self.subTest(text):
                out = run(question, bottom, [(text, "Volume rule")])
                self.assertEqual(out["kind"], "retrieval")
                self.assertFalse(out["used_ai"])
                self.assertFalse(check_result(out))
                self.assertNotIn("Sharpe", json.dumps(out["why"]) + out["bottom_line"])

    def test_a9_the_probe_text_about_a_stablecoin_never_reaches_the_model(self):
        out = run("What is a stablecoin?", "A stablecoin is a coin pegged to the dollar.", [("A stablecoin is pegged to a currency.", "Sentiment index")])
        self.assertEqual(out["kind"], "no_match")


class Numbers(unittest.TestCase):
    def forms(self, text, section=False):
        return rag.number_forms(text, section)

    def test_commas_percent_signs_and_trailing_zeros_do_not_matter(self):
        self.assertEqual(self.forms("1,000 coins"), {"1000"})
        self.assertEqual(self.forms("1000.0"), {"1000"})
        self.assertEqual(self.forms("2.50 and 95%"), {"2.5", "95"})
        self.assertEqual(self.forms("95.00 percent"), {"95"})
        self.assertEqual(self.forms("0.50"), {"0.5"})
        self.assertEqual(self.forms("007"), {"7"})
        self.assertEqual(self.forms("no digits here"), set())

    def test_a_sentence_end_and_a_dash_are_not_part_of_the_number(self):
        self.assertEqual(self.forms("the rank is 95."), {"95"})
        self.assertEqual(self.forms("the 30-day and 60-day windows"), {"30", "60"})
        self.assertEqual(self.forms("an 8.4% share."), {"8.4"})

    def test_spelled_out_numbers_count(self):
        self.assertEqual(self.forms("five hundred characters"), {"5", "100"})
        self.assertEqual(self.forms("Seven decisions"), {"7"})
        self.assertEqual(self.forms("about one day in twelve", section=True), {"1", "12"})
        self.assertEqual(self.forms("one short message"), set())              # a lone "one" in an answer is just a word

    def test_list_numbers_in_a_section_do_not_count_as_facts(self):
        section = "Rules:\n1. Alert on a coin.\n2. Calm market: every rank is below 90.\n6. Context."
        self.assertEqual(self.forms(section, section=True), {"90"})

    def test_a_spelled_out_wrong_number_is_caught(self):
        out = run("How many characters can a message have?", "A message may have at most five hundred characters.",
                  [("At most 280 characters.", "Wording rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(check_result(out))

    def test_a_number_found_in_a_different_section_does_not_support_the_point(self):
        # 95 is in Alert rules but not in Wording rules
        out = run("How many characters can a message have?", "A message has a character limit.", [("The limit is 95 characters.", "Wording rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(check_result(out))

    def test_every_number_in_the_answer_must_be_found(self):
        ok = run("What does near threshold mean?", "A rank from 90 up to 95 is near threshold.", [("Near threshold is a rank from 90 up to 95.", "Alert rules")])
        self.assertEqual(ok["kind"], "answer")
        bad = run("What does near threshold mean?", "A rank from 90 up to 95 is near threshold.", [("Near threshold is a rank from 90 up to 97.", "Alert rules")])
        self.assertEqual(bad["kind"], "retrieval")


class Words(unittest.TestCase):
    def test_support_is_the_share_of_key_words_found_in_the_cited_section(self):
        wording = SECTIONS["Wording rules"]
        self.assertEqual(rag.word_support("At most 280 characters.", wording), 1.0)
        self.assertEqual(rag.word_support("Bitcoin was created by Satoshi Nakamoto.", wording), 0.0)
        self.assertEqual(rag.word_support("280", wording), 0.0)           # no key words, nothing to support it
        self.assertAlmostEqual(rag.word_support("Messages are sent by SMS", wording), 1 / 3)      # only "message" is in the section

    def test_the_threshold_separates_the_faithful_set_from_the_invented_set(self):
        faithful = [rag.word_support(t, SECTIONS[s]) for _, _, points in FAITHFUL for t, s in points]
        invented = [rag.word_support(t, SECTIONS[s]) for t, s in [
            ("The Sharpe ratio measures the return of an investment per unit of risk taken.", "Volume rule"),
            ("The Sharpe ratio is the average excess return divided by its standard deviation.", "Volume rule"),
            ("A Sharpe ratio is the ratio of returns to risk.", "Volume rule"),
            ("A stablecoin is pegged to a currency.", "Sentiment index"),
            ("Extreme fear is usually followed by a rebound within a week, so traders often return.", "Sentiment index"),
            ("The agent alerts on a coin when the Fear and Greed index is extreme.", "Alert rules"),
            ("Bitcoin was created by Satoshi Nakamoto in a white paper.", "Data quality"),
            ("Messages are sent by SMS and must be written in capital letters.", "Wording rules"),
        ]]
        self.assertGreaterEqual(min(faithful), rag.MIN_WORD_SUPPORT)
        self.assertLess(max(invented), rag.MIN_WORD_SUPPORT)

    def test_a_point_with_only_a_few_matching_words_is_withheld(self):
        out = run("What does near threshold mean?", "A rank from 90 up to 95 is near threshold.",
                  [("The agent places a stop order with the broker when the rank hits 95.", "Alert rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(check_result(out))

    def test_one_unsupported_point_among_good_ones_withholds_the_whole_answer(self):
        good = ("Near threshold is a rank from 90 up to, but not including, 95.", "Alert rules")
        made_up = ("Every coin is reviewed by a human analyst before the Friday email goes out.", "Alert rules")
        out = run("What does near threshold mean?", "A rank from 90 up to 95 is near threshold.", [good, made_up])
        self.assertEqual(out["kind"], "retrieval")
        self.assertFalse(out["used_ai"])
        self.assertFalse(check_result(out))
        self.assertNotIn("analyst", json.dumps(out))


class Checks(unittest.TestCase):
    def test_the_new_check_is_named_and_comes_last(self):
        out = run("What does near threshold mean?", "A rank from 90 up to 95 is near threshold.", [("Near threshold is a rank from 90 up to 95.", "Alert rules")])
        names = [c["name"] for c in out["guardrails"]["checks"]]
        self.assertEqual(names, ["Cited sections exist", "No advice wording", "Only the retrieved sections are used", CHECK])
        self.assertEqual(out["guardrails"]["total"], 4)

    def test_every_other_kind_of_reply_lists_the_same_four_checks(self):
        for question in ("zzz qqq xxx", "Should I buy BTC now?", "a"):
            names = [c["name"] for c in rag.ask(question, INDEX, llm=None, ai=False)["guardrails"]["checks"]]
            self.assertEqual(len(names), 4, question)
            self.assertEqual(names[-1], CHECK)
        self.assertEqual(len(rag.ask("What does near threshold mean?", INDEX, ai=False)["guardrails"]["checks"]), 4)

    def test_guardrails_keeps_its_old_call_form(self):
        self.assertEqual(rag.guardrails()["total"], 4)
        self.assertEqual(rag.guardrails(False, True, True)["passed"], 3)
        self.assertEqual(rag.guardrails(supported=False)["passed"], 3)

    def test_the_withheld_answer_shows_the_playbook_text_and_says_why(self):
        out = run("How many characters can a message have?", "At most 500 characters.", [("The limit is 500 characters.", "Wording rules")])
        self.assertEqual(out["kind"], "retrieval")
        self.assertTrue(out["why"] and out["evidence"])
        self.assertIn("playbook", out["note"])
        for item in out["why"]:
            self.assertIn(item["text"], SECTIONS[item["section"]]["text"])

    def test_parse_answer_keeps_its_result_shape(self):
        raw = json.dumps({"bottom_line": "x", "why": [{"text": "At most 280 characters.", "section": "Wording rules"}]})
        hits = INDEX.search("How many characters can a message have?")
        bottom, why, related, problems = rag.parse_answer(raw, hits)
        self.assertEqual((bottom, len(why), related), ("x", 1, []))
        self.assertEqual(problems["missing_section"], 0)
        self.assertEqual(problems["not_retrieved"], 0)
        self.assertEqual(set(problems), {"missing_section", "not_retrieved"})      # unchanged: the support check runs in ask


if __name__ == "__main__":
    unittest.main()
