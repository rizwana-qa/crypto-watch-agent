"""D-09: a daily run where no coin has valid market data must exit 1 (after it saved its rows), and the report workflow
must still run after a failed daily run.

    python tests/test_fix_d09.py -v

Offline: the model is a script of canned replies, the tools are fakes, and the Sheet calls are patched.
"""

import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402

COINS = ("BTC", "ETH", "XRP")
OUTAGE = {"error": "Kraken request failed (simulated outage)"}
FINE = {"coin": "BTC", "ranks": {}, "breaches": []}
NO_ALERT = {"alert": False, "coins": [], "message": "", "sources": [], "reasoning": "calm"}
STDERR_LINE = "ERROR: no valid market data for any coin, see the market rows"


def read_all_coins():
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{coin}", "type": "function", "function": {"name": "get_market_summary", "arguments": json.dumps({"coin": coin})}}
        for coin in COINS]}


def final_answer():
    return {"role": "assistant", "content": json.dumps(NO_ALERT)}


def evidence_item(result, tool="get_market_summary", coin="BTC"):
    return {"tool": tool, "args": {"coin": coin}, "result": result}


class HasValidMarketData(unittest.TestCase):
    def test_one_good_summary_is_enough(self):
        self.assertTrue(agent.has_valid_market_data([evidence_item(OUTAGE, coin="ETH"), evidence_item(FINE)]))

    def test_nothing_usable_is_false(self):
        self.assertFalse(agent.has_valid_market_data([]))
        self.assertFalse(agent.has_valid_market_data([evidence_item(OUTAGE), evidence_item(OUTAGE, coin="ETH")]))

    def test_other_tools_do_not_count(self):
        self.assertFalse(agent.has_valid_market_data([evidence_item({"coins": []}, tool="get_market_context")]))

    def test_a_result_that_is_not_a_dict_does_not_count(self):
        for odd in ("garbage", None, [1, 2], 0):
            self.assertFalse(agent.has_valid_market_data([evidence_item(odd)]), repr(odd))


class RunLiveExitCode(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent, "log", lambda message: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_live(self, market, dry_run=False):
        """market: a function coin -> the result the fake get_market_summary tool returns."""
        tools = {"get_market_summary": market}
        stderr = io.StringIO()
        with mock.patch.object(agent, "TOOLS", tools), \
                mock.patch.object(agent, "call_llm", side_effect=[read_all_coins(), final_answer()]), \
                mock.patch.object(agent, "deliver") as deliver, \
                mock.patch.object(agent, "publish_market_data") as publish, \
                contextlib.redirect_stderr(stderr):
            code = agent.run_live("agent", False, dry_run)
        return code, deliver, publish, stderr.getvalue()

    def test_every_coin_failing_exits_1_but_still_saves_the_rows(self):
        code, deliver, publish, stderr = self.run_live(lambda coin: dict(OUTAGE))
        self.assertEqual(code, 1)
        deliver.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(stderr.strip(), STDERR_LINE)
        evidence = publish.call_args[0][0]
        self.assertEqual(len(evidence), len(COINS))
        self.assertTrue(all("error" in item["result"] for item in evidence))

    def test_one_coin_fine_exits_0_and_says_nothing_on_stderr(self):
        code, deliver, publish, stderr = self.run_live(lambda coin: dict(FINE, coin=coin) if coin == "ETH" else dict(OUTAGE))
        self.assertEqual(code, 0)
        deliver.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(stderr, "")

    def test_all_coins_fine_exits_0(self):
        code, deliver, publish, stderr = self.run_live(lambda coin: dict(FINE, coin=coin))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

    def test_dry_run_follows_the_same_rule_and_posts_nothing(self):
        code, deliver, publish, stderr = self.run_live(lambda coin: dict(OUTAGE), dry_run=True)
        self.assertEqual(code, 1)
        self.assertEqual(stderr.strip(), STDERR_LINE)
        deliver.assert_not_called()
        publish.assert_not_called()
        code, _, _, stderr = self.run_live(lambda coin: dict(FINE, coin=coin), dry_run=True)
        self.assertEqual((code, stderr), (0, ""))

    def test_main_passes_the_exit_code_through(self):
        stderr = io.StringIO()
        with mock.patch.object(agent, "TOOLS", {"get_market_summary": lambda coin: dict(OUTAGE)}), \
                mock.patch.object(agent, "call_llm", side_effect=[read_all_coins(), final_answer()]), \
                mock.patch.object(agent, "deliver"), mock.patch.object(agent, "publish_market_data"), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(agent.main(["--mode", "agent", "--reviewer", "off"]), 1)


class ReportWorkflowCondition(unittest.TestCase):
    TEXT = (ROOT / ".github" / "workflows" / "report.yml").read_text(encoding="utf-8")

    def job_condition(self):
        match = re.search(r"^    if: (.+)$", self.TEXT, re.MULTILINE)
        self.assertIsNotNone(match, "the report job has no if condition")
        return match.group(1)

    def test_runs_after_a_successful_or_a_failed_daily_run_and_by_hand(self):
        condition = self.job_condition()
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", condition)
        self.assertIn("github.event.workflow_run.conclusion == 'failure'", condition)

    def test_cancelled_and_skipped_daily_runs_do_not_start_the_report(self):
        condition = self.job_condition()
        for conclusion in ("cancelled", "skipped"):
            self.assertNotIn(conclusion, condition)
        self.assertNotIn("always()", condition)

    def test_the_top_comment_no_longer_says_only_after_a_successful_run(self):
        self.assertNotIn("right after a successful daily run", self.TEXT)

    def test_nothing_else_in_the_workflow_moved(self):
        self.assertIn("workflows: [\"Daily crypto watch\"]", self.TEXT)
        self.assertIn("types: [completed]", self.TEXT)
        self.assertIn("contents: read", self.TEXT)
        self.assertIn("group: executive-report", self.TEXT)
        self.assertIn("cancel-in-progress: false", self.TEXT)
        self.assertEqual(len(re.findall(r"uses: \S+@[0-9a-f]{40} #", self.TEXT)), 2)


if __name__ == "__main__":
    unittest.main()
