"""Fixes D-08 and D-13 of the audit: the Ask question guard and the recall of paraphrased questions. Offline: no network, no AI call.

D-08: eight advice or forecast questions got past the guard and reached the AI (or the no-match path) instead of being declined by code.
D-13: paraphrased questions found the right section first in only 3 of 8 cases and in the top three in 5 of 8.

The answerable questions below are the audit's own set (written from a reading of playbook.md, not taken from rag.CHECKS).
The browser engine (rag_static.js) takes the advice pattern and the synonyms from rag.py, so the Node run must agree with Python.

    python tests/test_fix_d08_d13.py -v
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asktab  # noqa: E402
import rag  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INDEX = rag.Index()
NODE = shutil.which("node")

# (question, kind, expected sections). Any expected section counts as a hit.
ANSWERABLE = [
    ("How is 30-day volatility calculated?", "direct", ["Volatility rule"]),
    ("What is the minimum number of days of history needed?", "direct", ["Data quality"]),
    ("How is the distance from the 30-day average defined?", "direct", ["Trend against the 30-day average"]),
    ("What is drawdown in this agent?", "direct", ["Drawdown rule"]),
    ("How is the volume ratio computed?", "direct", ["Volume rule"]),
    ("What is relative strength?", "direct", ["Relative strength rule"]),
    ("How is correlation between coins measured?", "direct", ["Correlation rule"]),
    ("When is the trend label up?", "direct", ["Trend label"]),
    ("How is the spread calculated from the order book?", "direct", ["Liquidity (order book)"]),
    ("What does a positive funding rate mean?", "direct", ["Derivatives (funding rate and open interest)"]),
    ("Which coins have a DVOL index?", "direct", ["Implied volatility"]),
    ("What is market-wide volume and where does it come from?", "direct", ["Market-wide volume"]),
    ("At what values is fear and greed extreme?", "direct", ["Sentiment index"]),
    ("When do the two price sources disagree?", "direct", ["Price source check"]),
    ("What is the alert history base rate?", "direct", ["Alert history base rate"]),
    ("What happens on a calm market day?", "direct", ["Alert rules"]),
    ("How many decisions back does the repeat rule look?", "direct", ["Repeat alerts"]),
    ("How long may an alert message be?", "direct", ["Wording rules"]),
    ("Which words are banned in an alert?", "direct", ["Wording rules"]),
    ("What band is used for order book depth?", "numeric", ["Liquidity (order book)"]),
    ("What is the near-threshold range?", "numeric", ["Alert rules"]),
    ("What tolerance does the price check use?", "numeric", ["Price source check"]),
    ("How many characters can a message have?", "numeric", ["Wording rules"]),
    ("What rank counts as an alert?", "numeric", ["Alert rules"]),
    ("How does the agent decide a coin is behaving abnormally?", "paraphrase", ["Alert rules"]),
    ("Why not use one fixed percentage move for every coin?", "paraphrase", ["Volatility rule"]),
    ("What if the exchange returns nothing for a coin?", "paraphrase", ["Data quality"]),
    ("Does the agent send the same news two days in a row?", "paraphrase", ["Repeat alerts"]),
    ("Can crowded leverage trigger an alert?", "paraphrase", ["Derivatives (funding rate and open interest)"]),
    ("Does a high options-market reading cause an alert?", "paraphrase", ["Implied volatility"]),
    ("Are alerts sent when one coin lags the others?", "paraphrase", ["Relative strength rule"]),
    ("How is the incomplete current day treated?", "paraphrase", ["Data quality"]),
    ("If volume is unusual but price ranks are calm, does the agent alert?", "multi", ["Volume rule", "Alert rules"]),
    ("Can an extreme Fear and Greed reading alone raise an alert?", "multi", ["Sentiment index", "Alert rules"]),
    ("If CoinGecko disagrees with Kraken is the alert blocked?", "multi", ["Price source check", "Alert rules"]),
]
PARAPHRASES = [(q, expected) for q, kind, expected in ANSWERABLE if kind == "paraphrase"]
# Misses that were there before this fix and are not part of it. A new miss outside this set is a regression.
KNOWN_FIRST_PLACE_MISSES = {
    "What is the minimum number of days of history needed?", "How long may an alert message be?", "What rank counts as an alert?",
}

# D-08: the eight that got through, then the wording of the same kinds of question.
DECLINED_BEFORE_FIX = [
    "Is it a good moment to accumulate XRP?", "What will ETH be worth in December?", "Tell me whether to sell my Bitcoin",
    "Do you recommend holding ETH?", "Is BTC about to crash?", "Which coin will outperform next month?",
    "Is now the time to enter a long position?", "Is Bitcoin a good investment?",
]
DECLINED_SIBLINGS = [
    "Is it the right time to buy ETH?", "Would you advise selling my XRP?", "Is XRP a good buy?", "Tell me whether to hold my ETH",
    "Is ETH about to surge?", "Will BTC be worth more in a year?", "Will the price be higher next week?", "Which crypto will go up this month?",
    "Is this the best moment to exit Bitcoin?", "What do you recommend for my ETH?",
]
# The seven that were already declined must stay declined.
DECLINED_ALREADY = ["Should I buy BTC now?", "Will bitcoin go up next week?", "Give me a price target for ETH", "Predict tomorrow's XRP close",
                    "Should we exit XRP?", "Can I trade on these alerts?", "What is the best coin to buy today?"]
# Questions about the rules themselves, which use the same words. None of them may be declined.
ABOUT_THE_RULES = [
    "Which words are banned in an alert message?", "What does the wording rule say about buy and sell?", "Why are the words buy and sell banned?",
    "Is the word hold allowed in an alert message?", "Why is the word recommend banned?", "Why is the word should banned?",
    "Which words does the wording rule ban?", "What does the playbook say about advice?", "Does the agent give advice or predictions?",
    "Does the agent tell anyone whether to buy?", "Does the agent recommend anything?", "What does the agent say instead of a recommendation?",
    "What does a positive funding rate mean for long positions?", "Who pays whom when funding is negative, longs or shorts?",
    "Will the price source check block an alert?", "Will XRP alert if the volatility rank is 96?", "Will it repeat an alert tomorrow?",
    "Which coin will the agent alert on first?", "Which coins will be in the alert message?", "Which coin leads in relative strength?",
    "What is a good rank for an alert?", "Is 90 days a good amount of history?", "What time does the daily run start?",
    "How much time is needed between alerts?", "What happens when a rank is about to reach 95?", "Is the trend label a direction or a rule?",
    "What is the stop loss rule in the playbook?", "Is a drawdown about to be flagged?",
]


def never_called(messages):
    raise AssertionError("the AI must not be called here")


def titles(question):
    return [h["title"] for h in INDEX.search(question)]


class AdviceGuard(unittest.TestCase):
    def test_every_advice_or_forecast_question_is_declined_by_code(self):
        for question in DECLINED_BEFORE_FIX + DECLINED_SIBLINGS + DECLINED_ALREADY:
            out = rag.ask(question, INDEX, llm=never_called)       # never_called raises if the guard lets it through
            self.assertEqual(out["kind"], "declined", question)
            self.assertEqual(out["bottom_line"], rag.DECLINED[0], question)
            self.assertEqual([w["section"] for w in out["why"]], ["Wording rules"], question)
            self.assertFalse(out["used_ai"], question)

    def test_the_eight_questions_of_the_audit_match_the_pattern(self):
        # The eight misses of the audit: the test fails if the pattern is ever narrowed back.
        for question in DECLINED_BEFORE_FIX:
            self.assertTrue(rag.ADVICE_REQUEST_RE.search(question), question)

    def test_no_answerable_question_is_declined(self):
        for question, _kind, _expected in ANSWERABLE:
            self.assertIsNone(rag.ADVICE_REQUEST_RE.search(question), question)

    def test_no_repo_check_is_declined(self):
        for question, _section in rag.CHECKS:
            self.assertIsNone(rag.ADVICE_REQUEST_RE.search(question), question)
        for question in rag.NO_MATCH:
            self.assertIsNone(rag.ADVICE_REQUEST_RE.search(question), question)

    def test_questions_about_the_rules_are_not_declined(self):
        for question in ABOUT_THE_RULES:
            self.assertIsNone(rag.ADVICE_REQUEST_RE.search(question), question)
            self.assertNotEqual(rag.ask(question, INDEX, ai=False)["kind"], "declined", question)

    def test_the_topic_tiles_are_not_declined(self):
        for topic in rag.topics(INDEX):
            self.assertIsNone(rag.ADVICE_REQUEST_RE.search(asktab.topic_question(topic["title"])), topic["title"])

    def test_the_pattern_has_no_construct_the_browser_cannot_run(self):
        for token in ("(?<", "(?i", "(?P", "(?#", "(?>"):
            self.assertNotIn(token, rag.ADVICE_REQUEST_RE.pattern)


class Recall(unittest.TestCase):
    def test_every_paraphrase_finds_its_section_first(self):
        for question, expected in PARAPHRASES:
            self.assertEqual(titles(question)[:1], expected, question)

    def test_precision_and_recall_on_the_audit_questions(self):
        first = sum(1 for q, _k, exp in ANSWERABLE if titles(q)[:1] and titles(q)[0] in exp)
        top3 = sum(1 for q, _k, exp in ANSWERABLE if any(e in titles(q) for e in exp))
        self.assertGreaterEqual(first, 32, f"{first} of {len(ANSWERABLE)} first")        # 27 before the fix
        self.assertGreaterEqual(top3, 34, f"{top3} of {len(ANSWERABLE)} in the top three")  # 31 before the fix

    def test_the_only_first_place_misses_are_the_known_ones(self):
        missed = {q for q, _k, exp in ANSWERABLE if not (titles(q) and titles(q)[0] in exp)}
        self.assertLessEqual(missed, KNOWN_FIRST_PLACE_MISSES)

    def test_every_synonym_target_is_a_term_of_the_playbook(self):
        # A typo or an unstemmed word would silently do nothing (the first draft of "implied" was one).
        for words, extra in rag.EVERYDAY_WORDS:
            for word in extra:
                self.assertGreater(INDEX.frequency.get(rag.stem(word), 0), 0, word)

    def test_the_synonyms_are_keyed_by_the_stem_the_question_produces(self):
        for words, _extra in rag.EVERYDAY_WORDS:
            for word in words:
                self.assertIn(rag.stem(word), rag.QUERY_SYNONYMS, word)
                self.assertIn(rag.stem(word), rag.terms(f"the {word} of a coin"), word)

    def test_the_synonyms_change_the_question_only(self):
        before = [list(d) for d in INDEX.docs]
        INDEX.search("Does an abnormal lag on the exchange leverage the options?")
        self.assertEqual(before, [list(d) for d in INDEX.docs])


@unittest.skipUnless(NODE, "Node is not installed, so the browser engine cannot be compared with rag.py here")
class BrowserEngineAgrees(unittest.TestCase):
    QUESTIONS = (DECLINED_BEFORE_FIX + DECLINED_SIBLINGS + DECLINED_ALREADY + ABOUT_THE_RULES + [q for q, _k, _e in ANSWERABLE]
                 + [q for q, _s in rag.CHECKS])

    @classmethod
    def setUpClass(cls):
        payload = json.dumps({"data": asktab.data(INDEX), "questions": cls.QUESTIONS, "texts": []})
        done = subprocess.run([NODE, str(ROOT / "tests" / "asktab_runner.js")], input=payload, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise AssertionError("node failed: " + done.stderr[-500:])
        cls.js = json.loads(done.stdout)

    def test_zero_differences_with_python(self):
        for question, got, hits in zip(self.QUESTIONS, self.js["answers"], self.js["hits"]):
            want = rag.ask(question, INDEX, ai=False)
            for key in ("kind", "reason", "bottom_line", "why", "evidence"):
                self.assertEqual(got[key], want[key], f"{key} differs for {question!r}")
            self.assertEqual([h["title"] for h in hits], titles(question), question)

    def test_the_browser_declines_the_same_questions(self):
        by_question = dict(zip(self.QUESTIONS, self.js["answers"]))
        for question in DECLINED_BEFORE_FIX + DECLINED_SIBLINGS + DECLINED_ALREADY:
            self.assertEqual(by_question[question]["kind"], "declined", question)
        for question in ABOUT_THE_RULES:
            self.assertNotEqual(by_question[question]["kind"], "declined", question)

    def test_the_browser_finds_every_paraphrase_first(self):
        by_question = dict(zip(self.QUESTIONS, self.js["answers"]))
        for question, expected in PARAPHRASES:
            self.assertEqual(by_question[question]["evidence"][0]["title"], expected[0], question)


if __name__ == "__main__":
    unittest.main()
