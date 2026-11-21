"""Fix D-10: the Sheet web app must not write twice for one run ID (a GitHub "Re-run jobs" keeps the same GITHUB_RUN_ID).

The check lives in apps_script/Code.gs, which cannot run here. tests/test_codegs_idempotency.js loads it with Node's vm module and a
small mock of the Apps Script services, so this proves the logic but NOT the real Apps Script runtime. Without Node the Node cases are skipped.

    python tests/test_fix_d10.py -v
"""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CODE_GS = (ROOT / "apps_script" / "Code.gs").read_text(encoding="utf-8")
NODE = shutil.which("node")


def run_node_cases():
    done = subprocess.run([NODE, str(ROOT / "tests" / "test_codegs_idempotency.js")], capture_output=True, text=True, timeout=120)
    try:
        return json.loads(done.stdout)["results"]
    except (ValueError, KeyError):
        raise AssertionError("the Node test printed no result. stderr: " + done.stderr[-500:])


@unittest.skipUnless(NODE, "Node is not installed, so Code.gs cannot be loaded here")
class CodeGsRunIdCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = run_node_cases()

    def test_the_node_file_ran_its_cases(self):
        names = " ".join(r["name"] for r in self.results)
        self.assertGreaterEqual(len(self.results), 20)
        for needle in ("save:", "append:", "same run ID", "no run ID", "different run ID", "lock"):
            self.assertIn(needle, names)

    def test_every_case_passes(self):
        for result in self.results:
            with self.subTest(result["name"]):
                self.assertTrue(result["ok"], result["detail"])

    def test_the_review_column_cases_ran(self):
        names = " ".join(r["name"] for r in self.results)
        for needle in ("the row has 10 cells", "without a review field", "cut at 200 characters", "old 9-column Agent tab gets the Review header",
                       "duplicate run ID is still ignored on an old 9-column tab", "old 9-column rows and new 10-column rows are read alike"):
            self.assertIn(needle, names)


def header_list(source, name):
    """The quoted names of the array that follows `name: [` or `const name = [` in Code.gs."""
    match = re.search(r"(?:const\s+%s\s*=\s*|\b%s:\s*)\[(.*?)\]" % (name, name), source, re.S)
    return re.findall(r"'([^']*)'", match.group(1))


class WhereTheRunIdLands(unittest.TestCase):
    """The web app finds the run ID by the name 'Run ID' in each tab's header list. These pin that agent.py puts it in that column."""

    def test_code_gs_has_a_run_id_column_in_every_tab(self):
        self.assertEqual(header_list(CODE_GS, "HEADERS").index("Run ID"), 2)
        for tab in ("market_data", "cross_asset", "context_data", "scenarios"):
            self.assertEqual(header_list(CODE_GS, tab).index("Run ID"), 1, tab)

    def test_the_save_payload_carries_the_run_id(self):
        reply = mock.Mock(status_code=200, json=lambda: {"success": True, "duplicate": True})
        reply.raise_for_status = lambda: None
        decision = {"alert": True, "coins": ["BTC"], "message": "m", "sources": ["a"], "reasoning": "r", "blocked_reason": ""}
        with mock.patch.dict("os.environ", {"SHEET_URL": "https://example.invalid/exec?key=k", "GITHUB_RUN_ID": "777"}), \
                mock.patch.object(agent.requests, "post", return_value=reply) as post:
            agent.deliver(decision, "Agent loop")     # a duplicate reply is a success, so the run does not fail
        self.assertEqual(post.call_args.kwargs["json"]["run_id"], "777")

    def test_a_duplicate_reply_is_accepted_by_the_python_side(self):
        reply = mock.Mock(status_code=200, json=lambda: {"success": True, "duplicate": True})
        reply.raise_for_status = lambda: None
        with mock.patch.dict("os.environ", {"SHEET_URL": "https://example.invalid/exec?key=k"}), \
                mock.patch.object(agent.requests, "post", return_value=reply):
            self.assertEqual(agent.sheet_post({"action": "append", "tab": "market_data", "rows": [["x"]]}), {"success": True, "duplicate": True})
            self.assertTrue(agent.sheet_append("market_data", [["x"]]))

    def test_the_rows_for_the_extra_tabs_carry_the_run_id_in_column_two(self):
        market = {c: scenarios.fake_market(c, 100.0, 1.0, 2.0, 3.0, -4.0, 50.0, 60.0, 70.0) for c in agent.WATCHLIST}
        evidence = [{"tool": "get_market_summary", "args": {"coin": c}, "result": r} for c, r in market.items()]
        evidence.append({"tool": "get_cross_asset_summary", "args": {}, "result": scenarios.fake_cross(corr_ranks=(2.0, 50.0, 50.0))})
        for rows in (agent.market_rows(evidence, "v", "t", "RID"), agent.cross_rows(evidence, "v", "t", "RID")):
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row[1], "RID")
        captured = []
        result = {"name": "n", "passed": True, "expected": "e", "calls": 1,
                  "decision": {"alert": False, "coins": [], "message": "m", "sources": [], "blocked_reason": ""}}
        with mock.patch.dict("os.environ", {"GITHUB_RUN_ID": "RID"}), \
                mock.patch.object(agent, "sheet_append", lambda tab, rows: captured.append((tab, rows))):
            scenarios.publish([(1, result)], "v")
        self.assertEqual(captured[0][1][0][1], "RID")


class TheReviewColumn(unittest.TestCase):
    """The optional last column of the Agent tab. The row-level behaviour is tested in the Node file; these pin the parts that Python can see."""

    def test_the_agent_tab_has_ten_headers_and_review_is_the_last(self):
        headers = header_list(CODE_GS, "HEADERS")
        self.assertEqual(len(headers), 10)
        self.assertEqual(headers[-1], "Review")
        self.assertEqual(headers[:3], ["Time (UTC)", "Version", "Run ID"])   # the columns that history and the duplicate check read did not move

    def test_the_header_comment_of_code_gs_describes_the_review_column(self):
        header = CODE_GS[:CODE_GS.index("const ")]
        self.assertIn("Review", header)
        self.assertIn("old 9 columns", header)   # it says what happens to an older tab with nine columns

    def test_the_save_payload_carries_the_short_review_text_and_stays_short(self):
        reply = mock.Mock(status_code=200, json=lambda: {"success": True, "row": 2, "emailed": False})
        reply.raise_for_status = lambda: None
        decision = {"alert": False, "coins": [], "message": "", "sources": [], "reasoning": "r", "blocked_reason": "",
                    "review_status": "code-only: no alert drafted", "review": {"failed": ["R7"], "revised": False}}
        with mock.patch.dict("os.environ", {"SHEET_URL": "https://example.invalid/exec?key=k", "GITHUB_RUN_ID": "9"}), \
                mock.patch.object(agent.requests, "post", return_value=reply) as post:
            agent.deliver(decision, "Agent loop + reviewer")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["review"], "code-only: no alert drafted | failed: R7")
        self.assertLessEqual(len(sent["review"]), agent.MAX_REVIEW_LINE_CHARS)


if __name__ == "__main__":
    unittest.main()
