"""A scenario that did not finish is shown as DID NOT FINISH with a reason, not as a FAIL of the agent (found in the Sheet check of 2026-11-21)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agent
import scenarios


def result(passed, finished=True, why="ok", expected="an alert for BTC"):
    return {"name": "x", "passed": passed, "finished": finished, "why": why, "decision": None if not finished else {"alert": True, "coins": ["BTC"],
            "message": "m", "sources": ["Alert rules"], "blocked_reason": ""}, "expected": "" if not finished else expected, "calls": 4, "tokens": 0}


class ScenarioRows(unittest.TestCase):
    def rows(self, results):
        sent = []
        with mock.patch.object(agent, "sheet_append", lambda tab, rows: sent.append((tab, rows))):
            scenarios.publish(list(enumerate(results, 1)), "Agent loop")
        self.assertEqual(sent[0][0], "scenarios")
        return sent[0][1]

    def test_pass_fail_and_did_not_finish_are_three_different_results(self):
        rows = self.rows([result(True), result(False, expected="no alert"), result(False, finished=False, why="did not finish: HTTP 429 ...")])
        self.assertEqual([row[5] for row in rows], ["PASS", "FAIL", "DID NOT FINISH"])
        self.assertEqual(rows[1][6], "no alert")

    def test_the_unfinished_row_says_why_without_copying_the_provider_text(self):
        limit = self.rows([result(False, finished=False, why="did not finish: Groq error 429 organization org_abc tokens per day")])[0]
        self.assertEqual(limit[6], "no decision: the AI call was refused with HTTP 429 (a rate limit); see the run log")
        self.assertNotIn("org_abc", " ".join(str(cell) for cell in limit))
        other = self.rows([result(False, finished=False, why="did not finish: timeout")])[0]
        self.assertEqual(other[6], "no decision: the AI call failed; see the run log")

    def test_a_result_without_the_finished_key_is_a_normal_result(self):
        old = {"name": "x", "passed": False, "decision": None, "expected": "e", "calls": 1}
        self.assertEqual(scenarios.result_label(old), "FAIL")
        self.assertEqual(scenarios.unfinished_reason(old), "e")


if __name__ == "__main__":
    unittest.main()
