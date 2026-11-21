"""D-03: the reviewer path has gaps. The model is a script of canned replies, so nothing here calls an AI.

    python tests/test_fix_d03.py -v

L5: a revision that adds an invented number was never checked again.
L6: the code number check was only text for the AI reviewer, so an approving AI could pass a flagged draft.
L7: an unusable reviewer reply fails open (by design) but left no marker.
L8: when the revision also fails, the first draft ships and nothing recorded that a problem stayed open.

Updated for the always-on review stage (tests/test_fix_review.py holds the new tests). What changed here: the review reply is
{"approved", "codes", "notes"}; a code finding (such as an invented number) goes to the one revision BEFORE any AI review, and the revised
alert is then reviewed by the AI; an unusable reviewer reply gives an UNCONFIRMED alert, never "approved" or "skipped"; the review is
logged as REVIEW lines and the Sheet gets one short "review" text.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import scenarios  # noqa: E402

CLEAN = dict(alert=True, coins=["BTC"], sources=["Volatility rule"],
             message="BTC volatility is at its highest rank in the market data.", reasoning="BTC volatility rank is at or above 95.")
INVENTED = dict(CLEAN, message="BTC fell 61.3% in one day, a record.", reasoning="Fell 61.3%.")
INVENTED_TWO = dict(CLEAN, message="BTC volatility is 9.87% and its rank is 99.9.", reasoning="Volatility 9.87%, rank 99.9.")
REJECT = {"approved": False, "codes": ["UNSUPPORTED_CLAIM"], "notes": ["rank not in the evidence"]}
APPROVE = {"approved": True, "codes": [], "notes": []}


def tool_calls():
    calls = [("get_market_summary", {"coin": c}) for c in ("BTC", "ETH", "XRP")] + [("get_recent_decisions", {}), ("search_playbook", {"query": "volatility alert rules"})]
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]}


def say(body):
    return {"role": "assistant", "content": body if isinstance(body, str) else json.dumps(body)}


class ReviewerCase(unittest.TestCase):
    def setUp(self):
        self.logs = []
        patcher = mock.patch.object(agent, "log", self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = mock.patch.object(agent, "utc_now", return_value=scenarios.FAKE_NOW)   # the fake data are dated the day before
        clock.start()
        self.addCleanup(clock.stop)
        self.tools = scenarios.fake_tools(scenarios.btc_spike(scenarios.calm()), [])

    def decide(self, replies, reviewer=True):
        with mock.patch.object(agent, "call_llm", side_effect=replies) as fake:
            decision, evidence = agent.decide("agent", reviewer, self.tools)
        self.assertEqual(fake.call_count, len(replies), "the code made fewer model calls than the script holds")
        return decision, evidence

    def review(self, replies, draft):
        """review_stage alone: the draft and the evidence come from a loop run, then the review gets its scripted replies."""
        with mock.patch.object(agent, "call_llm", side_effect=[tool_calls(), say(draft)]):
            first, evidence = agent.run_agent_loop(self.tools)
        run_history = agent.run_tool(self.tools, "get_recent_decisions", {}, evidence)   # decide() reads it before the review
        self.assertEqual(run_history, {"decisions": []})
        with mock.patch.object(agent, "call_llm", side_effect=replies) as fake:
            result = agent.review_stage(first, evidence)
        return result, fake.call_count

    def status_lines(self):
        return [line for line in self.logs if line.startswith("REVIEW {")]


class NumberCheckIsBinding(ReviewerCase):
    def test_the_fixtures_are_what_the_tests_assume(self):
        _, evidence = self.decide([tool_calls(), say(CLEAN)], reviewer=False)
        shown = agent.evidence_text(evidence)
        self.assertEqual(agent.unsupported_numbers(CLEAN["message"] + " " + CLEAN["reasoning"], shown), [])
        self.assertIn("61.3", agent.unsupported_numbers(INVENTED["message"], shown))
        self.assertIn("9.87", agent.unsupported_numbers(INVENTED_TWO["message"], shown))

    def test_l6_an_approving_reviewer_cannot_pass_a_draft_the_code_flagged(self):
        # The code finding goes to the revision first, so the AI review never sees the flagged draft; it reviews the revised alert.
        decision, _ = self.decide([tool_calls(), say(INVENTED_TWO), say(CLEAN), say(APPROVE)])
        self.assertNotIn("9.87", decision["message"])   # the draft was sent back for a revision, not shipped
        self.assertEqual(decision["review_status"], "revised")

    def test_l6_the_review_stage_reports_the_code_finding_as_a_problem(self):
        _, evidence = self.decide([tool_calls(), say(CLEAN)], reviewer=False)
        checks, findings = agent.code_review(INVENTED_TWO, evidence, scenarios.FAKE_NOW)
        self.assertEqual([item["check"] for item in checks if not item["passed"]], ["R1"])
        self.assertEqual([item["check"] for item in findings], ["R1"])
        self.assertIn("9.87", findings[0]["problem"])
        self.assertIn("99.9", findings[0]["problem"])
        self.assertEqual(agent.code_review(CLEAN, evidence, scenarios.FAKE_NOW)[1], [])

    def test_l6_the_code_check_still_binds_when_the_reviewer_reply_is_unusable(self):
        decision, _ = self.decide([tool_calls(), say(INVENTED_TWO), say(CLEAN), say("I think it is fine!")])
        self.assertNotIn("9.87", decision["message"])
        self.assertEqual(decision["review_status"], "unconfirmed: reviewer unavailable")

    def test_a_revision_that_still_holds_the_numbers_is_recorded_as_unresolved(self):
        decision, _ = self.decide([tool_calls(), say(INVENTED_TWO), say(INVENTED_TWO)])
        self.assertTrue(decision["review_status"].startswith("unresolved: numbers not in the evidence"))
        self.assertIn("9.87", decision["review_status"])
        self.assertFalse(decision["alert"])   # and the guardrail blocks the message numbers as before


class RevisionIsCheckedAgain(ReviewerCase):
    def test_l5_the_revision_gets_a_second_code_check_and_the_result_is_recorded(self):
        result, calls = self.review([say(REJECT), say(INVENTED)], CLEAN)
        self.assertEqual(calls, 2)                       # one review, one revision; a revision that the code flags gets no second AI review
        self.assertEqual(result["message"], INVENTED["message"])   # the loop's behaviour when it runs out of revisions is unchanged
        self.assertEqual(result["review_status"], "unresolved: numbers not in the evidence: 61.3")

    def test_l5_through_decide_the_status_reaches_the_decision(self):
        decision, _ = self.decide([tool_calls(), say(CLEAN), say(REJECT), say(INVENTED)])
        self.assertEqual(decision["review_status"], "unresolved: numbers not in the evidence: 61.3")
        self.assertTrue(any('"status":"unresolved: numbers not in the evidence: 61.3"' in line for line in self.status_lines()))

    def test_a_clean_revision_is_recorded_as_revised(self):
        # review (rejects), revision, a second review of the revised alert (approves)
        result, calls = self.review([say(REJECT), say(dict(CLEAN, message="BTC volatility is at its highest rank.")), say(APPROVE)], CLEAN)
        self.assertEqual(calls, 3)
        self.assertEqual(result["review_status"], "revised")

    def test_with_two_revisions_allowed_a_second_one_is_made_only_while_the_numbers_stay_wrong(self):
        with mock.patch.object(agent, "MAX_REVISIONS", 2):
            result, calls = self.review([say(REJECT), say(INVENTED), say(CLEAN), say(APPROVE)], CLEAN)
            self.assertEqual((calls, result["review_status"]), (4, "revised"))
            result, calls = self.review([say(REJECT), say(CLEAN), say(APPROVE)], CLEAN)       # a clean first revision is reviewed again and ends the loop
            self.assertEqual((calls, result["review_status"]), (3, "revised"))


class RevisionFails(ReviewerCase):
    def test_l8_a_failed_revision_keeps_the_first_draft_and_records_the_open_problem(self):
        result, calls = self.review([say(REJECT), say("sorry")], CLEAN)
        self.assertEqual(calls, 2)
        self.assertEqual(result["message"], CLEAN["message"])      # unchanged: the first draft is kept
        self.assertEqual(result["review_status"], "rejected: UNSUPPORTED_CLAIM")   # the AI review rejected it and nothing fixed it
        self.assertTrue(any("REVIEW revision not usable" in line for line in self.logs))

    def test_l8_a_code_finding_with_a_failed_revision_is_recorded_as_unresolved(self):
        result, calls = self.review([say("sorry")], INVENTED)
        self.assertEqual(calls, 1)
        self.assertEqual(result["message"], INVENTED["message"])
        self.assertEqual(result["review_status"], "unresolved: revision not usable")

    def test_l8_a_failed_revision_call_is_recorded_the_same_way(self):
        result, _ = self.review([say(REJECT), agent.AgentError("Groq error 500")], CLEAN)
        self.assertEqual(result["review_status"], "rejected: UNSUPPORTED_CLAIM")
        result, _ = self.review([agent.AgentError("Groq error 500")], INVENTED)
        self.assertEqual(result["review_status"], "unresolved: revision not usable")

    def test_l8_through_decide_the_rejected_alert_is_blocked(self):
        decision, _ = self.decide([tool_calls(), say(CLEAN), say(REJECT), say("sorry")])
        self.assertFalse(decision["alert"])
        self.assertEqual(decision["blocked_reason"], "reviewer: UNSUPPORTED_CLAIM")
        self.assertEqual(decision["review_status"], "rejected: UNSUPPORTED_CLAIM")

    def test_no_revision_allowed_is_recorded_too(self):
        with mock.patch.object(agent, "MAX_REVISIONS", 0):
            result, calls = self.review([say(REJECT)], CLEAN)
        self.assertEqual(calls, 1)
        self.assertEqual(result["review_status"], "rejected: UNSUPPORTED_CLAIM")


class ReviewerReplyUnusable(ReviewerCase):
    def test_l7_a_reply_that_is_not_json_is_unconfirmed_and_never_approved(self):
        decision, _ = self.decide([tool_calls(), say(CLEAN), say("I think it is fine!")])
        self.assertTrue(decision["alert"])
        self.assertEqual(decision["message"], "UNCONFIRMED: " + CLEAN["message"])   # the alert goes out, labelled by the code
        self.assertEqual(decision["review_status"], "unconfirmed: reviewer unavailable")
        self.assertTrue(any('"status":"unconfirmed: reviewer unavailable"' in line for line in self.status_lines()))
        self.assertFalse(any("approved" in line for line in self.logs))   # it was not approved, it was not reviewed

    def test_l7_a_wrong_shape_and_a_failed_call_are_marked_as_well(self):
        decision, _ = self.decide([tool_calls(), say(CLEAN), say('{"approved": "maybe"}')])
        self.assertEqual(decision["review_status"], "unconfirmed: reviewer unavailable")
        decision, _ = self.decide([tool_calls(), say(CLEAN), agent.AgentError("Groq error 500")])
        self.assertTrue(decision["alert"])
        self.assertEqual(decision["review_status"], "unconfirmed: reviewer unavailable")
        self.assertEqual(decision["review"]["ai_reason"], "reviewer call failed")


class ApprovedPathIsUnchanged(ReviewerCase):
    REVIEW_KEYS = ("review_status", "review")

    def test_an_approved_draft_ships_as_before_with_two_extra_keys(self):
        approved, _ = self.decide([tool_calls(), say(CLEAN), say(APPROVE)])
        plain, _ = self.decide([tool_calls(), say(CLEAN)], reviewer=False)
        self.assertEqual(approved["review_status"], "approved")
        self.assertEqual(plain["review_status"], "code-only: AI review off")   # not "approved": no AI looked at it
        strip = lambda decision: {k: v for k, v in decision.items() if k not in self.REVIEW_KEYS}   # noqa: E731
        self.assertEqual(strip(approved), strip(plain))
        self.assertTrue(any('"status":"approved"' in line for line in self.status_lines()))

    def test_the_call_count_of_the_approved_path_is_unchanged(self):
        # tool step + final answer + one review: nothing was added to the clean path
        self.decide([tool_calls(), say(CLEAN), say(APPROVE)])

    def test_the_reviewer_off_path_still_runs_the_code_review(self):
        decision, _ = self.decide([tool_calls(), say(CLEAN)], reviewer=False)
        # "check" and "code_breaches" are added by fix D-02 (the code's own breach check); they stay out of the Sheet payload.
        self.assertEqual(set(decision), {"alert", "coins", "message", "sources", "reasoning", "blocked_reason", "check", "code_breaches",
                                         "review_status", "review"})
        self.assertFalse(decision["review"]["ai_called"])
        self.assertEqual(len(decision["review"]["checks"]), 8)
        self.assertTrue(any(line.startswith("REVIEW code checks") for line in self.logs))


class SheetPayloadIsUnchanged(ReviewerCase):
    PAYLOAD_KEYS = {"action", "time", "version", "run_id", "alert", "coins", "message", "sources", "reasoning", "blocked_reason", "review"}

    def posted(self, decision):
        with mock.patch.object(agent, "sheet_post", return_value={"row": 2, "emailed": False}) as post:
            agent.deliver(decision, "Agent loop + reviewer")
        return post.call_args.args[0]

    def test_only_the_short_review_text_reaches_the_sheet(self):
        for replies in ([tool_calls(), say(CLEAN), say(APPROVE)], [tool_calls(), say(CLEAN), say(REJECT), say("sorry")]):
            decision, _ = self.decide(replies)
            self.assertIn("review_status", decision)
            payload = self.posted(decision)
            self.assertEqual(set(payload), self.PAYLOAD_KEYS)
            self.assertNotIn("review_status", json.dumps(payload))   # the key name, the structured record and the checks stay out
            self.assertNotIn("checks", json.dumps(payload))
            self.assertLessEqual(len(payload["review"]), 200)

    def test_the_payload_is_the_same_with_and_without_the_review_keys_except_for_the_review_text(self):
        decision, _ = self.decide([tool_calls(), say(CLEAN), say(APPROVE)])
        bare = {k: v for k, v in decision.items() if k not in ("review_status", "review")}
        with_key, without_key = self.posted(decision), self.posted(bare)
        self.assertEqual((with_key.pop("review"), without_key.pop("review")), ("approved", ""))
        for payload in (with_key, without_key):
            payload.pop("time")
        self.assertEqual(with_key, without_key)


if __name__ == "__main__":
    unittest.main()
