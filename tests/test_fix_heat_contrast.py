"""Offline test: the white rank numbers in the heatmap keep WCAG AA contrast (4.5:1) on every colour of the scale.

    python tests/test_fix_heat_contrast.py -v

The audit measured 3.6 to 4.4:1 on the amber and red ends of the old scale. Plotly mixes the colours between two stops, so every
step between the stops is checked, not only the stops.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import emailbody  # noqa: E402
import report  # noqa: E402


def channels(color):
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


def luminance(color):
    def part(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (part(v) for v in channels(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def mix(a, b, t):
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(channels(a), channels(b)))


class HeatContrast(unittest.TestCase):
    def test_every_stop_and_every_step_between_stops_passes_aa(self):
        worst = 99
        for (_, first), (_, second) in zip(report.HEAT_SCALE, report.HEAT_SCALE[1:]):
            for step in range(101):
                color = mix(first, second, step / 100)
                value = contrast(report.HEAT_TEXT, color)
                worst = min(worst, value)
                self.assertGreaterEqual(value, 4.5, f"{color} between {first} and {second}: {value:.2f}")
        self.assertGreaterEqual(worst, 4.5)

    def test_the_spec_uses_the_scale_and_the_text_colour(self):
        coin = {"coin": "BTC", "summary": {"ranks": {"volatility": 99.3, "distance": 91.0, "drawdown": 50.0}, "context": {"volume_rank": 96.0}}}
        spec = report.rank_heatmap({"cross": {}}, [coin])["data"][0]
        self.assertEqual([tuple(stop) for stop in spec["colorscale"]], list(report.HEAT_SCALE))
        self.assertEqual(spec["textfont"]["color"], report.HEAT_TEXT)

    def test_the_three_bands_stay_distinct(self):
        scale = dict(report.HEAT_SCALE)
        self.assertNotEqual(scale[0.9], scale[0.95])
        self.assertNotEqual(scale[0.949], scale[0.95])
        self.assertGreater(luminance(scale[0.9]), luminance(scale[0.89]))   # near threshold is lighter than calm

    def test_the_email_uses_the_same_amber_and_red(self):
        self.assertEqual(emailbody.RANK_BG["mid"], dict(report.HEAT_SCALE)[0.949])
        self.assertEqual(emailbody.RANK_BG["hi"], dict(report.HEAT_SCALE)[0.95])


if __name__ == "__main__":
    unittest.main()
