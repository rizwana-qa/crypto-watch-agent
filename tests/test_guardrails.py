"""Offline tests: no Kraken, no Groq, no Sheet. They prove that the guardrails and the control flow live in code.

    python tests/test_guardrails.py -v

The "AI" in the flow tests is a script of canned replies, so every run gives the same result.
"""

import json
import sys
import unittest
from datetime import datetime, timezone
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
GOOD_MARKET = [{"tool": "get_market_summary", "args": {"coin": "BTC"},
                "result": {"coin": "BTC", "volatility_30d_daily_pct": 4.8, "ranks": {"volatility": 99.3}, "breaches": ["volatility"]}}]


def draft(**changes):
    base = {"alert": True, "coins": ["BTC"], "message": "BTC 30-day daily volatility is 4.8%, rank 99.3.",
            "sources": ["Volatility rule"], "reasoning": "BTC volatility rank 99.3 is at or above 95."}
    base.update(changes)
    return base


def final_json(**changes):
    return json.dumps(draft(**changes))


def tool_call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(calls)]}


def text_message(text):
    return {"role": "assistant", "content": text}


def all_tool_calls():
    return tool_call_message(
        ("get_market_summary", {"coin": "BTC"}), ("get_market_summary", {"coin": "ETH"}),
        ("get_market_summary", {"coin": "XRP"}), ("get_recent_decisions", {}),
        ("search_playbook", {"query": "volatility alert rules"}))


class QuietCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent, "log", lambda message: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class AdviceFilter(unittest.TestCase):
    def test_advice_words_are_found(self):
        for text in ["You should buy BTC", "Time to SELL", "go long on ETH", "a good entry point", "take profit now",
                     "stop-loss at 60000", "I recommend waiting", "hold your coins", "short XRP"]:
            self.assertTrue(agent.ADVICE_RE.search(text), text)

    def test_description_is_allowed(self):
        for text in ["BTC volatility rank is 99.3, above the alert rank of 95.", "ETH is below its 30-day average by 4.4%.",
                     "A longer window, a shortfall, holdings and thresholds are plain words."]:
            self.assertIsNone(agent.ADVICE_RE.search(text), text)


class Guardrails(QuietCase):
    def run_guard(self, **changes):
        return agent.apply_guardrails(draft(**changes), TITLES, GOOD_MARKET)

    def test_valid_alert_passes(self):
        result = self.run_guard()
        self.assertTrue(result["alert"])
        self.assertEqual(result["sources"], ["Volatility rule"])

    def test_no_valid_source_no_alert(self):
        for sources in ([], ["Made up section"], ["Volatility rule", "Made up section"]):
            result = self.run_guard(sources=sources)
            self.assertFalse(result["alert"], sources)
            self.assertIn("source", result["blocked_reason"])
            self.assertEqual(result["message"], "")

    def test_source_titles_are_matched_ignoring_case_and_returned_as_in_the_playbook(self):
        self.assertEqual(self.run_guard(sources=["  volatility RULE "])["sources"], ["Volatility rule"])

    def test_advice_language_blocks_the_alert(self):
        result = self.run_guard(message="BTC fell 12.4%, a good time to buy.")
        self.assertFalse(result["alert"])
        self.assertIn("advice", result["blocked_reason"])
        self.assertEqual(result["message"], "")

    def test_alert_without_valid_market_data_is_blocked(self):
        errors = [{"tool": "get_market_summary", "args": {"coin": "BTC"}, "result": {"coin": "BTC", "error": "down"}}]
        result = agent.apply_guardrails(draft(), TITLES, errors)
        self.assertFalse(result["alert"])
        self.assertIn("market data", result["blocked_reason"])
        self.assertFalse(agent.apply_guardrails(draft(), TITLES, [])["alert"])

    def test_no_alert_keeps_its_valid_sources_and_is_not_blocked(self):
        result = self.run_guard(alert=False, coins=[], message="", sources=["Alert rules"])
        self.assertFalse(result["alert"])
        self.assertEqual(result["blocked_reason"], "")
        self.assertEqual(result["sources"], ["Alert rules"])


class RepeatGuard(QuietCase):
    """The playbook's Repeat alerts rule, enforced in code: no alert again for a coin that the last 7 decisions already alerted."""
    XRP = {"tool": "get_market_summary", "args": {"coin": "XRP"}, "result": {"coin": "XRP", "ranks": {"volatility": 99.1}, "breaches": ["volatility"]}}

    @staticmethod
    def history(*decisions, **result):
        return {"tool": "get_recent_decisions", "args": {}, "result": result or {"decisions": list(decisions)}}

    def run_guard(self, history, **changes):
        evidence = GOOD_MARKET + [RepeatGuard.XRP] + ([history] if history is not None else [])
        return agent.apply_guardrails(draft(**changes), TITLES, evidence)

    def test_a_coin_alerted_in_the_recent_decisions_is_not_alerted_again(self):
        result = self.run_guard(self.history({"alert": True, "coins": ["BTC"]}))
        self.assertFalse(result["alert"])
        self.assertIn("already alerted", result["blocked_reason"])
        self.assertIn("BTC", result["blocked_reason"])
        self.assertEqual((result["message"], result["coins"]), ("", []))

    def test_a_coin_that_was_not_alerted_may_alert(self):
        result = self.run_guard(self.history({"alert": True, "coins": ["BTC"]}), coins=["XRP"], message="XRP 30-day daily volatility rank is 99.1.")
        self.assertTrue(result["alert"])

    def test_an_alert_that_holds_one_new_coin_is_left_to_the_ai(self):
        result = self.run_guard(self.history({"alert": True, "coins": ["BTC"]}), coins=["BTC", "XRP"])
        self.assertTrue(result["alert"])

    def test_decisions_without_an_alert_do_not_count(self):
        self.assertTrue(self.run_guard(self.history({"alert": False, "coins": []}, {"alert": False, "coins": ["BTC"]}))["alert"])

    def test_no_history_or_unreadable_history_does_not_block(self):
        self.assertTrue(self.run_guard(None)["alert"])
        self.assertTrue(self.run_guard(self.history(error="history unavailable: the Sheet web app refused the request"))["alert"])
        for result in ({"decisions": None}, {"decisions": "BTC"}, {"decisions": ["BTC", 7, None]}, {"decisions": [{"alert": True, "coins": "BTC"}]},
                       {"decisions": [{"alert": "TRUE", "coins": ["BTC"]}]}):
            self.assertTrue(self.run_guard(self.history(**result))["alert"], result)

    def test_coin_names_are_compared_ignoring_case_and_spaces(self):
        self.assertFalse(self.run_guard(self.history({"alert": True, "coins": [" btc "]}))["alert"])

    def test_only_the_last_seven_decisions_count(self):
        quiet = {"alert": False, "coins": []}
        old_alert = [{"alert": True, "coins": ["BTC"]}] + [quiet] * agent.HISTORY_LIMIT
        self.assertTrue(self.run_guard(self.history(*old_alert))["alert"])
        recent_alert = [quiet] * (agent.HISTORY_LIMIT - 1) + [{"alert": True, "coins": ["BTC"]}]
        self.assertFalse(self.run_guard(self.history(*recent_alert))["alert"])

    def test_the_check_does_not_touch_a_decision_that_is_not_an_alert(self):
        result = self.run_guard(self.history({"alert": True, "coins": ["BTC"]}), alert=False, coins=[], message="", sources=["Alert rules"])
        self.assertEqual(result["blocked_reason"], "")

    def test_the_history_is_fetched_by_the_code_when_the_ai_did_not_ask_for_it(self):
        history = [{"time": "2026-11-18T05:17:02Z", "alert": True, "coins": ["BTC"], "message": "x", "sources": ["Volatility rule"]}]
        tools = scenarios.fake_tools(scenarios.btc_spike(scenarios.calm()), history)
        replies = [tool_call_message(("get_market_summary", {"coin": "BTC"}), ("get_market_summary", {"coin": "ETH"}), ("get_market_summary", {"coin": "XRP"})),
                   text_message(final_json(sources=["Volatility rule", "Alert rules"]))]
        with mock.patch.object(agent, "call_llm", side_effect=replies):
            decision, evidence = agent.decide("agent", False, tools)
        self.assertIn("get_recent_decisions", [e["tool"] for e in evidence])
        self.assertFalse(decision["alert"])
        self.assertIn("already alerted", decision["blocked_reason"])

    def test_a_first_alert_with_an_empty_history_still_goes_out(self):
        tools = scenarios.fake_tools(scenarios.btc_spike(scenarios.calm()), [])
        replies = [tool_call_message(("get_market_summary", {"coin": "BTC"}), ("get_market_summary", {"coin": "ETH"}), ("get_market_summary", {"coin": "XRP"})),
                   text_message(final_json(sources=["Volatility rule", "Alert rules"]))]
        with mock.patch.object(agent, "call_llm", side_effect=replies):
            decision, _ = agent.decide("agent", False, tools)
        self.assertTrue(decision["alert"])


class AnswerShape(unittest.TestCase):
    def test_valid_json_is_accepted_even_inside_a_code_fence(self):
        parsed = agent.parse_decision("```json\n" + final_json(coins=["btc"]) + "\n```")
        self.assertEqual(parsed["coins"], ["BTC"])

    def test_bad_answers_are_refused(self):
        bad = ["", "not json at all", "[1, 2]", json.dumps({"alert": True}),
               final_json(alert="yes"), final_json(coins="BTC"), final_json(coins=["SOL"]),
               final_json(message=""), final_json(coins=[]), final_json(message="x" * 281),
               final_json(sources="Volatility rule"), final_json(message=5)]
        for text in bad:
            with self.assertRaises(agent.ShapeError, msg=text[:60]):
                agent.parse_decision(text)

    def test_a_message_without_alert_is_dropped(self):
        parsed = agent.parse_decision(final_json(alert=False, coins=[], message="stray text"))
        self.assertEqual(parsed["message"], "")


class ToolRules(QuietCase):
    def test_allowed_calls(self):
        self.assertIsNone(agent.check_call("get_market_summary", {"coin": "btc"}))
        self.assertIsNone(agent.check_call("get_recent_decisions", {}))
        self.assertIsNone(agent.check_call("search_playbook", {"query": "alert rules"}))

    def test_refused_calls(self):
        refused = [("get_market_summary", {"coin": "SOL"}), ("get_market_summary", {}), ("get_market_summary", {"coin": 5}),
                   ("get_market_summary", {"coin": "BTC", "extra": "x"}), ("delete_everything", {}), (None, {}),
                   ("get_recent_decisions", {"limit": "5"}), ("search_playbook", {"query": "  "}),
                   ("search_playbook", {"query": "x" * 201}), ("get_market_summary", None), (["x"], {})]
        for name, args in refused:
            self.assertIsNotNone(agent.check_call(name, args), (name, args))

    def test_a_refused_call_returns_an_error_and_never_runs_the_tool(self):
        tool = mock.Mock()
        evidence = []
        result = agent.run_tool({"get_market_summary": tool}, "get_market_summary", {"coin": "SOL"}, evidence)
        self.assertIn("not on the watchlist", result["error"])
        tool.assert_not_called()
        self.assertEqual(evidence, [])

    def test_a_crashing_tool_becomes_an_error_result(self):
        def broken(coin):
            raise RuntimeError("boom")
        evidence = []
        result = agent.run_tool({"get_market_summary": broken}, "get_market_summary", {"coin": "btc"}, evidence)
        self.assertIn("error", result)
        self.assertEqual(evidence[0]["args"], {"coin": "BTC"})


class Playbook(unittest.TestCase):
    def test_has_at_least_five_sections(self):
        self.assertGreaterEqual(len(TITLES), 5)

    def test_search_finds_the_right_section(self):
        self.assertEqual(agent.tool_search_playbook("volatility spike")["sections"][0]["title"], "Volatility rule")
        self.assertEqual(agent.tool_search_playbook("drawdown peak trough")["sections"][0]["title"], "Drawdown rule")
        self.assertEqual(agent.tool_search_playbook("already alerted yesterday")["sections"][0]["title"], "Repeat alerts")
        self.assertEqual(agent.tool_search_playbook("alert wording advice")["sections"][0]["title"], "Wording rules")
        self.assertEqual(agent.tool_search_playbook("stale data")["sections"][0]["title"], "Data quality")

    def test_search_returns_at_most_two_sections_and_a_note_when_nothing_matches(self):
        self.assertLessEqual(len(agent.tool_search_playbook("alert rank volatility drawdown")["sections"]), 2)
        self.assertEqual(agent.tool_search_playbook("zzzz qqqq")["sections"], [])

    def test_every_threshold_in_the_code_appears_in_the_playbook(self):
        text = Path(agent.PLAYBOOK_PATH).read_text(encoding="utf-8")
        for value in (int(agent.ALERT_RANK), agent.WINDOW, agent.MIN_HISTORY_DAYS, agent.MAX_MESSAGE_CHARS, agent.HISTORY_LIMIT):
            self.assertIn(str(value), text, f"{value} is in the code but not in playbook.md")


class MarketMath(unittest.TestCase):
    """Hand-checked: 59 closes of 100, then 90. Last-30 returns are 29 zeros and -0.10."""

    def setUp(self):
        self.closes = [100.0] * 59 + [90.0]
        self.stamps = [86400 * (20000 + i) for i in range(60)]

    def test_numbers_match_a_hand_calculation(self):
        summary = agent.summarize("BTC", self.stamps, self.closes)
        self.assertEqual(summary["last_close"], 90.0)
        self.assertEqual(summary["daily_return_pct"], -10.0)
        self.assertEqual(summary["volatility_30d_daily_pct"], 1.83)    # stdev of [0]*29+[-0.1], divide by n-1
        self.assertEqual(summary["dist_from_30d_avg_pct"], -9.7)       # 90 / ((29*100+90)/30) - 1
        self.assertEqual(summary["max_drawdown_30d_pct"], -10.0)       # peak 100, trough 90
        self.assertEqual(summary["ranks"], {"volatility": 98.3, "distance": 98.3, "drawdown": 98.3})  # (29 + 0.5) / 30
        self.assertEqual(summary["breaches"], ["volatility", "distance", "drawdown"])
        self.assertEqual(summary["history_days"], 60)

    def test_a_flat_market_is_in_the_middle_and_never_breaches(self):
        summary = agent.summarize("ETH", self.stamps, [100.0] * 60)
        self.assertEqual(summary["ranks"], {"volatility": 50.0, "distance": 50.0, "drawdown": 50.0})
        self.assertEqual(summary["breaches"], [])
        self.assertEqual(summary["max_drawdown_30d_pct"], 0.0)

    def test_percentile_rank(self):
        self.assertEqual(agent.percentile_rank([1, 2, 3, 4], 4), 87.5)
        self.assertEqual(agent.percentile_rank([1, 2, 3, 4], 1), 12.5)

    def test_the_alert_rank_decides_the_breaches(self):
        with mock.patch.object(agent, "ALERT_RANK", 99.0):
            self.assertEqual(agent.summarize("BTC", self.stamps, self.closes)["breaches"], [])


class CompletedCandles(unittest.TestCase):
    NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    TODAY = int(NOW.replace(hour=0).timestamp())

    def candles(self, days_back, include_today=True, skip=None):
        stamps = [self.TODAY - 86400 * d for d in range(days_back, 0, -1) if d != skip]
        if include_today:
            stamps.append(self.TODAY)
        return [{"timestamp": t, "close": 100.0} for t in stamps]

    def test_the_unfinished_candle_of_today_is_dropped(self):
        stamps, closes = agent.completed_closes(self.candles(100), self.NOW)
        self.assertEqual(stamps[-1], self.TODAY - 86400)
        self.assertEqual(len(closes), 100)

    def test_bad_data_is_an_error(self):
        for candles, word in [(self.candles(50), "only 50"), (self.candles(100, skip=40), "gap"),
                              (self.candles(100)[:-5], "stale")]:
            with self.assertRaises(ValueError, msg=word) as caught:
                agent.completed_closes(candles, self.NOW)
            self.assertIn(word, str(caught.exception))

    def test_parse_candles_refuses_bad_rows(self):
        good = [1700000000, "1", "2", "0.5", "1.5", "1", "10", 3]
        self.assertEqual(agent.parse_candles([good], "XBTUSD")[0]["close"], 1.5)
        for bad in ([good[:7]], [[1700000000, "1", "2", "0.5", "nan", "1", "10", 3]], [[1700000000, "1", "2", "0.5", "-1", "1", "10", 3]],
                    [[1700000000, "1", "2", "0.5", "abc", "1", "10", 3]], [["x", "1", "2", "0.5", "1", "1", "10", 3]], ["notalist"]):
            with self.assertRaises(ValueError):
                agent.parse_candles(bad, "XBTUSD")

    def test_the_result_key_of_a_reply_must_match_the_pair(self):
        reply = mock.Mock(status_code=200, json=lambda: {"error": [], "result": {"XETHZUSD": [], "last": 1}})
        reply.raise_for_status = lambda: None
        with mock.patch.object(agent.requests, "get", return_value=reply):
            with self.assertRaises(RuntimeError) as caught:
                agent.fetch_candles("XBTUSD")
        self.assertIn("unexpected pair key", str(caught.exception))


class NumberCheck(unittest.TestCase):
    EVIDENCE = json.dumps([{"result": {"last_close": 67210.5, "ranks": {"volatility": 99.3}, "as_of": "2026-11-19", "daily_return_pct": -12.4}}])

    def test_numbers_that_the_evidence_holds_pass(self):
        self.assertEqual(agent.unsupported_numbers("BTC closed at 67,210 (-12.4%), rank 99.3 on 2026-11-19, 3 coins", self.EVIDENCE), [])

    def test_invented_numbers_are_found(self):
        self.assertEqual(agent.unsupported_numbers("BTC is at 67000 and rank 99.9", self.EVIDENCE), ["67000", "99.9"])


class FlowsWithAScriptedAI(QuietCase):
    def tools(self, markets=None, history=None):
        return scenarios.fake_tools(markets or scenarios.btc_spike(scenarios.calm()), history or [])

    def run_mode(self, replies, mode="agent", reviewer=False, tools=None):
        with mock.patch.object(agent, "call_llm", side_effect=replies) as fake:
            decision, evidence = agent.decide(mode, reviewer, tools or self.tools())
        return decision, evidence, fake.call_count

    # ---- agent loop
    def test_loop_runs_the_tools_the_ai_asks_for_and_returns_the_alert(self):
        decision, evidence, calls = self.run_mode([all_tool_calls(), text_message(final_json(sources=["Volatility rule", "Alert rules"]))])
        self.assertTrue(decision["alert"])
        self.assertEqual([e["tool"] for e in evidence].count("get_market_summary"), 3)
        self.assertEqual(calls, 2)

    def test_loop_refuses_a_coin_that_is_not_on_the_watchlist(self):
        replies = [tool_call_message(("get_market_summary", {"coin": "SOL"})),
                   text_message(final_json(alert=False, coins=[], message="", sources=[], reasoning="No coin could be read, so there is no alert."))]
        decision, evidence, _ = self.run_mode(replies, tools=self.tools(scenarios.calm()))
        self.assertNotIn("SOL", [e["args"].get("coin") for e in evidence])   # the blocked call left nothing in the evidence
        self.assertEqual([e["args"]["coin"] for e in evidence if e["tool"] == "get_market_summary"], list(agent.WATCHLIST))   # the code added the real coins
        self.assertFalse(decision["alert"])

    def test_loop_stops_with_an_error_at_the_step_limit(self):
        with mock.patch.object(agent, "call_llm", return_value=tool_call_message(("get_recent_decisions", {}))):
            with self.assertRaises(agent.AgentError) as caught:
                agent.decide("agent", False, self.tools())
        self.assertIn("did not finish in time", str(caught.exception))

    def test_loop_asks_again_after_an_invalid_answer_but_only_twice(self):
        decision, _, calls = self.run_mode([all_tool_calls(), text_message("oops"), text_message("{}"), text_message(final_json())])
        self.assertTrue(decision["alert"])
        self.assertEqual(calls, 4)  # tools, two invalid answers, a valid one
        with mock.patch.object(agent, "call_llm", return_value=text_message("oops")):
            with self.assertRaises(agent.AgentError):
                agent.decide("agent", False, self.tools())

    def test_an_ai_that_invents_a_source_cannot_get_an_alert_through(self):
        decision, _, _ = self.run_mode([all_tool_calls(), text_message(final_json(sources=["Totally real section"]))])
        self.assertFalse(decision["alert"])
        self.assertIn("source", decision["blocked_reason"])

    def test_an_ai_that_gives_advice_cannot_get_an_alert_through(self):
        # The advice is a code finding (R2), so the agent gets its one revision first. A model that repeats the advice is still blocked.
        advice = text_message(final_json(message="BTC dropped 12.4%, buy the dip."))
        decision, _, calls = self.run_mode([all_tool_calls(), advice, advice])
        self.assertFalse(decision["alert"])
        self.assertIn("advice", decision["blocked_reason"])
        self.assertEqual(calls, 3)  # loop 2 + the one revision, no review call

    # ---- planner
    def plan(self, **changes):
        steps = [{"tool": "get_market_summary", "args": {"coin": c}, "reason": f"data for {c}"} for c in ("BTC", "ETH", "XRP")]
        steps += [{"tool": "get_recent_decisions", "args": {}, "reason": "memory"},
                  {"tool": "search_playbook", "args": {"query": "volatility alert rules"}, "reason": "rules"}]
        return json.dumps({"steps": changes.get("steps", steps)})

    def test_planner_code_runs_every_step_then_the_ai_decides(self):
        decision, evidence, calls = self.run_mode([text_message(self.plan()), text_message(final_json(sources=["Alert rules"]))], mode="planner")
        self.assertTrue(decision["alert"])
        self.assertEqual(len(evidence), 8)   # the 5 planned steps and the 3 data tools that the plan left out, added by the code
        self.assertEqual([e["tool"] for e in evidence[5:]], ["get_cross_asset_summary", "get_market_context", "get_alert_history_stats"])
        self.assertEqual(calls, 2)  # one plan, one decision: the AI never ran a tool

    def test_planner_gets_one_retry_for_a_bad_plan(self):
        bad = self.plan(steps=[{"tool": "get_market_summary", "args": {"coin": "SOL"}, "reason": "x"}])
        decision, _, calls = self.run_mode([text_message(bad), text_message(self.plan()), text_message(final_json())], mode="planner")
        self.assertTrue(decision["alert"])
        self.assertEqual(calls, 3)

    def test_planner_stops_after_a_second_bad_plan(self):
        with mock.patch.object(agent, "call_llm", return_value=text_message("not a plan")):
            with self.assertRaises(agent.AgentError) as caught:
                agent.decide("planner", False, self.tools())
        self.assertIn("not valid twice", str(caught.exception))

    def test_every_kind_of_bad_plan_is_refused(self):
        step = {"tool": "get_recent_decisions", "args": {}, "reason": "memory"}
        bad_plans = [{}, {"steps": []}, {"steps": "x"}, {"steps": [step] * (agent.MAX_PLAN_STEPS + 1)},
                     {"steps": [{"tool": "get_recent_decisions", "args": {}}]},
                     {"steps": [dict(step, reason=" ")]}, {"steps": [dict(step, tool="rm")]},
                     {"steps": [{"tool": "get_market_summary", "args": {"coin": "SOL"}, "reason": "x"}]},
                     {"steps": [{"tool": "get_market_summary", "args": "BTC", "reason": "x"}]},
                     {"steps": [dict(step, extra=1)]}]
        for plan in bad_plans:
            with self.assertRaises(agent.ShapeError, msg=str(plan)[:60]):
                agent.validate_plan(plan)
        self.assertEqual(len(agent.validate_plan({"steps": [step]})), 1)

    # ---- reviewer
    def test_reviewer_off_makes_no_review_call(self):
        _, _, calls = self.run_mode([all_tool_calls(), text_message(final_json())], reviewer=False)
        self.assertEqual(calls, 2)

    def test_reviewer_problem_leads_to_exactly_one_revision(self):
        replies = [all_tool_calls(), text_message(final_json()),
                   text_message(json.dumps({"approved": False, "codes": ["UNSUPPORTED_CLAIM"], "notes": ["rank 99.3 is not shown"]})),
                   text_message(final_json(alert=False, coins=[], message="", sources=["Alert rules"], reasoning="BTC rank 99.3 is at or above 95 but not shown."))]
        decision, _, calls = self.run_mode(replies, reviewer=True)
        self.assertFalse(decision["alert"])
        self.assertEqual(calls, 4)  # loop 2 + review 1 + one revision (a calm revision needs no second review)

    def test_reviewer_approval_keeps_the_draft(self):
        replies = [all_tool_calls(), text_message(final_json(sources=["Volatility rule"])), text_message(json.dumps({"approved": True, "problems": []}))]
        decision, _, calls = self.run_mode(replies, reviewer=True)
        self.assertTrue(decision["alert"])
        self.assertEqual(calls, 3)

    def test_a_broken_reviewer_reply_never_blocks_the_job(self):
        # The alert still goes out, but labelled UNCONFIRMED: nobody confirmed it.
        for reply in (text_message("I refuse to answer"), text_message(""), text_message('{"approved": "maybe"}')):
            decision, _, _ = self.run_mode([all_tool_calls(), text_message(final_json()), reply], reviewer=True)
            self.assertTrue(decision["alert"])
            self.assertTrue(decision["message"].startswith("UNCONFIRMED: "))

    def test_a_failed_reviewer_call_never_blocks_the_job(self):
        replies = [all_tool_calls(), text_message(final_json()), agent.AgentError("Groq error 500")]
        decision, _, _ = self.run_mode(replies, reviewer=True)
        self.assertTrue(decision["alert"])
        self.assertTrue(decision["message"].startswith("UNCONFIRMED: "))

    def test_code_guardrails_still_apply_after_a_reviewer_that_approves_everything(self):
        approve = text_message(json.dumps({"approved": True, "problems": []}))
        decision, _, _ = self.run_mode([all_tool_calls(), text_message(final_json(sources=["Invented"])), approve], reviewer=True)
        self.assertFalse(decision["alert"])

    def test_a_bad_revision_keeps_the_first_draft(self):
        # A reviewer that rejects the first draft and a revision that is not usable: the first draft is kept, and it is blocked.
        verdict = text_message(json.dumps({"approved": False, "codes": ["OVERSTATED"], "notes": ["x"]}))
        decision, _, _ = self.run_mode([all_tool_calls(), text_message(final_json()), verdict, text_message("broken")], reviewer=True)
        self.assertFalse(decision["alert"])
        self.assertEqual(decision["blocked_reason"], "reviewer: OVERSTATED")

    def test_planner_and_reviewer_combine(self):
        replies = [text_message(self.plan()), text_message(final_json()), text_message(json.dumps({"approved": True, "problems": []}))]
        decision, evidence, calls = self.run_mode(replies, mode="planner", reviewer=True)
        self.assertTrue(decision["alert"])
        self.assertEqual(calls, 3)


class Delivery(QuietCase):
    def test_the_key_never_appears_in_an_error(self):
        url = "https://script.google.com/macros/s/ABC/exec?key=not-a-real-key"
        error = agent.requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
        with mock.patch.dict("os.environ", {"SHEET_URL": url}), mock.patch.object(agent.requests, "post", side_effect=error):
            with self.assertRaises(agent.AgentError) as caught:
                agent.sheet_post({"action": "history"})
        self.assertNotIn("not-a-real-key", str(caught.exception))

    def test_a_refusal_by_the_web_app_is_an_error(self):
        reply = mock.Mock(status_code=200, json=lambda: {"success": False, "error": "UNAUTHORIZED"})
        reply.raise_for_status = lambda: None
        with mock.patch.dict("os.environ", {"SHEET_URL": "https://example.invalid/exec?key=k"}), mock.patch.object(agent.requests, "post", return_value=reply):
            with self.assertRaises(agent.AgentError) as caught:
                agent.sheet_post({"action": "history"})
        self.assertIn("UNAUTHORIZED", str(caught.exception))

    def test_an_unauthorized_hint_says_what_is_wrong_without_the_key(self):
        reply = mock.Mock(status_code=200, json=lambda: {"success": False, "error": "UNAUTHORIZED"})
        reply.raise_for_status = lambda: None
        cases = [
            ("https://example.invalid/exec", "no ?key="),
            ("https://example.invalid/exec?key=short", "5 characters"),
            ("https://example.invalid/exec?key=abcdefghijklmnopqrst%0A", "line break"),
        ]
        for url, expected in cases:
            with mock.patch.dict("os.environ", {"SHEET_URL": url}), mock.patch.object(agent.requests, "post", return_value=reply):
                with self.assertRaises(agent.AgentError) as caught:
                    agent.sheet_post({"action": "history"})
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("abcdefghijklmnopqrst", str(caught.exception))

    def test_a_trailing_newline_in_the_secret_is_removed(self):
        reply = mock.Mock(status_code=200, json=lambda: {"success": True})
        reply.raise_for_status = lambda: None
        with mock.patch.dict("os.environ", {"SHEET_URL": "https://example.invalid/exec?key=abcdefghijklmnop\n"}), \
                mock.patch.object(agent.requests, "post", return_value=reply) as post:
            agent.sheet_post({"action": "history"})
        self.assertEqual(post.call_args.args[0], "https://example.invalid/exec?key=abcdefghijklmnop")

    def test_the_posted_row_holds_the_decision(self):
        reply = mock.Mock(status_code=200, json=lambda: {"success": True, "row": 2, "emailed": True})
        reply.raise_for_status = lambda: None
        decision = dict(draft(), blocked_reason="")
        with mock.patch.dict("os.environ", {"SHEET_URL": "https://example.invalid/exec?key=k", "GITHUB_RUN_ID": "42"}), \
                mock.patch.object(agent.requests, "post", return_value=reply) as post:
            agent.deliver(decision, "Agent loop")
        sent = post.call_args.kwargs["json"]
        self.assertEqual((sent["action"], sent["alert"], sent["run_id"], sent["version"]), ("save", True, "42", "Agent loop"))

    def test_memory_failure_is_an_error_result_not_a_guess(self):
        with mock.patch.dict("os.environ", {"SHEET_URL": ""}):
            self.assertIn("error", agent.tool_recent_decisions())


if __name__ == "__main__":
    unittest.main()
