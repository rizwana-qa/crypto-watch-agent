"""Offline tests for rag.py (retrieval and the answer guardrails) and rag_server.py (the local server). No network, no AI call.

    python tests/test_rag.py -v
"""

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import rag  # noqa: E402
import rag_server  # noqa: E402

INDEX = rag.Index()
NEAR = "What does near threshold mean?"          # best section: Alert rules
WORDS = "Which words are banned in an alert message?"   # best section: Wording rules


def fake(content):
    """A stand-in for the Groq call that records what it was given."""
    calls = []

    def llm(messages):
        calls.append(messages)
        return {"content": content if isinstance(content, str) else json.dumps(content)}
    llm.calls = calls
    return llm


def never_called(messages):
    raise AssertionError("the AI must not be called here")


def titles(question):
    return [h["title"] for h in INDEX.search(question)]


class Retrieval(unittest.TestCase):
    def test_every_check_passes(self):
        failed = [text for passed, text in rag.run_checks(INDEX) if not passed]
        self.assertEqual(failed, [])

    def test_the_check_run_has_a_refusal_section_that_reaches_every_reason_code(self):
        results = rag.run_checks(INDEX)
        self.assertGreater(len(results), 26)                       # 26 retrieval checks before the refusal section was added
        text = "\n".join(t for _, t in results)
        for code in rag.REASON_TEXT:
            self.assertIn(code, text, code)
        self.assertIn("every reason code is reached at least once", text)

    def test_first_hit_is_right_for_nearly_every_check(self):
        first = sum(1 for q, section in rag.CHECKS if titles(q)[:1] == [section])
        self.assertGreaterEqual(first, len(rag.CHECKS) - 3, f"{first} of {len(rag.CHECKS)}")

    def test_the_topic_tiles_find_their_own_section_first(self):
        for topic in rag.topics(INDEX):
            question = f"What does the playbook say about {topic['title']}?"
            self.assertEqual(titles(question)[0], topic["title"], question)

    def test_every_suggested_follow_up_finds_a_section(self):
        suggested = {q for qs in rag.RELATED.values() for q in qs} | set(rag.DEFAULT_RELATED)
        for question in suggested:
            self.assertTrue(titles(question), question)

    def test_the_playbook_has_eighteen_sections(self):
        self.assertEqual(len(INDEX.sections), 18)
        self.assertEqual(len(rag.topics(INDEX)), 18)

    def test_stemming_joins_the_forms_of_a_word(self):
        self.assertEqual(rag.stem("rules"), rag.stem("rule"))
        self.assertEqual(rag.stem("ranking"), rag.stem("rank"))
        self.assertEqual(rag.stem("ranks"), rag.stem("rank"))
        self.assertEqual(rag.stem("words"), rag.stem("word"))
        self.assertEqual(rag.stem("wording"), rag.stem("word"))
        self.assertEqual(rag.stem("breaches"), rag.stem("breach"))

    def test_terms_drop_filler_and_keep_numbers(self):
        found = rag.terms("What is the rank of BTC at 95?")
        self.assertIn("rank", found)
        self.assertIn("btc", found)
        self.assertIn("95", found)
        self.assertNotIn("the", found)
        self.assertNotIn("what", found)

    def test_raise_and_trigger_reach_the_alert_rules(self):
        self.assertEqual(titles("When does the agent raise an alert?")[0], "Alert rules")
        self.assertEqual(titles("What triggers an alert?")[0], "Alert rules")

    def test_synonyms_change_the_question_only_not_the_playbook(self):
        before = [list(d) for d in INDEX.docs]
        INDEX.search("When does the agent raise an alert?")
        self.assertEqual(before, [list(d) for d in INDEX.docs])

    def test_nothing_matches_off_topic_words(self):
        for question in rag.NO_MATCH:
            self.assertEqual(INDEX.search(question), [], question)

    def test_scores_are_stable_and_ordered(self):
        first, second = INDEX.search(NEAR), INDEX.search(NEAR)
        self.assertEqual([h["title"] for h in first], [h["title"] for h in second])
        scores = [h["score"] for h in first]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertLessEqual(len(first), rag.TOP_K)

    def test_a_quote_is_a_sentence_of_the_section_word_for_word(self):
        for question, _ in rag.CHECKS:
            for hit in INDEX.search(question):
                quote = rag.quote_for(hit, question)
                self.assertIn(quote, hit["text"], f"{question} / {hit['title']}")

    def test_clip_cuts_at_a_word_and_leaves_short_text_alone(self):
        self.assertEqual(rag.clip("short text"), "short text")
        clipped = rag.clip("alpha beta gamma delta epsilon zeta eta theta", 20)
        self.assertTrue(clipped.endswith("…"))
        self.assertLessEqual(len(clipped), 21)
        self.assertIn(clipped[:-1], "alpha beta gamma delta epsilon zeta eta theta")
        self.assertTrue(clipped[:-1].split()[-1] in "alpha beta gamma delta epsilon zeta eta theta".split())

    def test_topic_lines_are_short_and_never_cut_inside_a_word(self):
        for topic in rag.topics(INDEX):
            self.assertLessEqual(len(topic["line"]), 121, topic["title"])
            if topic["line"].endswith("…"):
                body = topic["line"][:-1]
                self.assertFalse(body.endswith((" ", ",", "(")), topic["title"])

    def test_the_reference_comes_from_the_agent_settings(self):
        ref = rag.reference()
        self.assertEqual(ref["alert_rank"], agent.ALERT_RANK)
        self.assertEqual(ref["max_chars"], agent.MAX_MESSAGE_CHARS)
        self.assertEqual(ref["banned_words"], list(agent.BANNED_WORDS))


class Answers(unittest.TestCase):
    def good(self, **extra):
        data = {"bottom_line": "A rank from 90 up to 95 is near threshold and raises no alert.",
                "why": [{"text": "Near threshold is a rank from 90 up to, but not including, 95.", "section": "Alert rules"}],
                "related": ["When does the agent not repeat an alert?"]}
        data.update(extra)
        return data

    def test_a_valid_answer_is_used_and_all_checks_pass(self):
        llm = fake(self.good())
        out = rag.ask(NEAR, INDEX, llm=llm)
        self.assertEqual(out["kind"], "answer")
        self.assertEqual((out["reason"], out["reason_text"]), ("", ""))       # a normal answer has no reason
        self.assertTrue(out["used_ai"])
        self.assertEqual((out["guardrails"]["passed"], out["guardrails"]["total"]), (4, 4))
        self.assertEqual([w["section"] for w in out["why"]], ["Alert rules"])
        self.assertIn("Alert rules", [e["title"] for e in out["evidence"]])
        self.assertEqual(len(llm.calls), 1)

    def test_the_ai_sees_only_the_retrieved_sections(self):
        llm = fake(self.good())
        rag.ask(NEAR, INDEX, llm=llm)
        system, user = llm.calls[0]
        retrieved = titles(NEAR)
        for title in retrieved:
            self.assertIn(f"### {title}", system["content"])
        for section in INDEX.sections:
            if section["title"] not in retrieved:
                self.assertNotIn(f"### {section['title']}", system["content"])
        self.assertEqual(user["content"], NEAR)

    def test_an_invented_section_is_dropped_and_the_check_fails(self):
        data = self.good(why=[{"text": "Something made up.", "section": "Moon phases"},
                              {"text": "Near threshold is a rank from 90 up to, but not including, 95.", "section": "Alert rules"}])
        out = rag.ask(NEAR, INDEX, llm=fake(data))
        self.assertEqual([w["section"] for w in out["why"]], ["Alert rules"])
        self.assertEqual(out["guardrails"]["passed"], 3)
        self.assertFalse(out["guardrails"]["checks"][0]["passed"])

    def test_a_real_section_that_was_not_retrieved_is_dropped(self):
        other = next(s["title"] for s in INDEX.sections if s["title"] not in titles(NEAR))
        data = self.good(why=[{"text": "From somewhere else in the playbook.", "section": other},
                              {"text": "Near threshold is a rank from 90 up to, but not including, 95.", "section": "Alert rules"}])
        out = rag.ask(NEAR, INDEX, llm=fake(data))
        self.assertEqual([w["section"] for w in out["why"]], ["Alert rules"])
        self.assertFalse(out["guardrails"]["checks"][2]["passed"])

    def test_when_every_point_is_dropped_the_playbook_text_is_shown(self):
        data = self.good(why=[{"text": "Made up.", "section": "Moon phases"}])
        out = rag.ask(NEAR, INDEX, llm=fake(data))
        self.assertEqual(out["kind"], "retrieval")
        self.assertEqual(out["reason"], "ai_no_citation")
        self.assertFalse(out["used_ai"])
        self.assertIn("no valid citation", out["note"])
        self.assertTrue(out["why"])

    def test_advice_wording_is_withheld(self):
        data = self.good(bottom_line="You should buy when a coin is near threshold.")
        out = rag.ask(NEAR, INDEX, llm=fake(data))
        self.assertEqual(out["kind"], "retrieval")
        self.assertEqual(out["reason"], "ai_withheld_advice")
        self.assertFalse(out["used_ai"])
        self.assertIn("withheld", out["note"])
        self.assertFalse(out["guardrails"]["checks"][1]["passed"])
        self.assertNotIn("buy", json.dumps(out["why"]).lower().replace("buy, sell", ""))

    def test_the_wording_rules_question_may_list_the_banned_words(self):
        data = {"bottom_line": "Words such as buy, sell and hold are never used in an alert message.",
                "why": [{"text": "Do not use the words buy, sell, long, short, hold, entry, exit, accumulate, should or recommend.",
                         "section": "Wording rules"}]}
        out = rag.ask(WORDS, INDEX, llm=fake(data))
        self.assertEqual(out["kind"], "answer")
        self.assertEqual(out["guardrails"]["passed"], 4)

    def test_malformed_or_failed_ai_replies_fall_back_to_the_playbook_text(self):
        for reply in ("not json at all", "", "[1, 2, 3]", '{"bottom_line": "x", "why": "none"}'):
            out = rag.ask(NEAR, INDEX, llm=fake(reply))
            self.assertEqual(out["kind"], "retrieval", reply)
            self.assertTrue(out["why"], reply)

        def broken(messages):
            raise agent.AgentError("GROQ_API_KEY is not set")
        out = rag.ask(NEAR, INDEX, llm=broken)
        self.assertEqual(out["kind"], "retrieval")
        self.assertEqual(out["reason"], "ai_unavailable")
        self.assertIn("not used", out["note"])

    def test_json_in_a_code_fence_is_accepted(self):
        out = rag.ask(NEAR, INDEX, llm=fake("```json\n" + json.dumps(self.good()) + "\n```"))
        self.assertEqual(out["kind"], "answer")

    def test_not_in_the_playbook_is_stated_without_evidence(self):
        out = rag.ask(NEAR, INDEX, llm=fake({"bottom_line": rag.NOT_IN_PLAYBOOK, "why": []}))
        self.assertEqual(out["kind"], "not_in_playbook")
        self.assertEqual(out["reason"], "not_in_playbook")
        self.assertEqual((out["why"], out["evidence"]), ([], []))
        self.assertTrue(out["used_ai"])

    def test_advice_and_forecast_requests_are_declined_before_any_ai_call(self):
        for question in rag.DECLINE + ["Should we sell ETH?", "Is this a good time to buy?", "Give me financial advice", "Predict the price of XRP"]:
            out = rag.ask(question, INDEX, llm=never_called)
            self.assertEqual(out["kind"], "declined", question)
            self.assertEqual(out["why"][0]["section"], "Wording rules")
            self.assertIn(out["reason"], ("advice_request", "price_forecast"), question)
            self.assertFalse(out["used_ai"])

    def test_questions_about_the_rules_are_not_declined(self):
        for question in (WORDS, "Why is the word should banned?", "What is the stop loss rule in the playbook?", NEAR):
            self.assertNotEqual(rag.ask(question, INDEX, llm=fake(self.good()))["kind"], "declined", question)

    def test_no_match_is_stated_and_the_ai_is_not_called(self):
        out = rag.ask("zzz qqq xxx", INDEX, llm=never_called)
        self.assertEqual(out["kind"], "no_match")
        self.assertEqual(out["reason"], "off_topic")
        self.assertEqual(out["bottom_line"], rag.NOT_IN_PLAYBOOK)

    def test_with_the_ai_off_only_the_playbook_text_is_shown(self):
        out = rag.ask(NEAR, INDEX, llm=never_called, ai=False)
        self.assertEqual(out["kind"], "retrieval")
        self.assertEqual(out["reason"], "ai_off")
        self.assertFalse(out["used_ai"])
        self.assertTrue(out["why"] and out["evidence"])
        for item in out["why"]:
            self.assertIn(item["text"], next(s["text"] for s in INDEX.sections if s["title"] == item["section"]))

    def test_a_blank_question_is_an_error_not_a_crash(self):
        for question in ("", "  ", "a", None):
            out = rag.ask(question, INDEX, llm=never_called)
            self.assertEqual((out["kind"], out["reason"]), ("error", "empty_question"))
            self.assertEqual(out["reason_text"], rag.REASON_TEXT["empty_question"])

    def test_a_long_question_is_cut(self):
        llm = fake(self.good())
        out = rag.ask(NEAR + " " + "x" * 1000, INDEX, llm=llm)
        self.assertLessEqual(len(out["question"]), rag.MAX_QUESTION)

    def test_the_answer_is_capped_in_length(self):
        why = [{"text": "Near threshold is a rank from 90 up to, but not including, 95.", "section": "Alert rules"} for _ in range(9)]
        out = rag.ask(NEAR, INDEX, llm=fake({"bottom_line": "x" * 900, "why": why}))
        self.assertEqual(out["kind"], "answer")
        self.assertLessEqual(len(out["bottom_line"]), rag.MAX_BOTTOM_LINE)
        self.assertLessEqual(len(out["why"]), rag.MAX_WHY)

    def test_the_question_just_asked_is_not_offered_again(self):
        for ai in (False, True):
            out = rag.ask(NEAR, INDEX, llm=fake(self.good(related=[NEAR, "When does the agent not repeat an alert?"])), ai=ai)
            self.assertNotIn(NEAR, out["related"])
            self.assertTrue(out["related"])

    def test_the_ai_cannot_add_unlisted_text_to_the_evidence(self):
        out = rag.ask(NEAR, INDEX, llm=fake(self.good()))
        for item in out["evidence"]:
            section = next(s for s in INDEX.sections if s["title"] == item["title"])
            self.assertIn(item["quote"], section["text"])


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.quiet = mock.patch("sys.stderr", io.StringIO())      # the server writes one [ask] line per question; keep the test output clean
        cls.quiet.start()
        cls.llm = fake(Answers().good())
        cls.server = rag_server.make_server(0, ai=True, llm=cls.llm, index=INDEX)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.quiet.stop()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, data, headers or {})
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), payload

    def post(self, body, headers=None):
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return self.call("POST", "/api/ask", body, merged)

    def setUp(self):
        self.server._asked.clear()
        self.llm_calls_before = len(self.llm.calls)

    def test_it_listens_on_this_computer_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_the_page_is_served_with_a_strict_policy(self):
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Created by", body)
        self.assertIn(b"Rizwana Zahoor", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_the_page_never_puts_text_into_html(self):
        html = Path(rag_server.UI_PATH).read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("document.write", html)
        self.assertNotIn("eval(", html)
        self.assertNotIn("Densight", html)
        self.assertEqual(html.count("Rizwana Zahoor"), 3)    # header, footer, copied brief

    def test_the_sections_endpoint_lists_topics_the_settings_and_the_mode(self):
        status, _, body = self.call("GET", "/api/sections")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["topics"]), 18)
        self.assertEqual(data["reference"]["alert_rank"], agent.ALERT_RANK)
        self.assertTrue(data["ai"])
        self.assertEqual(data["model"], agent.MODEL)
        self.assertNotIn("GROQ", body.decode())

    def test_a_question_gets_a_cited_answer(self):
        status, _, body = self.post({"question": NEAR})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["kind"], "answer")
        self.assertEqual(data["guardrails"]["passed"], 4)
        self.assertEqual((data["reason"], data["reason_text"]), ("", ""))

    def test_a_refused_question_comes_back_with_its_reason(self):
        status, _, body = self.post({"question": "What is the price of BTC right now?"})
        data = json.loads(body)
        self.assertEqual((status, data["kind"], data["reason"]), (200, "declined", "live_data_request"))
        self.assertEqual(data["reason_text"], rag.REASON_TEXT["live_data_request"])
        self.assertEqual(len(self.llm.calls), self.llm_calls_before)       # the AI was not called for it

    def test_the_key_is_never_in_a_reply(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "gsk_secret_value_123"}):
            for call in (self.call("GET", "/api/sections"), self.post({"question": NEAR}), self.call("GET", "/nothing")):
                self.assertNotIn(b"gsk_secret_value_123", call[2])
                self.assertNotIn("gsk_secret_value_123", json.dumps(call[1]))

    def test_a_question_that_makes_the_code_fail_gets_a_plain_error(self):
        with mock.patch.object(rag, "ask", side_effect=RuntimeError("boom with gsk_secret inside")):
            status, _, body = self.post({"question": NEAR})
        self.assertEqual(status, 500)
        self.assertNotIn(b"boom", body)
        self.assertNotIn(b"gsk_secret", body)

    def test_a_request_for_another_host_name_is_refused(self):
        for host in ("evil.example", "127.0.0.1", "127.0.0.1:1", "localhost"):
            status, _, _ = self.call("GET", "/api/sections", headers={"Host": host})
            self.assertEqual(status, 403, host)

    def test_localhost_and_the_ip_are_both_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            self.assertEqual(self.call("GET", "/api/sections", headers={"Host": host})[0], 200, host)

    def test_a_request_from_another_page_is_refused(self):
        for origin in ("http://evil.example", "http://127.0.0.1:1", "null", "https://127.0.0.1:%d" % self.port):
            status, _, _ = self.post({"question": NEAR}, {"Origin": origin})
            self.assertEqual(status, 403, origin)
        self.assertEqual(self.post({"question": NEAR}, {"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        self.assertEqual(self.post({"question": NEAR}, {"Origin": f"http://localhost:{self.port}"})[0], 200)

    def test_only_json_is_accepted(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            status, _, _ = self.call("POST", "/api/ask", b'{"question": "x"}', {"Content-Type": content_type})
            self.assertEqual(status, 415, content_type)

    def test_a_large_body_is_refused(self):
        status, _, _ = self.post(b"x" * (rag_server.MAX_BODY + 1))
        self.assertEqual(status, 413)

    def test_a_wrong_shape_is_a_clear_400(self):
        for body in (b"not json", b"[]", b'{"question": 5}', b'{"q": "x"}', b""):
            self.assertEqual(self.post(body)[0], 400, body)

    def test_too_many_questions_in_a_minute_get_429(self):
        with mock.patch.object(rag_server, "RATE_LIMIT", 3):
            codes = [self.post({"question": NEAR})[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_unknown_paths_and_methods_are_refused(self):
        self.assertEqual(self.call("GET", "/nothing")[0], 404)
        self.assertEqual(self.call("GET", "/api/ask")[0], 404)
        self.assertEqual(self.call("POST", "/api/sections", b"{}", {"Content-Type": "application/json"})[0], 404)
        self.assertEqual(self.call("GET", "/../agent.py")[0], 404)
        self.assertEqual(self.call("GET", "/%2e%2e/agent.py")[0], 404)
        self.assertIn(self.call("PUT", "/api/ask", b"{}", {"Content-Type": "application/json"})[0], (501, 405))
        self.assertIn(self.call("DELETE", "/", None)[0], (501, 405))

    def test_the_daily_report_is_served_only_when_it_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "executive-report.html"
            with mock.patch.object(rag_server, "REPORT_PATH", path):
                self.assertEqual(self.call("GET", "/report")[0], 404)
                self.assertFalse(json.loads(self.call("GET", "/api/sections")[2])["report"])
                path.write_text("<!DOCTYPE html><title>brief</title>", encoding="utf-8")
                status, headers, body = self.call("GET", "/report")
                self.assertEqual(status, 200)
                self.assertIn(b"brief", body)
                self.assertIn("connect-src 'none'", headers["Content-Security-Policy"])
                self.assertIn("https://cdn.plot.ly", headers["Content-Security-Policy"])
                self.assertTrue(json.loads(self.call("GET", "/api/sections")[2])["report"])

    def test_the_server_never_writes_a_log_line_with_a_question(self):
        handler = rag_server.Handler.__new__(rag_server.Handler)
        with mock.patch("sys.stderr") as err:
            handler.log_message("%s", "POST /api/ask question text")
        err.write.assert_not_called()


class Startup(unittest.TestCase):
    def test_ai_follows_the_key_unless_switched_off(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            server = rag_server.make_server(0)
            self.addCleanup(server.server_close)
            self.assertFalse(server.ai)
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "x"}, clear=True):
            server = rag_server.make_server(0)
            self.addCleanup(server.server_close)
            self.assertTrue(server.ai)
            off = rag_server.make_server(0, ai=False)
            self.addCleanup(off.server_close)
            self.assertFalse(off.ai)

    def test_a_busy_port_gives_a_plain_message_and_exit_code_1(self):
        first = rag_server.make_server(0, ai=False)
        self.addCleanup(first.server_close)
        port = first.server_address[1]
        with mock.patch("sys.stderr") as err:
            self.assertEqual(rag_server.main(["--port", str(port), "--no-ai"]), 1)
        self.assertIn("Use --port", "".join(call.args[0] for call in err.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
