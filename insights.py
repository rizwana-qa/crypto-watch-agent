"""Facts for the daily brief that code computes from the candles and the other free sources.

Three things live here, all pure functions of the report's data (standard library only, no network):

  1. event_study: what the history says after a coin reaches the alert rank (how long it lasts, what the next 7, 14 and 30 days looked like).
  2. top_headlines: the three most important facts of the day, ranked by code.
  3. subject_line: the email subject, built from the same numbers.

Nothing here forecasts. Every sentence describes past data, and the wording is checked against the agent's banned-word list.
"""

import statistics

import agent
import sources

HORIZONS = (7, 14, 30)        # days after the first day of an alert episode
PERSIST_DAYS = 21             # how far the "still at the alert rank" curve goes
MIN_EPISODES = 8              # fewer episodes than this: the page says the sample is too small and shows no outcome numbers
FIRST_RANKED = agent.WINDOW + agent.MIN_HISTORY_DAYS - 1   # index of the first candle that can be ranked (same rule as agent.base_rate)
NAMES = {"BTC": "Bitcoin", "ETH": "Ether", "XRP": "XRP"}
METRIC_TEXT = {"volatility": "30-day volatility", "distance": "distance from the 30-day average", "drawdown": "30-day drawdown"}
SHORT_METRIC = {"volatility": "volatility", "distance": "distance", "drawdown": "drawdown"}


# ---------------------------------------------------------------------------------------------
# 1. The event study
# ---------------------------------------------------------------------------------------------
def _stats(values):
    """Middle half, median and range of a list of percentage moves. None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) >= 2:
        q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    else:
        q1 = q3 = ordered[0]
    return {"n": len(ordered), "median": statistics.median(ordered), "q1": q1, "q3": q3, "low": ordered[0], "high": ordered[-1],
            "up_pct": 100.0 * sum(1 for v in ordered if v > 0) / len(ordered)}


def _runs(flags):
    """Lengths of the runs of True in a list."""
    runs, run = [], 0
    for flag in flags:
        if flag:
            run += 1
        else:
            if run:
                runs.append(run)
            run = 0
    if run:
        runs.append(run)
    return runs


def event_study(coins):
    """What the past says after a coin reaches the alert rank, pooled over the coins.

    coins: the report's analyse() results (a coin with an "error" is skipped). The alert days are the ones agent.base_rate found,
    so the rule is the same one the agent uses and every day was ranked only against the days before it.
    An episode is a run of consecutive alert days. Returns None when no coin has the candles.
    """
    moves = {h: [] for h in HORIZONS}          # return h days after the first day of each episode
    anyday = {h: [] for h in HORIZONS}         # return h days after every ranked day
    again = {k: [] for k in range(1, PERSIST_DAYS + 1)}   # for every alert day: was the coin at the alert rank k days later
    lengths, current, used = [], {}, 0
    for c in coins:
        closes = c.get("closes")
        flags = (c.get("base_rate") or {}).get("flags")
        if "error" in c or not closes or not flags:
            continue
        used += 1
        n = len(closes)
        at = {FIRST_RANKED + j: flag for j, flag in enumerate(flags)}      # candle index -> at the alert rank
        for h in HORIZONS:
            for i in at:
                if i + h < n:
                    anyday[h].append((closes[i + h] / closes[i] - 1) * 100)
            for i, flag in at.items():
                if flag and not at.get(i - 1, False) and i + h < n:
                    moves[h].append((closes[i + h] / closes[i] - 1) * 100)
        for i, flag in at.items():
            if flag:
                for k in again:
                    if i + k in at:
                        again[k].append(at[i + k])
        lengths += _runs(flags)
        run = 0
        for flag in reversed(flags):
            if not flag:
                break
            run += 1
        current[c["coin"]] = run
    if not used:
        return None
    return {
        "episodes": len(lengths), "lengths": lengths,
        "run_median": statistics.median(lengths) if lengths else None,
        "run_mean": statistics.fmean(lengths) if lengths else None,
        "run_max": max(lengths) if lengths else None,
        "again": [100.0 * sum(again[k]) / len(again[k]) if again[k] else None for k in sorted(again)],
        "after_episode": {h: _stats(moves[h]) for h in HORIZONS},
        "any_day": {h: _stats(anyday[h]) for h in HORIZONS},
        "current_run": current,
        "enough": len(lengths) >= MIN_EPISODES,
    }


# ---------------------------------------------------------------------------------------------
# 2. Headlines
# ---------------------------------------------------------------------------------------------
def _ok(coins):
    return [c for c in coins if "error" not in c]


def unavailable(coins):
    """Symbols of the coins that have no valid data today."""
    return [c["coin"] for c in coins if "error" in c]


def _and(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def metric_value(summary, metric):
    if metric == "volatility":
        return f"{summary['volatility_30d_daily_pct']:.2f}% a day"
    if metric == "distance":
        return f"{summary['dist_from_30d_avg_pct']:+.2f}%"
    return f"{abs(summary['max_drawdown_30d_pct']):.2f}%"


def _phrase(metric, summary):
    """The fact as a clause that follows a coin name."""
    if metric == "distance":
        gap = summary["dist_from_30d_avg_pct"]
        return f"closes {abs(gap):.2f}% {'below' if gap < 0 else 'above'} its 30-day average"
    if metric == "drawdown":
        return f"shows a 30-day drawdown of {abs(summary['max_drawdown_30d_pct']):.2f}%"
    return f"has 30-day volatility of {summary['volatility_30d_daily_pct']:.2f}% a day"


def breaches(coins):
    """[(rank, coin, metric, summary)] for every metric at the alert rank, strongest first."""
    out = []
    for c in _ok(coins):
        s = c["summary"]
        for m in s["breaches"]:
            out.append((s["ranks"][m], c["coin"], m, s))
    return sorted(out, key=lambda item: (-item[0], item[1]))


def near(coins):
    out = []
    for c in _ok(coins):
        s = c["summary"]
        for m, rank in s["ranks"].items():
            if m not in s["breaches"] and 90.0 <= rank < agent.ALERT_RANK:
                out.append((rank, c["coin"], m, s))
    return sorted(out, key=lambda item: (-item[0], item[1]))


def _volume_fact(coins):
    ratios = []
    for c in _ok(coins):
        ctx = c["summary"].get("context") or {}
        if "volume" in c["summary"].get("flags", []) and ctx.get("volume_ratio_30d") is not None:
            ratios.append((c["coin"], ctx["volume_ratio_30d"]))
    return ratios


def _mood(context):
    fng = (context or {}).get("sentiment")
    return fng if sources.is_data(fng) else None


def top_headlines(coins, context=None):
    """Exactly three {"kicker", "text"} items, most important first. Only numbers from the data."""
    items = []
    hits = breaches(coins)
    if hits:
        rank, coin, metric, s = hits[0]
        items.append({"kicker": "Strongest signal", "text": f"{NAMES.get(coin, coin)} {_phrase(metric, s)}, rank {rank:.1f} against its own history"})
        by_coin = {}
        for r, cn, m, _ in hits[1:]:
            by_coin.setdefault(cn, []).append(f"{SHORT_METRIC[m]} {r:.1f}")
        if by_coin:
            parts = [f"{cn} {', '.join(found)}" for cn, found in sorted(by_coin.items())][:4]
            items.append({"kicker": "Also at the alert rank", "text": "; ".join(parts) + " (ranks)"})
    else:
        closest = sorted(((max(c["summary"]["ranks"].values()), c["coin"]) for c in _ok(coins)), reverse=True)
        down = unavailable(coins)
        if closest and down:
            rank, coin = closest[0]
            items.append({"kicker": "Partial data", "text": f"{_and(down)} unavailable. No other coin is at the alert rank of {agent.ALERT_RANK:g}. The closest is {coin} at rank {rank:.1f}"})
        elif closest:
            rank, coin = closest[0]
            items.append({"kicker": "Calm market", "text": f"No coin is at the alert rank of {agent.ALERT_RANK:g}. The closest is {coin} at rank {rank:.1f}"})
    if len(items) < 2 and near(coins):
        rank, coin, metric, s = near(coins)[0]
        items.append({"kicker": "Near threshold", "text": f"{coin} {METRIC_TEXT[metric]} is at rank {rank:.1f}, {agent.ALERT_RANK - rank:.1f} points under the alert rank"})
    volume = _volume_fact(coins)
    mood = _mood(context)
    if volume:
        lo, hi = min(r for _, r in volume), max(r for _, r in volume)
        span = f"{lo:.2f} times" if len(volume) == 1 or abs(hi - lo) < 0.005 else f"{lo:.2f} to {hi:.2f} times"
        text = f"{'/'.join(c for c, _ in volume)} volume runs at {span} its 30-day average"
        if mood:
            text += f", while Fear and Greed reads {mood['value']} ({mood['label']})"
        items.append({"kicker": "Volume and mood" if mood else "Volume", "text": text})
    elif mood:
        items.append({"kicker": "Market mood", "text": f"Fear and Greed reads {mood['value']} ({mood['label']}), against a 30-day mean of {mood['mean_prior_30d']:.0f}"})
    fillers = [{"kicker": "Data", "text": f"Candles are complete and current for {len(_ok(coins))} of {len(coins)} coins"},
               {"kicker": "Method", "text": f"Each coin is ranked against its own history, alert rank {agent.ALERT_RANK:g}"},
               {"kicker": "Scope", "text": "Price ranks can alert. Volume, sentiment and derivatives are context only"}]
    for filler in fillers:
        if len(items) >= 3:
            break
        items.append(filler)
    return items[:3]


# ---------------------------------------------------------------------------------------------
# 3. Email subject
# ---------------------------------------------------------------------------------------------
def subject_line(coins):
    """One line, at most about 100 characters, built from the numbers. Falls back to a plain line if a banned word slips in."""
    ok = _ok(coins)
    if not ok:
        return "Crypto Watch: market data unavailable today, no statement made"
    hits = breaches(coins)
    if hits:
        by_coin = {}
        for rank, coin, metric, s in hits:
            by_coin.setdefault(coin, (rank, metric, s))
        n = len(by_coin)
        rank, coin, metric, s = hits[0]
        lead = f"{coin} {_phrase(metric, s)}"
        if n == 1:
            line = f"Crypto Watch ALERT: {coin} breaches its own history, {lead.split(' ', 1)[1]}"
        else:
            line = f"Crypto Watch ALERT: {n} coins breach their own history, {lead}"
        fallback = f"Crypto Watch ALERT: {n} of {len(ok)} coins at the alert rank"
    elif unavailable(coins):
        line = f"Crypto Watch: partial data, {_and(unavailable(coins))} unavailable, no other coin at its alert rank"
        fallback = "Crypto Watch: partial data, some coins unavailable"
    else:
        closest = sorted(((max(c["summary"]["ranks"].values()), c["coin"]) for c in ok), reverse=True)[0]
        line = f"Crypto Watch: calm market, no coin at its alert rank (closest {closest[1]}, rank {closest[0]:.0f})"
        fallback = "Crypto Watch: calm market, no coin at its alert rank"
    return fallback if agent.ADVICE_RE.search(line) else line


def wording_ok(text):
    """True when the text has none of the agent's banned words (advice or trading instructions)."""
    return not agent.ADVICE_RE.search(text or "")
