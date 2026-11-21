"""D-02: the agent now compares a 'no alert' answer with the ranks it gathered, and says so when they disagree.

    python tests/test_fix_d02.py -v

The model is a script of canned replies (as in the other flow tests), so nothing here calls an AI, Kraken or the Sheet.
The check only logs and adds two keys to the returned decision. It never changes the alert, the email or the Sheet row.
"""

import json
import sys
import unittest
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


def tool_call_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(calls)]}


def text_message(text):
    return {"role": "assistant", "content": text}


def final_json(**changes):
    body = {"alert": False, "coins": [], "message": "", "sources": [], "reasoning": "calm day"}
    body.update(changes)
    return json.dumps(body)


READ_MARKETS = tool_call_message(("get_market_summary", {"coin": "BTC"}), ("get_market_summary", {"coin": "ETH"}),
                                 ("get_market_summary", {"coin": "XRP"}))
READ_MARKETS_AND_HISTORY = tool_call_message(("get_market_summary", {"coin": "BTC"}), ("get_market_summary", {"coin": "ETH"}),
                                             ("get_market_summary", {"coin": "XRP"}), ("get_recent_decisions", {}))
ALERT_BTC = dict(alert=True, coins=["BTC"], sources=["Alert rules"], message="BTC volatility rank is 99.3, above the alert rank.",
                 reasoning="BTC rank 99.3 is at or above 95.")


def warnings(logs):
    return [line for line in logs if line.startswith("::warning::")]


class D02Case(unittest.TestCase):
    def go(self, replies, markets=None, history=None, mode="agent", tools=None):
        """Runs decide() with a scripted model. Returns (decision, evidence, log lines)."""
        logs = []
        tools = tools or scenarios.fake_tools(markets if markets is not None else scenarios.calm(), history or [])
        with mock.patch.object(agent, "log", logs.append), mock.patch.object(agent, "call_llm", side_effect=replies):
            decision, evidence = agent.decide(mode, False, tools)
        return decision, evidence, logs


class MissedBreachIsFlagged(D02Case):
    def test_l2_no_tool_calls_and_no_alert_is_flagged_as_unverified(self):
        # The AI makes no tool call. The code now completes the evidence itself, so "no market data" means that the data tools all failed.
        outage = {coin: scenarios.outage(coin) for coin in agent.WATCHLIST}
        decision, evidence, logs = self.go([text_message(final_json())], markets=outage)
        self.assertEqual([e["tool"] for e in evidence].count("get_market_summary"), 3)
        self.assertTrue(all("error" in e["result"] for e in evidence if e["tool"] == "get_market_summary"))
        self.assertEqual(len(warnings(logs)), 1)
        self.assertIn("no valid market data", warnings(logs)[0])
        self.assertTrue(decision["check"].startswith("unverified"), decision["check"])
        self.assertEqual(decision["code_breaches"], [])
        # the check never changes the decision itself
        self.assertFalse(decision["alert"])
        self.assertEqual(decision["blocked_reason"], "")

    def test_l3_btc_at_the_alert_rank_but_the_ai_says_no_alert_is_flagged(self):
        decision, _, logs = self.go([READ_MARKETS, text_message(final_json()), text_message(final_json())], markets=scenarios.btc_spike(scenarios.calm()))
        self.assertEqual(len(warnings(logs)), 1)
        self.assertIn("BTC", warnings(logs)[0])
        self.assertEqual(decision["code_breaches"], ["BTC"])
        self.assertEqual(decision["check"], "mismatch: BTC at the alert rank, AI said no alert")
        self.assertFalse(decision["alert"])
        self.assertEqual(decision["blocked_reason"], "")

    def test_several_coins_are_all_named(self):
        markets = scenarios.btc_spike(scenarios.calm())
        markets["ETH"] = scenarios.fake_market("ETH", 2800.0, -9.0, -8.0, 4.0, -12.0, 97.0, 96.0, 98.0)
        decision, _, logs = self.go([READ_MARKETS, text_message(final_json()), text_message(final_json())], markets=markets)
        self.assertEqual(decision["code_breaches"], ["BTC", "ETH"])
        self.assertEqual(decision["check"], "mismatch: BTC, ETH at the alert rank, AI said no alert")
        self.assertIn("BTC, ETH", warnings(logs)[0])

    def test_planner_mode_is_checked_too(self):
        # The plan holds no data step. The code completes the evidence, finds BTC at the alert rank, the one revision does not change the answer.
        plan = json.dumps({"steps": [{"tool": "search_playbook", "args": {"query": "alert rules"}, "reason": "rules"}]})
        decision, _, logs = self.go([text_message(plan), text_message(final_json()), text_message(final_json())],
                                    markets=scenarios.btc_spike(scenarios.calm()), mode="planner")
        self.assertEqual(len(warnings(logs)), 1)
        self.assertTrue(decision["check"].startswith("mismatch"), decision["check"])
        self.assertEqual(decision["code_breaches"], ["BTC"])

    def test_all_market_tools_failing_counts_as_no_market_data(self):
        markets = {coin: scenarios.outage(coin) for coin in agent.WATCHLIST}
        decision, _, logs = self.go([READ_MARKETS, text_message(final_json())], markets=markets)
        self.assertEqual(len(warnings(logs)), 1)
        self.assertTrue(decision["check"].startswith("unverified"), decision["check"])
        self.assertEqual(decision["code_breaches"], [])

    def test_a_tool_that_returns_something_odd_does_not_crash_the_check(self):
        tools = scenarios.fake_tools(scenarios.calm(), [])
        tools["get_market_summary"] = lambda coin: None
        decision, _, logs = self.go([READ_MARKETS, text_message(final_json())], tools=tools)
        self.assertEqual(len(warnings(logs)), 1)
        self.assertTrue(decision["check"].startswith("unverified"), decision["check"])


class AgreeingRunsStaySilent(D02Case):
    def test_calm_day_with_calm_ranks_agrees(self):
        decision, _, logs = self.go([READ_MARKETS, text_message(final_json())])
        self.assertEqual(warnings(logs), [])
        self.assertEqual((decision["code_breaches"], decision["check"]), ([], "agrees"))

    def test_alert_for_a_coin_at_the_alert_rank_agrees(self):
        decision, _, logs = self.go([READ_MARKETS, text_message(final_json(**ALERT_BTC))], markets=scenarios.btc_spike(scenarios.calm()))
        self.assertEqual(warnings(logs), [])
        self.assertTrue(decision["alert"])
        self.assertEqual((decision["code_breaches"], decision["check"]), (["BTC"], "agrees"))

    def test_no_alert_after_an_alert_for_the_same_coin_is_not_a_miss(self):
        # the repeat rule: BTC alerted yesterday, so a calm answer today is the right one
        history = [scenarios.YESTERDAY_ALERT]
        decision, _, logs = self.go([READ_MARKETS_AND_HISTORY, text_message(final_json())],
                                    markets=scenarios.btc_spike(scenarios.calm()), history=history)
        self.assertEqual(warnings(logs), [])
        self.assertEqual(decision["code_breaches"], ["BTC"])
        self.assertEqual(decision["check"], "agrees")

    def test_a_new_coin_at_the_alert_rank_is_still_a_miss_when_another_coin_was_alerted(self):
        markets = scenarios.btc_spike(scenarios.calm())
        markets["XRP"] = scenarios.fake_market("XRP", 1.9, -9.0, -8.0, 4.0, -12.0, 97.0, 96.0, 98.0)
        decision, _, logs = self.go([READ_MARKETS_AND_HISTORY, text_message(final_json()), text_message(final_json())], markets=markets, history=[scenarios.YESTERDAY_ALERT])
        self.assertEqual(decision["code_breaches"], ["BTC", "XRP"])
        self.assertEqual(decision["check"], "mismatch: XRP at the alert rank, AI said no alert")
        self.assertEqual(len(warnings(logs)), 1)


class AlertedCoinWithoutARank(D02Case):
    def test_an_alert_for_a_coin_not_at_the_alert_rank_is_noted_but_the_guardrail_stays_the_only_blocker(self):
        decision, _, logs = self.go([READ_MARKETS, text_message(final_json(**dict(ALERT_BTC, coins=["BTC", "ETH"])))],
                                    markets=scenarios.btc_spike(scenarios.calm()))
        self.assertFalse(decision["alert"])
        self.assertIn("ETH", decision["blocked_reason"])
        self.assertEqual(decision["check"], "mismatch: ETH alerted but not at the alert rank")
        self.assertEqual(warnings(logs), [])


class NothingElseChanges(D02Case):
    def test_the_sheet_payload_has_the_same_fields_with_or_without_the_new_keys(self):
        decision, _, _ = self.go([READ_MARKETS, text_message(final_json()), text_message(final_json())], markets=scenarios.btc_spike(scenarios.calm()))
        self.assertIn("check", decision)
        bare = {k: v for k, v in decision.items() if k not in ("check", "code_breaches")}
        sent = []
        reply = mock.Mock(status_code=200, json=lambda: {"success": True, "row": 2, "emailed": False})
        reply.raise_for_status = lambda: None
        with mock.patch.dict("os.environ", {"SHEET_URL": "https://example.invalid/exec?key=k", "GITHUB_RUN_ID": "7"}), \
                mock.patch.object(agent, "log", lambda message: None), \
                mock.patch.object(agent, "datetime") as clock, \
                mock.patch.object(agent.requests, "post", return_value=reply) as post:
            clock.now.return_value.strftime.return_value = "2026-11-21T05:00:00Z"
            for item in (decision, bare):
                agent.deliver(item, "Agent loop")
                sent.append(post.call_args.kwargs["json"])
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(set(sent[0]), {"action", "time", "version", "run_id", "alert", "coins", "message", "sources", "reasoning", "blocked_reason", "review"})

    def test_the_decision_is_still_json_serialisable_for_the_log_line(self):
        decision, _, _ = self.go([READ_MARKETS, text_message(final_json()), text_message(final_json())], markets=scenarios.btc_spike(scenarios.calm()))
        self.assertEqual(json.loads(json.dumps(decision))["code_breaches"], ["BTC"])

    def test_the_warning_text_has_no_dash_or_advice_wording(self):
        _, _, logs = self.go([READ_MARKETS, text_message(final_json()), text_message(final_json())], markets=scenarios.btc_spike(scenarios.calm()))
        _, _, logs_empty = self.go([text_message(final_json())], markets={coin: scenarios.outage(coin) for coin in agent.WATCHLIST})
        self.assertEqual((len(warnings(logs)), len(warnings(logs_empty))), (1, 1))
        for line in warnings(logs) + warnings(logs_empty):
            self.assertNotIn("—", line)
            self.assertNotIn("–", line)
            self.assertIsNone(agent.ADVICE_RE.search(line), line)


if __name__ == "__main__":
    unittest.main()
