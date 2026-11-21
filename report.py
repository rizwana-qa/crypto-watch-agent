"""Executive intelligence brief for the Crypto Watch Agent.

One self-contained HTML page (interactive charts: plotly.js from a CDN) and an HTML email whose body carries the brief itself
(emailbody.py draws it with tables, because an email program runs no scripts). It reuses the agent's own functions, so every
number is the number the agent saw.

Who writes what, stated on the page itself:
  - Computed by code: prices, the three metrics, the ranks, the headlines and the monitoring triggers.
  - Written by AI: the analysis text, which is the agent's latest decision read back from the Sheet.
  - Checked by code: the guardrails the decision passed (valid playbook sources, no advice wording, market data, numbers found in the evidence, no repeat alert).

There is no price forecast and no recommendation in this report, on purpose: it is live market-data analysis and decision support, not investment advice.
The "what the history says" section is an event study of past data with its sample sizes shown (insights.py), never a prediction.

Usage:
  python report.py                      # live Kraken data + the Sheet history (needs SHEET_URL for the AI analysis)
  python report.py --email              # also email it (subject built from the data) through the Apps Script web app (needs the updated Code.gs)
  python report.py --demo               # synthetic data, clearly labelled, to preview the layout offline
"""
import argparse
import html
import json
import math
import os
import sys
from datetime import datetime, timezone

import agent
import asktab
import emailbody
import insights
import sources

PLOTLY_URL = "https://cdn.plot.ly/plotly-2.35.2.min.js"
LABELS = {"volatility": "30-day volatility", "distance": "Distance from 30-day average", "drawdown": "30-day max drawdown"}
COLORS = {"BTC": "#F6B73C", "ETH": "#7DA2FF", "XRP": "#5ED0C3"}
CREDIT = "Rizwana Zahoor"          # the byline on the page, in the email and in the local Ask page
CHART_DAYS = 240     # candles kept for the price and average charts (the page has range buttons for 30, 60 and 90 and for 60, 120 and 240 days)
RANK_DAYS = 60       # length of the rank history chart
NEAR_RANK = 90.0     # same band as playbook.md, "Near threshold"
DECISION_ROWS = 10
MONITORING_QUESTION = (
    "Across BTC/USD, ETH/USD and XRP/USD, what has changed in the latest completed daily market data, which movements or relationships "
    "are unusual compared with each asset's historical behaviour, what do trends, volatility, volume, relative strength and cross-asset "
    "relationships indicate, and what evidence-based forward scenarios, uncertainty levels, risk flags and invalidation conditions should be monitored?"
)


# ------------------------------------------------------------------------------------------------
# 1. Data
# ------------------------------------------------------------------------------------------------
def value_at_rank(values, rank):
    """The smallest value in `values` whose own percentile rank is at least `rank` (None for an empty list)."""
    for value in sorted(set(values)):
        if agent.percentile_rank(values, value) >= rank:
            return value
    return None


def _volatility_level(window_returns, target, direction, last_close):
    """The next-day close that pushes the 30-day volatility to `target`, found by bisection (None if even a 50% move cannot)."""
    def spread(r):
        return agent.statistics.stdev(window_returns + [r])
    start = agent.statistics.fmean(window_returns)
    if spread(start) >= target:
        return "already"
    edge = 0.5 * direction
    if spread(edge) < target:
        return None
    low, high = start, edge
    for _ in range(60):
        middle = (low + high) / 2
        low, high = (middle, high) if spread(middle) < target else (low, middle)
    return last_close * (1 + high)


def trigger_levels(closes):
    """The next-day closes at which each metric would reach the alert rank, from the coin's own history.
    These are conditions to monitor. They are not forecasts and not price targets."""
    series, returns = agent.metric_series(closes)
    last = closes[-1]
    k = agent.WINDOW - 1
    levels = {"history_days": len(closes), "distance": None, "drawdown": None, "volatility": None}
    # A flat history has no top 5 percent, so there is no level to give (value_at_rank returns None).
    d = value_at_rank([abs(v) for v in series["distance"] if v is not None], agent.ALERT_RANK)
    if d is not None:
        total = sum(closes[-k:])
        levels["distance"] = {"value_pct": d * 100, "up": (1 + d) * total / (k - d), "down": (1 - d) * total / (k + d)}
    depth = value_at_rank([v for v in series["drawdown"] if v is not None], agent.ALERT_RANK)
    if depth is not None:
        window = closes[-agent.WINDOW:]
        already = -agent.max_drawdown(window) >= depth
        levels["drawdown"] = {"value_pct": depth * 100, "down": "already" if already else max(window) * (1 - depth)}
    vol = value_at_rank([v for v in series["volatility"] if v is not None], agent.ALERT_RANK)
    if vol is not None:
        recent = returns[-k:]
        levels["volatility"] = {"value_pct": vol * 100, "up": _volatility_level(recent, vol, 1, last),
                                "down": _volatility_level(recent, vol, -1, last)}
    return levels


# The base rate is computed in agent.py now (the get_alert_history_stats tool uses it); the brief shows the same numbers.
episodes = agent.episodes
base_rate = agent.base_rate


def analyse(coin, stamps, closes, volumes=None):
    """The agent's own summary plus the series the charts need."""
    summary = agent.summarize(coin, stamps, closes)
    if volumes is not None:
        agent.add_context(summary, closes, volumes)
    series, _ = agent.metric_series(closes)
    n = len(closes)
    start = max(0, n - CHART_DAYS)
    rank_dates, rank_top = [], []
    for i in range(max(agent.WINDOW, n - RANK_DAYS), n):
        ranks = agent.ranks_at(series, i)
        rank_dates.append(agent.format_date(stamps[i]))
        rank_top.append(round(max(ranks.values()), 1))
    return {
        "coin": coin,
        "summary": summary,
        "base_rate": base_rate(stamps, closes),
        "closes": list(closes),
        "stamps": list(stamps),
        "dates": [agent.format_date(t) for t in stamps[start:]],
        "closes_recent": [round(c, 6) for c in closes[start:]],
        "rank_dates": rank_dates,
        "rank_top": rank_top,
        "levels": trigger_levels(closes),
    }


def cross_from(series):
    """Relationships between the coins that have data, or an error note."""
    if len(series) < 2:
        return {"error": "at least two coins with data are needed for relative strength and correlation"}
    try:
        return agent.cross_asset_summary(series)
    except (ValueError, agent.statistics.StatisticsError) as error:
        return {"error": str(error)}


def gather(now):
    coins, series = [], {}
    for coin in agent.WATCHLIST:
        try:
            stamps, closes, volumes = agent.load_coin(coin, now)
            coins.append(analyse(coin, stamps, closes, volumes))
            series[coin] = (stamps, closes)
        except (RuntimeError, ValueError, agent.statistics.StatisticsError) as error:
            coins.append({"coin": coin, "error": str(error)})
    return coins, cross_from(series)


def gather_context():
    """The same market context the agent reads (free sources, fail-soft). Never raises."""
    return agent.tool_market_context()


def fetch_decisions():
    try:
        body = agent.sheet_post({"action": "history", "limit": DECISION_ROWS, "detail": True})
    except agent.AgentError as error:
        return [], f"The decision history could not be read from the Sheet: {error}"
    return body.get("decisions", []), ""


def demo_series():
    """Synthetic closes and volumes, only to preview the layout. The page says so in its banner."""
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    today = int(now.timestamp()) // agent.SECONDS_PER_DAY * agent.SECONDS_PER_DAY
    out = {}
    for index, (coin, start_price) in enumerate((("BTC", 90000.0), ("ETH", 3100.0), ("XRP", 2.4))):
        n = 400
        stamps = [today - (n - i) * agent.SECONDS_PER_DAY for i in range(n)]
        closes, volumes = [], []
        for i in range(n):
            wave = math.sin(i / (9 + 3 * index)) * 0.012 + math.sin(i / 3.1 + index) * 0.006
            drop = -0.0035 * max(0, i - (n - 14)) if coin in ("BTC", "XRP") else 0.0
            closes.append(start_price * (1 + 0.0004 * i + wave + drop))
            volumes.append(1000.0 * (1 + 0.3 * math.sin(i / 5.0 + index)) * (2.2 if i >= n - 2 and coin == "BTC" else 1.0))
        out[coin] = (stamps, closes, volumes)
    return out


def demo_coins():
    return [analyse(coin, stamps, closes, volumes) for coin, (stamps, closes, volumes) in demo_series().items()]


def demo_cross():
    return cross_from({coin: (stamps, closes) for coin, (stamps, closes, _) in demo_series().items()})


def demo_context(coins=None):
    """Sample context for the layout preview. Prices follow the synthetic closes so the page does not contradict itself."""
    import scenarios                                   # only the preview needs the fake builder
    closes = {c["coin"]: c["summary"]["last_close"] for c in (coins or demo_coins()) if "error" not in c}
    return scenarios.fake_context(btc_mid=closes.get("BTC", 67210.5), mids=closes, iv_rank=96.5, btc_price_gap=0.3)


def demo_decisions():
    return [{
        "time": "2026-11-20T09:17:41Z", "version": "Agent loop (demo)", "alert": True, "coins": ["BTC", "XRP"],
        "message": "Demo text: BTC and XRP are above the alert rank for drawdown and distance from the 30-day average.",
        "sources": ["Alert rules", "Drawdown rule"], "blocked_reason": "",
        "reasoning": "Demo text: the ranks in the market summary are at or above 95 for these coins.",
    }], ""


# ------------------------------------------------------------------------------------------------
# 2. Facts the report states (all computed by code)
# ------------------------------------------------------------------------------------------------
def good(data):
    return [c for c in data["coins"] if "error" not in c]


def breaching(data):
    return [c for c in good(data) if c["summary"]["breaches"]]


def near_threshold(data):
    found = []
    for c in good(data):
        for metric in agent.METRICS:
            rank = c["summary"]["ranks"][metric]
            if NEAR_RANK <= rank < agent.ALERT_RANK:
                found.append((c["coin"], metric, rank))
    return found


def metric_value(summary, metric):
    if metric == "volatility":
        return f"{summary['volatility_30d_daily_pct']:.2f}% daily"
    if metric == "distance":
        return f"{summary['dist_from_30d_avg_pct']:+.2f}%"
    return f"{summary['max_drawdown_30d_pct']:.2f}%"


def status_of(data):
    if not good(data):
        return "NO DATA", "grey", "Market data unavailable for every coin"
    alerted = [c["coin"] for c in breaching(data)]
    down = insights.unavailable(data["coins"])
    if alerted:
        return "ALERT", "red", "At the alert rank: " + ", ".join(alerted) + (". Unavailable: " + ", ".join(down) if down else "")
    if down:
        return "PARTIAL", "amber", f"Partial data: {_join_and(down)} unavailable. No other coin at the alert rank"
    return "CALM", "green", "No coin at the alert rank"


def headlines(data):
    lines = []
    for c in breaching(data):
        s = c["summary"]
        parts = [f"{LABELS[m].lower()} {metric_value(s, m)} (rank {s['ranks'][m]})" for m in s["breaches"]]
        lines.append(f"{c['coin']} is unusual compared with its own history: " + "; ".join(parts) + ".")
    if not breaching(data) and good(data):
        lines.append(f"No coin reached the alert rank of {agent.ALERT_RANK:g} on volatility, distance from the 30-day average or drawdown.")
    for coin, metric, rank in near_threshold(data):
        lines.append(f"{coin} is near the threshold on {LABELS[metric].lower()} (rank {rank}, alert rank {agent.ALERT_RANK:g}).")
    for c in good(data):
        ctx = c["summary"].get("context", {})
        if "volume" in c["summary"].get("flags", []):
            lines.append(f"{c['coin']} volume is unusual: {ctx['volume_ratio_30d']:.2f} times its 30-day average (rank {ctx['volume_rank']}). "
                         "Volume is context and never raises an alert on its own.")
    cross = data.get("cross") or {}
    if cross.get("flags"):
        lines.append("Unusual relationships, context only: " + "; ".join(cross["flags"]) + " (each at an extreme of its own history, see the relationships table).")
    context = data.get("context") or {}
    if context.get("flags"):
        lines.append("Market context, context only and never an alert on its own: " + "; ".join(context["flags"]) + " (see the market context table).")
    broken = [c for c in data["coins"] if "error" in c]
    if broken:
        lines.append("Data unavailable, so no statement is made for: " + ", ".join(c["coin"] for c in broken) + ".")
    else:
        lines.append(f"Data is complete and current for all {len(data['coins'])} coins.")
    return lines


def triggers(data):
    lines = []
    breached = breaching(data)
    if breached:
        who = "; ".join(f"{c['coin']} ({', '.join(LABELS[m].lower() for m in c['summary']['breaches'])})" for c in breached)
        lines.append(f"{who}: a spike stays inside the 30-day window for 30 days, so these ranks can stay high for days. "
                     f"The agent does not repeat an alert for a coin that is in its last {agent.HISTORY_LIMIT} decisions.")
    for coin, metric, rank in near_threshold(data):
        lines.append(f"{coin}: {LABELS[metric].lower()} is {agent.ALERT_RANK - rank:.1f} rank points below the alert rank of {agent.ALERT_RANK:g}.")
    broken = [c["coin"] for c in data["coins"] if "error" in c]
    if broken:
        lines.append("Unavailable data excludes a coin from alerts until Kraken returns complete, current candles: " + ", ".join(broken) + ".")
    lines.append("Next scheduled agent run: every day at 05:17 UTC (09:17 in Dubai).")
    return lines


def latest_decision(data):
    return data["decisions"][-1] if data["decisions"] else None


def agreement(data):
    decision = latest_decision(data)
    if decision is None or not good(data):
        return None
    ai = set(decision.get("coins") or []) if decision.get("alert") else set()
    code = {c["coin"] for c in breaching(data)}
    if ai == code:
        return True, "The AI's latest decision and the code's own rank check name the same coins."
    return False, (f"The AI's latest decision names {', '.join(sorted(ai)) or 'no coin'}; today's rank check names "
                   f"{', '.join(sorted(code)) or 'no coin'}. They can differ when the decision was made on an earlier day.")


# ------------------------------------------------------------------------------------------------
# 3. Charts (plotly.js specs written as plain data; the page adds hover, zoom, legend toggles and range buttons)
# ------------------------------------------------------------------------------------------------
GRID = "#1F2937"
MUTED = "#A3ACBD"
TEXT = "#F2F4F8"
AMBER = "#F6B73C"
BASE_LAYOUT = {
    "margin": {"l": 56, "r": 16, "t": 14, "b": 44}, "height": 340,
    "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"family": "Inter, Segoe UI, Roboto, Arial, sans-serif", "size": 12, "color": MUTED},
    "legend": {"orientation": "h", "y": -0.2, "font": {"color": TEXT}}, "hovermode": "x unified",
    "hoverlabel": {"bgcolor": "#0A0E16", "bordercolor": GRID, "font": {"color": TEXT}},
    "xaxis": {"gridcolor": GRID, "linecolor": GRID, "zeroline": False},
    "yaxis": {"gridcolor": GRID, "linecolor": GRID, "zeroline": False},
}


def layout(**changes):
    merged = json.loads(json.dumps(BASE_LAYOUT))
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def threshold_lines(y_value, color, label=None, dash="dash"):
    shape = {"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": y_value, "y1": y_value,
             "line": {"color": color, "width": 1.5, "dash": dash}}
    note = None
    if label:
        note = {"xref": "paper", "x": 1, "y": y_value, "xanchor": "right", "yanchor": "bottom", "showarrow": False,
                "text": label, "font": {"size": 11, "color": color}}
    return shape, note


def range_tabs(labels, per_range):
    """What the page needs to draw range buttons: one group of traces is visible at a time. per_range: how many traces each range owns."""
    total = per_range * len(labels)
    return [{"label": label, "visible": [per_range * i <= t < per_range * (i + 1) for t in range(total)]} for i, label in enumerate(labels)]


HEAT_COLUMNS = [("Volatility", "volatility"), ("Vs 30-day<br>average", "distance"), ("Max<br>drawdown", "drawdown"),
                ("Volume<br>(context)", "volume"), ("Relative<br>strength<br>(context)", "strength")]


# The cell colours. White cell text must keep a contrast of 4.5:1 or more on every colour between two stops (WCAG AA), so the amber and red
# ends are the darker ones that the email already uses (emailbody.RANK_BG). tests/test_fix_heat_contrast.py checks every step of the scale.
HEAT_TEXT = "#FFFFFF"
HEAT_SCALE = ((0, "#161E2D"), (0.89, "#1E2838"), (0.9, "#8A6A1E"), (0.949, "#9A6414"), (0.95, "#C23B33"), (1, "#C93D35"))


def rank_heatmap(data, ok):
    """Coins by measures. The colour shows how extreme the rank is: dark is ordinary, amber is 90 up to 95, red is 95 or higher.
    Relative strength can be unusual at either end, so its colour uses the distance from the middle and its text is the rank."""
    strength = {x["coin"]: x["rank"] for x in (data.get("cross") or {}).get("relative_strength", [])}
    z, text = [], []
    for c in ok:
        s, ctx = c["summary"], c["summary"].get("context", {})
        values = [s["ranks"]["volatility"], s["ranks"]["distance"], s["ranks"]["drawdown"], ctx.get("volume_rank"), strength.get(c["coin"])]
        text.append(["n/a" if v is None else f"{v:.1f}" for v in values])
        z.append([None if v is None else (max(v, 100.0 - v) if i == 4 else v) for i, v in enumerate(values)])
    scale = [list(stop) for stop in HEAT_SCALE]
    spec = {"type": "heatmap", "x": [name for name, _ in HEAT_COLUMNS], "y": [c["coin"] for c in ok], "z": z, "text": text,
            "texttemplate": "%{text}", "textfont": {"size": 15, "color": HEAT_TEXT}, "zmin": 50, "zmax": 100, "colorscale": scale,
            "showscale": False, "xgap": 4, "ygap": 4, "hovertemplate": "%{y}, %{x}: rank %{text}<extra></extra>"}
    line = {"type": "line", "xref": "x", "yref": "paper", "x0": 2.5, "x1": 2.5, "y0": 0, "y1": 1, "line": {"color": MUTED, "width": 1.5, "dash": "dot"}}
    notes = [{"x": 1, "y": 1.12, "xref": "x", "yref": "paper", "text": "Can raise an alert", "showarrow": False, "font": {"size": 12, "color": "#FF9C94"}},
             {"x": 3.5, "y": 1.12, "xref": "x", "yref": "paper", "text": "Context only", "showarrow": False, "font": {"size": 12, "color": MUTED}}]
    return {"data": [spec], "layout": layout(hovermode="closest", yaxis={"autorange": "reversed"}, xaxis={"side": "bottom", "tickfont": {"size": 11}, "tickangle": 0},
                                             shapes=[line], annotations=notes, margin={"l": 50, "r": 10, "t": 40, "b": 70})}


def lead_coin(ok):
    """The coin to feature: the one with the highest rank at the alert rank, else the highest rank of all."""
    return max(ok, key=lambda c: (max(c["summary"]["ranks"].values()), c["coin"]))


def moving_average(closes, window=agent.WINDOW):
    """The mean of each close and the window - 1 closes before it (None until there are enough), the same average the distance metric uses."""
    return [None if i < window - 1 else agent.statistics.fmean(closes[i - window + 1:i + 1]) for i in range(len(closes))]


def average_chart(c):
    """The lead coin's close against its own 30-day average, with range buttons. The shaded gap is the distance metric."""
    closes, stamps = c.get("closes"), c.get("stamps")
    if not closes or len(closes) < agent.WINDOW + 5:
        return None
    avg = moving_average(closes)
    dates = [agent.format_date(t) for t in stamps]
    ranges = [r for r in (60, 120, 240) if r <= len(closes) - agent.WINDOW + 1] or [len(closes) - agent.WINDOW + 1]
    traces = []
    for r in ranges:
        lo = len(closes) - r
        shown = r == ranges[0]
        traces.append({"type": "scatter", "mode": "lines", "name": "30-day average", "x": dates[lo:], "y": [round(v, 6) for v in avg[lo:]],
                       "line": {"color": AMBER, "width": 1.6, "dash": "dash"}, "visible": shown, "hovertemplate": "%{y:,.2f}"})
        traces.append({"type": "scatter", "mode": "lines", "name": f"{c['coin']} close", "x": dates[lo:], "y": [round(v, 6) for v in closes[lo:]],
                       "line": {"color": TEXT, "width": 2.2}, "fill": "tonexty", "fillcolor": "rgba(229,72,63,.16)", "visible": shown,
                       "hovertemplate": "%{y:,.2f}"})
    return {"data": traces, "ranges": range_tabs([f"{r} days" for r in ranges], 2),
            "layout": layout(yaxis={"title": {"text": f"{c['coin']} close, USD"}, "tickformat": ",.0f" if closes[-1] >= 100 else ",.3f"}, margin={"l": 64, "r": 16, "t": 14, "b": 44})}


def price_chart(ok):
    """Every coin indexed to 100 at the start of the chosen range, with range buttons (30, 60 and 90 days)."""
    longest = min(len(c.get("closes") or []) for c in ok)
    ranges = [r for r in (30, 60, 90) if r <= longest] or ([longest] if longest > 1 else [])
    if not ranges:
        return None
    traces = []
    for r in ranges:
        for c in ok:
            window = c["closes"][-r:]
            traces.append({"type": "scatter", "mode": "lines", "name": c["coin"], "x": [agent.format_date(t) for t in c["stamps"][-r:]],
                           "y": [round(v / window[0] * 100, 2) for v in window], "line": {"color": COLORS.get(c["coin"], MUTED), "width": 2.5},
                           "visible": r == ranges[0], "showlegend": r == ranges[0], "legendgroup": c["coin"]})
    return {"data": traces, "ranges": range_tabs([f"{r} days" for r in ranges], len(ok)),
            "layout": layout(yaxis={"title": {"text": "Index, start of range = 100"}}, margin={"l": 56, "r": 16, "t": 14, "b": 44})}


def history_chart(ok):
    alert_shape, _ = threshold_lines(agent.ALERT_RANK, "#FF7A70")
    near_shape, _ = threshold_lines(NEAR_RANK, AMBER, dash="dot")
    traces = [{"type": "scatter", "mode": "lines", "name": c["coin"], "x": c["rank_dates"], "y": c["rank_top"],
               "line": {"color": COLORS.get(c["coin"], MUTED), "width": 2.5}} for c in ok]
    traces += [{"type": "scatter", "mode": "lines", "name": f"Alert rank {agent.ALERT_RANK:g}", "x": [None], "y": [None], "line": {"color": "#FF7A70", "width": 1.5, "dash": "dash"}},
               {"type": "scatter", "mode": "lines", "name": f"Near threshold {NEAR_RANK:g}", "x": [None], "y": [None], "line": {"color": AMBER, "width": 1.5, "dash": "dot"}}]
    return {"data": traces, "layout": layout(yaxis={"range": [0, 105], "title": {"text": "Highest of the 3 ranks"}}, shapes=[alert_shape, near_shape])}


def persist_chart(events):
    """Of all days at the alert rank, the share still at it k days later."""
    values = events["again"]
    days = [k + 1 for k, v in enumerate(values) if v is not None]
    shares = [round(v, 1) for v in values if v is not None]
    if not shares:
        return None
    trace = {"type": "scatter", "mode": "lines+markers", "name": "Still at the alert rank", "x": days, "y": shares, "fill": "tozeroy",
             "fillcolor": "rgba(246,183,60,.14)", "line": {"color": AMBER, "width": 2.5}, "marker": {"size": 6, "color": AMBER},
             "hovertemplate": "%{y:.0f}% still at the alert rank after %{x} days<extra></extra>"}
    return {"data": [trace], "layout": layout(hovermode="closest", showlegend=False, xaxis={"title": {"text": "Days after a day at the alert rank"}, "dtick": 3, "range": [0.5, 21.5]},
                                              yaxis={"range": [0, 100], "title": {"text": "Share still at the alert rank, %"}}, height=300)}


def moves_chart(events):
    """Coin returns 7, 14 and 30 days after an episode starts, against any day. Box = middle half, line = median, whiskers = full range."""
    horizons = [h for h in insights.HORIZONS if events["after_episode"].get(h) and events["any_day"].get(h)]
    if not horizons:
        return None
    labels = [f"{h} days later" for h in horizons]
    traces = []
    for name, key, color in (("After an episode starts", "after_episode", AMBER), ("Any day", "any_day", "#6B7686")):
        stats = [events[key][h] for h in horizons]
        traces.append({"type": "box", "name": name, "orientation": "h", "y": labels, "q1": [s["q1"] for s in stats], "median": [s["median"] for s in stats],
                       "q3": [s["q3"] for s in stats], "lowerfence": [s["low"] for s in stats], "upperfence": [s["high"] for s in stats],
                       "marker": {"color": color}, "line": {"color": color}, "fillcolor": "rgba(246,183,60,.35)" if key == "after_episode" else "rgba(107,118,134,.35)"})
    return {"data": traces, "layout": layout(hovermode="closest", boxmode="group", xaxis={"title": {"text": "Return after N days, %"}, "zeroline": True, "zerolinecolor": MUTED},
                                              yaxis={"autorange": "reversed"}, height=340,
                                              legend={"orientation": "h", "y": -0.32, "font": {"color": TEXT}}, margin={"l": 112, "r": 16, "t": 14, "b": 84})}


def charts(data):
    ok = good(data)
    if not ok:
        return {}
    specs = {"ranks": rank_heatmap(data, ok), "history": history_chart(ok)}
    if all(c.get("closes") for c in ok):
        specs["price"] = price_chart(ok)
        specs["average"] = average_chart(lead_coin(ok))
    events = data.get("events")
    if events and events["enough"]:
        specs["persist"] = persist_chart(events)
        specs["moves"] = moves_chart(events)
    return {key: spec for key, spec in specs.items() if spec}


# ------------------------------------------------------------------------------------------------
# 4. The page
# ------------------------------------------------------------------------------------------------
CSS = """
:root{--ink:#0A0E16;--bar:#05080D;--panel:#111826;--line:#1F2937;--line2:#2A3446;--amber:#F6B73C;--teal:#5ED0C3;--red:#E5483F;--alert:#B3261E;
--neg:#FF7A70;--muted:#A3ACBD;--text:#F2F4F8;--soft:#D5DAE4;--violet:#C4B5FD;--green:#7DD9A0}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;background:var(--ink);color:var(--text);font-family:Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.5}
a{color:#8FE3D8}a:hover{color:#BDF1EA}a:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
.cond{font-family:"Barlow Condensed","Arial Narrow",sans-serif}
.mono{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace}
.wrap{max-width:1360px;margin:0 auto;padding:0 40px}
.bar{background:var(--bar);border-bottom:1px solid #1B2433}
.bar .wrap{padding-top:14px;padding-bottom:14px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px 24px}
.brand{display:flex;flex-wrap:wrap;align-items:center;gap:12px 14px}
.logo{font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:28px;letter-spacing:.04em;text-transform:uppercase}
.live{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#fff;background:var(--alert);border-radius:4px;padding:4px 10px}
.live.calm{background:#1F6F4A}.live.nodata{background:#4A5568}
.dot{width:8px;height:8px;border-radius:50%;background:#fff;display:inline-block}
.byline{font-size:13px;line-height:1.25;color:var(--muted);padding-left:14px;border-left:1px solid var(--line2);animation:byline .9s ease-out .3s both}
.credit{font-weight:600;background:linear-gradient(100deg,#F2F4F8 0%,#F2F4F8 38%,#F6B73C 50%,#F2F4F8 62%,#F2F4F8 100%);background-size:200% 100%;
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;animation:sheen 6s 1.2s ease-in-out infinite}
.bar .right{display:flex;flex-wrap:wrap;align-items:center;gap:8px 28px;font-size:14px;color:var(--muted)}
.bar .right a{display:inline-flex;align-items:center;min-height:44px;font-weight:600;text-decoration:none}
.ticker{background:#0F1623;border-bottom:1px solid #1B2433;overflow:hidden}
.ticker .wrap{display:flex;align-items:stretch;gap:20px}
.ticker .tag{flex:none;display:inline-flex;align-items:center;font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--amber);padding:12px 0}
.ticker .view{flex:1 1 auto;min-width:0;overflow:hidden}
.track{display:flex;width:max-content;animation:tick 70s linear infinite}
.track .set{display:flex;flex:none;align-items:center}
.tick{display:inline-flex;align-items:baseline;gap:10px;padding:12px 28px 12px 0;white-space:nowrap;font-size:14px}
.tick b{color:var(--muted);letter-spacing:.06em;text-transform:uppercase;font-size:12px;font-weight:600}
.tick span{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-weight:500}
.neg{color:var(--neg)}.pos{color:var(--green)}.amb{color:var(--amber)}
main.wrap{padding-top:40px;padding-bottom:56px;display:flex;flex-direction:column;gap:40px}
.hero{display:flex;flex-wrap:wrap;gap:32px;align-items:stretch}
.hero .lead{flex:999 1 640px;min-width:0;display:flex;flex-direction:column;gap:22px}
.hero .meta{display:flex;flex-wrap:wrap;align-items:center;gap:12px;font-size:14px;color:var(--muted)}
h1{margin:0;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:clamp(46px,7vw,96px);line-height:.96;text-transform:uppercase;text-wrap:balance}
.deck{margin:0;font-size:22px;line-height:1.5;color:var(--soft);max-width:780px;text-wrap:pretty}
.method{margin:0;font-size:14px;line-height:1.55;color:var(--muted);max-width:780px}
.badge2{display:inline-flex;align-items:center;font-size:14px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#fff;background:var(--alert);border-radius:4px;padding:6px 12px}
.badge2.calm{background:#1F6F4A}.badge2.nodata{background:#4A5568}
.badge2 .dot{margin-right:8px}
.score{flex:1 1 380px;min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:22px 26px 10px;display:flex;flex-direction:column;gap:4px}
.cap{font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.score{container:score/inline-size}
.srow{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;padding:16px 0;border-top:1px solid var(--line);align-items:center}
.srow .who{display:flex;flex-direction:row;flex-wrap:wrap;align-items:center;gap:4px 12px;min-width:0}
.srow .code{font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:34px;line-height:1}
.srow .nm{font-size:12px;line-height:1.2;color:var(--muted)}
.srow dl{margin:0;display:grid;grid-template-columns:minmax(0,1fr) auto 52px;gap:8px 12px;align-items:center;font-size:14px}
.srow dt{color:var(--muted);white-space:nowrap}
.srow dd{margin:0;white-space:nowrap}
.srow dd.v{text-align:right;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace}
.srow dd.r{display:flex;justify-content:flex-end}
@container score (min-width:460px){.srow{grid-template-columns:96px minmax(0,1fr);gap:14px 18px}.srow .who{flex-direction:column;align-items:flex-start;gap:5px}.srow .code{font-size:44px}}
.score .foot{font-size:12px;color:var(--muted);padding:6px 0 12px;border-top:1px solid var(--line)}
.sh{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.sh h2{margin:0;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:30px;text-transform:uppercase;letter-spacing:.02em}
.sh .rule{flex:1 1 40px;height:1px;background:var(--line2)}
.block{display:flex;flex-direction:column;gap:16px}
.top3{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:16px}
.hl{display:flex;flex-direction:column;gap:10px;padding:18px 22px 22px;background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--amber);border-radius:6px;animation:fade .6s ease-out both}
.hl .kick{display:flex;align-items:baseline;gap:12px}
.hl .n{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:13px;color:var(--amber)}
.hl .k{font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.hl p{margin:0;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:600;font-size:26px;line-height:1.2;text-wrap:pretty}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:16px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:20px 22px;display:flex;flex-direction:column;gap:6px;animation:fade .6s ease-out both}
.tile .v{font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:48px;line-height:1}
.tile .s{font-size:13px;color:var(--muted)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:22px 26px;margin:0;overflow-x:auto}
.card[tabindex="0"]:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
.card h2{margin:0 0 6px;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:24px;text-transform:uppercase;letter-spacing:.02em}
.card .sub,.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.card b{color:var(--text)}
.chip{display:inline-block;font-family:Inter,"Segoe UI",Arial,sans-serif;font-size:11px;font-weight:700;border-radius:4px;padding:2px 8px;margin-left:8px;vertical-align:middle;letter-spacing:.06em;text-transform:uppercase}
.chip.code{background:rgba(94,208,195,.14);color:var(--teal)}.chip.ai{background:rgba(196,181,253,.16);color:var(--violet)}
.chip.check{background:rgba(125,217,160,.14);color:var(--green)}.chip.watch{background:rgba(246,183,60,.16);color:var(--amber)}
ul.clean{margin:0;padding-left:20px}ul.clean li{margin:7px 0;color:var(--soft)}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;padding:8px 10px;border-bottom:2px solid var(--line2)}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top;color:var(--soft)}
td b{color:var(--text)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.rank{display:inline-block;min-width:52px;text-align:center;border-radius:4px;padding:1px 6px;font-weight:700;font-size:13px;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace}
.rank.hi{background:rgba(229,72,63,.2);color:#FF9C94}.rank.mid{background:rgba(246,183,60,.16);color:var(--amber)}.rank.lo{background:#1A2231;color:var(--muted)}
.badge{font-weight:700;font-size:12px;border-radius:4px;padding:2px 8px}
.badge.ALERT{background:rgba(229,72,63,.2);color:#FF9C94}.badge.NEAR{background:rgba(246,183,60,.16);color:var(--amber)}
.badge.CALM{background:rgba(125,217,160,.14);color:var(--green)}.badge.NODATA{background:#2A3446;color:var(--muted)}
.badge.PARTIAL{background:rgba(246,183,60,.16);color:var(--amber)}.live.partial,.badge2.partial{background:#9A6414}
.row2{display:flex;flex-wrap:wrap;gap:16px;align-items:stretch}
.panel{flex:1 1 560px;min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:22px 24px 20px;display:flex;flex-direction:column;gap:6px}
.panel h3{margin:0;font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--text)}
.panel .sub{margin:0 0 6px}
.chart{min-height:300px;color:var(--muted);font-size:13px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 2px}
.tabs button{min-height:32px;padding:4px 14px;border-radius:999px;border:1px solid #3A465C;background:transparent;color:var(--soft);font:600 13px Inter,"Segoe UI",Arial,sans-serif;cursor:pointer}
.tabs button:hover{border-color:var(--amber)}
.tabs button[aria-pressed=true]{background:var(--amber);border-color:var(--amber);color:#0A0E16;font-weight:700}
.tabs button:focus-visible{outline:3px solid #8FE3D8;outline-offset:2px}
.aicard{border-top:3px solid var(--violet)}
.aicard blockquote{margin:10px 0;padding:0;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:600;font-size:30px;line-height:1.2;color:var(--text)}
.aicard p{color:var(--soft)}
.facts{display:flex;flex-wrap:wrap;gap:8px 22px;font-size:13px;color:var(--muted);margin:8px 0}
.q{margin:6px 0 12px;padding:14px 18px;border-left:4px solid var(--teal);background:rgba(94,208,195,.08);border-radius:4px;font-size:16px;font-weight:500;color:var(--text)}
.muted{color:var(--muted);font-size:12px}
.legend{display:flex;flex-wrap:wrap;gap:10px 20px;font-size:13px;color:var(--muted)}
.st{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));gap:16px}
.st .tile .v{font-size:44px}
.qs{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:16px}
.qs .tile{gap:8px}.qs .qn{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:13px;color:var(--amber)}
.qs h4{margin:0;font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:600;font-size:24px;line-height:1.15}
.qs p{margin:0;font-size:14px;color:var(--soft)}
.callout{font-family:"Barlow Condensed","Arial Narrow",sans-serif;font-weight:700;font-size:clamp(30px,4vw,44px);line-height:1.05;text-transform:uppercase;margin:0}
footer{border-top:1px solid var(--line);padding:26px 0 40px;font-size:12px;color:var(--muted);line-height:1.6}
footer .wrap{display:flex;flex-direction:column;gap:12px}
footer .by{font-size:15px;color:var(--soft)}
footer .by .credit{font-size:18px}
@keyframes tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@keyframes fade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes sheen{0%{background-position:200% 0}100%{background-position:-200% 0}}
@keyframes byline{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}
.live .dot,.badge2 .dot{animation:pulse 1.8s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){.track,.hl,.tile,.byline,.live .dot,.badge2 .dot{animation:none}.credit{animation:none;background:none;-webkit-text-fill-color:var(--text);color:var(--text)}}
@media (max-width:640px){.wrap{padding:0 18px}.deck{font-size:18px}.card{padding:18px}table{font-size:13px}}
@media print{body{background:#fff;color:#111}.track{animation:none}.card,.tile,.hl,.score,.panel{break-inside:avoid}}
"""

ATTACHMENT_NAME = "crypto-watch-executive-brief.html"
LAUNCHER = "start_ask_page.bat"


def public_url(name):
    """An https address the owner has published, from an environment variable (a GitHub repository variable in the workflow).
    Anything else, including an empty value, is ignored, so the email never carries a link that cannot work."""
    value = os.environ.get(name, "").strip()
    return value if value.startswith("https://") and len(value) <= 300 and not any(ch.isspace() or ch in '"<>' for ch in value) else None
FONTS_URL = ("https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Inter:wght@400;500;600;700"
             "&family=JetBrains+Mono:wght@500;700&display=swap")

def esc(value):
    return html.escape(str(value), quote=True)


def coin_name(coin):
    return insights.NAMES.get(coin, coin)


NUMBER_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


def rarity(c, metric):
    """(days as extreme or more, days ranked) for one metric over the coin's whole history, today included. None without the candles."""
    closes = c.get("closes")
    if not closes:
        return None
    series, _ = agent.metric_series(closes)
    values = [abs(v) for v in series[metric] if v is not None]
    if not values:
        return None
    return sum(1 for v in values if v >= values[-1]), len(values)


def history_days(data):
    return max((c["summary"]["history_days"] for c in good(data)), default=0)


def hero_title(data):
    if not good(data):
        return "Market data unavailable"
    names = []
    for _, coin, _, _ in insights.breaches(data["coins"]):
        if coin not in names:
            names.append(coin)
    if not names:
        down = insights.unavailable(data["coins"])
        if down:
            return f"Partial data: {_join_and(down)} unavailable, no other coin at its alert rank"
        return "Calm market: no coin at its alert rank"
    if len(names) == 1:
        return f"{coin_name(names[0])} breaches its own history"
    return f"{NUMBER_WORDS.get(len(names), len(names))} coins breach their own history"


def _join_and(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def hero_deck(data):
    """Two or three sentences, every number from the data."""
    coins = data["coins"]
    if not good(data):
        return "Kraken did not return complete, current candles for any coin, so no statement is made about the market today."
    hits = insights.breaches(coins)
    down = insights.unavailable(coins)
    gone = f"{_join_and(down)} {'is' if len(down) == 1 else 'are'} unavailable today, so no statement is made for {'it' if len(down) == 1 else 'them'}." if down else ""
    parts = []
    if hits:
        rank, coin, metric, s = hits[0]
        lead = f"{coin_name(coin)} {insights._phrase(metric, s)}"
        seen = rarity(next(c for c in good(data) if c["coin"] == coin), metric)
        if seen:
            lead += f", a reading matched or exceeded on only {seen[0]} of {seen[1]} days in its history, today included"
        parts.append(lead + ".")
        per_coin = {}
        for _, other, m, _ in hits:
            if (other, m) != (coin, metric) and m not in per_coin.setdefault(other, []):
                per_coin[other].append(m)
        by_metrics = {}
        for other, metrics in per_coin.items():
            by_metrics.setdefault(tuple(metrics), []).append(other)
        sentences = [f"{' and '.join(names)} {'is' if len(names) == 1 else 'are'} also at the alert rank on {_join_and([insights.METRIC_TEXT[m] for m in metrics])}"
                     for metrics, names in by_metrics.items()]
        if sentences:
            parts.append("; ".join(sentences[:2]) + ".")
        if gone:
            parts.append(gone)
    else:
        closest = sorted(((max(c["summary"]["ranks"].values()), c["coin"], c) for c in good(data)), key=lambda t: (-t[0], t[1]))[0]
        metric = max(closest[2]["summary"]["ranks"], key=closest[2]["summary"]["ranks"].get)
        if gone:
            parts.append(gone)
        parts.append(f"No {'other ' if down else ''}coin reached the alert rank of {agent.ALERT_RANK:g}. The closest is {closest[1]} at rank {closest[0]:.1f} on {insights.METRIC_TEXT[metric]}.")
    volume = insights._volume_fact(coins)
    if volume:
        lo, hi = min(r for _, r in volume), max(r for _, r in volume)
        span = f"{lo:.2f} times" if abs(hi - lo) < 0.005 else f"{lo:.2f} to {hi:.2f} times"
        parts.append(f"Volume runs at {span} its 30-day average.")
    mood = insights._mood(data.get("context"))
    if mood:
        parts.append(f"Fear and Greed reads {mood['value']} ({mood['label']}).")
    return " ".join(parts)


def rank_cell(rank):
    cls = "hi" if rank >= agent.ALERT_RANK else "mid" if rank >= NEAR_RANK else "lo"
    return f'<span class="rank {cls}">{rank:.1f}</span>'


def coin_status(summary):
    if summary["breaches"]:
        return "ALERT"
    if any(NEAR_RANK <= r < agent.ALERT_RANK for r in summary["ranks"].values()):
        return "NEAR"
    return "CALM"


def tone_class(tone):
    return {"red": "", "green": " calm", "grey": " nodata", "amber": " partial"}[tone]


def brand_bar(data, as_of):
    label, tone, _ = status_of(data)
    shown = {"ALERT": "Alert", "CALM": "Calm", "NO DATA": "No data", "PARTIAL": "Partial data"}[label]
    return (f'<div class="bar"><div class="wrap"><div class="brand"><span class="logo">Crypto Watch</span>'
            f'<span class="live{tone_class(tone)}"><span class="dot"></span>{shown}</span>'
            f'<span class="byline">Created by <span class="credit">{esc(CREDIT)}</span></span></div>'
            f'<div class="right"><span>{esc(data["generated_long"])}</span><span>Data to the {esc(as_of)} UTC close</span>'
            f'{ask_link()}</div></div></div>')


def ask_link():
    """The brand-bar link: opens the Ask tab inside this same file (no server, no key, no AI)."""
    return asktab.link()


def ticker_items(data):
    items = []
    for c in good(data):
        s = c["summary"]
        up = s["daily_return_pct"] >= 0
        arrow = "&#9650;" if up else "&#9660;"
        items.append((c["coin"], f'{_price(s["last_close"])} {arrow} {abs(s["daily_return_pct"]):.2f}%', "pos" if up else "neg"))
    fng = insights._mood(data.get("context"))
    if fng:
        items.append(("Fear and Greed", f'{fng["value"]} {esc(fng["label"])}', "amb"))
    btc_iv = (((data.get("context") or {}).get("coins") or {}).get("BTC") or {}).get("implied_volatility")
    if sources.is_data(btc_iv):
        items.append(("BTC implied vol", f'{btc_iv["implied_vol_pct"]:.2f}%', ""))
    for c in good(data):
        ctx = c["summary"].get("context") or {}
        if ctx.get("volume_ratio_30d") is not None:
            items.append((f'{c["coin"]} volume', f'{ctx["volume_ratio_30d"]:.2f}&times; 30-day avg', ""))
    return items


def ticker(data):
    items = ticker_items(data)
    if not items:
        return ""
    one = "".join(f'<span class="tick"><b>{esc(label)}</b><span class="{cls}">{value}</span></span>' for label, value, cls in items)
    return (f'<div class="ticker"><div class="wrap"><span class="tag">Markets</span><div class="view"><div class="track">'
            f'<div class="set">{one}</div><div class="set" aria-hidden="true">{one}</div></div></div></div></div>')


SCORE_LABELS = {"volatility": "Volatility", "distance": "Vs 30-day avg", "drawdown": "Drawdown"}


def scoreboard(data):
    rows = []
    for c in data["coins"]:
        if "error" in c:
            rows.append(f'<div class="srow"><div class="who"><div class="code">{esc(c["coin"])}</div></div><div class="muted">Data unavailable: {esc(c["error"])}. No statement is made for this coin.</div></div>')
            continue
        s = c["summary"]
        status = coin_status(s)
        lines = "".join(f'<dt>{esc(SCORE_LABELS[m])}</dt><dd class="v">{esc(insights.metric_value(s, m))}</dd><dd class="r">{rank_cell(s["ranks"][m])}</dd>'
                        for m in agent.METRICS)
        rows.append(f'<div class="srow"><div class="who"><div class="code">{esc(c["coin"])}</div><div class="nm">{esc(coin_name(c["coin"]))}</div>'
                    f'<span class="badge {status}">{status}</span></div><dl>{lines}</dl></div>')
    return ('<div class="score"><span class="cap">Breach scoreboard</span>' + "".join(rows) +
            f'<span class="foot">Alert at rank {agent.ALERT_RANK:g} or higher. Near threshold from {NEAR_RANK:g} up to {agent.ALERT_RANK:g}.</span></div>')


def hero(data, as_of):
    label, tone, _ = status_of(data)
    shown = {"ALERT": "Alert", "CALM": "Calm market", "NO DATA": "No data", "PARTIAL": "Partial data"}[label]
    demo = '<span class="badge2" style="background:#A06A1A">Demo data: synthetic prices</span>' if data.get("demo") else ""
    return (f'<div class="hero"><div class="lead"><div class="meta"><span class="badge2{tone_class(tone)}"><span class="dot"></span>{shown}</span>{demo}'
            f'<span>Data as of {esc(as_of)} (latest completed UTC day) &middot; generated {esc(data["generated"])} UTC</span></div>'
            f'<h1>{esc(hero_title(data))}</h1><p class="deck">{esc(hero_deck(data))}</p>'
            f'<p class="method">Each coin is ranked against its own past {history_days(data)} days. This is a description of how unusual today is, not a forecast of what comes next.</p></div>'
            f'{scoreboard(data)}</div>')


def section(title, body, chip=""):
    return f'<section class="block"><div class="sh"><h2>{esc(title)}</h2>{chip}<span class="rule"></span></div>{body}</section>'


def top3_section(data):
    cards = "".join(f'<div class="hl" style="animation-delay:{i * 120}ms"><div class="kick"><span class="n">0{i + 1}</span><span class="k">{esc(item["kicker"])}</span></div>'
                    f'<p>{esc(item["text"])}</p></div>' for i, item in enumerate(insights.top_headlines(data["coins"], data.get("context"))))
    return section("Top 3 headlines", f'<div class="top3">{cards}</div>', '<span class="chip code">Computed by code</span>')


def tile_list(data):
    """The four headline numbers as (label, value, sub-line, tone). The page and the email show the same ones."""
    ok = good(data)
    if not ok:
        return []
    events = data.get("events")
    down = insights.unavailable(data["coins"])   # a coin with no data is not counted as checked, and is named under the number
    tiles = [("Coins at the alert rank", f"{len(breaching(data))} of {len(ok)}",
              f"Alert rank {agent.ALERT_RANK:g}, near threshold {NEAR_RANK:g}" + (f". {_join_and(down)} unavailable" if down else ""), "")]
    widest = max(ok, key=lambda c: abs(c["summary"]["dist_from_30d_avg_pct"]))
    gap = widest["summary"]["dist_from_30d_avg_pct"]
    tiles.append(("Widest gap to average", f"{gap:+.2f}%", f"{widest['coin']} against its own 30-day average", "neg" if gap < 0 else "pos"))
    mood = insights._mood(data.get("context"))
    if mood:
        tiles.append(("Fear and Greed", f"{mood['value']}", f"{mood['label']}, 30-day mean {mood['mean_prior_30d']:.0f}", "amb"))
    else:
        top = max(ok, key=lambda c: (c["summary"].get("context") or {}).get("volume_ratio_30d") or 0)
        ratio = (top["summary"].get("context") or {}).get("volume_ratio_30d")
        if ratio:
            tiles.append(("Volume vs 30-day average", f"{ratio:.2f}x", f"{top['coin']}, the highest of the coins", ""))
    runs = {coin: run for coin, run in ((events or {}).get("current_run") or {}).items() if run}
    if runs:
        coin, run = max(runs.items(), key=lambda kv: (kv[1], kv[0]))
        typical = f"the typical episode lasts {events['run_median']:g} days" if events.get("run_median") else "past episodes are in the table below"
        tiles.append(("Longest current run", f"Day {run}", f"{coin} at the alert rank; {typical}", "amb"))
    else:
        tiles.append(("Data reliability", f"{len(ok)} of {len(data['coins'])}", "coins with complete, current Kraken candles", ""))
    return tiles


def stat_tiles(data):
    tiles = tile_list(data)
    if not tiles:
        return ""
    body = "".join(f'<div class="tile" style="animation-delay:{i * 100}ms"><span class="cap">{esc(k)}</span><span class="v {cls}">{esc(v)}</span><span class="s">{esc(sub)}</span></div>'
                   for i, (k, v, sub, cls) in enumerate(tiles))
    return f'<div class="tiles">{body}</div>'


def chart_panel(key, title, sub, specs):
    if key not in specs:
        return ""
    return f'<div class="panel"><h3>{esc(title)}</h3><p class="sub">{esc(sub)}</p><div class="chart" data-chart="{key}"></div></div>'


def charts_section(data):
    ok = good(data)
    if not ok:
        return ""
    specs = charts(data)
    lead = lead_coin(ok)["coin"]
    first = (chart_panel("average", f"{lead} against its own 30-day average", "The shaded gap is the distance metric. Buttons: 60, 120 or 240 days. Hover for values.", specs)
             + chart_panel("price", "Price, indexed to 100", "Every coin starts at 100 at the start of the range you pick (30, 60 or 90 days). Click a name in the legend to hide it.", specs))
    second = (chart_panel("ranks", "Today against each coin's own history", "Rank 0 to 100: 95 means higher than about 95% of the coin's own days.", specs)
              + chart_panel("history", "How unusual each coin has been", "Highest of the three ranks, last 60 days. Above the red line is the alert rank.", specs))
    rows = "".join(f'<div class="row2">{row}</div>' for row in (first, second) if row.strip())
    return section("The charts", rows, '<span class="chip code">Computed by code</span><span class="chip watch">Interactive: hover, zoom, drag</span>')


def question_section():
    coins = " &middot; ".join(esc(c) for c in agent.WATCHLIST)
    return (
        '<section class="card"><h2>The question this agent answers</h2>'
        f'<blockquote class="q">{esc(MONITORING_QUESTION)}</blockquote>'
        '<p class="sub"><b>What the agent answers each day today:</b> ' + esc(agent.QUESTION) + ' '
        'Trend, volume, relative strength and the correlation between coins are context: the agent describes them and they never raise an alert on their own. '
        'Forward scenarios are shown below as conditions to monitor, not predictions. The agent makes no forecast, by design.</p>'
        f'<div class="facts"><span>Watchlist: {coins} (USD pairs)</span><span>Data: Kraken daily candles, latest completed UTC day</span>'
        f'<span>Cadence: once a day, 05:17 UTC</span><span>Output: one Sheet row, and this brief by email every day</span></div></section>')


def snapshot_table(data):
    rows = []
    for c in data["coins"]:
        if "error" in c:
            rows.append(f'<tr><td><b>{esc(c["coin"])}</b></td><td colspan="5">Data unavailable: {esc(c["error"])}. No statement is made for this coin.</td>'
                        f'<td><span class="badge NODATA">NO DATA</span></td></tr>')
            continue
        s = c["summary"]
        status = coin_status(s)
        rows.append(
            f'<tr><td><b>{esc(c["coin"])}</b><br><span style="color:#718299;font-size:12px">{esc(s["pair"])}</span></td>'
            f'<td class="num">${s["last_close"]:,.4f}</td><td class="num">{s["daily_return_pct"]:+.2f}%</td>'
            f'<td class="num">{s["volatility_30d_daily_pct"]:.2f}% {rank_cell(s["ranks"]["volatility"])}</td>'
            f'<td class="num">{s["dist_from_30d_avg_pct"]:+.2f}% {rank_cell(s["ranks"]["distance"])}</td>'
            f'<td class="num">{s["max_drawdown_30d_pct"]:.2f}% {rank_cell(s["ranks"]["drawdown"])}</td>'
            f'<td><span class="badge {status}">{status}</span></td></tr>')
    head = ('<tr><th>Coin</th><th class="num">Last close</th><th class="num">Day</th><th class="num">Volatility (rank)</th>'
            '<th class="num">Vs 30-day average (rank)</th><th class="num">Max drawdown (rank)</th><th>Status</th></tr>')
    return f"<table>{head}{''.join(rows)}</table>"


def scenario_rows():
    """What the agent does in each situation. Each row has a scenario test in scenarios.py, so the rules are checked, not only described."""
    return [
        ("Calm market", f"Every rank is below {NEAR_RANK:g}.", "No alert."),
        ("Unusual move", f"Any rank of volatility, distance from the 30-day average or drawdown is {agent.ALERT_RANK:g} or higher.",
         f"One brief alert (at most {agent.MAX_MESSAGE_CHARS} characters) that names every coin involved and cites the playbook rule."),
        ("Near the threshold", f"A rank from {NEAR_RANK:g} up to, but not including, {agent.ALERT_RANK:g}.", "No alert. It may be mentioned as near threshold."),
        ("Already alerted", f"The coin is in the agent's last {agent.HISTORY_LIMIT} saved decisions.", "No repeat alert for that coin."),
        ("Data problem", f"Kraken is unreachable, a candle is stale or missing, or fewer than {agent.MIN_HISTORY_DAYS} days exist.",
         "No alert for that coin. The agent says the data was unavailable and never estimates a number."),
        ("Instructions in the data", "Text in the data, such as forum chatter, pushes the agent to give trading instructions.", "Blocked by code. Alerts only describe numbers and never tell anyone what to do."),
        ("One coin down", "One coin has a data problem while the others are calm.", "No alert. The failure is reported, and the other coins are still assessed."),
        ("Context without a price move", "Volume, relative strength or a correlation is unusual, but no price rank is at the alert rank.",
         "No alert. The context is described in the brief and never raises an alert on its own."),
        ("Several coins at once", "Two or more coins reach the alert rank on the same day.", "One brief alert that names every coin involved. A coin that is not at the alert rank is never named."),
    ]


def scenario_section():
    rows = "".join(f"<tr><td><b>{esc(a)}</b></td><td>{esc(b)}</td><td>{esc(c)}</td></tr>" for a, b, c in scenario_rows())
    return ('<section class="card" tabindex="0" role="region" aria-label="Agent situations table, scrolls sideways"><h2>What the agent does in each situation<span class="chip check">EACH ROW IS A SCENARIO TEST</span></h2>'
            '<p class="sub">The AI recommends and the code decides. These rules are in the playbook, and every row has a test that runs on each code change.</p>'
            f'<table><tr><th>Situation</th><th>Trigger</th><th>What the agent does</th></tr>{rows}</table></section>')


def ai_section(data):
    decision = latest_decision(data)
    chips = '<span class="chip ai">WRITTEN BY AI</span>'
    if decision is None:
        note = data.get("decisions_note") or "No decision has been saved to the Sheet yet."
        return (f'<section class="card aicard"><h2>AI analysis{chips}</h2><p class="sub">{esc(note)}</p></section>')
    blocked = str(decision.get("blocked_reason") or "")
    verdict = (f'<span class="chip check">PASSED THE CODE GUARDRAILS</span>' if not blocked
               else f'<span class="chip watch">BLOCKED BY A GUARDRAIL</span>')
    message = decision.get("message") or "(no message: the agent saw nothing to alert on, or a guardrail removed it)"
    reasoning = decision.get("reasoning") or "Not available. Update apps_script/Code.gs and redeploy it to include the AI's reasoning here."
    sources = ", ".join(decision.get("sources") or []) or "none"
    cross = agreement(data)
    cross_html = ""
    if cross is not None:
        mark = "Consistent" if cross[0] else "Different"
        cross_html = f'<p><b>Cross-check by code: {mark}.</b> {esc(cross[1])}</p>'
    return (
        f'<section class="card aicard"><h2>AI analysis{chips}{verdict}</h2>'
        f'<p class="sub">The agent is an AI model that recommends; the code decides what is allowed through. This is its latest saved decision.</p>'
        f'<blockquote>{esc(message)}</blockquote>'
        f'<p><b>Reasoning given by the AI:</b> {esc(reasoning)}</p>'
        f'<p><b>Playbook rules cited:</b> {esc(sources)}</p>'
        f'{cross_html}'
        + (f'<p><b>Guardrail result:</b> {esc(blocked)}</p>' if blocked else '')
        + f'<div class="facts"><span>Model: {esc(agent.MODEL)} on Groq</span><span>Version: {esc(decision.get("version") or "unknown")}</span>'
          f'<span>Decided: {esc(decision.get("time") or "unknown")}</span><span>Alert: {"yes" if decision.get("alert") else "no"}</span></div></section>')


def decision_table(data):
    rows = []
    for d in reversed(data["decisions"]):
        coins = ", ".join(d.get("coins") or []) or "none"
        message = str(d.get("message") or "")
        short = message if len(message) <= 150 else message[:147] + "..."
        rows.append(f'<tr><td>{esc(d.get("time", ""))}</td><td>{"<b>ALERT</b>" if d.get("alert") else "no alert"}</td>'
                    f'<td>{esc(coins)}</td><td>{esc(short)}</td><td>{esc(", ".join(d.get("sources") or []))}</td></tr>')
    if not rows:
        return '<p class="sub">No saved decisions yet.</p>'
    return ('<table><tr><th>Time (UTC)</th><th>Result</th><th>Coins</th><th>Message</th><th>Sources</th></tr>' + "".join(rows) + "</table>")


def rank_cell_two_sided(rank):
    cls = "hi" if rank >= agent.ALERT_RANK or rank <= agent.UNUSUAL_LOW else "mid" if rank >= NEAR_RANK or rank <= 100 - NEAR_RANK else "lo"
    return f'<span class="rank {cls}">{rank:.1f}</span>'


def context_section(data):
    cross = data.get("cross") or {}
    strength = {x["coin"]: x for x in cross.get("relative_strength", [])}
    rows = []
    for c in good(data):
        ctx = c["summary"].get("context")
        if not ctx:
            continue
        volume = (f'{ctx["volume_ratio_30d"]:.2f}x {rank_cell(ctx["volume_rank"])}' if "volume_rank" in ctx else "n/a")
        rs = strength.get(c["coin"])
        rs_text = (f'{rs["vs_group_pct"]:+.2f} pts {rank_cell_two_sided(rs["rank"])}' if rs else "n/a")
        rows.append(f'<tr><td><b>{esc(c["coin"])}</b></td><td>{esc(ctx["trend"])}</td>'
                    f'<td class="num">{ctx["ret_30d_pct"]:+.2f}% / {ctx["ret_60d_pct"]:+.2f}%</td>'
                    f'<td class="num">{volume}</td><td class="num">{rs_text}</td></tr>')
    if not rows:
        return ""
    table = ('<table><tr><th>Coin</th><th>Trend</th><th class="num">Change 30d / 60d</th><th class="num">Volume vs 30-day average (rank)</th>'
             '<th class="num">Relative strength vs group (rank)</th></tr>' + "".join(rows) + "</table>")
    pairs = ""
    if cross.get("correlations"):
        flagged = set(cross.get("flags", []))
        body = "".join(f'<tr><td><b>{esc(x["pair"])}</b></td><td class="num">{x["corr_30d"]:+.2f}</td><td class="num">{rank_cell_two_sided(x["rank"])}</td>'
                       f'<td>{"unusual" if "correlation " + x["pair"] in flagged else "within its normal range"}</td></tr>' for x in cross["correlations"])
        pairs = ('<p class="sub" style="margin-top:14px"><b>How the coins move together</b> (correlation of the last 30 daily returns, ranked against the pair\'s own history; '
                 'near 100 is unusually tight, near 0 unusually loose).</p>'
                 f'<table><tr><th>Pair</th><th class="num">Correlation</th><th class="num">Rank</th><th>Reading</th></tr>{body}</table>')
    elif cross.get("error"):
        pairs = f'<p class="sub">Relationships unavailable: {esc(cross["error"])}.</p>'
    return ('<section class="card" tabindex="0" role="region" aria-label="Trend, volume and relationships tables, scrolls sideways"><h2>Context: trend, volume and relationships<span class="chip code">COMPUTED BY CODE</span></h2>'
            '<p class="sub">These describe the past and never raise an alert on their own. Volume is the coin\'s traded volume on Kraken, not market-wide volume. '
            f'Relative strength is a coin\'s 30-day return minus the group average.</p>{table}{pairs}</section>')


def _usd(value):
    """$14.2M, $6.8B, $950K: the dollar figures of the order book and open interest are read at a glance."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"${value / limit:,.1f}{suffix}"
    return f"${value:,.0f}"


def _missing(piece, fallback="unavailable"):
    reason = piece.get("unavailable", fallback) if isinstance(piece, dict) else fallback
    return f'<span class="muted">unavailable: {esc(reason)}</span>'


def market_context_section(data):
    """What the other free sources say about each coin today. Description only: none of it raises an alert."""
    context = data.get("context") or {}
    if "error" in context or not context.get("coins"):
        if context.get("error"):
            return f'<section class="card"><h2>Market context<span class="chip code">COMPUTED BY CODE</span></h2><p class="sub">Unavailable: {esc(context["error"])}.</p></section>'
        return ""
    rows = []
    for coin, piece in context["coins"].items():
        liq, der, iv, wide, check = (piece.get(k) for k in ("liquidity", "derivatives", "implied_volatility", "market_wide", "price_check"))
        if sources.is_data(liq):
            liq_html = (f'spread {liq["spread_pct"]:.4f}%<br><span class="muted">bid {_usd(liq["bid_depth_usd"])} / '
                        f'ask {_usd(liq["ask_depth_usd"])} within {liq["depth_band_pct"]:g}% of the price</span>')
        else:
            liq_html = _missing(liq)
        if sources.is_data(der):
            oi = _usd(der["open_interest_usd_approx"]) if der.get("open_interest_usd_approx") else f'{der["open_interest"]:,.0f} contracts'
            der_html = (f'{der["funding_rate_pct_per_hour"]:+.4f}% per hour<br><span class="muted">{esc(der["funding_pays"])}, open interest {oi}</span>')
        else:
            der_html = _missing(der)
        if sources.is_data(iv):
            rank = rank_cell(iv["rank_1y"]) if iv.get("rank_1y") is not None else f'<span class="muted">no rank yet ({iv["history_days"]} days)</span>'
            iv_html = f'{iv["implied_vol_pct"]:.1f}% annualised<br>{rank} <span class="muted">vs past year</span>'
        else:
            iv_html = _missing(iv)
        if sources.is_data(wide) and "market_volume_rank" in wide:
            wide_html = (f'{_usd(wide["volume_24h_usd"])} in 24h<br>{wide["market_volume_ratio_30d"]:.2f}x its 30-day average '
                         f'{rank_cell(wide["market_volume_rank"])}')
        elif sources.is_data(wide):
            wide_html = f'{_usd(wide["volume_24h_usd"])} in 24h<br>{_missing({"unavailable": wide.get("volume_history", "history unavailable")})}'
        else:
            wide_html = _missing(wide)
        if sources.is_data(check):
            tone = "lo" if check["agree"] else "hi"
            check_html = (f'<span class="rank {tone}">{check["difference_pct"]:+.2f}%</span> '
                          f'<span class="muted">{"agree" if check["agree"] else "differ"}</span><br>'
                          f'<span class="muted">Kraken {_price(check["kraken_mid_usd"])} vs CoinGecko {_price(check["coingecko_price_usd"])}</span>')
        else:
            check_html = _missing(check)
        rows.append(f'<tr><td><b>{esc(coin)}</b></td><td>{liq_html}</td><td>{der_html}</td><td>{iv_html}</td><td>{wide_html}</td><td>{check_html}</td></tr>')
    table = ('<table><tr><th>Coin</th><th>Liquidity (Kraken order book)</th><th>Funding and open interest (Kraken Futures)</th>'
             '<th>Implied volatility (Deribit DVOL)</th><th>Market-wide volume (CoinGecko)</th><th>Price check</th></tr>' + "".join(rows) + "</table>")
    fng = context.get("sentiment")
    if sources.is_data(fng):
        mood = (f'<p class="sub" style="margin-top:14px"><b>Fear and greed index (whole market):</b> {fng["value"]} of 100, {esc(fng["label"])}; '
                f'{fng["change_vs_yesterday"]:+d} since yesterday, 30-day mean {fng["mean_prior_30d"]:.0f}.</p>')
    else:
        mood = f'<p class="sub" style="margin-top:14px"><b>Fear and greed index:</b> {_missing(fng)}</p>'
    flags = context.get("flags") or []
    flag_html = (f'<p class="sub"><b>Unusual today:</b> {esc("; ".join(flags))}.</p>' if flags
                 else '<p class="sub"><b>Unusual today:</b> nothing among the sources that responded.</p>')
    return ('<section class="card" tabindex="0" role="region" aria-label="Market context table, scrolls sideways"><h2>Market context: liquidity, derivatives, implied volatility, sentiment<span class="chip code">COMPUTED BY CODE</span></h2>'
            '<p class="sub">Extra free sources that describe how the market is positioned. They are context only: they never raise an alert, and a field that cannot be read says so '
            'instead of being estimated. A positive funding rate means longs pay shorts. Implied volatility is what option prices imply, not a forecast.</p>'
            f'{table}{mood}{flag_html}</section>')


def _price(value):
    return f"${value:,.2f}" if value >= 100 else f"${value:,.4f}"


def _level_cell(level, last, side="above"):
    if level == "already":
        return "already at the alert rank"
    if level is None:
        return "not reachable"
    text = f"{_price(level)} ({(level / last - 1) * 100:+.1f}%)"
    if (side == "above" and last >= level) or (side == "below" and last <= level):
        text += ", already beyond"
    return text


def base_rate_section(data):
    ok = [c for c in good(data) if c.get("base_rate") and c["base_rate"]["ranked_days"]]
    if not ok:
        return ""
    rows, union = [], {}
    for c in ok:
        b = c["base_rate"]
        for day, flag in zip(b["days"], b["flags"]):
            union[day] = union.get(day, False) or flag
        last = next((d for d, f in zip(reversed(b["days"]), reversed(b["flags"])) if f), None)
        rows.append(f'<tr><td><b>{esc(c["coin"])}</b></td><td class="num">{b["ranked_days"]}</td><td class="num">{b["alert_days"]}</td>'
                    f'<td class="num">{100.0 * b["alert_days"] / b["ranked_days"]:.1f}%</td><td class="num">{b["episodes"]}</td><td>{esc(last or "never")}</td></tr>')
    flags = [union[d] for d in sorted(union)]
    rows.append(f'<tr><td><b>Any coin</b></td><td class="num">{len(flags)}</td><td class="num">{sum(flags)}</td>'
                f'<td class="num">{100.0 * sum(flags) / len(flags):.1f}%</td><td class="num">{episodes(flags)}</td><td></td></tr>')
    return ('<section class="card" tabindex="0" role="region" aria-label="Alert rank frequency table, scrolls sideways"><h2>How often the alert rank is reached<span class="chip code">COMPUTED BY CODE</span></h2>'
            f'<p class="sub">The same rule run over each coin\'s own history: a day counts when volatility, distance from the 30-day average or drawdown ranks at {agent.ALERT_RANK:g} or higher. '
            'Each day is ranked only against the days before it, so no future data leaks in. Three days in a row are one episode.</p>'
            '<table><tr><th>Coin</th><th class="num">Days ranked</th><th class="num">Days at the alert rank</th><th class="num">Share of days</th>'
            '<th class="num">Separate episodes</th><th>Latest day at the alert rank</th></tr>' + "".join(rows) + '</table>'
            '<p class="sub" style="margin-top:12px"><b>How to read it.</b> A top-5% rule reaches the alert rank on about 5% of days for each metric by construction. '
            'The three metrics move together and a spike stays in the 30-day window, so days cluster into episodes. Because three coins are watched, an alert for some coin is more common than an alert for one coin. '
            'These are in-sample counts on past data and say nothing about what comes next.</p></section>')


def watch_scenarios():
    return [
        ("Stress persists", f"A metric that is at rank {agent.ALERT_RANK:g} or higher stays there after the next completed day.",
         f"The agent stays quiet for that coin: it is in the last {agent.HISTORY_LIMIT} decisions.", f"The rank falls below {NEAR_RANK:g}."),
        ("Back to normal", f"Every rank of a coin is below {NEAR_RANK:g}.", "The coin reads as calm. No alert.", f"Any rank returns to {agent.ALERT_RANK:g} or higher."),
        ("A new breach", "A coin that has not breached closes beyond one of the levels above.", "The next daily run sends one brief alert for that coin and cites the playbook rule.",
         "The close stays between the levels, or the candle or data is not valid."),
    ]


LEVEL_COLUMNS = [("distance", "up", "above"), ("distance", "down", "below"), ("volatility", "up", "above"), ("volatility", "down", "below"), ("drawdown", "down", "below")]


def level_rows(data):
    """(coin, last price text, five level texts) for each coin that has trigger levels. The page and the email show the same ones."""
    rows = []
    for c in good(data):
        lv = c.get("levels")
        if not lv:
            continue
        last = c["summary"]["last_close"]
        cells = []
        for metric, key, side in LEVEL_COLUMNS:
            level = lv[metric][key] if lv.get(metric) and key in lv[metric] else "none"
            cells.append("n/a" if level == "none" else _level_cell(level, last, side))
        rows.append((c["coin"], _price(last), cells))
    return rows


def watch_section(data):
    rows = [f'<tr><td><b>{esc(coin)}</b><br><span class="muted">last {last}</span></td>' + "".join(f'<td class="num">{esc(x)}</td>' for x in cells) + "</tr>"
            for coin, last, cells in level_rows(data)]
    if not rows:
        return ""
    levels = ('<table><tr><th>Coin</th><th class="num">Distance: close above</th><th class="num">Distance: close below</th>'
              '<th class="num">Volatility: close above</th><th class="num">Volatility: close below</th><th class="num">Drawdown: close below</th></tr>'
              + "".join(rows) + "</table>")
    days = max((c["levels"]["history_days"] for c in good(data) if c.get("levels")), default=0)
    scenarios_rows = watch_scenarios()
    body = "".join(f"<tr><td><b>{esc(a)}</b></td><td>{esc(b)}</td><td>{esc(c)}</td><td>{esc(d)}</td></tr>" for a, b, c, d in scenarios_rows)
    return ('<section class="card" tabindex="0" role="region" aria-label="Watch scenarios and levels tables, scrolls sideways"><h2>Conditional watch scenarios<span class="chip watch">CONDITIONS TO MONITOR, NOT PREDICTIONS</span></h2>'
            '<p class="sub">No probability is given and no price is forecast. Each scenario is a condition that can be observed, what the agent does when it happens, '
            'and what would show the scenario no longer applies.</p>'
            f'<table><tr><th>Scenario</th><th>Condition to observe</th><th>What the agent does</th><th>Invalidation</th></tr>{body}</table>'
            '<p class="sub" style="margin-top:14px"><b>Next-day closes that would put a metric at the alert rank.</b> Worked out from each coin\'s own history by code. A close between the two levels of a metric leaves it below the alert rank.</p>'
            f'{levels}'
            f'<p class="sub"><b>Uncertainty.</b> The levels use up to {days} days of each coin\'s own history and assume the 30-day window moves forward by one day. '
            'A new day also shifts the history a little, so read each level as approximate. A rank within five points of the alert rank can cross it in a single day. '
            'A level is no longer valid once the next candle completes, or if Kraken returns incomplete or stale data.</p></section>')


MINUS = "\u2212"


def _sign(v, digits=1):
    text = f"{v:+.{digits}f}"
    if float(text) == 0:
        text = f"{0:.{digits}f}"
    return text.replace("-", MINUS) + "%"


def _band(v):
    """A move for a range: whole percent when large, one decimal when small, so a small negative never reads as -0%."""
    return _sign(v, 0 if abs(v) >= 10 else 1)


def outlook_headline(events):
    """The headline is chosen from the numbers, so it cannot claim something the history does not show."""
    again = events["again"]
    next_day, week = again[0], again[6] if len(again) > 6 else None
    if next_day is None:
        sticky = "Stress is hard to read."
    elif next_day >= 70 and (week or 0) >= 40:
        sticky = "Stress is sticky."
    elif next_day < 50:
        sticky = "Stress fades fast."
    else:
        sticky = "Stress lasts a while."
    after, any_day = events["after_episode"].get(7), events["any_day"].get(7)
    if after and any_day and abs(after["up_pct"] - any_day["up_pct"]) < 15:
        direction = "Direction is not."
    elif after and any_day:
        direction = "Direction differs from other days, on a small sample."
    else:
        direction = ""
    return f"{sticky} {direction}".strip()


def outlook_tiles(events):
    again, tiles = events["again"], []
    if again and again[0] is not None:
        week = f"Falls to {again[6]:.0f}% seven days later" if len(again) > 6 and again[6] is not None else ""
        tiles.append(("Still at the alert rank", f"{again[0]:.0f}%", "of alert days are followed by another one the next day", week))
    if events["run_median"] is not None:
        runs = ", ".join(f"{coin} day {run}" for coin, run in sorted(events["current_run"].items()) if run)
        tiles.append(("Typical episode length", f"{events['run_median']:g} days", f"Mean {events['run_mean']:.1f} days, longest {events['run_max']}",
                      f"Today: {runs}" if runs else "Today: no coin at the alert rank"))
    after7, any7 = events["after_episode"].get(7), events["any_day"].get(7)
    if after7 and any7:
        similar = "a similar share, so no visible edge" if abs(after7["up_pct"] - any7["up_pct"]) < 15 else "a different share, on a small sample"
        tiles.append(("Up after 7 days", f"{after7['up_pct']:.0f}%", f"of coins, 7 days after an episode starts (n={after7['n']})", f"On any day it is {any7['up_pct']:.0f}%, {similar}"))
    after30 = events["after_episode"].get(30)
    if after30:
        tiles.append(("Median 30-day move", _sign(after30["median"]), f"after an episode starts, middle half {_band(after30['q1'])} to {_band(after30['q3'])}",
                      f"Full range {_band(after30['low'])} to {_band(after30['high'])} (n={after30['n']})"))
    return tiles


def reader_questions(data, events):
    """Five questions for the reader, each answered with numbers from today's data. None of them is an instruction."""
    out = []
    runs = {coin: run for coin, run in events["current_run"].items() if run}
    typical = events["run_median"]
    if runs and typical is not None:
        text = ", ".join(f"{coin_name(coin)} is on day {run}" for coin, run in sorted(runs.items())) + f", against a typical episode of {typical:g} days."
        out.append(("Is this a regime or a single-day shock?", text + f" A rank that drops below {NEAR_RANK:g} inside a week would look like how most past episodes ended."))
    else:
        out.append(("Is this a regime or a single-day shock?", "No coin is at the alert rank today, so there is no run to measure."))
    flagged = list((data.get("context") or {}).get("flags") or [])
    flagged += [f"{c['coin']} volume" for c in good(data) if "volume" in c["summary"].get("flags", [])]
    flagged += [f"{c['coin']} price ranks" for c in breaching(data)]
    out.append(("Do the independent signals agree?",
                "Price ranks, volume, funding, implied volatility and sentiment come from different feeds. "
                + (f"Flagged as unusual today: {'; '.join(flagged)}. " if flagged else "None of them is flagged as unusual today. ")
                + "When only one feed is unusual, it is weaker evidence."))
    after = events["after_episode"].get(30)
    if after:
        out.append(("How wide is the range of past outcomes?",
                    f"Thirty days after an episode started, coins ended between {_band(after['low'])} and {_band(after['high'])}, with the middle half from "
                    f"{_band(after['q1'])} to {_band(after['q3'])}. The width matters more than the median."))
    else:
        out.append(("How wide is the range of past outcomes?", "Too few past episodes for a range of outcomes."))
    liq = [(coin, piece.get("liquidity")) for coin, piece in ((data.get("context") or {}).get("coins") or {}).items()]
    liq = [(coin, x) for coin, x in liq if sources.is_data(x)]
    if liq:
        coin, x = liq[0]
        out.append(("Is the market deep enough to absorb size?",
                    f"On Kraken, {coin} shows a spread of {x['spread_pct']:.4f}% with {_usd(x['bid_depth_usd'])} of bids and {_usd(x['ask_depth_usd'])} of asks within "
                    f"{x['depth_band_pct']:g}% of the price. Thin books make the same move cost more."))
    else:
        out.append(("Is the market deep enough to absorb size?", "The order-book feed did not respond today, so depth is not stated."))
    out.append(("What would change the picture?",
                f"Every rank back under {NEAR_RANK:g} reads as calm. A coin that was not at the alert rank and closes beyond its trigger level is a new breach (see the watch scenarios below)."))
    return out


def too_few_note(events):
    return (f'Only {events["episodes"]} past alert episodes exist in this history, fewer than the {insights.MIN_EPISODES} needed before '
            'outcome statistics are shown. Persistence and typical length are not shown either, because they would rest on too few events. Nothing here is a forecast.')


def outlook_summary(data, events):
    return (f'Across {events["episodes"]} alert episodes in the last {history_days(data)} days, the alert rank tends to {"last" if events["again"][0] and events["again"][0] >= 50 else "end quickly"}, '
            'but the price moves that follow are spread widely. The rank describes the regime. It does not call the next move.')


def trust_points(data, events):
    return [
        f"Small sample: {events['episodes']} episodes, and the coins tend to breach together, so there are fewer independent events than it looks.",
        f"{history_days(data)} days of daily data cover one stretch of the market. A different regime can behave differently.",
        "Ranks are relative to each coin's own past, not to the wider market or to fundamentals.",
        "No fees, spreads or slippage are included in any return shown.",
    ]


def outlook_section(data):
    events = data.get("events")
    if not events:
        return ""
    chip = '<span class="chip code">Computed by code</span><span class="chip watch">Past data, not a forecast</span>'
    ok = good(data)
    ranked = max((len((c.get("base_rate") or {}).get("flags") or []) for c in ok), default=0)
    if not events["enough"]:
        note = f'<div class="card"><p class="sub" style="margin:0">{esc(too_few_note(events))}</p></div>'
        return section("What the history says", note, chip)
    tiles = "".join(f'<div class="tile" style="animation-delay:{i * 100}ms"><span class="cap">{esc(k)}</span><span class="v amb">{esc(v)}</span><span class="s">{esc(s1)}</span>'
                    f'<span class="s">{esc(s2)}</span></div>' for i, (k, v, s1, s2) in enumerate(outlook_tiles(events)))
    summary = outlook_summary(data, events)
    charts_row = (
        '<div class="row2">'
        '<div class="panel"><h3>How many days the alert rank lasts</h3><p class="sub">Of all days at the alert rank, the share that is at it again k days later. Pooled over the coins.</p>'
        '<div class="chart" data-chart="persist"></div>'
        f'<p class="sub" style="margin:0">Computed on {ranked} ranked days per coin. Hover for values.</p></div>'
        '<div class="panel"><h3>Where prices went afterwards</h3><p class="sub">Coin returns 7, 14 and 30 days after an episode starts, against any day. Box: middle half. Line: median. Whiskers: full range.</p>'
        '<div class="chart" data-chart="moves"></div>'
        f'<p class="sub" style="margin:0">The boxes overlap almost everywhere. With {(events["after_episode"].get(7) or {}).get("n", 0)} episode starts, and coins that move together, this is a hint about spread, not a model of direction.</p></div>'
        '</div>')
    questions = "".join(f'<div class="tile"><span class="qn">0{i + 1}</span><h4>{esc(q)}</h4><p>{esc(a)}</p></div>' for i, (q, a) in enumerate(reader_questions(data, events)))
    trust = trust_points(data, events)
    body = (f'<p class="callout">{esc(outlook_headline(events))}</p><p class="deck" style="font-size:18px">{esc(summary)}</p>'
            f'<div class="st">{tiles}</div>{charts_row}'
            f'<div class="sh" style="margin-top:8px"><h2 style="font-size:24px">Five questions this data raises</h2><span class="rule"></span></div><div class="qs">{questions}</div>'
            '<p class="sub" style="margin:0">These are questions for the reader, not instructions. The report does not tell anyone to act.</p>'
            '<div class="card"><h2>How far to trust this</h2><ul class="clean">' + "".join(f"<li>{esc(t)}</li>" for t in trust) + "</ul></div>")
    return section("What the history says", body, chip)


def chart_script(specs):
    payload = json.dumps(specs).replace("</", "<\\/")
    return ('<script src="%s"></script><script>(function(){var specs=%s;'
            'var cfg={displayModeBar:"hover",displaylogo:false,responsive:true,modeBarButtonsToRemove:["select2d","lasso2d"]};'
            'document.querySelectorAll(".chart").forEach(function(el){var spec=specs[el.dataset.chart];'
            'if(!spec){return;}if(!window.Plotly){el.textContent="The charts need an internet connection to load plotly.js.";return;}'
            'el.textContent="";Plotly.newPlot(el,spec.data,spec.layout,cfg);'
            'if(spec.ranges&&spec.ranges.length>1){var bar=document.createElement("div");bar.className="tabs";bar.setAttribute("role","group");'
            'bar.setAttribute("aria-label","Range");spec.ranges.forEach(function(r,i){var b=document.createElement("button");b.type="button";'
            'b.textContent=r.label;b.setAttribute("aria-pressed",i===0?"true":"false");b.addEventListener("click",function(){'
            'Plotly.update(el,{visible:r.visible},{"xaxis.autorange":true,"yaxis.autorange":true});'
            'bar.querySelectorAll("button").forEach(function(o){o.setAttribute("aria-pressed",o===b?"true":"false");});});bar.appendChild(b);});'
            'el.parentNode.insertBefore(bar,el);}});})();</script>') % (PLOTLY_URL, payload)


def footer_html():
    return ('<footer><div class="wrap"><div class="by">Created by <span class="credit">' + esc(CREDIT) + '</span></div>'
            '<div><b>Live market-data analysis and decision support. Not investment advice.</b> This report contains no price forecast and makes no recommendation. '
            f'Thresholds are descriptive conventions (rank {agent.ALERT_RANK:g}, 30-day window, daily candles), documented with reasons in playbook.md. '
            'Price data: Kraken public OHLC API. Market context, where available: Kraken order book and Kraken Futures, Deribit volatility index (DVOL), '
            'market-wide volume and prices from <a href="https://www.coingecko.com/en/api">CoinGecko API</a>, and the Fear and Greed Index from '
            '<a href="https://alternative.me/crypto/fear-and-greed-index/">alternative.me</a>. '
            'Generated by the Crypto Watch Agent from its own tools; see the repository README for the method and guardrails.</div></div></footer>')


def build_html(data):
    ok = good(data)
    as_of = max((c["summary"]["as_of"] for c in ok), default="unavailable")
    data = {**data, "generated_long": data.get("generated_long") or data["generated"][:10]}
    head = "".join(f"<li>{esc(line)}</li>" for line in headlines(data))
    watch = "".join(f"<li>{esc(line)}</li>" for line in triggers(data))
    snapshot = (f'<section class="card" tabindex="0" role="region" aria-label="Market snapshot table, scrolls sideways"><h2>Market snapshot<span class="chip code">Computed by code</span></h2>'
                f'<p class="sub">Each value is followed by its rank (0 to 100) against that coin\'s own history. Red is at or above {agent.ALERT_RANK:g}, amber is {NEAR_RANK:g} to below {agent.ALERT_RANK:g}.</p>'
                f'{snapshot_table(data)}</section>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Watch: Executive Intelligence Brief</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="{FONTS_URL}" rel="stylesheet">
<style>{CSS}{asktab.CSS}</style></head><body>
{brand_bar(data, as_of)}
{ticker(data)}
{asktab.tab_bar()}
<main class="wrap" id="panel-report" role="tabpanel" aria-labelledby="tab-report">
{hero(data, as_of)}
{top3_section(data)}
{stat_tiles(data)}
{charts_section(data)}
{ai_section(data)}
{outlook_section(data)}
{question_section()}
<section class="card"><h2>Everything the code found today<span class="chip code">Computed by code</span></h2><ul class="clean">{head}</ul></section>
{snapshot}
{context_section(data)}
{market_context_section(data)}
{base_rate_section(data)}
<section class="card"><h2>What to watch at the next review<span class="chip watch">Monitoring triggers, not forecasts</span></h2><ul class="clean">{watch}</ul></section>
{watch_section(data)}
<section class="card" tabindex="0" role="region" aria-label="Decision history table, scrolls sideways"><h2>Decision history<span class="chip code">From the Sheet</span></h2>{decision_table(data)}</section>
{scenario_section()}
<section class="card"><h2>How to read this report</h2><div class="legend">
<span><span class="chip code">Computed by code</span> numbers, ranks, headlines, triggers, event study</span>
<span><span class="chip ai">Written by AI</span> the analysis text</span>
<span><span class="chip check">Checked by code</span> guardrails on the AI's decision</span></div></section>
</main>
{asktab.panel()}
{footer_html()}
{chart_script(charts(data))}
{asktab.script()}</body></html>"""


# ------------------------------------------------------------------------------------------------
# 5. Email: the brief itself is the body (emailbody.py draws it with tables and inline styles)
# ------------------------------------------------------------------------------------------------
STRIP_DAYS = 30
HEAT_NAMES = ["Volatility", "Vs 30-day avg", "Max drawdown", "Volume (context)", "Rel. strength (context)"]
LEVEL_NAMES = ["Distance, close above", "Distance, close below", "Volatility, close above", "Volatility, close below", "Drawdown, close below"]
PERSIST_MARKS = (1, 2, 3, 5, 7, 14, 21)


def _tone(rank, two_sided=False):
    if two_sided:
        return "hi" if rank >= agent.ALERT_RANK or rank <= agent.UNUSUAL_LOW else "mid" if rank >= NEAR_RANK or rank <= 100 - NEAR_RANK else "lo"
    return "hi" if rank >= agent.ALERT_RANK else "mid" if rank >= NEAR_RANK else "lo"


def _rank(rank, two_sided=False):
    return {"text": f"{rank:.1f}", "tone": _tone(rank, two_sided)}


def _strip_level(rank):
    return 4 if rank >= agent.ALERT_RANK else 3 if rank >= NEAR_RANK else 2 if rank >= 70 else 1 if rank >= 40 else 0


def _heat_facts(data):
    strength = {x["coin"]: x["rank"] for x in (data.get("cross") or {}).get("relative_strength", [])}
    rows = []
    for c in good(data):
        s, ctx = c["summary"], c["summary"].get("context", {})
        values = [s["ranks"]["volatility"], s["ranks"]["distance"], s["ranks"]["drawdown"], ctx.get("volume_rank"), strength.get(c["coin"])]
        cells = [{"text": "n/a", "tone": "none"} if v is None else {"text": f"{v:.1f}", "tone": _tone(max(v, 100.0 - v) if i == 4 else v)} for i, v in enumerate(values)]
        rows.append({"coin": c["coin"], "cells": cells})
    return {"columns": HEAT_NAMES, "rows": rows, "alert": f"{agent.ALERT_RANK:g}", "near": f"{NEAR_RANK:g}"} if rows else None


def _history_facts(data):
    rows = []
    for c in good(data):
        ranks = [v for v in (c.get("rank_top") or []) if v is not None][-STRIP_DAYS:]
        if ranks:
            rows.append({"coin": c["coin"], "cells": [_strip_level(v) for v in ranks], "now": f"{ranks[-1]:.1f}", "peak": f"{max(ranks):.1f}"})
    if not rows:
        return None
    days = min(len(r["cells"]) for r in rows)
    for r in rows:
        r["cells"] = r["cells"][-days:]
    return {"rows": rows, "days": days, "note": ("Each block is one day, the oldest on the left. It shows the highest of a coin's three ranks that day. "
                                                 f"Red is rank {agent.ALERT_RANK:g} or higher, amber is {NEAR_RANK:g} to below {agent.ALERT_RANK:g}, grey is ordinary.")}


def _ai_facts(data):
    decision = latest_decision(data)
    if decision is None:
        return {"note": data.get("decisions_note") or "No decision has been saved to the Sheet yet."}
    blocked = str(decision.get("blocked_reason") or "")
    if decision.get("message"):
        message = decision["message"]
    elif blocked:
        message = "The agent's message was removed by a guardrail (see the guardrail result below)."
    else:
        message = "No alert message with this decision: the agent found nothing new to alert on."
    cross = agreement(data)
    return {"message": message, "reasoning": decision.get("reasoning") or "Not available. Update apps_script/Code.gs and redeploy it to include the AI's reasoning here.",
            "sources": ", ".join(decision.get("sources") or []) or "none", "blocked": blocked, "verdict": "blocked" if blocked else "passed",
            "cross": (("Consistent" if cross[0] else "Different"), cross[1]) if cross is not None else None,
            "facts": [f"Model: {agent.MODEL} on Groq", f"Version: {decision.get('version') or 'unknown'}", f"Decided: {decision.get('time') or 'unknown'}",
                      f"Alert: {'yes' if decision.get('alert') else 'no'}"]}


def _outlook_facts(data):
    events = data.get("events")
    if not events:
        return None
    if not events["enough"]:
        return {"note": too_few_note(events)}
    again = events["again"]
    persist = [(f"{k} day{'s' if k > 1 else ''} later", again[k - 1]) for k in PERSIST_MARKS if k <= len(again) and again[k - 1] is not None]
    outcomes = []
    for h in insights.HORIZONS:
        st = events["after_episode"].get(h)
        if st:
            outcomes.append({"label": f"{h} days later", "median": _sign(st["median"]), "middle": f"{_band(st['q1'])} to {_band(st['q3'])}", "range": f"{_band(st['low'])} to {_band(st['high'])}", "n": st["n"]})
    seven = (events["after_episode"].get(7) or {}).get("n", 0)
    return {"headline": outlook_headline(events), "summary": outlook_summary(data, events),
            "tiles": [{"label": k, "value": v, "sub": s1, "sub2": s2, "tone": "amb"} for k, v, s1, s2 in outlook_tiles(events)],
            "persist": persist, "persist_note": "Of all days at the alert rank, the share that is at it again some days later. Pooled over the coins.",
            "outcomes": outcomes, "outcomes_note": f"Coin returns after an episode starts, from {seven} episode starts at 7 days. A hint about spread, not a model of direction.",
            "trust": trust_points(data, events)}


def _context_facts(data):
    cross = data.get("cross") or {}
    strength = {x["coin"]: x for x in cross.get("relative_strength", [])}
    rows = []
    for c in good(data):
        ctx = c["summary"].get("context")
        if not ctx:
            continue
        lines = [("Change 30d / 60d", f"{ctx['ret_30d_pct']:+.2f}% / {ctx['ret_60d_pct']:+.2f}%", None)]
        if "volume_rank" in ctx:
            lines.append(("Volume vs 30-day average", f"{ctx['volume_ratio_30d']:.2f}x", _rank(ctx["volume_rank"])))
        rs = strength.get(c["coin"])
        if rs:
            lines.append(("Relative strength vs group", f"{rs['vs_group_pct']:+.2f} pts", _rank(rs["rank"], True)))
        rows.append({"coin": c["coin"], "trend": ctx["trend"], "lines": lines})
    if not rows:
        return None
    pairs, pairs_note = [], ""
    if cross.get("correlations"):
        flagged = set(cross.get("flags", []))
        pairs = [{"pair": x["pair"], "corr": f"{x['corr_30d']:+.2f}", "rank": _rank(x["rank"], True),
                  "reading": "unusual" if "correlation " + x["pair"] in flagged else "normal range"} for x in cross["correlations"]]
        pairs_note = "How the coins move together: correlation of the last 30 daily returns, ranked against the pair's own history. Near 100 is unusually tight, near 0 unusually loose."
    elif cross.get("error"):
        pairs_note = f"Relationships unavailable: {cross['error']}."
    return {"rows": rows, "pairs": pairs, "pairs_note": pairs_note,
            "note": "These describe the past and never raise an alert on their own. Volume is traded volume on Kraken. Relative strength is a coin's 30-day return minus the group average."}


def _market_lines(piece):
    def gone(x):
        return "unavailable: " + (x.get("unavailable", "unavailable") if isinstance(x, dict) else "unavailable")
    liq, der, iv, wide, check = (piece.get(k) for k in ("liquidity", "derivatives", "implied_volatility", "market_wide", "price_check"))
    lines = []
    lines.append(("Liquidity (Kraken)", f"spread {liq['spread_pct']:.4f}%, bids {_usd(liq['bid_depth_usd'])}, asks {_usd(liq['ask_depth_usd'])} within {liq['depth_band_pct']:g}%" if sources.is_data(liq) else gone(liq)))
    if sources.is_data(der):
        oi = _usd(der["open_interest_usd_approx"]) if der.get("open_interest_usd_approx") else f"{der['open_interest']:,.0f} contracts"
        lines.append(("Funding, open interest", f"{der['funding_rate_pct_per_hour']:+.4f}% per hour, {der['funding_pays']}, open interest {oi}"))
    else:
        lines.append(("Funding, open interest", gone(der)))
    if sources.is_data(iv):
        rank = f"rank {iv['rank_1y']:.1f} vs past year" if iv.get("rank_1y") is not None else f"no rank yet ({iv['history_days']} days)"
        lines.append(("Implied volatility (DVOL)", f"{iv['implied_vol_pct']:.1f}% annualised, {rank}"))
    else:
        lines.append(("Implied volatility (DVOL)", gone(iv)))
    if sources.is_data(wide) and "market_volume_rank" in wide:
        lines.append(("Market-wide volume", f"{_usd(wide['volume_24h_usd'])} in 24h, {wide['market_volume_ratio_30d']:.2f}x its 30-day average, rank {wide['market_volume_rank']:.1f}"))
    elif sources.is_data(wide):
        lines.append(("Market-wide volume", f"{_usd(wide['volume_24h_usd'])} in 24h, history unavailable"))
    else:
        lines.append(("Market-wide volume", gone(wide)))
    if sources.is_data(check):
        lines.append(("Price check", f"{check['difference_pct']:+.2f}% ({'agree' if check['agree'] else 'differ'}), Kraken {_price(check['kraken_mid_usd'])} vs CoinGecko {_price(check['coingecko_price_usd'])}"))
    else:
        lines.append(("Price check", gone(check)))
    return lines


def _market_facts(data):
    context = data.get("context") or {}
    if "error" in context or not context.get("coins"):
        return {"note": f"Unavailable: {context['error']}."} if context.get("error") else None
    fng = context.get("sentiment")
    mood = (f"Fear and greed index, whole market: {fng['value']} of 100, {fng['label']}; {fng['change_vs_yesterday']:+d} since yesterday, 30-day mean {fng['mean_prior_30d']:.0f}."
            if sources.is_data(fng) else "Fear and greed index: unavailable.")
    flags = context.get("flags") or []
    return {"rows": [{"coin": coin, "lines": _market_lines(piece)} for coin, piece in context["coins"].items()],
            "intro": "Extra free sources on how the market is positioned. Context only: they never raise an alert, and a field that cannot be read says so. A positive funding rate means longs pay shorts. Implied volatility is what option prices imply, not a forecast.",
            "mood": mood, "flags": ("Unusual today: " + "; ".join(flags) + ".") if flags else "Unusual today: nothing among the sources that responded."}


def _decision_facts(data, limit=5):
    out = []
    for d in reversed(data["decisions"][-limit:]):
        message = str(d.get("message") or "")
        out.append({"time": str(d.get("time", "")).replace("T", " ")[:16], "alert": bool(d.get("alert")), "result": "ALERT" if d.get("alert") else "no alert",
                    "coins": ", ".join(d.get("coins") or []) or "no coin", "message": message if len(message) <= 90 else message[:87] + "..."})
    return out


def email_facts(data):
    """The plain facts the email shows. Everything comes from the same functions the page uses, so the two cannot disagree."""
    label, tone, _ = status_of(data)
    as_of = max((c["summary"]["as_of"] for c in good(data)), default="unavailable")
    deck = hero_deck(data)
    scoreboard = []
    for c in data["coins"]:
        if "error" in c:
            scoreboard.append({"coin": c["coin"], "error": c["error"]})
            continue
        s = c["summary"]
        scoreboard.append({"coin": c["coin"], "name": coin_name(c["coin"]), "status": coin_status(s), "error": None,
                           "price": f"{_price(s['last_close'])}, day {_sign(s['daily_return_pct'], 2)}",
                           "lines": [{"label": SCORE_LABELS[m], "value": insights.metric_value(s, m), "rank": _rank(s["ranks"][m])} for m in agent.METRICS]})
    levels = level_rows(data)
    days = max((c["levels"]["history_days"] for c in good(data) if c.get("levels")), default=0)
    events = data.get("events")
    return {
        "title": hero_title(data), "deck": deck, "preheader": deck[:140], "as_of": as_of, "generated": data["generated"], "credit": CREDIT, "demo": bool(data.get("demo")),
        "status": {"label": {"ALERT": "ALERT", "CALM": "CALM MARKET", "NO DATA": "NO DATA", "PARTIAL": "PARTIAL DATA"}[label], "tone": tone},
        "method": f"Each coin is ranked against its own past {history_days(data)} days. This describes how unusual today is. It is not a forecast of what comes next.",
        "links": {"report_url": public_url("REPORT_URL"), "ask_url": public_url("ASK_URL"), "attachment": ATTACHMENT_NAME, "launcher": LAUNCHER},
        "scoreboard": scoreboard,
        "scoreboard_note": f"Alert at rank {agent.ALERT_RANK:g} or higher. Near threshold from {NEAR_RANK:g} up to {agent.ALERT_RANK:g}.",
        "headlines": insights.top_headlines(data["coins"], data.get("context")),
        "tiles": [{"label": k, "value": v, "sub": sub, "tone": tone_} for k, v, sub, tone_ in tile_list(data)],
        "heat": _heat_facts(data), "history": _history_facts(data), "ai": _ai_facts(data), "outlook": _outlook_facts(data),
        "questions": reader_questions(data, events) if events and events["enough"] else [],
        "watch": {"lines": triggers(data)},
        "levels": ({"columns": LEVEL_NAMES, "rows": [{"coin": coin, "last": last, "cells": cells} for coin, last, cells in levels],
                    "note": f"A close between the two levels of a metric leaves it below the alert rank. The levels use up to {days} days of each coin's own history and are approximate: "
                            "they stop being valid once the next candle completes."} if levels else None),
        "scenarios": watch_scenarios(), "context": _context_facts(data), "market": _market_facts(data), "decisions": _decision_facts(data),
        "disclaimer_lead": "Live market-data analysis and decision support. Not investment advice.",
        "disclaimer": ("This brief contains no price forecast and makes no recommendation. Every number is computed by code from Kraken daily candles, with market context from "
                       "Kraken Futures, Deribit, CoinGecko and alternative.me where they responded. Thresholds are descriptive conventions documented in playbook.md."),
    }


def build_email_parts(data, budget=emailbody.EMAIL_BUDGET):
    """(html, dropped sections): the email body, kept under `budget` bytes by leaving out the least important sections."""
    return emailbody.render(email_facts(data), budget)


def build_email(data, budget=emailbody.EMAIL_BUDGET):
    return build_email_parts(data, budget)[0]


def send_email(data, page):
    subject = insights.subject_line(data["coins"])[:200]
    for budget in (emailbody.EMAIL_BUDGET, emailbody.COMPACT_BUDGET):
        body, dropped = build_email_parts(data, budget)
        try:
            agent.sheet_post({"action": "report", "subject": subject, "summary_html": body, "html": page})
        except agent.AgentError as error:
            text = str(error)
            if "INVALID_PAYLOAD" in text and budget == emailbody.EMAIL_BUDGET and len(body) > emailbody.COMPACT_BUDGET:
                print("::warning::The deployed web app limits the email body to 30,000 characters (older Code.gs). Sending the shorter body instead. "
                      "Update apps_script/Code.gs and redeploy it (Manage deployments > pencil > New version) for the full body.", flush=True)
                continue
            hint = (" The deployed web app is the older version. Update apps_script/Code.gs from the repository and redeploy it (Manage deployments > pencil > New version)."
                    if ("INVALID_ACTION" in text or "INVALID_REQUEST" in text) else "")
            print(f"::warning::Report email not sent: {text}.{hint}", flush=True)
            return False
        print(f"[report] email body {len(body.encode('utf-8')) // 1024} KB, sections left out: {', '.join(dropped) or 'none'}", flush=True)
        print(f"[report] email sent: {subject}", flush=True)
        return True
    return False


# ------------------------------------------------------------------------------------------------
# 6. Command line
# ------------------------------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the executive intelligence brief.")
    parser.add_argument("--out", default="executive-report.html")
    parser.add_argument("--email", action="store_true", help="email the report through the Apps Script web app")
    parser.add_argument("--demo", action="store_true", help="use synthetic data to preview the layout")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    if args.demo:
        coins, cross = demo_coins(), demo_cross()
        context = demo_context(coins)
        decisions, note = demo_decisions()
    else:
        coins, cross = gather(now)
        context = gather_context()
        decisions, note = fetch_decisions()
    data = {"coins": coins, "cross": cross, "context": context, "decisions": decisions, "decisions_note": note, "demo": args.demo,
            "generated": now.strftime("%Y-%m-%d %H:%M"), "generated_long": now.strftime("%A %d %B %Y"),
            "events": insights.event_study(coins)}
    page = build_html(data)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(page)
    print(f"[report] wrote {args.out} ({len(page) // 1024} KB): {status_of(data)[0]}, {len(good(data))} of {len(coins)} coins with data", flush=True)
    print(f"[report] subject: {insights.subject_line(coins)}", flush=True)
    down = insights.unavailable(coins)
    if down:
        print(f"::warning::No valid data for {', '.join(down)}. Report status: {status_of(data)[0]}.", flush=True)
    for line in headlines(data):
        print(f"[report]   {line}", flush=True)
    email_failed = False
    if args.email and not args.demo:
        email_failed = not send_email(data, page)
        if email_failed:
            print(f"ERROR: the report email was not sent (the reason is in the warning line). The page was still written to {args.out}.", file=sys.stderr, flush=True)
    return 1 if email_failed or not (good(data) or args.demo) else 0


if __name__ == "__main__":
    sys.exit(main())
