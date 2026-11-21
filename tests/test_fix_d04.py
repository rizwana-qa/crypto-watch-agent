"""D-04: report.py --email must not exit 0 when the email was not sent.

    python tests/test_fix_d04.py -v

Offline: no Kraken, no Groq, no Sheet. The data functions and sheet_post are patched.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import report  # noqa: E402

REFUSED = agent.AgentError("The Sheet web app refused the request: UNAUTHORIZED")


class MainExitCode(unittest.TestCase):
    def run_main(self, *args, coins=None, post=None, sheet_url="set"):
        """Run report.main with fixed data. Returns (exit code, stdout, stderr, the sheet_post mock, whether the page file exists)."""
        coins = coins if coins is not None else report.demo_coins()
        env = {k: v for k, v in os.environ.items() if k != "SHEET_URL"}
        if sheet_url:
            env["SHEET_URL"] = "https://script.google.com/macros/s/test/exec"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(report, "gather", return_value=(coins, report.demo_cross())), \
                mock.patch.object(report, "gather_context", return_value=report.demo_context(report.demo_coins())), \
                mock.patch.object(report, "fetch_decisions", return_value=report.demo_decisions()), \
                mock.patch.object(agent, "sheet_post", **(post or {"return_value": {"success": True}})) as sheet_post, \
                mock.patch.dict(os.environ, env, clear=True), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            path = Path(tmp) / "r.html"
            code = report.main(["--out", str(path), *args])
            written = path.exists()
        return code, out.getvalue(), err.getvalue(), sheet_post, written

    def test_a_refused_email_exits_1_and_says_so_on_stderr(self):
        code, out, err, post, written = self.run_main("--email", post={"side_effect": REFUSED})
        self.assertEqual(code, 1)
        self.assertEqual(post.call_count, 1)
        self.assertIn("ERROR:", err)
        self.assertIn("email", err.lower())
        self.assertTrue(written, "the page is still written when the send fails")

    def test_an_accepted_email_exits_0_with_nothing_on_stderr(self):
        code, out, err, post, written = self.run_main("--email")
        self.assertEqual(code, 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(err, "")
        self.assertIn("email sent", out)
        self.assertTrue(written)

    def test_email_asked_but_no_sheet_url_is_a_failure(self):
        # The real sheet_post runs here and raises "SHEET_URL is not set".
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(report, "gather", return_value=(report.demo_coins(), report.demo_cross())), \
                mock.patch.object(report, "gather_context", return_value=None), \
                mock.patch.object(report, "fetch_decisions", return_value=([], "")), \
                mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "SHEET_URL"}, clear=True), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = report.main(["--out", str(Path(tmp) / "r.html"), "--email"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", err.getvalue())

    def test_a_local_run_without_email_still_exits_0_even_with_no_sheet_url(self):
        code, out, err, post, written = self.run_main(post={"side_effect": REFUSED}, sheet_url="")
        self.assertEqual(code, 0)
        self.assertEqual(post.call_count, 0)
        self.assertEqual(err, "")
        self.assertTrue(written)

    def test_demo_with_email_sends_nothing_and_exits_0(self):
        code, out, err, post, written = self.run_main("--demo", "--email", post={"side_effect": REFUSED})
        self.assertEqual(code, 0)
        self.assertEqual(post.call_count, 0)
        self.assertEqual(err, "")

    def test_bad_data_still_exits_1_with_or_without_a_working_email(self):
        broken = [{"coin": c, "error": "Kraken request failed"} for c in agent.WATCHLIST]
        code, _, err, _, _ = self.run_main(coins=broken)
        self.assertEqual(code, 1)
        code, _, err, post, _ = self.run_main("--email", coins=broken)
        self.assertEqual(code, 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(err, "", "a sent email is not an error even when the data is bad")

    def test_bad_data_and_a_refused_email_exits_1_and_reports_the_email(self):
        broken = [{"coin": c, "error": "Kraken request failed"} for c in agent.WATCHLIST]
        code, _, err, _, _ = self.run_main("--email", coins=broken, post={"side_effect": REFUSED})
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", err)


if __name__ == "__main__":
    unittest.main()
