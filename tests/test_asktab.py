"""Offline tests for the Ask tab inside the HTML report (asktab.py, rag_static.js, rag_tab.js). No network, no AI call.

The browser engine is a port of rag.py, so the main test runs both on the same questions and requires the same answers.
That needs Node (present on GitHub's runners); without it those tests are skipped and say so.

    python tests/test_asktab.py -v
"""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import asktab  # noqa: E402
import rag  # noqa: E402
import report  # noqa: E402
from rag_reason_cases import REFUSED, STILL_ANSWERED  # noqa: E402
from test_report import make_data  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INDEX = rag.Index()
NODE = shutil.which("node")
EXTRA = [
    "", "   ", "ab", "x" * 400, "raise", "trigger fire", "What are the banned words?", "WHAT IS DRAWDOWN???", "Rank ranking ranks",
    "Keywords", "95", "5 percent", "Fear & Greed – extreme", "should I wait?", "price prediction for BTC", "what is the forecast",
    "can I buy", "Is it a good time to sell", "  When   does the agent   raise an alert?  ", "What does the playbook say about Volume rule?",
    "stale data", "Kraken", "alerts alerted alerting", "30-day average", "éè café volatility", "<b>bold</b> volatility",
    "What is the alert rank?", "tell me about DVOL", "how many candles are needed", "open interest and funding",
]
TOPIC_QUESTIONS = [asktab.topic_question(t["title"]) for t in rag.topics(INDEX)]
REASON_QUESTIONS = [q for q, _ in REFUSED] + STILL_ANSWERED      # the reason codes: refused by code, and the tricky ones still answered
QUESTIONS = [q for q, _ in rag.CHECKS] + rag.NO_MATCH + rag.DECLINE + TOPIC_QUESTIONS + EXTRA + REASON_QUESTIONS
TEXTS = [s["title"] + "\n" + s["text"] for s in INDEX.sections] + QUESTIONS


def run_node():
    runner = ROOT / "tests" / "asktab_runner.js"
    done = subprocess.run([NODE, str(runner)], input=json.dumps({"data": asktab.data(INDEX), "questions": QUESTIONS, "texts": TEXTS}),
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError("node failed: " + done.stderr[-500:])
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "Node is not installed, so the browser engine cannot be compared with rag.py here")
class SameAsPython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = run_node()

    def test_every_question_gets_the_same_answer_as_in_rag_py(self):
        for question, got in zip(QUESTIONS, self.js["answers"]):
            want = rag.ask(question, INDEX, ai=False)
            for key in ("question", "kind", "reason", "reason_text", "bottom_line", "why", "related", "evidence"):
                self.assertEqual(got[key], want[key], f"{key} differs for {question!r}")
            if want["kind"] != "retrieval":     # the note on a retrieval answer says "no AI in this tab" instead of "AI answers are off"
                self.assertEqual(got["note"], want["note"], f"note differs for {question!r}")
            else:
                self.assertTrue(got["note"])

    def test_every_new_reason_question_gets_the_same_kind_and_reason_in_the_browser(self):
        by_question = dict(zip(QUESTIONS, self.js["answers"]))
        for question, code in REFUSED:
            self.assertEqual((by_question[question]["kind"], by_question[question]["reason"]), ("declined", code), question)
            self.assertEqual(by_question[question]["reason"], rag.ask(question, INDEX, ai=False)["reason"], question)
        for question in STILL_ANSWERED:
            self.assertEqual((by_question[question]["kind"], by_question[question]["reason"]), ("retrieval", "ai_off"), question)

    def test_empty_and_off_topic_questions_carry_their_reason_in_the_browser(self):
        by_question = dict(zip(QUESTIONS, self.js["answers"]))
        for question in ("", "   ", "ab"):
            self.assertEqual((by_question[question]["kind"], by_question[question]["reason"]), ("error", "empty_question"), question)
        for question in rag.NO_MATCH:
            self.assertEqual((by_question[question]["kind"], by_question[question]["reason"]), ("no_match", "off_topic"), question)

    def test_the_ranked_sections_and_scores_match(self):
        for question, got in zip(QUESTIONS, self.js["hits"]):
            want = INDEX.search(question)
            self.assertEqual([h["title"] for h in got], [h["title"] for h in want], question)
            for a, b in zip(got, want):
                self.assertAlmostEqual(a["score"], b["score"], delta=0.0011, msg=question)

    def test_terms_and_sentences_match(self):
        for text, terms, sentences in zip(TEXTS, self.js["terms"], self.js["sentences"]):
            self.assertEqual(terms, rag.terms(text), text[:60])
            self.assertEqual(sentences, rag.sentences(text), text[:60])

    def test_the_retrieval_test_set_passes_in_the_browser_engine_too(self):
        by_question = dict(zip(QUESTIONS, self.js["answers"]))
        for question, expected in rag.CHECKS:
            self.assertIn(expected, [e["title"] for e in by_question[question]["evidence"]], question)
        for question in rag.NO_MATCH:
            self.assertEqual(by_question[question]["kind"], "no_match", question)
        for question in rag.DECLINE:
            self.assertEqual(by_question[question]["kind"], "declined", question)

    def test_every_topic_tile_finds_its_own_section_first(self):
        by_question = dict(zip(QUESTIONS, self.js["answers"]))
        for topic, question in zip(rag.topics(INDEX), TOPIC_QUESTIONS):
            self.assertEqual(by_question[question]["evidence"][0]["title"], topic["title"], question)

    def test_a_quote_is_a_sentence_of_the_section_word_for_word(self):
        sections = {s["title"]: s["text"] for s in INDEX.sections}
        for answer in self.js["answers"]:
            for item in answer["evidence"]:
                self.assertIn(item["quote"], sections[item["title"]])


class Data(unittest.TestCase):
    def test_the_engine_gets_its_numbers_from_rag_py(self):
        d = asktab.data(INDEX)
        self.assertEqual(d["config"], {"top_k": rag.TOP_K, "min_share": rag.MIN_SHARE, "k1": rag.K1, "b": rag.B,
                                       "title_repeat": rag.TITLE_REPEAT, "max_question": rag.MAX_QUESTION})
        self.assertEqual(d["stopwords"], sorted(rag.STOPWORDS))
        self.assertEqual(d["advice"]["source"], rag.ADVICE_REQUEST_RE.pattern)
        self.assertEqual(d["advice_only"]["source"], rag.ADVICE_ONLY_RE.pattern)
        self.assertEqual(d["personal"]["source"], rag.PERSONAL_RE.pattern)
        self.assertEqual(d["live"]["sources"], [p.pattern for p in rag.LIVE_DATA_RES])
        self.assertEqual(d["security"]["sources"], [p.pattern for p in rag.SECURITY_RES])
        self.assertEqual(d["reasons"], rag.REASON_TEXT)
        self.assertEqual(d["refused"], rag.REFUSED_LINE)
        self.assertEqual(len(d["sections"]), 18)
        self.assertEqual([s["title"] for s in d["sections"]], [s["title"] for s in INDEX.sections])

    def test_json_in_the_page_cannot_close_the_script_or_open_a_comment(self):
        out = asktab.as_json({"x": "</script><script>alert(1)</script><!-- <b>"})
        self.assertNotIn("<", out)
        self.assertEqual(json.loads(out)["x"], "</script><script>alert(1)</script><!-- <b>")

    def test_the_playbook_inside_the_page_is_the_playbook(self):
        page = report.build_html(make_data())
        raw = re.search(r'<script type="application/json" id="ask-data">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(raw), json.loads(asktab.as_json(asktab.data())))


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = report.build_html(make_data())

    def test_the_page_has_two_tabs_and_two_panels_and_opens_on_the_report(self):
        p = self.page
        self.assertIn('role="tablist"', p)
        self.assertEqual(len(re.findall(r'role="tab"', p)), 2)
        self.assertEqual(len(re.findall(r'role="tabpanel"', p)), 2)
        self.assertIn('id="tab-report" aria-controls="panel-report" aria-selected="true"', p)
        self.assertIn('id="tab-ask" aria-controls="panel-ask" aria-selected="false"', p)
        self.assertRegex(p, r'<main class="wrap" id="panel-ask"[^>]*\bhidden>')
        self.assertNotRegex(p, r'<main class="wrap" id="panel-report"[^>]*\bhidden>')

    def test_without_javascript_there_are_no_dead_tabs(self):
        self.assertRegex(self.page, r'<nav class="tabbar" id="tabbar"[^>]*\bhidden>')

    def test_the_brand_bar_link_opens_the_tab_in_the_same_file(self):
        self.assertIn('<a href="#ask" data-tab="ask"', self.page)
        self.assertNotIn("127.0.0.1:8765/\"", self.page.split('id="panel-ask"')[0])

    def test_no_id_is_used_twice(self):
        ids = re.findall(r'\bid="([^"]+)"', self.page)
        self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [])

    def test_a_hash_name_is_never_an_element_id(self):
        # opening the page at #ask must not jump to an element of that name
        self.assertNotIn('id="ask"', self.page)
        self.assertNotIn('id="report"', self.page)

    def test_the_topic_tiles_and_example_chips_are_built_by_the_server(self):
        panel = asktab.panel(INDEX)
        self.assertEqual(panel.count('class="topic"'), 18)
        for question in asktab.EXAMPLES:
            self.assertIn(f'data-q="{question}"', panel)
        for title in (s["title"] for s in INDEX.sections):
            self.assertIn(f"What does the playbook say about {title}?".replace("&", "&amp;"), panel)

    def test_the_reference_numbers_come_from_the_agent_settings(self):
        panel = asktab.panel(INDEX)
        self.assertIn(f"{agent.ALERT_RANK:g} to 100", panel)
        self.assertIn(f"{agent.MAX_MESSAGE_CHARS} characters at most", panel)
        for word in agent.BANNED_WORDS:
            self.assertIn(f"<span>{word}</span>", panel)

    def test_the_page_stays_far_below_the_size_the_email_script_accepts(self):
        limit = int(re.search(r"MAX_REPORT_CHARS\s*=\s*([\d_]+)", (ROOT / "apps_script" / "Code.gs").read_text(encoding="utf-8")).group(1).replace("_", ""))
        self.assertLess(len(self.page), limit // 2)

    def test_the_panel_says_what_the_tab_is_and_what_it_is_not(self):
        panel = asktab.panel(INDEX)
        self.assertIn("No AI in this tab", panel)
        self.assertIn("does not give advice", panel)
        self.assertIn(asktab.LAUNCHER, panel)


class Code(unittest.TestCase):
    """The tab's JavaScript makes no request, stores nothing and never writes user text as HTML."""
    FILES = ("rag_static.js", "rag_tab.js")

    def test_no_network_no_key_no_storage_no_html_injection(self):
        banned = ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "GROQ", "api_key", "apiKey", "localStorage", "sessionStorage", "cookie",
                  "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "import(")
        for name in self.FILES:
            code = (ROOT / name).read_text(encoding="utf-8")
            for word in banned:
                self.assertNotIn(word, code, f"{name} contains {word}")

    def test_the_files_are_safe_to_inline_and_run_in_older_phone_browsers(self):
        for name in self.FILES:
            code = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("</script", code.lower(), name)
            self.assertNotIn("<!--", code, name)
            self.assertNotIn("(?<", code, f"{name} uses regex look-behind, which older Safari cannot parse")
            self.assertNotIn("?.", code.replace("?.length", ""), f"{name} uses optional chaining")
            self.assertNotIn("??", code, f"{name} uses nullish coalescing")

    def test_user_text_is_only_ever_set_as_text(self):
        code = (ROOT / "rag_tab.js").read_text(encoding="utf-8")
        self.assertIn("textContent", code)
        self.assertNotRegex(code, r"\.html\s*=|\.innerHTML")


if __name__ == "__main__":
    unittest.main()
