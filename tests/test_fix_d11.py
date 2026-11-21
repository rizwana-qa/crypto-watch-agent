"""D-11: the report page must pass colour contrast (4.5:1 for small text) and its sideways-scrolling cards must be reachable by keyboard.

Offline, no network, no browser. The contrast function is plain Python (WCAG 2.x relative luminance).

    python -m pytest tests/test_fix_d11.py -q
"""

import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import insights  # noqa: E402
import report  # noqa: E402

MIN_TEXT = 4.5      # WCAG 1.4.3, small text
MIN_UI = 3.0        # WCAG 1.4.11, focus outline against its surroundings
PAGE_BACKGROUNDS = {"ink": "#0A0E16", "bar": "#05080D", "panel": "#111826", "ticker": "#0F1623"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


# ------------------------------------------------------------------------------------------------
# Contrast, no dependencies
# ------------------------------------------------------------------------------------------------
def to_rgb(value):
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def luminance(rgb):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    """WCAG contrast ratio of two colours given as '#RRGGBB' strings or (r, g, b) tuples."""
    fg, bg = (to_rgb(c) if isinstance(c, str) else c for c in (fg, bg))
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def over(rgb, alpha, base):
    """A translucent colour laid over an opaque one."""
    return tuple(round(alpha * f + (1 - alpha) * b) for f, b in zip(to_rgb(rgb) if isinstance(rgb, str) else rgb, base))


class ContrastFunction(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(contrast("#000000", "#FFFFFF"), 21.0, places=6)
        self.assertAlmostEqual(contrast("#777777", "#FFFFFF"), 4.48, places=2)
        # the grey the audit flagged, on the card colour: axe reported 3.73
        self.assertAlmostEqual(contrast("#64748b", "#111826"), 3.73, places=2)


# ------------------------------------------------------------------------------------------------
# Reading the CSS in report.py
# ------------------------------------------------------------------------------------------------
def css_variables(css):
    root = re.search(r":root\{([^}]*)\}", css).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", root))


def resolve(value, variables):
    """(rgb, alpha) for '#RRGGBB', 'var(--x)' or 'rgba(r,g,b,a)'; None for anything else (transparent, gradients, inherit)."""
    value = value.strip()
    var = re.fullmatch(r"var\(--([\w-]+)\)", value)
    if var:
        value = variables[var.group(1)]
    if re.fullmatch(r"#[0-9A-Fa-f]{3}|#[0-9A-Fa-f]{6}", value):
        return to_rgb(value), 1.0
    rgba = re.fullmatch(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", value)
    if rgba:
        return tuple(int(x) for x in rgba.groups()[:3]), float(rgba.group(4))
    return None


def css_rules(css):
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        decls = {}
        for prop, val in re.findall(r"([\w-]+)\s*:\s*([^;]+)", body):
            decls.setdefault(prop.strip(), val.strip())
        yield selector.strip(), decls


class Cards(HTMLParser):
    """Collects every element with the class 'card' (the CSS gives it overflow-x:auto) and how many tables it holds."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.cards = [], []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        attrs = dict(attrs)
        record = None
        if "card" in (attrs.get("class") or "").split():
            record = {"tag": tag, "attrs": attrs, "tables": 0}
            self.cards.append(record)
        if tag == "table":
            for _, rec in self.stack:
                if rec is not None:
                    rec["tables"] += 1
        self.stack.append((tag, record))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def demo_data():
    coins = report.demo_coins()
    decisions, note = report.demo_decisions()
    return {"coins": coins, "cross": report.demo_cross(), "context": report.demo_context(coins), "decisions": decisions, "decisions_note": note,
            "demo": True, "generated": "2026-11-20 12:00", "generated_long": "Friday 20 November 2026", "events": insights.event_study(coins)}


class Colours(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.variables = css_variables(report.CSS)
        cls.page = report.build_html(demo_data())

    def test_every_text_variable_passes_on_every_page_background(self):
        for name in ("muted", "text", "soft", "amber", "teal", "neg", "violet", "green"):
            for bg_name, bg in PAGE_BACKGROUNDS.items():
                with self.subTest(text=name, background=bg_name):
                    self.assertGreaterEqual(contrast(self.variables[name], bg), MIN_TEXT)

    def test_background_variables_match_the_ones_this_test_uses(self):
        for name in ("ink", "bar", "panel"):
            self.assertEqual(self.variables[name].upper(), PAGE_BACKGROUNDS[name].upper())

    def test_every_css_rule_with_a_text_colour_passes(self):
        """Each rule that sets `color`: against its own background when it sets one, otherwise against every page background."""
        checked = 0
        for selector, decls in css_rules(report.CSS):
            if "color" not in decls:
                continue
            fg = resolve(decls["color"], self.variables)
            if fg is None or fg[1] < 1:
                continue            # transparent or inherited: the gradient text is checked separately
            own = resolve(decls.get("background", decls.get("background-color", "")), self.variables)
            if own:
                bgs = {"own": over(own[0], own[1], to_rgb(PAGE_BACKGROUNDS["panel"])) if own[1] < 1 else own[0]}
            else:
                bgs = {k: to_rgb(v) for k, v in PAGE_BACKGROUNDS.items()}
            for bg_name, bg in bgs.items():
                checked += 1
                with self.subTest(selector=selector, background=bg_name):
                    self.assertGreaterEqual(contrast(fg[0], bg), MIN_TEXT)
        self.assertGreater(checked, 40)

    def test_white_status_badges_pass(self):
        """`.live` and `.badge2` set white text in one rule and the background in the tone rules (.calm, .nodata)."""
        checked = 0
        for selector, decls in css_rules(report.CSS):
            if not re.search(r"\.(live|badge2)(\.\w+)?$", selector) or "background" not in decls:
                continue
            bg = resolve(decls["background"], self.variables)
            checked += 1
            with self.subTest(selector=selector):
                self.assertGreaterEqual(contrast("#FFFFFF", bg[0]), MIN_TEXT)
        self.assertGreaterEqual(checked, 3)

    def test_gradient_text_passes_on_the_bar_and_the_footer(self):
        stops = re.findall(r"#[0-9A-Fa-f]{6}", re.search(r"\.credit\{[^}]*linear-gradient\(([^)]*)\)", report.CSS).group(1))
        self.assertTrue(stops)
        for stop in stops:
            for bg in ("bar", "ink"):
                with self.subTest(stop=stop, background=bg):
                    self.assertGreaterEqual(contrast(stop, PAGE_BACKGROUNDS[bg]), MIN_TEXT)

    def test_inline_text_colours_in_the_built_page_pass(self):
        """Colours written straight into the HTML (style="color:#...") sit in table cells, on the card colour."""
        found = re.findall(r'style="[^"]*?(?<![-\w])color:\s*(#[0-9A-Fa-f]{6})', self.page)
        self.assertTrue(found, "the page no longer has an inline text colour: update this test")
        for colour in set(found):
            for bg in ("panel", "ink"):
                with self.subTest(colour=colour, background=bg):
                    self.assertGreaterEqual(contrast(colour, PAGE_BACKGROUNDS[bg]), MIN_TEXT)

    def test_the_demo_badge_has_readable_white_text(self):
        found = re.search(r'<span class="badge2" style="background:(#[0-9A-Fa-f]{6})">Demo data', self.page)
        self.assertIsNotNone(found)
        self.assertGreaterEqual(contrast("#FFFFFF", found.group(1)), MIN_TEXT)

    def test_no_known_low_contrast_grey_is_left(self):
        self.assertNotIn("#64748b", report.CSS.lower() + self.page.lower())


class ScrollCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = report.build_html(demo_data())
        parser = Cards()
        parser.feed(cls.page)
        cls.scrollers = [c for c in parser.cards if c["tables"]]

    def test_the_css_makes_cards_scroll_sideways(self):
        rule = next(d for s, d in css_rules(report.CSS) if s == ".card")
        self.assertEqual(rule.get("overflow-x"), "auto")

    def test_every_card_with_a_table_is_a_keyboard_reachable_labelled_region(self):
        self.assertGreaterEqual(len(self.scrollers), 7)          # snapshot, context, market context, frequency, scenarios, history, situations
        labels = []
        for card in self.scrollers:
            attrs = card["attrs"]
            with self.subTest(label=attrs.get("aria-label")):
                self.assertEqual(attrs.get("tabindex"), "0")
                self.assertEqual(attrs.get("role"), "region")
                self.assertTrue((attrs.get("aria-label") or "").strip())
            labels.append(attrs.get("aria-label"))
        self.assertEqual(len(labels), len(set(labels)), "two regions share a label")

    def test_the_decision_history_card_is_labelled_as_in_the_task(self):
        labels = [c["attrs"].get("aria-label") for c in self.scrollers]
        self.assertIn("Decision history table, scrolls sideways", labels)

    def test_a_visible_focus_outline_in_the_accent_colour(self):
        rules = [d for s, d in css_rules(report.CSS) if ".card" in s and ":focus-visible" in s]
        self.assertTrue(rules, "no .card:focus-visible rule")
        outline = rules[0].get("outline", "")
        self.assertRegex(outline, r"^[3-9]px solid #8FE3D8$")      # the outline colour the links and tabs already use
        for bg in ("panel", "ink"):
            self.assertGreaterEqual(contrast("#8FE3D8", PAGE_BACKGROUNDS[bg]), MIN_UI)

    def test_the_decision_history_card_with_no_decisions_is_still_reachable(self):
        data = {**demo_data(), "decisions": [], "decisions_note": ""}
        parser = Cards()
        parser.feed(report.build_html(data))
        history = [c for c in parser.cards if (c["attrs"].get("aria-label") or "").startswith("Decision history")]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["attrs"].get("tabindex"), "0")


if __name__ == "__main__":
    unittest.main()
