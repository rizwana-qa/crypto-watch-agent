"""Offline tests for fix D-20: a coin without data must show in the status badge, the hero and the email subject, not only in the scoreboard.

    python tests/test_fix_d20.py -v

States: PARTIAL (a coin has an error, no working coin is at the alert rank), ALERT (a working coin is at the alert rank, the
unavailable coin is named), NO DATA (every coin failed), and the unchanged CALM and ALERT states when every coin has data.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import emailbody  # noqa: E402
import insights  # noqa: E402
import report  # noqa: E402

NOW_DAY = 1_800_000_000 // agent.SECONDS_PER_DAY * agent.SECONDS_PER_DAY
STAMPS = [NOW_DAY - (300 - i) * agent.SECONDS_PER_DAY for i in range(300)]
CALM_CLOSES = [100.0 + (i % 7) * 0.5 for i in range(300)]
CRASH_CLOSES = [100.0 + (i % 7) * 0.5 for i in range(299)] + [60.0]
STALE = "stale data: the latest completed candle is 2026-11-17"
FAILED = "request to Kraken failed after 3 attempts"


def calm(coin):
    return report.analyse(coin, STAMPS, CALM_CLOSES)


def crashed(coin):
    return report.analyse(coin, STAMPS, CRASH_CLOSES)


def make_data(coins):
    return {"coins": coins, "cross": report.demo_cross(), "context": None, "decisions": [], "decisions_note": "", "demo": True,
            "generated": "2026-11-21 08:30", "generated_long": "Saturday 21 November 2026", "events": insights.event_study(coins)}


def btc_stale_others_calm():
    return make_data([{"coin": "BTC", "error": STALE}, calm("ETH"), calm("XRP")])


def btc_stale_eth_alert():
    return make_data([{"coin": "BTC", "error": STALE}, crashed("ETH"), calm("XRP")])


def all_failed():
    return make_data([{"coin": c, "error": FAILED} for c in agent.WATCHLIST])


def all_calm():
    return make_data([calm(c) for c in agent.WATCHLIST])


def hero_html(data):
    return report.hero(data, "2026-11-20")


def run_main(data):
    """report.main on the given data, with every network step stubbed. Returns (exit code, printed text)."""
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(report, "gather", return_value=(data["coins"], data["cross"])), \
            mock.patch.object(report, "gather_context", return_value=None), \
            mock.patch.object(report, "fetch_decisions", return_value=([], "")), \
            redirect_stdout(out):
        code = report.main(["--out", str(Path(tmp) / "r.html")])
    return code, out.getvalue()


class PartialData(unittest.TestCase):
    """BTC has no valid data and ETH and XRP are calm."""

    def test_the_status_is_partial_and_names_the_coin(self):
        label, tone, detail = report.status_of(btc_stale_others_calm())
        self.assertEqual((label, tone), ("PARTIAL", "amber"))
        self.assertEqual(detail, "Partial data: BTC unavailable. No other coin at the alert rank")

    def test_two_unavailable_coins_are_both_named(self):
        data = make_data([{"coin": "BTC", "error": STALE}, calm("ETH"), {"coin": "XRP", "error": FAILED}])
        label, _, detail = report.status_of(data)
        self.assertEqual(label, "PARTIAL")
        self.assertEqual(detail, "Partial data: BTC and XRP unavailable. No other coin at the alert rank")
        self.assertEqual(insights.subject_line(data["coins"]), "Crypto Watch: partial data, BTC and XRP unavailable, no other coin at its alert rank")

    def test_the_subject_says_partial_and_has_no_advice_word(self):
        subject = insights.subject_line(btc_stale_others_calm()["coins"])
        self.assertEqual(subject, "Crypto Watch: partial data, BTC unavailable, no other coin at its alert rank")
        self.assertNotIn("calm", subject.lower())
        self.assertTrue(insights.wording_ok(subject))
        self.assertLessEqual(len(subject), 110)

    def test_the_subject_falls_back_if_a_banned_word_slips_in(self):
        coins = [{"coin": "go long", "error": STALE}, calm("ETH")]
        subject = insights.subject_line(coins)
        self.assertEqual(subject, "Crypto Watch: partial data, some coins unavailable")
        self.assertTrue(insights.wording_ok(subject))

    def test_the_badge_the_brand_bar_and_the_heading_say_partial(self):
        data = btc_stale_others_calm()
        hero = hero_html(data)
        self.assertIn('class="badge2 partial"', hero)
        self.assertIn("Partial data", hero)
        self.assertNotIn("Calm market", hero)
        self.assertIn("Partial data: BTC unavailable, no other coin at its alert rank", hero)
        bar = report.brand_bar(data, "2026-11-20")
        self.assertIn('class="live partial"', bar)
        self.assertNotIn(">Calm<", bar.replace("</span>", "<"))

    def test_the_deck_names_the_unavailable_coin_and_does_not_claim_a_calm_market(self):
        deck = report.hero_deck(btc_stale_others_calm())
        self.assertIn("BTC is unavailable today, so no statement is made for it.", deck)
        self.assertIn("No other coin reached the alert rank", deck)

    def test_the_first_headline_is_not_calm_market(self):
        items = insights.top_headlines(btc_stale_others_calm()["coins"], None)
        self.assertEqual(items[0]["kicker"], "Partial data")
        self.assertIn("BTC unavailable", items[0]["text"])
        self.assertTrue(insights.wording_ok(items[0]["text"]))

    def test_the_email_says_the_same_as_the_page(self):
        data = btc_stale_others_calm()
        facts = report.email_facts(data)
        self.assertEqual(facts["status"], {"label": "PARTIAL DATA", "tone": "amber"})
        self.assertEqual(facts["title"], report.hero_title(data))
        self.assertEqual(facts["deck"], report.hero_deck(data))
        html = report.build_email(data)
        self.assertIn("PARTIAL DATA", html)
        self.assertNotIn("CALM MARKET", html)
        self.assertIn(emailbody.PILL_BG["amber"], html)
        self.assertIn("Partial data: BTC unavailable, no other coin at its alert rank", html)

    def test_the_page_has_the_amber_styles(self):
        page = report.build_html(btc_stale_others_calm())
        for rule in (".badge.PARTIAL{", ".live.partial,.badge2.partial{background:#9A6414}"):
            self.assertIn(rule, page)

    def test_the_scoreboard_still_marks_the_coin_unavailable(self):
        page = report.build_html(btc_stale_others_calm())
        self.assertIn("Data unavailable: " + STALE, page)

    def test_main_warns_once_naming_the_coin_and_exits_zero(self):
        code, text = run_main(btc_stale_others_calm())
        warnings = [line for line in text.splitlines() if line.startswith("::warning::")]
        self.assertEqual(code, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("BTC", warnings[0])
        self.assertNotIn("ETH", warnings[0])
        self.assertIn(": PARTIAL, 2 of 3 coins with data", text)
        self.assertIn("subject: Crypto Watch: partial data, BTC unavailable", text)


class AlertWithAnUnavailableCoin(unittest.TestCase):
    """BTC has no valid data and ETH is at the alert rank: the status stays ALERT and BTC is named."""

    def test_the_status_stays_alert_and_names_both(self):
        label, tone, detail = report.status_of(btc_stale_eth_alert())
        self.assertEqual((label, tone), ("ALERT", "red"))
        self.assertEqual(detail, "At the alert rank: ETH. Unavailable: BTC")

    def test_the_subject_is_the_alert_subject(self):
        data = btc_stale_eth_alert()
        subject = insights.subject_line(data["coins"])
        self.assertTrue(subject.startswith("Crypto Watch ALERT: ETH breaches its own history"), subject)
        self.assertNotIn("partial", subject.lower())

    def test_the_hero_is_alert_and_the_deck_names_the_unavailable_coin(self):
        data = btc_stale_eth_alert()
        hero = hero_html(data)
        self.assertIn('class="badge2"', hero)
        self.assertNotIn("partial", hero.lower().replace("partial-", ""))
        deck = report.hero_deck(data)
        self.assertIn("BTC is unavailable today, so no statement is made for it.", deck)
        self.assertEqual(report.hero_title(data), "Ether breaches its own history")

    def test_the_email_status_is_alert(self):
        facts = report.email_facts(btc_stale_eth_alert())
        self.assertEqual(facts["status"], {"label": "ALERT", "tone": "red"})
        self.assertNotIn("PARTIAL", report.build_email(btc_stale_eth_alert()))


class EveryCoinFailed(unittest.TestCase):
    def test_the_status_stays_no_data(self):
        self.assertEqual(report.status_of(all_failed()), ("NO DATA", "grey", "Market data unavailable for every coin"))

    def test_the_subject_the_hero_and_the_email_stay_no_data(self):
        data = all_failed()
        self.assertEqual(insights.subject_line(data["coins"]), "Crypto Watch: market data unavailable today, no statement made")
        self.assertEqual(report.hero_title(data), "Market data unavailable")
        self.assertIn("No data", hero_html(data))
        self.assertEqual(report.email_facts(data)["status"], {"label": "NO DATA", "tone": "grey"})
        self.assertNotIn("PARTIAL", report.build_html(data).replace(".badge.PARTIAL", "").replace("partial", ""))

    def test_main_still_exits_one(self):
        code, text = run_main(all_failed())
        self.assertEqual(code, 1)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("::warning::")]), 1)


class EveryCoinHasData(unittest.TestCase):
    """Nothing changes when no coin is unavailable."""

    def test_a_calm_market_is_unchanged(self):
        data = all_calm()
        self.assertEqual(report.status_of(data), ("CALM", "green", "No coin at the alert rank"))
        self.assertEqual(report.hero_title(data), "Calm market: no coin at its alert rank")
        self.assertTrue(report.hero_deck(data).startswith("No coin reached the alert rank of 95. The closest is "))
        self.assertNotIn("unavailable", report.hero_deck(data))
        self.assertEqual(insights.subject_line(data["coins"]), "Crypto Watch: calm market, no coin at its alert rank (closest XRP, rank 65)")
        self.assertEqual(insights.top_headlines(data["coins"], None)[0]["kicker"], "Calm market")
        self.assertEqual(report.email_facts(data)["status"], {"label": "CALM MARKET", "tone": "green"})
        self.assertIn('class="badge2 calm"', hero_html(data))

    def test_an_alert_is_unchanged(self):
        data = make_data(report.demo_coins())
        self.assertEqual(report.status_of(data), ("ALERT", "red", "At the alert rank: BTC, XRP"))
        self.assertNotIn("unavailable", report.hero_deck(data))
        self.assertEqual(report.email_facts(data)["status"], {"label": "ALERT", "tone": "red"})

    def test_main_prints_no_warning_when_every_coin_has_data(self):
        code, text = run_main(all_calm())
        self.assertEqual(code, 0)
        self.assertNotIn("::warning::", text)


if __name__ == "__main__":
    unittest.main()
