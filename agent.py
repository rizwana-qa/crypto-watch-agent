"""Crypto Watch Agent: the AI recommends, the code checks, decides and acts.

Run it:
    python agent.py                                  one live run (Kraken + Groq + Google Sheet); the code review always runs,
                                                     and the AI also reviews a drafted alert
    python agent.py --mode planner --reviewer off    another version; --reviewer off skips only the AI review call
    python agent.py --dry-run                        live data and AI, but nothing is posted to the Sheet
    python agent.py --test                           fake tools, real AI; exit code 1 if a scenario fails
    python agent.py --measure                        4 versions x 3 runs, prints the results table
    python agent.py --check-sources                  call every free data source once, no AI

This project is for education only and is not financial advice.
"""

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

import sources

# ==============================================================================================
# 1. SETTINGS. A coin, a threshold, a banned word or the reviewer switch is changed here.
# ==============================================================================================
QUESTION = (
    "Is BTC, ETH or XRP showing unusual volatility, distance from its 30-day average or drawdown "
    "in its latest completed daily candle compared with its own history, what do volume, relative "
    "strength and the correlation between the coins add as context, and is the data reliable "
    "enough to say so?"
)
WATCHLIST = {"BTC": "XBTUSD", "ETH": "ETHUSD", "XRP": "XRPUSD"}  # Kraken calls Bitcoin "XBT"
# Kraken files the candles under its own name for the pair. A reply under any other key is refused.
# BTC and ETH were checked in Assignment 1; XRP was checked against Kraken's AssetPairs endpoint.
RESULT_KEYS = {"XBTUSD": "XXBTZUSD", "ETHUSD": "XETHZUSD", "XRPUSD": "XXRPZUSD"}

WINDOW = 30             # 30 daily returns need 31 closes (same window as Assignment 1)
MIN_HISTORY_DAYS = 90   # fewer completed candles than this: the history is too short to rank against
ALERT_RANK = 95.0       # a rank at or above this is "unusual" (see playbook.md, "Alert rules")
UNUSUAL_LOW = 100.0 - ALERT_RANK  # two-sided context metrics: a rank at or below this (5) is unusually low

DEFAULT_MODE = "agent"  # "agent" (tool loop) or "planner" (plan first, code runs the steps)
USE_REVIEWER = True     # the AI review switch; --reviewer on|off overrides it. It never turns the code review off: that always runs
MAX_STEPS = 12          # agent loop: the most AI calls before the run stops with an error (8 before the two context tools; a model that calls one tool at a time needs 9 or more)
MAX_FIX_ATTEMPTS = 2    # how often the AI may be asked again after an invalid answer
MAX_REVISIONS = 1       # how often the agent may revise after the review found problems (a code finding or an AI rejection)
REVIEW_MAX_TOKENS = 1000 # reply cap for the AI review call: a small JSON verdict. A reasoning model may spend part of the cap on thinking, so it is not set tight
REVIEW_REASONING_EFFORT = "low"   # sent only with the review call and only to an openai/gpt-oss model (Groq documents low, medium and high for them): a verdict needs little thinking
REVIEW_REQUEST_CAP = 3000  # the most the whole AI review request (system plus user) may be, in ESTIMATED tokens (characters / 4, not a Groq count); the free tier refuses one request that is too large
MAX_REVIEW_LINE_CHARS = 200  # the short review status that goes into the Sheet's Review column
MAX_PLAN_STEPS = 12     # planner: the most steps a plan may have (3 market + cross-asset + context + stats + history + playbook searches)
TREND_LONG_DAYS = 60    # the longer of the two trend windows (the shorter one is WINDOW, 30 days)
MAX_MESSAGE_CHARS = 280
MAX_REASONING_CHARS = 600
HISTORY_LIMIT = 7       # how many recent decisions the memory tool returns
MAX_LLM_ATTEMPTS = 8    # tries per AI call when the free tier answers "rate limit" (8000 tokens per minute)

# The advice filter: an alert message containing any of these (whole words, any case) is blocked.
BANNED_WORDS = [
    "buy", "buying", "bought", "sell", "selling", "sold", "long", "short", "hold", "holding",
    "entry", "exit", "accumulate", "should", "recommend", "recommends", "recommended",
    "recommendation", "advise", "advice", "take profit", "stop loss", "stop-loss",
]
ADVICE_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in BANNED_WORDS) + r")\b", re.IGNORECASE)
# Plain market terms and the disclaimer that hold a banned word but give no advice. Only these exact phrases are cut out
# (whole phrase, any case) before has_advice_language() checks the text. Nothing else is let through.
ADVICE_SAFE_PHRASES = ["sell-off", "selloff", "long-term", "short-term", "mid-term", "short squeeze", "holding steady",
                       "not financial advice", "not investment advice", "not trading advice"]
SAFE_PHRASE_RE = re.compile(r"\s*\b(?:" + "|".join(re.escape(p) for p in ADVICE_SAFE_PHRASES) + r")\b\s*", re.IGNORECASE)


# Second layer (D-05): advice and forecasts written without a banned word ("add to their positions", "will likely rebound", "price target").
# It only ever BLOCKS an alert, it never rewrites or adds anything, and it is narrow on purpose: every pattern needs a clear future or
# instruction form, so a plain past-tense description ("BTC fell 8%", "volume was 2.4 times its average") never matches.
# The playbook already says "no advice and no prediction"; this enforces the second half in code. A forecast can only be described as such
# when the model writes one, so a miss costs nothing; a false block costs a missed alert, which is why tests/test_fix_d05_layer2.py holds a
# large set of honest messages that must still pass.
_MOVES = (r"(?:rise|rally|rebound|recover|crash|drop|fall|surge|moon|pump|dump|climb|soar|plunge|bounce|spike|tank|collapse|explode|"
          r"break\s?out|reverse|double|triple|reach|hit|go\s+(?:up|down|higher|lower)|move\s+(?:up|down|higher|lower)|"
          r"continue\s+(?:to\s+)?(?:rise|fall|climb|drop|rally|decline|higher|lower|up|down)|keep\s+(?:rising|falling|climbing|dropping))")
_FUTURE = (r"(?:will|won't|going\s+to|gonna|expected\s+to|likely\s+to|probably\s+(?:will|to)|poised\s+to|bound\s+to|"
           r"certain\s+to|sure\s+to|about\s+to)")
_ADVICE_PATTERNS = [
    # a forecast: a future or expectation word, up to two filler words, then a price move
    rf"\b{_FUTURE}\s+(?:(?:likely|probably|soon|then|also|still|further|just|quickly|sharply|slowly|keep|continue)\s+){{0,2}}{_MOVES}\b",
    r"\bexpect(?:s|ing)?\s+(?:\w+\s+){0,3}?(?:upside|downside|gains?|rally|rebound|recovery|bounce|pullback|correction|decline|drop|crash|"
    r"breakout|reversal|surge|dip|continuation)\b",
    r"\bprice\s+targets?\b|\btarget\s+price\b|\bprice\s+objective\b",
    # a promise: "guaranteed", "risk free" (a plain "not guaranteed" disclaimer is cut out first)
    r"\bguarantee[sd]?\b|\brisk[- ]free\b|\briskless\b|\bsure[- ]?(?:thing|fire)\b|\bcan(?:'t|not)\s+lose\b|\bno\s+risk\b|\beasy\s+money\b",
    # advice written as a suggestion or a hint
    r"\badd(?:ing)?\s+to\s+(?:(?:your|their|his|her|the|an?|existing)\s+)?(?:\w+\s+)?positions?\b",
    r"\bload(?:ing)?\s+up\b",
    r"\bget(?:ting)?\s+(?:in|out)\s+(?:now|here|early|before|while|fast|quickly|soon)\b|\bjump\s+in\s+(?:now|here|early|before|while)\b",
    r"\bgo(?:ing)?\s+all[- ]in\b|\bdouble\s+down\b|\bcatch\s+the\s+(?:dip|falling\s+knife|wave|move)\b|\b(?:do\s+not|don'?t)\s+miss\b|\bfomo\b",
    r"\bdiversif\w+\b|\brebalanc\w+\b|\bcut\s+(?:your\s+|their\s+)?losses\b",
    r"\b(?:traders|investors|holders|readers|users|everyone|anyone)\s+(?:may|might|could|can|would|will)\s+(?:want|wish|consider|be\s+wise|do\s+well)\b",
    r"\byou\s+(?:may|might|could|can|would|will)\s+(?:want|wish|consider|do\s+well)\b",
    r"\b(?:consider|think\s+about)\s+(?:adding|reducing|trimming|increasing|decreasing|raising|lowering|cutting|closing|opening|entering|exiting|"
    r"reallocating|rotating)\b",
    r"\bwould\s+be\s+(?:wise|smart|prudent|sensible)\b",
    r"\b(?:good|great|ideal|perfect|best|right|prime)\s+(?:moment|time|opportunity|chance|spot)\s+(?:to|for)\b",
    r"\btime\s+to\s+(?:act|move|get|go|jump|enter|exit|close|add|trim|cut|reduce|increase|rotate|rebalance|reallocate|position)\b",
    r"\b(?:opportunity|chance)\s+to\s+(?:add|enter|exit|trim|cut|reduce|increase|rotate|reallocate)\b",
    r"\bwait(?:ing)?\s+for\s+(?:an?|the)\s+(?:\w+\s+)?(?:pullback|dip|retrace\w*|correction|breakout|confirmation|bounce|rebound|reversal)\b",
    r"\b(?:use|exercise|take)\s+(?:extra\s+)?(?:caution|care)\b|\bproceed\s+with\s+caution\b|\bbe\s+(?:careful|cautious|wary|prepared|ready)\b|"
    r"\bstay\s+(?:cautious|careful|vigilant|wary|safe)\b|\bwatch\s+out\b|\bdo\s+your\s+own\s+research\b|\bdyor\b",
]
ADVICE_PATTERN_RE = re.compile("|".join(f"(?:{p})" for p in _ADVICE_PATTERNS), re.IGNORECASE)
# Plain disclaimers that hold a promise word but promise nothing. Cut out before the second layer only (never before the word list).
ADVICE_DISCLAIMERS = ["not guaranteed", "no guarantee", "no guarantees", "not a guarantee", "cannot guarantee", "can't guarantee",
                      "does not guarantee", "doesn't guarantee", "do not guarantee", "not a price target", "not price targets", "no price target"]
DISCLAIMER_RE = re.compile(r"\s*\b(?:" + "|".join(re.escape(p) for p in ADVICE_DISCLAIMERS) + r")\b\s*", re.IGNORECASE)
_HIDDEN_CHARS_RE = re.compile("[​‌‍⁠­﻿]")
_SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z][\s.\-_*]){2,}[A-Za-z]\b")


def plain_text(text):
    """The text as a reader sees it: full-width and ligature letters become plain letters (NFKC), invisible characters are dropped and
    letters spaced out one by one ('B U Y', 'b.u.y') are joined, so a word cannot be hidden from the check by its spelling."""
    text = _HIDDEN_CHARS_RE.sub("", unicodedata.normalize("NFKC", str(text)))
    return _SPACED_LETTERS_RE.sub(lambda match: re.sub(r"[\s.\-_*]", "", match.group(0)), text)


def has_advice_language(text):
    """True when the text has a banned word or one of the second-layer patterns. The text is first made plain (plain_text). The safe
    phrases are cut out next, each replaced by one space (with the spaces around it), so a cut can never join two words, and a phrase
    placed inside a banned phrase ('take sell-off profit') still shows up. The plain disclaimers are cut out only before the patterns."""
    cut = SAFE_PHRASE_RE.sub(" ", plain_text(text))
    if ADVICE_RE.search(cut):
        return True
    return bool(ADVICE_PATTERN_RE.search(DISCLAIMER_RE.sub(" ", cut)))


MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
# REVIEWER_MODEL (optional) names another Groq model for the AI review call only. A second model gives an independent opinion and has
# its own free-tier token budget. When it is not set, the review uses MODEL, so nothing changes.


def reviewer_model():
    return os.environ.get("REVIEWER_MODEL", "").strip() or MODEL


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
PLAYBOOK_PATH = Path(__file__).with_name("playbook.md")
SECONDS_PER_DAY = 86400
METRICS = ("volatility", "distance", "drawdown")
STATS = {"llm_calls": 0, "rate_limit_waits": 0, "llm_tokens": 0}   # llm_tokens: what Groq reports as used, to plan against the free daily cap


class AgentError(Exception):
    """The run cannot continue. main() prints it and exits with code 1."""


class ShapeError(ValueError):
    """An AI answer or plan does not have the required shape."""


def log(message):
    print(message, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def redact(text, *secrets):
    """Remove secrets (and any key=... parameter) from text before it is printed."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[hidden]")
    return re.sub(r"key=[^&\s'\"]+", "key=[hidden]", text)


def format_date(unix_seconds):
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def round_clean(value, digits):
    return round(value, digits) + 0.0  # "+ 0.0" turns -0.0 into 0.0


# ==============================================================================================
# 2. MARKET TOOL: Kraken data and the numbers. Adapted from Assignment 1 (src/fetch_data.py,
#    src/analysis.py). Plain arithmetic, no AI.
# ==============================================================================================
CANDLE_TOLERANCE = 1e-9   # relative slack when one candle's own prices are compared (floating-point noise only)


def as_number(value):
    """A finite float from a number or numeric text, else None. Booleans, NaN, infinity, words and text that is not plain ASCII digits
    ("1_000", full-width digits, Unicode spaces) are refused (D-15). Plain spaces around a number are still accepted."""
    if isinstance(value, bool) or (isinstance(value, str) and not sources.NUMBER_TEXT_RE.match(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):   # OverflowError: a JSON integer too large for a float
        return None
    return number if math.isfinite(number) else None


def parse_candles(raw, pair):
    """Kraken candle rows are [time, open, high, low, close, vwap, volume, count]. The agent keeps time, close and volume,
    but every candle must be sound before any of it is used: positive prices, a high that is the highest and a low that
    is the lowest of the day, and a whole, non-negative trade count. The vwap is checked by validate_data.py only."""
    candles = []
    for number, row in enumerate(raw):
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError(f"{pair}: candle {number} is not a list of 8 values")
        timestamp = row[0]
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise ValueError(f"{pair}: candle {number} has no integer timestamp")
        close = as_number(row[4])
        if close is None:
            raise ValueError(f"{pair}: candle {number} has a close that is not a number")
        if close <= 0:
            raise ValueError(f"{pair}: candle {number} has a close that is not a positive number")
        open_, high, low = as_number(row[1]), as_number(row[2]), as_number(row[3])
        if None in (open_, high, low) or min(open_, high, low) <= 0:
            raise ValueError(f"{pair}: candle {number} has an open, high or low that is not a positive number")
        slack = CANDLE_TOLERANCE * high
        if high + slack < max(open_, close, low) or low - slack > min(open_, close, high):
            raise ValueError(f"{pair}: candle {number} has a high or low that contradicts its open or close")
        volume = as_number(row[6])
        if volume is None:
            raise ValueError(f"{pair}: candle {number} has a volume that is not a number")
        if volume < 0:
            raise ValueError(f"{pair}: candle {number} has a volume that is not zero or a positive number")
        trades = row[7]
        if not isinstance(trades, int) or isinstance(trades, bool) or trades < 0:
            raise ValueError(f"{pair}: candle {number} has a trade count that is not a whole number of zero or more")
        candles.append({"timestamp": timestamp, "close": close, "volume": volume})
    return candles


def fetch_rows(pair):
    """The raw daily candle rows Kraken returns for one pair (8 values each). Any failure is an error, never a guess."""
    last_error = "unknown error"
    for attempt in range(1, 4):
        try:
            response = requests.get(KRAKEN_URL, params={"pair": pair, "interval": 1440}, timeout=15)
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.RequestException(f"HTTP {response.status_code}")
            response.raise_for_status()
            body = response.json()
            break
        except requests.HTTPError as error:  # another 4xx: retrying will not help
            raise RuntimeError(f"{pair}: Kraken request failed: {error}")
        except (requests.RequestException, ValueError) as error:
            last_error = type(error).__name__
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    else:
        raise RuntimeError(f"{pair}: request to Kraken failed after 3 attempts ({last_error})")
    if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("result"), dict):
        raise RuntimeError(f"{pair}: Kraken returned an error or an unexpected reply")
    keys = [key for key in body["result"] if key != "last"]  # "last" is a pointer, not candles
    if keys != [RESULT_KEYS[pair]]:
        raise RuntimeError(f"{pair}: unexpected pair key {keys}, expected {RESULT_KEYS[pair]!r}")
    return body["result"][keys[0]]


def fetch_candles(pair):
    """Daily candles for one Kraken pair, checked. Any failure is an error, never a guess."""
    return parse_candles(fetch_rows(pair), pair)


def completed_closes(candles, now):
    """(timestamps, closes) of the completed UTC days. The unfinished day of today is dropped."""
    today = int(now.timestamp()) // SECONDS_PER_DAY * SECONDS_PER_DAY
    every = [c["timestamp"] for c in candles]     # all candles, the unfinished one of today included
    if any(t > int(now.timestamp()) for t in every):
        raise ValueError("a candle is dated in the future")
    if any(t % SECONDS_PER_DAY for t in every):
        raise ValueError("a candle does not start at midnight UTC")
    if any(b <= a for a, b in zip(every, every[1:])):
        raise ValueError("the daily data has a duplicate or is out of order")
    done = [c for c in candles if c["timestamp"] < today]
    stamps = [c["timestamp"] for c in done]
    if len(done) < MIN_HISTORY_DAYS:
        raise ValueError(f"only {len(done)} completed candles, need at least {MIN_HISTORY_DAYS}")
    if any(b - a != SECONDS_PER_DAY for a, b in zip(stamps, stamps[1:])):
        raise ValueError("the daily data has a gap")
    if stamps[-1] != today - SECONDS_PER_DAY:
        raise ValueError(f"stale data: the latest completed candle is {format_date(stamps[-1])}")
    return stamps, [c["close"] for c in done]


def max_drawdown(closes):
    """Largest fall from a running peak, as a number <= 0."""
    peak, worst = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        worst = min(worst, close / peak - 1)
    return worst


def metric_series(closes):
    """Three series aligned with closes (None until 31 closes exist): volatility, distance, drawdown depth."""
    n = len(closes)
    returns = [later / earlier - 1 for earlier, later in zip(closes, closes[1:])]
    volatility, distance, depth = [None] * n, [None] * n, [None] * n
    for i in range(WINDOW, n):
        volatility[i] = statistics.stdev(returns[i - WINDOW:i])           # sample st. dev. of 30 returns
        distance[i] = closes[i] / statistics.fmean(closes[i - WINDOW + 1:i + 1]) - 1  # vs 30-day average
        depth[i] = -max_drawdown(closes[i - WINDOW:i + 1])                # 31 closes, depth >= 0
    return {"volatility": volatility, "distance": distance, "drawdown": depth}, returns


def percentile_rank(values, latest):
    """0 to 100: the share of values below latest, counting a tie as half."""
    below = sum(1 for v in values if v < latest)
    ties = sum(1 for v in values if v == latest)
    return 100.0 * (below + 0.5 * ties) / len(values)


def ranks_at(series_by_metric, i):
    """Rank of day i inside the history up to and including day i, for each metric."""
    ranks = {}
    for name, series in series_by_metric.items():
        values = [abs(v) if name == "distance" else v for v in series[:i + 1] if v is not None]
        ranks[name] = percentile_rank(values, values[-1])
    return ranks


def episodes(flags):
    """How many separate runs of True a list has: three days in a row at the alert rank are one episode."""
    return sum(1 for i, flag in enumerate(flags) if flag and (i == 0 or not flags[i - 1]))


def base_rate(stamps, closes):
    """How often the coin was at the alert rank on any of the three price metrics, each day ranked only against the days before it.
    The first days are skipped until the history is as long as the agent needs (MIN_HISTORY_DAYS), so every day counted could have raised an alert."""
    series, _ = metric_series(closes)
    first = WINDOW + MIN_HISTORY_DAYS - 1
    days, flags = [], []
    for i in range(first, len(closes)):
        ranks = ranks_at(series, i)
        days.append(format_date(stamps[i]))
        flags.append(max(ranks.values()) >= ALERT_RANK)
    return {"days": days, "flags": flags, "ranked_days": len(flags), "alert_days": sum(flags), "episodes": episodes(flags)}


def add_breaches(summary):
    """Python, not the AI, says which ranks reach the alert rank."""
    summary["breaches"] = [m for m in METRICS if summary["ranks"][m] >= ALERT_RANK]
    return summary


def summarize(coin, stamps, closes):
    """The small summary the AI sees: never the 700 candles."""
    series, returns = metric_series(closes)
    last = len(closes) - 1
    ranks = ranks_at(series, last)
    return add_breaches({
        "coin": coin,
        "pair": WATCHLIST[coin],
        "as_of": format_date(stamps[-1]),
        "last_close": round_clean(closes[-1], 4),
        "daily_return_pct": round_clean(returns[-1] * 100, 2),
        "dist_from_30d_avg_pct": round_clean(series["distance"][last] * 100, 2),
        "volatility_30d_daily_pct": round_clean(series["volatility"][last] * 100, 2),
        "max_drawdown_30d_pct": round_clean(-series["drawdown"][last] * 100, 2),
        "ranks": {name: round_clean(rank, 1) for name, rank in ranks.items()},
        "history_days": len(closes),
    })


def load_coin(coin, now):
    """(stamps, closes, volumes) of the completed days of one watchlist coin. Any problem is an error."""
    candles = fetch_candles(WATCHLIST[coin])
    stamps, closes = completed_closes(candles, now)
    by_time = {c["timestamp"]: c for c in candles}
    volumes = [by_time[t].get("volume") for t in stamps]
    return stamps, closes, volumes


# ---- context metrics: volume and trend. They describe; they never raise an alert on their own.
def volume_ratio_series(volumes):
    """Each day's volume divided by the mean volume of the 30 days before it (None until 30 earlier days exist)."""
    ratios = [None] * len(volumes)
    if any(v is None for v in volumes):
        return ratios
    for i in range(WINDOW, len(volumes)):
        mean = statistics.fmean(volumes[i - WINDOW:i])
        ratios[i] = volumes[i] / mean if mean > 0 else None
    return ratios


def trend_label(closes):
    """'up' when the 30-day and 60-day changes are both positive and the close is above its 30-day average,
    'down' for the mirror image, otherwise 'mixed'. A description of the past, not a forecast."""
    ret30 = closes[-1] / closes[-1 - WINDOW] - 1
    ret60 = closes[-1] / closes[-1 - TREND_LONG_DAYS] - 1
    above = closes[-1] > statistics.fmean(closes[-WINDOW:])
    if ret30 > 0 and ret60 > 0 and above:
        return "up", ret30, ret60
    if ret30 < 0 and ret60 < 0 and not above:
        return "down", ret30, ret60
    return "mixed", ret30, ret60


def add_context(summary, closes, volumes):
    """Adds the context block (trend, volume) and the context flags. The alert 'breaches' are not touched."""
    label, ret30, ret60 = trend_label(closes)
    context = {"trend": label, "ret_30d_pct": round_clean(ret30 * 100, 2), "ret_60d_pct": round_clean(ret60 * 100, 2)}
    flags = []
    ratios = volume_ratio_series(volumes)
    history = [r for r in ratios if r is not None]
    if ratios[-1] is not None and history:
        rank = percentile_rank(history, ratios[-1])
        context["volume_ratio_30d"] = round_clean(ratios[-1], 2)
        context["volume_rank"] = round_clean(rank, 1)
        if rank >= ALERT_RANK:
            flags.append("volume")
    summary["context"] = context
    summary["flags"] = flags
    return summary


def tool_market_summary(coin):
    try:
        stamps, closes, volumes = load_coin(coin, datetime.now(timezone.utc))
        return add_context(summarize(coin, stamps, closes), closes, volumes)
    except (RuntimeError, ValueError, statistics.StatisticsError) as error:
        return {"coin": coin, "error": str(error)}


# ---- cross-asset context: relative strength and correlation between the coins.
def align_series(series_by_coin):
    """{coin: (stamps, closes)} -> (common stamps, {coin: closes}) on the days every coin has.
    Every coin must end on the same day: a coin that is a day behind would be compared with the others on the wrong dates."""
    ends = {coin: stamps[-1] for coin, (stamps, _) in series_by_coin.items()}
    if len(set(ends.values())) > 1:
        raise ValueError("the coins end on different days: " + ", ".join(f"{coin} {format_date(end)}" for coin, end in ends.items()))
    common =sorted(set.intersection(*(set(stamps) for stamps, _ in series_by_coin.values())))
    if len(common) < MIN_HISTORY_DAYS:
        raise ValueError(f"only {len(common)} days in common, need at least {MIN_HISTORY_DAYS}")
    lookup = {coin: dict(zip(stamps, closes)) for coin, (stamps, closes) in series_by_coin.items()}
    return common, {coin: [lookup[coin][t] for t in common] for coin in series_by_coin}



def relative_strength_series(closes_by_coin):
    """{coin: spread}: for each day, the coin's 30-day return minus the mean 30-day return of the coins (None early on)."""
    n = len(next(iter(closes_by_coin.values())))
    spreads = {coin: [None] * n for coin in closes_by_coin}
    for i in range(WINDOW, n):
        returns = {coin: closes[i] / closes[i - WINDOW] - 1 for coin, closes in closes_by_coin.items()}
        mean = statistics.fmean(returns.values())
        for coin, value in returns.items():
            spreads[coin][i] = value - mean
    return spreads


def correlation_series(closes_a, closes_b):
    """Rolling correlation of the last 30 daily returns of two coins (None early on, or when a window is flat)."""
    n = len(closes_a)
    ra = [b / a - 1 for a, b in zip(closes_a, closes_a[1:])]
    rb = [b / a - 1 for a, b in zip(closes_b, closes_b[1:])]
    out = [None] * n
    for i in range(WINDOW, n):
        try:
            out[i] = statistics.correlation(ra[i - WINDOW:i], rb[i - WINDOW:i])
        except statistics.StatisticsError:
            out[i] = None
    return out


def cross_asset_summary(series_by_coin):
    """Relative strength and correlation for the coins that have data. Plain arithmetic, no AI."""
    stamps, closes_by_coin = align_series(series_by_coin)
    coins = list(closes_by_coin)
    spreads = relative_strength_series(closes_by_coin)
    strength, flags = [], []
    for coin in coins:
        history = [v for v in spreads[coin] if v is not None]
        rank = percentile_rank(history, history[-1])
        closes = closes_by_coin[coin]
        strength.append({"coin": coin, "ret_30d_pct": round_clean((closes[-1] / closes[-1 - WINDOW] - 1) * 100, 2),
                         "vs_group_pct": round_clean(history[-1] * 100, 2), "rank": round_clean(rank, 1)})
        if rank >= ALERT_RANK or rank <= UNUSUAL_LOW:
            flags.append(f"relative strength {coin}")
    correlations = []
    for a_index, a in enumerate(coins):
        for b in coins[a_index + 1:]:
            series = correlation_series(closes_by_coin[a], closes_by_coin[b])
            history = [v for v in series if v is not None]
            if not history or series[-1] is None:
                continue
            rank = percentile_rank(history, series[-1])
            correlations.append({"pair": f"{a}-{b}", "corr_30d": round_clean(series[-1], 2), "rank": round_clean(rank, 1)})
            if rank >= ALERT_RANK or rank <= UNUSUAL_LOW:
                flags.append(f"correlation {a}-{b}")
    return {"as_of": format_date(stamps[-1]), "history_days": len(stamps), "coins": coins,
            "relative_strength": strength, "correlations": correlations, "flags": flags}


def tool_cross_asset_summary():
    now = datetime.now(timezone.utc)
    series, unavailable = {}, {}
    for coin in WATCHLIST:
        try:
            stamps, closes, _ = load_coin(coin, now)
            series[coin] = (stamps, closes)
        except (RuntimeError, ValueError) as error:
            unavailable[coin] = str(error)
    if len(series) < 2:
        return {"error": f"at least two coins with valid data are needed; unavailable: {unavailable}"}
    try:
        result = cross_asset_summary(series)
    except (ValueError, statistics.StatisticsError) as error:
        return {"error": str(error)}
    if unavailable:
        result["unavailable"] = unavailable
    return result


# ---- market context from other free sources (sources.py): liquidity, derivatives, implied volatility, market-wide volume, sentiment.
def tool_market_context():
    try:
        return sources.fetch_context(WATCHLIST, time.time(), os.environ.get("COINGECKO_API_KEY", "").strip())
    except Exception as error:   # fetch_context never raises on a source problem; this keeps a coding error from stopping the run
        return {"error": f"market context failed ({type(error).__name__})"}


# ---- how often each coin was at the alert rank in its own history (a base rate, computed by code).
def tool_alert_history_stats():
    now = datetime.now(timezone.utc)
    coins, unavailable_coins, union = {}, {}, {}
    for coin in WATCHLIST:
        try:
            stamps, closes, _ = load_coin(coin, now)
            rate = base_rate(stamps, closes)
        except (RuntimeError, ValueError, statistics.StatisticsError) as error:
            unavailable_coins[coin] = str(error)
            continue
        if not rate["ranked_days"]:
            unavailable_coins[coin] = "history too short to count"
            continue
        for day, flag in zip(rate["days"], rate["flags"]):
            union[day] = union.get(day, False) or flag
        last = next((d for d, f in zip(reversed(rate["days"]), reversed(rate["flags"])) if f), None)
        coins[coin] = {"ranked_days": rate["ranked_days"], "alert_days": rate["alert_days"],
                       "share_pct": round(100.0 * rate["alert_days"] / rate["ranked_days"], 1),
                       "episodes": rate["episodes"], "last_alert_day": last}
    if not coins:
        return {"error": f"no coin has enough data: {unavailable_coins}"}
    flags = [union[d] for d in sorted(union)]
    result = {"coins": coins, "any_coin": {"ranked_days": len(flags), "share_pct": round(100.0 * sum(flags) / len(flags), 1),
                                            "episodes": episodes(flags)}}
    if unavailable_coins:
        result["unavailable"] = unavailable_coins
    return result


# ==============================================================================================
# 3. GOOGLE SHEET (Apps Script web app): memory tool and delivery. The key travels inside SHEET_URL.
# ==============================================================================================
def key_hint(url):
    """Say what is wrong with the key part of SHEET_URL without printing the key itself."""
    values = parse_qs(urlparse(url).query).get("key")
    if not values or not values[0]:
        return "SHEET_URL has no ?key=... part at the end"
    key = values[0]
    notes = [f"the key in SHEET_URL has {len(key)} characters"]
    if any(ch.isspace() for ch in key):
        notes.append("it contains a space or line break")
    if len(key) < 16:
        notes.append("it is shorter than 16 characters")
    return "; ".join(notes) + ". It must be identical to the KEY Script Property"


def url_shape(url):
    """The form of the web app URL with the long id hidden, to spot typos such as /exe or a missing part."""
    parsed = urlparse(url)
    parts = [f"<{len(part)} characters>" if len(part) > 30 else part for part in parsed.path.split("/")]
    return f"{parsed.scheme}://{parsed.netloc}{'/'.join(parts)}"


def sheet_post(payload):
    url = os.environ.get("SHEET_URL", "").strip()
    if not url:
        raise AgentError("SHEET_URL is not set")
    try:
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as error:
        raise AgentError("Sheet request failed: " + redact(repr(error), url) + f" (URL shape: {url_shape(url)})")
    log(f"[sheet] {payload['action']} -> HTTP {response.status_code}")
    if not isinstance(body, dict) or body.get("success") is not True:
        code = body.get("error") if isinstance(body, dict) else "unknown"
        extra = f" ({key_hint(url)})" if code == "UNAUTHORIZED" else ""
        raise AgentError(f"The Sheet web app refused the request: {code}{extra}")
    return body


def tool_recent_decisions():
    try:
        body = sheet_post({"action": "history", "limit": HISTORY_LIMIT})
    except AgentError as error:
        return {"error": f"history unavailable: {error}"}
    return {"decisions": body.get("decisions", [])}


def deliver(decision, version):
    """One row in the Agent tab; the web app also sends the email when alert is true."""
    payload = {
        "action": "save",
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": version,
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "alert": decision["alert"],
        "coins": decision["coins"],
        "message": decision["message"],
        "sources": decision["sources"],
        "reasoning": decision["reasoning"],
        "blocked_reason": decision["blocked_reason"],
        "review": review_line(decision),   # the short review status; an older web app ignores this field
    }
    body = sheet_post(payload)
    log(f"Delivered: row {body.get('row')}, email sent: {body.get('emailed')}")


def sheet_append(tab, rows):
    """Adds rows to one of the extra tabs. A problem here is a warning and never fails the run."""
    try:
        body = sheet_post({"action": "append", "tab": tab, "rows": rows})
    except AgentError as error:
        log(f"::warning::the {tab} tab was not updated: {error}. The deployed web app may be an older Code.gs.")
        return False
    log(f"[sheet] {tab}: {body.get('added')} row(s) added")
    return True


def last_result(evidence, tool, coin=None):
    """The most recent result of a tool (for one coin) in the evidence, or None."""
    for item in reversed(evidence):
        if item["tool"] == tool and (coin is None or item["args"].get("coin") == coin):
            return item["result"]
    return None


def market_rows(evidence, version, stamp, run_id):
    """One row per coin the agent looked at: the numbers fetched from Kraken and how they were ranked."""
    cross = last_result(evidence, "get_cross_asset_summary")
    strength = {x["coin"]: x for x in cross.get("relative_strength", [])} if isinstance(cross, dict) else {}
    rows = []
    for coin in WATCHLIST:
        s = last_result(evidence, "get_market_summary", coin)
        if s is None:
            continue
        if "error" in s:
            rows.append([stamp, run_id, version, coin, "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                         "ERROR: " + str(s["error"])[:200]])
            continue
        c, r, rs = s.get("context", {}), s["ranks"], strength.get(coin, {})
        rows.append([stamp, run_id, version, coin, s["as_of"], s["last_close"], s["daily_return_pct"],
                     s["volatility_30d_daily_pct"], s["dist_from_30d_avg_pct"], s["max_drawdown_30d_pct"],
                     r["volatility"], r["distance"], r["drawdown"], ", ".join(s["breaches"]),
                     c.get("trend", ""), c.get("ret_30d_pct", ""), c.get("ret_60d_pct", ""),
                     c.get("volume_ratio_30d", ""), c.get("volume_rank", ""), ", ".join(s.get("flags", [])),
                     rs.get("vs_group_pct", ""), rs.get("rank", ""), s["history_days"], "ok"])
    return rows


def cross_rows(evidence, version, stamp, run_id):
    cross = last_result(evidence, "get_cross_asset_summary")
    if not isinstance(cross, dict) or "error" in cross:
        return []
    flagged = set(cross.get("flags", []))
    return [[stamp, run_id, version, cross["as_of"], x["pair"], x["corr_30d"], x["rank"],
             "unusual" if f"correlation {x['pair']}" in flagged else ""] for x in cross.get("correlations", [])]


def context_rows(evidence, version, stamp, run_id):
    """One row per coin: what the free context sources returned, and how often the coin reached the alert rank."""
    context = last_result(evidence, "get_market_context")
    if not isinstance(context, dict) or "coins" not in context:
        return []
    stats = last_result(evidence, "get_alert_history_stats")
    stats = stats.get("coins", {}) if isinstance(stats, dict) else {}
    sentiment = context.get("sentiment") if sources.is_data(context.get("sentiment")) else {}
    flags = ", ".join(context.get("flags", []))
    rows = []
    for coin, piece in context["coins"].items():
        def part(name):
            value = piece.get(name)
            return value if sources.is_data(value) else {}
        liq, der, iv, wide, check = part("liquidity"), part("derivatives"), part("implied_volatility"), part("market_wide"), part("price_check")
        missing = [name for name in ("liquidity", "derivatives", "implied_volatility", "market_wide", "price_check") if not part(name)]
        s = stats.get(coin, {})
        rows.append([stamp, run_id, version, coin, liq.get("mid_price", ""), liq.get("spread_pct", ""),
                     liq.get("bid_depth_usd", ""), liq.get("ask_depth_usd", ""), liq.get("depth_band_pct", ""),
                     der.get("funding_rate_pct_per_hour", ""), der.get("funding_pays", ""), der.get("open_interest_usd_approx") or "",
                     iv.get("implied_vol_pct", ""), "" if iv.get("rank_1y") is None else iv["rank_1y"],
                     wide.get("volume_24h_usd", ""), wide.get("market_volume_ratio_30d", ""), wide.get("market_volume_rank", ""),
                     check.get("coingecko_price_usd", ""), check.get("difference_pct", ""),
                     sentiment.get("value", ""), sentiment.get("label", ""), s.get("share_pct", ""), s.get("episodes", ""),
                     flags, "ok" if not missing else "partial: " + ", ".join(missing)])
    return rows


def publish_market_data(evidence, version):
    """The extra tabs show what the agent saw: the Kraken numbers per coin and the relationships between coins."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    rows = market_rows(evidence, version, stamp, run_id)
    if rows:
        sheet_append("market_data", rows)
    rows = cross_rows(evidence, version, stamp, run_id)
    if rows:
        sheet_append("cross_asset", rows)
    rows = context_rows(evidence, version, stamp, run_id)
    if rows:
        sheet_append("context_data", rows)


# ==============================================================================================
# 4. PLAYBOOK TOOL: search playbook.md (RAG). Matches words, not meaning.
# ==============================================================================================
STOPWORDS = {"the", "and", "for", "are", "with", "that", "this", "when", "what", "from", "not", "any", "how"}


def load_playbook(path=PLAYBOOK_PATH):
    """Sections that start with a line beginning '## '. Text before the first one is ignored."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        raise AgentError("playbook.md could not be read")
    sections, title, body = [], None, []
    for line in lines:
        if line.startswith("## "):
            if title:
                sections.append({"title": title, "text": "\n".join(body).strip()})
            title, body = line[3:].strip(), []
        elif title:
            body.append(line)
    if title:
        sections.append({"title": title, "text": "\n".join(body).strip()})
    if not sections:
        raise AgentError("playbook.md has no sections")
    return sections


def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS}


def tool_search_playbook(query):
    wanted = tokens(query)
    scored = []
    for order, section in enumerate(load_playbook()):
        score = 3 * len(wanted & tokens(section["title"])) + len(wanted & tokens(section["text"]))
        if score:
            scored.append((-score, order, section))
    scored.sort(key=lambda item: item[:2])
    best = [section for _, _, section in scored[:2]]
    if not best:
        return {"sections": [], "note": "no section matched; try other words"}
    return {"sections": best}


# ==============================================================================================
# 5. TOOL REGISTRY AND THE RULES FOR CALLING TOOLS
# ==============================================================================================
TOOL_SPECS = {
    "get_market_summary": {
        "description": (
            "Live Kraken daily data for ONE watchlist coin: last close, percent distance from its 30-day "
            "average, 30-day daily volatility and 30-day maximum drawdown, each ranked 0-100 against that "
            "coin's own history, plus 'breaches' (the ranks at or above the alert rank). The 'context' block "
            "adds the trend (up, down or mixed), the 30 and 60-day changes, today's volume as a multiple of "
            "its 30-day average (volume_ratio_30d) and that volume's rank; 'flags' lists context that is "
            "unusual (rank 95 or higher). Returns an error object when the data is unavailable."
        ),
        "params": {"coin": {"type": "string", "enum": list(WATCHLIST)}},
    },
    "get_cross_asset_summary": {
        "description": (
            "How the watchlist coins relate to each other, from Kraken daily data: each coin's 30-day return "
            "against the group (relative strength) and the 30-day correlation of daily returns for each pair "
            "(BTC-ETH, BTC-XRP, ETH-XRP), each ranked 0-100 against its own history (near 100 unusually "
            "high, near 0 unusually low), plus 'flags' for ranks at or above 95 or at or below 5. "
            "Context only: it never raises an alert. Returns an error object when the data is unavailable."
        ),
        "params": {},
    },
    "get_market_context": {
        "description": (
            "Market context for every watchlist coin from other free public sources: order-book liquidity "
            "(spread, depth within 1%), the perpetual futures funding rate and who pays it, open interest, "
            "implied volatility with its rank (BTC and ETH only), market-wide 24-hour volume with its rank "
            "against its own history, a second price and whether the two prices agree, and the market-wide "
            "fear and greed index. Any field may say 'unavailable'; never estimate it. 'flags' lists what is "
            "unusual. Context only: it never raises an alert. Describe at most one or two facts of it."
        ),
        "params": {},
    },
    "get_alert_history_stats": {
        "description": (
            "How often each coin was at the alert rank in its own history (days ranked, alert days, share of "
            "days, separate episodes, latest alert day), and the same for any coin. A base rate for context; "
            "it never raises an alert."
        ),
        "params": {},
    },
    "get_recent_decisions": {
        "description": (
            "Your own most recent decisions from the Sheet (time, alert, coins, message), oldest first. "
            "Use it so you do not repeat an alert."
        ),
        "params": {},
    },
    "search_playbook": {
        "description": (
            "Search the playbook (own rules, thresholds, wording rules) by words. Returns the best "
            "matching sections with their exact titles. Cite those titles in 'sources'."
        ),
        "params": {"query": {"type": "string"}},
    },
}
TOOLS = {
    "get_market_summary": tool_market_summary,
    "get_cross_asset_summary": tool_cross_asset_summary,
    "get_market_context": tool_market_context,
    "get_alert_history_stats": tool_alert_history_stats,
    "get_recent_decisions": tool_recent_decisions,
    "search_playbook": tool_search_playbook,
}


def groq_tools():
    return [
        {"type": "function", "function": {
            "name": name,
            "description": spec["description"],
            "parameters": {"type": "object", "properties": spec["params"],
                           "required": list(spec["params"]), "additionalProperties": False},
        }}
        for name, spec in TOOL_SPECS.items()
    ]


def check_call(name, args):
    """Guardrail: None if the call is allowed, otherwise the reason it is not."""
    if not isinstance(name, str) or name not in TOOL_SPECS:
        return f"tool {name!r} is not on the allowed list {sorted(TOOL_SPECS)}"
    params = TOOL_SPECS[name]["params"]
    if not isinstance(args, dict) or set(args) != set(params):
        return f"{name} needs exactly these arguments: {sorted(params)}"
    for key in params:
        if not isinstance(args[key], str) or not args[key].strip():
            return f"argument {key!r} must be a non-empty string"
    if "coin" in args and args["coin"].strip().upper() not in WATCHLIST:
        return f"coin {args['coin']!r} is not on the watchlist {sorted(WATCHLIST)}"
    if "query" in args and len(args["query"]) > 200:
        return "query is longer than 200 characters"
    return None


def normalise_args(args):
    clean = {key: value.strip() for key, value in args.items()}
    if "coin" in clean:
        clean["coin"] = clean["coin"].upper()
    return clean


def run_tool(tools, name, args, evidence):
    """The code runs the tool, never the AI. Every call the AI asks for is logged."""
    problem = check_call(name, args)
    if problem:
        log(f"TOOL BLOCKED {name} {json.dumps(args, default=str)}: {problem}")
        return {"error": problem}
    args = normalise_args(args)
    log(f"TOOL {name} {json.dumps(args)}")
    try:
        result = tools[name](**args)
    except Exception as error:  # a broken tool must not crash the agent; it becomes an error result
        result = {"error": f"tool failed: {type(error).__name__}"}
    if isinstance(result, dict) and "error" in result:
        log(f"  -> {name} returned an error: {str(result['error'])[:200]}")
    evidence.append({"tool": name, "args": args, "result": result})
    return result


# ==============================================================================================
# 6. THE AI (Groq) AND THE PROMPTS
# ==============================================================================================
RULES = """Rules:
- Alert only when a playbook rule says the numbers justify it. Do not repeat an alert that your recent decisions already show.
- Use only numbers that the tools returned. If a tool returned an error, say the data was unavailable. Never estimate or invent a number.
- Describe what the numbers show. Give no advice and no prediction: never use the words buy, sell, long, short or hold, and never tell anyone what to do."""

FORMAT = """Your final answer is ONLY a JSON object and nothing else:
{"alert": true or false, "coins": ["BTC"], "message": "...", "sources": ["exact playbook section titles"], "reasoning": "..."}
- message: the alert text, at most 280 characters; an empty string when alert is false.
- sources: the playbook section titles you relied on, copied exactly from search_playbook results.
- reasoning: at most 600 characters; copy numbers exactly from the tool results."""

INTRO = ("You are a crypto market watch agent. This is for education only.\n"
         f"Watchlist: {', '.join(WATCHLIST)} (USD pairs). Your daily question: {QUESTION}\n")

SYSTEM_PROMPT = INTRO + """
How to work:
1. Call get_market_summary for every watchlist coin, and get_cross_asset_summary, get_market_context and get_alert_history_stats once each.
2. Call get_recent_decisions to see your own recent alerts.
3. Call search_playbook for the rules that apply (alert rules, repeat alerts, wording rules, and the rule for each metric that looks unusual).
4. Then give the final answer. Volume, relative strength, correlation and the market context (liquidity, funding, implied volatility, market-wide volume, sentiment, the price check) are context: describe at most one or two facts of them, and they never raise an alert alone. When a context field says unavailable, say so and never estimate it.

""" + RULES + "\n\n" + FORMAT

DECIDE_PROMPT = INTRO + "\nYou are given the evidence: everything the tools returned.\n\n" + RULES + "\n\n" + FORMAT

PLAN_PROMPT = INTRO + """
Write the plan for today's work as ONLY a JSON object:
{"steps": [{"tool": "...", "args": {...}, "reason": "..."}]}
Rules: at most %d steps; use only the tools below; every coin must be one of the watchlist. The plan must contain one get_market_summary step for every watchlist coin, one get_cross_asset_summary step, one get_market_context step, one get_alert_history_stats step, one get_recent_decisions step, and playbook searches for the rules you will need: alert rules, repeat alerts, wording rules, and the rule for each metric. Plan the steps in a way that leaves room for all of them: one search query may name several rules, for example "alert rules repeat alerts wording".
Tools:
%s"""

# The AI review answers with codes from this closed list, so a verdict can be logged, counted and acted on without reading prose.
REVIEW_CODES = {
    "UNSUPPORTED_CLAIM": "the message or reasoning says something the digest does not show",
    "WRONG_COIN": "a number or statement belongs to a different coin than the one it names",
    "OVERSTATED": "the wording is stronger than the numbers (for example 'crash' for a small move)",
    "CAUSE_NOT_IN_DATA": "it names a cause or reason that no data gives",
    "ADVICE_OR_FORECAST": "it advises, predicts or tells anyone to act",
    "MISSING_CAVEAT": "a source was unavailable or partial and the draft does not say so",
    "OTHER": "any other problem against the playbook rules",
}

REVIEW_PROMPT = """You confirm the draft alert of a crypto watch agent (education only). You get the playbook rules that apply, a digest of the evidence (the alerted coins in full, the other coins in brief; what the tools returned) and the draft. The code has already checked the numbers, the wording words and the alert rank; you look for what code cannot check.
Report a problem with one of these codes:
%s
Reply with ONLY a JSON object: {"approved": true or false, "codes": ["CODE"], "notes": ["one short sentence per code"]}. Approve (true, with empty lists) when none applies.""" % "\n".join(
    f"- {code}: {meaning}" for code, meaning in REVIEW_CODES.items())


def tool_list_text():
    return "\n".join(f"- {name}({', '.join(spec['params'])}): {spec['description']}"
                     for name, spec in TOOL_SPECS.items())


def call_llm(messages, tools=None, max_tokens=None, model=None):
    """One Groq chat call. Waits and retries on a rate limit. Returns the assistant message.
    max_tokens and model are optional and only the AI review call sets them; every other caller gets the defaults (no cap, MODEL)."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise AgentError("GROQ_API_KEY is not set")
    payload = {"model": model or MODEL, "messages": messages, "temperature": 0}
    if max_tokens:
        payload["max_tokens"] = max_tokens
        if payload["model"].startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = REVIEW_REASONING_EFFORT
    if tools:
        payload.update(tools=tools, tool_choice="auto")
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        try:
            response = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=90)
        except requests.RequestException as error:
            raise AgentError(f"Groq request failed: {type(error).__name__}")
        if response.status_code == 429 and attempt < MAX_LLM_ATTEMPTS:
            try:
                wait = float(response.headers.get("retry-after", 20))
            except ValueError:
                wait = 20
            wait = min(max(wait, 5) + 3, 60)  # the token window rolls, so a short hint is often not enough
            STATS["rate_limit_waits"] += 1
            log(f"Groq rate limit: waiting {wait:.0f}s (attempt {attempt} of {MAX_LLM_ATTEMPTS})")
            time.sleep(wait)
            continue
        if response.status_code == 400 and "tool_use_failed" in response.text:
            STATS["llm_calls"] += 1  # the model produced a malformed tool call; treated as an empty reply
            return {"role": "assistant", "content": ""}
        if not response.ok:
            raise AgentError(f"Groq error {response.status_code}: {response.text[:300]}")
        STATS["llm_calls"] += 1
        body = response.json()
        usage = body.get("usage")
        used = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(used, int) and not isinstance(used, bool) and used > 0:
            STATS["llm_tokens"] += used
        message = body["choices"][0]["message"]
        if body["choices"][0].get("finish_reason") == "length":
            message = dict(message, finish_reason="length")   # the reply was cut off at max_tokens; only the AI review reads this
        return message
    raise AgentError("Groq kept answering with a rate limit")


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ShapeError("the reply holds no JSON object (empty reply or malformed tool call)")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        raise ShapeError("the reply is not valid JSON")
    if not isinstance(data, dict):
        raise ShapeError("the JSON must be an object")
    return data


def parse_decision(text):
    """Guardrail: the answer must be JSON with the right keys and shapes, or the AI is asked again."""
    data = extract_json(text)
    missing = [k for k in ("alert", "coins", "message", "sources", "reasoning") if k not in data]
    if missing:
        raise ShapeError(f"missing keys: {missing}")
    if not isinstance(data["alert"], bool):
        raise ShapeError("'alert' must be true or false")
    if not isinstance(data["coins"], list) or not all(isinstance(c, str) for c in data["coins"]):
        raise ShapeError("'coins' must be a list of coin symbols")
    coins = [c.strip().upper() for c in data["coins"]]
    bad = [c for c in coins if c not in WATCHLIST]
    if bad:
        raise ShapeError(f"coins {bad} are not on the watchlist {sorted(WATCHLIST)}")
    if not isinstance(data["message"], str) or not isinstance(data["reasoning"], str):
        raise ShapeError("'message' and 'reasoning' must be text")
    if not isinstance(data["sources"], list) or not all(isinstance(s, str) for s in data["sources"]):
        raise ShapeError("'sources' must be a list of section titles")
    message = data["message"].strip()
    if data["alert"] and not message:
        raise ShapeError("an alert needs a message")
    if data["alert"] and not coins:
        raise ShapeError("an alert needs at least one coin")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ShapeError(f"the message is longer than {MAX_MESSAGE_CHARS} characters")
    return {
        "alert": data["alert"],
        "coins": coins,
        "message": message if data["alert"] else "",
        "sources": [s.strip() for s in data["sources"]],
        "reasoning": data["reasoning"].strip()[:MAX_REASONING_CHARS],
    }


def evidence_text(evidence):
    return json.dumps(evidence, indent=1, default=str)


# ==============================================================================================
# 7. VERSION 1: THE AGENT LOOP. The AI asks for tools, the code runs them, the AI answers.
# ==============================================================================================
def run_agent_loop(tools):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Do today's watch now."}]
    evidence, fixes = [], 0
    for step in range(1, MAX_STEPS + 1):
        message = call_llm(messages, tools=groq_tools())
        calls = message.get("tool_calls") or []
        if calls:
            messages.append({"role": "assistant", "content": message.get("content") or None, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
                for c in calls]})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = None
                result = run_tool(tools, name, args, evidence)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})
            continue
        content = message.get("content") or ""
        try:
            return parse_decision(content), evidence
        except ShapeError as error:
            fixes += 1
            log(f"Answer not valid ({error}); asking again ({fixes} of {MAX_FIX_ATTEMPTS})")
            if fixes > MAX_FIX_ATTEMPTS:
                raise AgentError(f"The AI did not give a valid answer after {MAX_FIX_ATTEMPTS} retries: {error}")
            messages.append({"role": "assistant", "content": content or "(empty)"})
            messages.append({"role": "user", "content":
                             f"That was not valid: {error}. Call a tool with valid arguments, or reply with ONLY the final JSON object."})
    raise AgentError(f"The agent did not finish in time (step limit {MAX_STEPS})")


# ==============================================================================================
# 8. VERSION 2: THE PLANNER. The AI writes a plan, the code validates it and runs every step,
#    then the AI decides from the results. The AI never runs a tool.
# ==============================================================================================
def validate_plan(data):
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ShapeError("'steps' must be a non-empty list")
    if len(steps) > MAX_PLAN_STEPS:
        raise ShapeError(f"the plan has {len(steps)} steps, the maximum is {MAX_PLAN_STEPS}")
    clean = []
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or set(step) != {"tool", "args", "reason"}:
            raise ShapeError(f"step {number} needs exactly the keys tool, args, reason")
        if not isinstance(step["reason"], str) or not step["reason"].strip():
            raise ShapeError(f"step {number} needs a reason")
        problem = check_call(step["tool"], step["args"])
        if problem:
            raise ShapeError(f"step {number}: {problem}")
        clean.append({"tool": step["tool"], "args": normalise_args(step["args"]), "reason": step["reason"].strip()})
    return clean


def make_plan():
    messages = [{"role": "system", "content": PLAN_PROMPT % (MAX_PLAN_STEPS, tool_list_text())},
                {"role": "user", "content": "Write the plan."}]
    for attempt in (1, 2):
        reply = call_llm(messages).get("content") or ""
        try:
            return validate_plan(extract_json(reply))
        except ShapeError as error:
            if attempt == 2:
                raise AgentError(f"The plan was not valid twice, so the run stops: {error}")
            log(f"The plan was not valid, trying again: {error}")
            messages.append({"role": "assistant", "content": reply or "(empty)"})
            messages.append({"role": "user", "content": f"The plan was not valid: {error}. Write a corrected plan as ONLY the JSON object."})


def ask_for_decision(evidence):
    messages = [{"role": "system", "content": DECIDE_PROMPT},
                {"role": "user", "content": "Evidence:\n" + evidence_text(evidence) + "\n\nGive the final answer."}]
    for fixes in range(MAX_FIX_ATTEMPTS + 1):
        content = call_llm(messages).get("content") or ""
        try:
            return parse_decision(content)
        except ShapeError as error:
            log(f"Answer not valid ({error}); asking again ({fixes + 1} of {MAX_FIX_ATTEMPTS})")
            messages.append({"role": "assistant", "content": content or "(empty)"})
            messages.append({"role": "user", "content": f"That was not valid: {error}. Reply with ONLY the final JSON object."})
    raise AgentError(f"The AI did not give a valid answer after {MAX_FIX_ATTEMPTS} retries")


def run_planner(tools):
    plan = make_plan()
    log(f"Plan accepted: {len(plan)} steps")
    evidence = []
    for step in plan:
        log(f"STEP {step['tool']}: {step['reason']}")
        run_tool(tools, step["tool"], step["args"], evidence)
    return ask_for_decision(evidence), evidence


# ==============================================================================================
# 9. THE REVIEW STAGE. It runs on every run and always says what it did.
#    The code review (eight checks, no tokens) runs every time. The AI review (one small call) runs only when an alert is
#    drafted, because an alert is a claim and a calm day has nothing to confirm. --reviewer off skips only the AI call.
#    A finding that the code can state as a fact goes straight to the one revision: the AI is never asked to review a draft that
#    is already known to be wrong. Before the review, the code completes the evidence: it runs every data tool that the AI left out
#    (complete_evidence), so what is reviewed and what is saved does not depend on which tools the AI chose to call.
#    The AI review request is kept under a cap (REVIEW_REQUEST_CAP, an estimate) and is trimmed in a fixed order when it is over.
# ==============================================================================================
ALLOWED_NUMBERS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 30, 31, 90, 95, 100, 280, 600}
NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d[\d,]*\.?\d*")


def numbers_in(text):
    found = []
    for raw in NUMBER_RE.findall(text):
        cleaned = raw.replace(",", "").rstrip(".")
        try:
            found.append((raw, float(cleaned), len(cleaned.split(".")[1]) if "." in cleaned else 0))
        except ValueError:
            continue
    return found


def unsupported_numbers(text, evidence_json):
    """Numbers in text that no number in the evidence matches (to the precision that was written)."""
    known = [abs(value) for _, value, _ in numbers_in(evidence_json)]
    bad = []
    for raw, value, decimals in numbers_in(text):
        tolerance = 0.5 * 10 ** -decimals + 1e-9
        if abs(value) in ALLOWED_NUMBERS or any(abs(abs(value) - k) <= tolerance for k in known):
            continue
        bad.append(raw)
    return bad


def number_problem(numbers):
    return ("numbers not in the evidence: " + ", ".join(dict.fromkeys(numbers)))[:200]


# ---- R3: unsafe text. The check names the kind of text it found and never repeats the text, so its detail is safe to log.
KEY_LIKE_RE = re.compile(r"\b(?:gsk|sk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}|\bAKIA[0-9A-Z]{12,}\b|[A-Za-z0-9+/=_-]{32,}|"
                         r"\b(?:api[ _-]?key|secret|password|passwd|bearer)\b\s*[:=]|\bkey=", re.IGNORECASE)
HYGIENE_PATTERNS = {
    "a link": re.compile(r"https?:|\bwww\.|\]\(|\b[a-z0-9-]+\.(?:com|org|net|io|ai|app|dev|xyz|info|biz|ly|me)\b", re.IGNORECASE),
    "markup or code": re.compile(r"</?[a-z][^<>]*>|`|~~~", re.IGNORECASE),
    "key-like text": KEY_LIKE_RE,
    "an instruction": re.compile(
        r"\bignore\s+(?:(?:all|any|the|your|my|previous|prior|above|earlier)\s+){1,3}(?:instructions?|rules?|prompts?|messages?|guidelines?|directions?)\b|"
        r"\bignore\s+(?:previous|prior|above|earlier)\b|\bdisregard\s+(?:all|any|the|your|previous|prior|above)\b|\bsystem\s+prompt\b|"
        r"\byou\s+are\s+now\b|\bnew\s+instructions?\b|\bforget\s+(?:all|your|the|previous)\b|\breveal\s+(?:the|your)\b|\bjailbreak\b|"
        r"\boverride\s+(?:the\s+|your\s+)?(?:rules|instructions|guardrails)\b", re.IGNORECASE),
}


def hygiene_kinds(text):
    """The kinds of unsafe text in a string: a link, markup or code, key-like text, an instruction. Names only."""
    plain = plain_text(text)
    return [kind for kind, pattern in HYGIENE_PATTERNS.items() if pattern.search(plain)]


def scrub(text):
    """Text that is about to be logged, with key-like text and any key=... part hidden."""
    return redact(KEY_LIKE_RE.sub("[hidden]", str(text)))


def latest_completed_day(now):
    """The UTC date of the newest completed daily candle on the day `now`: yesterday."""
    return format_date(int(now.timestamp()) // SECONDS_PER_DAY * SECONDS_PER_DAY - SECONDS_PER_DAY)


# ---- the evidence is completed by code, so it does not depend on which tools the AI chose to call.
# The tools that only read data. search_playbook is not one of them: it is the AI's own look-up of the rules, and the sources that an alert
# cites must come from what the AI searched (check R4).
DATA_TOOLS = ("get_market_summary", "get_cross_asset_summary", "get_market_context", "get_alert_history_stats", "get_recent_decisions")


def tool_label(item):
    """How an evidence item is named in the logs and in check R5: the tool, with the coin for a market summary."""
    coin = item["args"].get("coin")
    return f"{item['tool']}({coin})" if coin else item["tool"]


def was_called(evidence, tool, coin=None):
    """True when the evidence holds a result of the tool (for the coin), an error result included: a tool that failed was still called."""
    return any(item["tool"] == tool and (coin is None or item["args"].get("coin") == coin) for item in evidence)


def complete_evidence(tools, evidence):
    """Runs, with run_tool, every data tool that the AI did not call (a market summary for every watchlist coin, the cross-asset summary, the
    market context, the alert history stats and the recent decisions), so the evidence is the same whichever tools the AI chose. The results
    are appended to the same evidence list. A tool that fails stays in the evidence as an error result; nothing here raises. A tool that the
    AI already called is never called again. A tool that is not in `tools` is skipped. Code only: no tokens. Returns the labels of what it added.
    The draft was written without these extra results and is checked against the full evidence. It is not written again just because evidence
    was added: it changes only through the normal finding paths (a missed breach, a number problem, advice wording)."""
    wanted = []
    for name in DATA_TOOLS:
        if name not in tools:
            continue
        if name == "get_market_summary":
            wanted += [(name, {"coin": coin}) for coin in WATCHLIST if not was_called(evidence, name, coin)]
        elif not was_called(evidence, name):
            wanted.append((name, {}))
    added = []
    for name, args in wanted:
        before = len(evidence)
        run_tool(tools, name, args, evidence)
        if len(evidence) > before:
            added.append(tool_label(evidence[-1]))
    log("REVIEW evidence completed by code: " + (", ".join(added) or "nothing missing"))
    return added


# ---- the eight code checks. Each returns (passed, detail). A check that has nothing to look at (no alert drafted) passes and says so.
CHECK_NAMES = {"R1": "numbers", "R2": "wording", "R3": "hygiene", "R4": "sources", "R5": "coverage", "R6": "freshness",
               "R7": "breach", "R8": "corroboration"}
NO_ALERT = "no alert drafted"


def check_numbers(draft, shown):
    """R1: every number in the message and the reasoning is in the evidence. The message is read the way the guardrail reads it
    (a scale such as 59.1k is understood); the reasoning keeps the stricter reader that the reviewer always used."""
    found = unsupported_message_numbers(draft["message"], shown) + unsupported_numbers(draft["reasoning"], shown)
    return (False, number_problem(found)) if found else (True, "every number is in the evidence")


def check_wording(draft):
    """R2: no advice or forecast wording in the message or in the reasoning (the guardrail itself only reads the message)."""
    where = [name for name in ("message", "reasoning") if has_advice_language(draft[name])]
    return (False, "advice or forecast wording in the " + " and the ".join(where)) if where else (True, "no advice or forecast wording")


def check_hygiene(draft):
    """R3: no link, markup, key-like text or instruction-like text in the message or in the reasoning."""
    found = []
    for name in ("message", "reasoning"):
        kinds = hygiene_kinds(draft[name])
        if kinds:
            found.append(f"the {name} holds " + " and ".join(kinds))
    return (False, "; ".join(found)) if found else (True, "no link, markup, key-like text or instruction")


def check_cited_sources(draft, evidence, titles):
    """R4: every cited source is a real playbook section title, and the playbook was searched in this run."""
    if not draft["alert"]:
        return True, NO_ALERT
    known = {title.lower() for title in titles}
    wrong = [s for s in draft["sources"] if s.strip().lower() not in known]
    problems = ["the alert cites no source"] if not draft["sources"] else []
    if wrong:
        problems.append(f"{len(wrong)} cited source(s) are not playbook section titles")
    if not any(item["tool"] == "search_playbook" for item in evidence):
        problems.append("the playbook was not searched in this run")
    return (False, "; ".join(problems)) if problems else (True, "every source is a playbook section and the playbook was searched")


def unusable(item):
    """True when a data tool's latest result is an error (or not a dict; the recent decisions must also hold a list)."""
    result = item["result"]
    if not isinstance(result, dict) or "error" in result:
        return True
    return item["tool"] == "get_recent_decisions" and not isinstance(result.get("decisions"), list)


def check_coverage(draft, evidence, added=()):
    """R5: no data tool returned an error and no coin is without data (an alerted coin must have a market summary too). It does not fail
    because the AI skipped a tool: complete_evidence ran what was missing. It runs on calm days as well. The detail records how many
    tools the AI called itself and which ones the code added. The text of an error is not repeated here."""
    latest = {tool_label(item): item for item in evidence}
    by_code = [label for label in latest if label in added]
    origin = f"AI called {len(latest) - len(by_code)}, code added {len(by_code)}" + (f": {', '.join(by_code)}" if by_code else "")
    have = {summary.get("coin") for summary in market_summaries(evidence)}
    tried = {item["args"].get("coin") for item in evidence if item["tool"] == "get_market_summary"}
    wanted = list(dict.fromkeys([coin for coin in WATCHLIST if coin in tried] + (draft["coins"] if draft["alert"] else [])))
    no_data = [coin for coin in wanted if coin not in have]
    errors = [label for label, item in latest.items() if item["tool"] in DATA_TOOLS and unusable(item)
              and not (item["tool"] == "get_market_summary" and item["args"].get("coin") in no_data)]
    problems = []
    if no_data:
        problems.append(f"no valid market data for {', '.join(no_data)}")
    if errors:
        problems.append("tool result is an error: " + ", ".join(errors))
    if problems:
        return False, "; ".join(problems) + f" ({origin})"
    return True, f"every data tool result is usable and every coin has data ({origin})"


def check_freshness(draft, evidence, now):
    """R6: each alerted coin's data is dated the latest completed UTC day, never the open candle."""
    if not draft["alert"]:
        return True, NO_ALERT
    latest = latest_completed_day(now)
    stale = []
    for coin in draft["coins"]:
        summary = last_result(evidence, "get_market_summary", coin)
        if isinstance(summary, dict) and "error" not in summary and summary.get("as_of") != latest:
            stale.append(coin)
    return (False, f"{', '.join(stale)} not dated {latest}") if stale else (True, f"dated {latest}")


def breach_comparison(draft, evidence):
    """The comparison of the AI's answer with the code's own rank check, without logging: (breached, check, missed, unverified).
    A coin that the last HISTORY_LIMIT decisions already alerted is not a miss when the history is in the evidence (the repeat rule)."""
    breached = code_breached_coins(evidence)
    if not market_summaries(evidence):
        return breached, "unverified: no valid market data in this run", [], True
    if draft["alert"]:
        extra = [str(coin) for coin in draft["coins"] if coin not in breached]   # apply_guardrails blocks and logs these
        return breached, (f"mismatch: {', '.join(extra)} alerted but not at the alert rank" if extra else "agrees"), [], False
    missed = [coin for coin in breached if coin not in (recently_alerted_coins(evidence) or set())]
    return breached, (f"mismatch: {', '.join(missed)} at the alert rank, AI said no alert" if missed else "agrees"), missed, False


def corroboration_tier(piece):
    """R8, for one coin: 'corroborated' (a second price source agrees and the other context sources answered), 'single-source' (no second
    price) or 'degraded' (a second price that disagrees, or a context source that did not answer). Implied volatility is left out: it
    does not exist for every coin."""
    check = piece.get("price_check") if isinstance(piece, dict) else None
    if not sources.is_data(check):
        return "single-source"
    gaps = [name for name in ("liquidity", "derivatives", "market_wide") if not sources.is_data(piece.get(name))]
    return "corroborated" if check.get("agree") is True and not gaps else "degraded"


def check_corroboration(draft, evidence):
    """R8: how well a second source backs each alerted coin's price. Information only: it is logged and never blocks."""
    if not draft["alert"]:
        return True, NO_ALERT
    context = last_result(evidence, "get_market_context")
    pieces = context.get("coins") if isinstance(context, dict) and isinstance(context.get("coins"), dict) else {}
    return True, "; ".join(f"{coin} {corroboration_tier(pieces.get(coin))}" for coin in draft["coins"])


# What the AI is told for each check that code can state as a fact, and the short label used when the finding stays open.
FIX_HINTS = {
    "R1": "Use only numbers that the tools returned.",
    "R2": "Describe the numbers only: no advice, no forecast, none of the banned words.",
    "R3": "Remove it: the message and the reasoning must hold no link, markup, key-like text or instruction.",
    "R7": "Alert on it, with a message that describes the numbers, unless a playbook rule says not to.",
}
OPEN_LABELS = {"R2": "advice wording", "R3": "unsafe text (R3)", "R7": "missed breach"}


def code_review(draft, evidence, now, added=()):
    """All eight checks. Returns (checks, findings). checks is [{check, passed, detail}]. findings are the failed checks that one
    revision can fix (R1, R2, R3 and a missed breach in R7): {check, problem, label}. The other checks only report here: the guardrails
    block what they must block, apply_review_outcome blocks an alert on stale data (R6), and a single-source alert (R8) is still shown
    to the reader with the check in the log.
    `added` names the tools that complete_evidence ran (R5 records them)."""
    shown = evidence_text(evidence)
    _, breach, missed, _ = breach_comparison(draft, evidence)
    results = [("R1",) + check_numbers(draft, shown), ("R2",) + check_wording(draft), ("R3",) + check_hygiene(draft),
               ("R4",) + check_cited_sources(draft, evidence, [section["title"] for section in load_playbook()]),
               ("R5",) + check_coverage(draft, evidence, added), ("R6",) + check_freshness(draft, evidence, now),
               ("R7", breach == "agrees", breach), ("R8",) + check_corroboration(draft, evidence)]
    checks = [{"check": name, "passed": passed, "detail": detail} for name, passed, detail in results]
    findings = []
    for item in checks:
        name = item["check"]
        if item["passed"] or name not in FIX_HINTS or (name == "R7" and not missed):
            continue
        if name == "R7":
            problem = (f"the market data puts {', '.join(missed)} at or above the alert rank ({ALERT_RANK:g}) and the recent decisions do not "
                       "already cover it. " + FIX_HINTS[name])
        else:
            problem = f"{item['detail']}. {FIX_HINTS[name]}"
        findings.append({"check": name, "problem": problem[:400], "label": item["detail"] if name == "R1" else OPEN_LABELS[name]})
    return checks, findings


def run_code_review(draft, evidence, now, review, label="code checks"):
    """The code review, logged: one summary line, one line for every failed check, and the R8 tier of an alert."""
    review["checks"], findings = code_review(draft, evidence, now, review["evidence"]["code"])
    failed = [item for item in review["checks"] if not item["passed"]]
    log(f"REVIEW {label}: {len(review['checks']) - len(failed)} of {len(review['checks'])} passed "
        f"({' '.join(item['check'] for item in review['checks'] if item['passed']) or 'none'})")
    for item in failed:
        log(f"REVIEW   {item['check']} {CHECK_NAMES[item['check']]} failed: {scrub(item['detail'])}")
    if draft["alert"]:
        log("REVIEW   R8 corroboration: " + next(item["detail"] for item in review["checks"] if item["check"] == "R8"))
    return findings


# ---- the AI review: small input, small answer.
DIGEST_FIELDS = ("error", "as_of", "last_close", "daily_return_pct", "dist_from_30d_avg_pct", "volatility_30d_daily_pct",
                 "max_drawdown_30d_pct", "ranks", "breaches", "context", "flags")
OTHER_FIELDS = ("last_close", "daily_return_pct", "ranks", "breaches")   # what the AI review sees of a coin that is not alerted
REVIEW_RULES = ("Alert rules", "Wording rules")                      # always sent to the AI review
METRIC_RULES = {"volatility": "Volatility rule", "distance": "Trend against the 30-day average", "drawdown": "Drawdown rule"}


def review_digest(draft, evidence, with_others=True):
    """What the AI review sees of the evidence, built by code: the alerted coins in full, and the other coins in brief (four fields each,
    so a number that belongs to another coin can be recognised). Not the tool JSON, not the playbook text the AI searched, not the free
    text of a market summary and not the text of the old decisions (only their time and coins). with_others=False is the second trim step."""
    context = last_result(evidence, "get_market_context")
    cross = last_result(evidence, "get_cross_asset_summary")
    stats = last_result(evidence, "get_alert_history_stats")
    history = last_result(evidence, "get_recent_decisions")
    coins = {}
    for coin in draft["coins"]:
        summary = last_result(evidence, "get_market_summary", coin)
        entry = {key: summary[key] for key in DIGEST_FIELDS if isinstance(summary, dict) and key in summary}
        if isinstance(context, dict) and isinstance(context.get("coins"), dict) and coin in context["coins"]:
            entry["market_context"] = context["coins"][coin]
        if isinstance(cross, dict) and "error" not in cross:
            entry["relative_strength"] = [x for x in cross.get("relative_strength", []) if isinstance(x, dict) and x.get("coin") == coin]
            entry["correlations"] = [x for x in cross.get("correlations", []) if isinstance(x, dict) and coin in str(x.get("pair", "")).split("-")]
        if isinstance(stats, dict) and isinstance(stats.get("coins"), dict) and coin in stats["coins"]:
            entry["alert_history"] = stats["coins"][coin]
        coins[coin] = entry
    digest = {"coins": coins}
    if with_others:
        others = {}
        for coin in WATCHLIST:
            summary = last_result(evidence, "get_market_summary", coin)
            if coin not in draft["coins"] and isinstance(summary, dict) and "error" not in summary:
                others[coin] = {key: summary[key] for key in OTHER_FIELDS if key in summary}
        if others:
            digest["other_coins"] = others
    if isinstance(context, dict) and "error" not in context:
        digest["sentiment"], digest["context_flags"] = context.get("sentiment"), context.get("flags", [])
    if isinstance(history, dict) and isinstance(history.get("decisions"), list):
        digest["recent_alerts"] = [{"time": d.get("time"), "coins": d.get("coins")} for d in history["decisions"]
                                   if isinstance(d, dict) and d.get("alert") is True]
    errors = {}
    for tool in TOOL_SPECS:
        result = last_result(evidence, tool)
        if isinstance(result, dict) and "error" in result:
            errors[tool] = str(result["error"])[:120]
    if errors:
        digest["tool_errors"] = errors
    return digest


def review_rule_titles(draft, evidence):
    """The playbook sections the AI review gets, in the order they are sent: the two fixed ones first, then the section of the first metric
    that an alerted coin breaches. Three at most."""
    breached = set()
    for coin in draft["coins"]:
        summary = last_result(evidence, "get_market_summary", coin)
        if isinstance(summary, dict) and isinstance(summary.get("breaches"), list):
            breached.update(summary["breaches"])
    return list(REVIEW_RULES) + [title for metric, title in METRIC_RULES.items() if metric in breached][:1]


def review_rules(draft, evidence, titles=None):
    """The playbook rules text for the AI review. Only the rule lines: the keywords and the reasons are left out."""
    titles = review_rule_titles(draft, evidence) if titles is None else titles
    sections = {section["title"]: section["text"] for section in load_playbook()}
    parts = []
    for title in titles:
        lines = []
        for line in sections.get(title, "").splitlines():
            if line.startswith("Reason"):
                break
            if not line.startswith("Keywords:"):
                lines.append(line)
        if lines:
            parts.append(f"## {title}\n" + "\n".join(lines).strip())
    return "\n\n".join(parts)


def estimate_tokens(messages):
    """An ESTIMATE of the request size in tokens: the characters of every message divided by 4, rounded up. It is not Groq's count (the
    run reports that one); it only has to be good enough to keep one request far below what the free tier refuses."""
    return -(-sum(len(message["content"]) for message in messages) // 4)


def review_messages_for(draft, evidence, titles, with_others):
    """The request. The text that stays the same from run to run comes first (the instructions, then the fixed playbook rules) and the text
    of this run comes last (the digest, then the draft), so the start of the request is identical every time."""
    rules = review_rules(draft, evidence, titles) or "(left out to keep the request small)"
    shown = {key: draft[key] for key in ("alert", "coins", "message", "sources", "reasoning")}
    digest = json.dumps(review_digest(draft, evidence, with_others), separators=(",", ":"), default=str)
    content = (f"Playbook rules:\n{rules}\n\nDigest of the evidence (alerted coins in full, other coins in brief):\n{digest}"
               f"\n\nDraft decision:\n{json.dumps(shown)}")
    return [{"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content": content}]


def review_request(draft, evidence, cap=None):
    """The AI review request, kept within the cap (REVIEW_REQUEST_CAP estimated tokens, system plus user). When it is over, it is trimmed in
    this fixed order, one piece at a time, until it fits: the playbook excerpts (the metric rule, then the wording rules, then the alert
    rules), then the other coins. The draft and the alerted coins' numbers are never trimmed. Returns {messages, tokens_estimate, trimmed,
    fits}; fits is False when even the trimmed request is over the cap (then the AI is not called)."""
    cap = REVIEW_REQUEST_CAP if cap is None else cap
    titles, with_others, trimmed = review_rule_titles(draft, evidence), True, []
    while True:
        messages = review_messages_for(draft, evidence, titles, with_others)
        size = estimate_tokens(messages)
        if size <= cap:
            return {"messages": messages, "tokens_estimate": size, "trimmed": trimmed, "fits": True}
        if titles:
            trimmed.append("playbook excerpt: " + titles.pop())
        elif with_others:
            with_others = False
            trimmed.append("other coins")
        else:
            return {"messages": messages, "tokens_estimate": size, "trimmed": trimmed, "fits": False}


def review_messages(draft, evidence):
    return review_request(draft, evidence)["messages"]


def parse_verdict(text):
    """The AI review's reply as {approved, codes, notes}. A reply of the wrong shape raises ShapeError (the reviewer is then unusable and the
    alert is never called approved). Codes outside the closed list become OTHER. A 'no' without a code is still a 'no' (OTHER)."""
    data = extract_json(text)
    if not isinstance(data.get("approved"), bool):
        raise ShapeError("the reviewer reply has no true or false 'approved'")
    codes, notes = data.get("codes", []), data.get("notes", data.get("problems", []))
    if not isinstance(codes, list) or not isinstance(notes, list):
        raise ShapeError("the reviewer reply has codes or notes that are not lists")
    codes = list(dict.fromkeys(str(c).strip().upper() if str(c).strip().upper() in REVIEW_CODES else "OTHER" for c in codes))[:6]  # a code that is not on the list is OTHER
    if data["approved"] and codes:
        raise ShapeError("the reviewer approved and listed problems in the same reply")
    if not data["approved"] and not codes:
        codes = ["OTHER"]
    return {"approved": data["approved"], "codes": codes, "notes": [str(n)[:160] for n in notes][:4]}


def ai_review(messages, review):
    """One compact AI review call. Returns the verdict, or None when the reviewer gave no usable answer. Never a guess. A reply that was
    cut off at the small token cap is not usable, even when the part that arrived looks complete."""
    review["ai_called"] = True
    before = STATS["llm_tokens"]
    try:
        reply = call_llm(messages, max_tokens=REVIEW_MAX_TOKENS, model=reviewer_model())
        if reply.get("finish_reason") == "length":
            raise ShapeError(f"the reply was cut off at the {REVIEW_MAX_TOKENS} token cap")
        return parse_verdict(reply.get("content") or "")
    except (ShapeError, AgentError) as error:
        review["ai_reason"] = "reviewer call failed" if isinstance(error, AgentError) else "reviewer reply not usable"
        log(f"REVIEW AI review gave no usable answer ({review['ai_reason']}: {scrub(error)[:150]})")
        return None
    finally:
        used = STATS["llm_tokens"] - before   # the provider's own count; nothing is added when it did not report one
        if used > 0:
            review["tokens"] = review.get("tokens", 0) + used


def revise_draft(draft, problems, evidence):
    content = ("Evidence:\n" + evidence_text(evidence) + f"\n\nYour draft:\n{json.dumps(draft)}\n\n"
               "The review found these problems:\n- " + "\n- ".join(problems) +
               "\n\nReturn the corrected final JSON. If the problems cannot be fixed, set alert to false.")
    reply = call_llm([{"role": "system", "content": DECIDE_PROMPT}, {"role": "user", "content": content}])
    return parse_decision(reply.get("content") or "")


def revise_once(draft, problems, evidence):
    """The one revision. Returns the revised draft, or None when it was not usable (then the first draft stays)."""
    try:
        revised = revise_draft({key: draft[key] for key in ("alert", "coins", "message", "sources", "reasoning")}, problems, evidence)
    except (ShapeError, AgentError) as error:
        log(f"REVIEW revision not usable ({scrub(error)[:150]}); the first draft stays")
        return None
    log("REVIEW revision: the agent revised its draft once")
    return revised


def review_stage(draft, evidence, use_ai=True, now=None, added=()):
    """The review stage. Returns the draft (or its one revision) with two new keys: review_status, a short plain status, and review, the
    structured record (checks, failed, evidence, ai_called, ai_reason, tokens, codes, revised). It never writes 'approved' for a review that
    did not happen. Statuses: 'code-only: no alert drafted', 'code-only: AI review off', 'approved', 'revised' (the AI approved the revised
    alert), 'unconfirmed: reviewer unavailable', 'rejected: CODES' (decide() blocks the alert), 'unresolved: <what>'.
    `added` names the tools that complete_evidence ran; the record keeps which tools the AI called and which the code added."""
    now = now or utc_now()
    review = {"checks": [], "failed": [], "ai_called": False, "ai_reason": "", "revised": False, "codes": [], "unconfirmed": False,
              "evidence": {"ai": [label for label in dict.fromkeys(tool_label(item) for item in evidence) if label not in added],
                           "code": list(added)}}
    findings = run_code_review(draft, evidence, now, review)
    revisions_left, cause, status, called = MAX_REVISIONS, "code", "", 0
    while True:
        if findings:
            if revisions_left <= 0:
                if cause == "ai":
                    status = "rejected: " + ", ".join(review["codes"])
                else:
                    status = "unresolved: " + "; ".join(dict.fromkeys(f["label"] for f in findings))
                break
            revisions_left -= 1
            if cause == "code" and use_ai:
                log("REVIEW AI review: not needed yet, a code finding goes straight to the one revision (the finding is a fact)")
            revised = revise_once(draft, [f["problem"] for f in findings], evidence)
            if revised is None:   # the first draft stays; an alert that the AI review rejected is still rejected
                if cause == "ai":
                    status = "rejected: " + ", ".join(review["codes"])
                else:
                    status = "unresolved: revision not usable"
                break
            draft, cause, review["revised"] = revised, "code", True
            findings = run_code_review(draft, evidence, now, review, "code checks after the revision")
            continue
        if not draft["alert"] or not use_ai:
            break
        request = review_request(draft, evidence)
        review["request_tokens_estimate"], review["trimmed"] = request["tokens_estimate"], request["trimmed"]
        if request["trimmed"]:
            log(f"REVIEW AI review request was over the cap of {REVIEW_REQUEST_CAP} estimated tokens, so it was trimmed: dropped "
                + "; ".join(request["trimmed"]))
        if request["fits"]:
            called += 1
            log(f"REVIEW AI review: called{' again for the revised alert' if called > 1 else ''} (an alert is drafted, so there is a claim "
                f"to confirm; model {reviewer_model()})")
            log(f"REVIEW AI review request: about {request['tokens_estimate']} tokens (an estimate: characters / 4; cap {REVIEW_REQUEST_CAP})")
            verdict = ai_review(request["messages"], review)
        else:
            review["ai_reason"] = "reviewer request too large"
            log(f"REVIEW AI review: not called: even trimmed, the request is about {request['tokens_estimate']} tokens (an estimate) and "
                f"the cap is {REVIEW_REQUEST_CAP}")
            verdict = None
        if verdict is None:
            review["unconfirmed"] = True
            break
        review["codes"] = verdict["codes"]   # the codes of the last verdict
        if verdict["approved"]:
            log("REVIEW AI verdict: approved")
            break
        log("REVIEW AI verdict: rejected (" + ", ".join(verdict["codes"]) + ")")
        problems = [f"{code}: {REVIEW_CODES[code]}" for code in verdict["codes"]] + verdict["notes"]
        findings, cause = [{"check": "AI", "problem": "; ".join(problems)[:400], "label": ""}], "ai"
    if not status:
        if review["unconfirmed"]:
            status = "unconfirmed: reviewer unavailable"
        elif not draft["alert"]:
            status = "code-only: no alert drafted"
        elif not use_ai:
            status = "code-only: AI review off"
        else:
            status = "revised" if review["revised"] else "approved"
    if not review["ai_called"] and not review["ai_reason"]:
        review["ai_reason"] = (f"not run: {status}" if draft["alert"] and use_ai else
                               "skipped: --reviewer off, the code review still ran" if draft["alert"] else
                               "not needed: no alert drafted, so there is nothing to confirm")
        log("REVIEW AI review: " + review["ai_reason"])
    elif not review["ai_reason"]:
        review["ai_reason"] = "called: an alert was drafted"
    review["failed"] = [item["check"] for item in review["checks"] if not item["passed"]]
    return dict(draft, review_status=status, review=review)


def review_line(decision):
    """The short review status for the Sheet's Review column (at most MAX_REVIEW_LINE_CHARS characters). Empty when there is none."""
    status, review = decision.get("review_status"), decision.get("review")
    if not status or not isinstance(review, dict):
        return ""
    parts = [status]
    if review.get("revised") and "revised" not in status:
        parts.append("revised once")
    if review.get("failed"):
        parts.append("failed: " + ", ".join(review["failed"]))
    return " | ".join(parts)[:MAX_REVIEW_LINE_CHARS]


def log_review_summary(decision):
    """The last review line of a run: {"status", "failed", "ai_called", "tokens"}. tokens is the AI review's own count, left out when unknown."""
    review = decision["review"]
    summary = {"status": decision["review_status"], "failed": review["failed"], "ai_called": review["ai_called"]}
    if review.get("tokens"):
        summary["tokens"] = review["tokens"]
    log("REVIEW " + json.dumps(summary, separators=(",", ":")))


UNCONFIRMED_LABEL = "UNCONFIRMED: "


def label_unconfirmed(message):
    """(labelled message, number of sentences dropped). The label goes in front and the result is never longer than MAX_MESSAGE_CHARS.
    When it does not fit, the last whole sentences are dropped (a sentence is never cut in half, so no number or word is damaged).
    The text is None when not even the first sentence fits."""
    room = MAX_MESSAGE_CHARS - len(UNCONFIRMED_LABEL)
    if len(message) <= room:
        return UNCONFIRMED_LABEL + message, 0
    sentences = re.split(r"(?<=[.!?])\s+", message.strip())
    kept = []
    while sentences and len(" ".join(kept + sentences[:1])) <= room:
        kept.append(sentences.pop(0))
    return (UNCONFIRMED_LABEL + " ".join(kept) if kept else None), len(sentences)


def apply_review_outcome(final):
    """Runs after the guardrails. An alert that the AI review rejected and the revision did not fix is blocked ('reviewer: CODES'), so is
    an alert whose message still holds unsafe text (R3). An alert that the reviewer could not confirm goes out labelled UNCONFIRMED."""
    if not final["alert"]:
        return final
    review, reason, unsafe = final["review"], "", hygiene_kinds(final["message"])
    stale = [item["detail"] for item in review["checks"] if item["check"] == "R6" and not item["passed"]]
    if final["review_status"].startswith("rejected: "):
        reason = "reviewer: " + ", ".join(review["codes"])
    elif unsafe:
        reason = "reviewer: R3 unsafe text in the message (" + ", ".join(unsafe) + ")"
    elif stale:   # an alert that calls old data today's would mislead, so it is blocked and the reason is logged
        reason = "reviewer: R6 stale data (" + stale[0] + ")"
    elif review["unconfirmed"]:
        labelled, dropped = label_unconfirmed(final["message"])
        if labelled is None:
            reason = "reviewer unavailable: the message is too long to carry the UNCONFIRMED label"
        else:
            review["label"] = "UNCONFIRMED" + (f", {dropped} sentence(s) dropped to fit" if dropped else "")
            final["message"] = labelled
            log(f"::warning::the AI reviewer could not confirm this alert ({review['ai_reason']}), so it is sent labelled UNCONFIRMED"
                + (f" and its last {dropped} sentence(s) were dropped to stay within {MAX_MESSAGE_CHARS} characters" if dropped else "")
                + ". The code review ran: see the REVIEW lines above.")
    if reason:
        log(f"GUARDRAIL blocked the alert: {reason}")
        final.update(alert=False, coins=[], message="", blocked_reason=reason)
    return final


# ==============================================================================================
# 10. GUARDRAILS IN CODE, applied last, after the AI (and the reviewer) have answered
# ==============================================================================================
def recently_alerted_coins(evidence):
    """The coins that the agent's own last HISTORY_LIMIT decisions (read from the Sheet) show as alerted, as a set of upper-case symbols.
    None when the history is missing or cannot be read: then nothing is known, and the repeat check does not block."""
    result = last_result(evidence, "get_recent_decisions")
    if not isinstance(result, dict) or "error" in result or not isinstance(result.get("decisions"), list):
        return None
    coins = set()
    for item in result["decisions"][-HISTORY_LIMIT:]:
        if isinstance(item, dict) and item.get("alert") is True and isinstance(item.get("coins"), list):
            coins.update(str(coin).strip().upper() for coin in item["coins"])
    return coins


# The number check for an alert message, used by apply_guardrails (the reviewer keeps unsupported_numbers above, unchanged).
# It reads numbers like unsupported_numbers does (same parsing, same allowed numbers, same rounding: half a unit in the last
# written digit, so 20.5 matches 20.46). It adds two ways an honest message writes a real number differently from the JSON:
# a scale after the number ("$59.1k", "$31.5B", "900 million": checked as the full number) and a window label from the code's own
# windows ("60-day", "24-hour": a bare 60 or 24 is still checked). It only reports numbers; it never changes the message.
SCALE_WORDS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9,
               "t": 1e12, "tn": 1e12, "trillion": 1e12}
WINDOW_LABELS = {TREND_LONG_DAYS: ("day", "days", "d"), 24: ("hour", "hours", "h")}
WORD_AFTER_RE = re.compile(r"[-\s]?([A-Za-z]+)(?![A-Za-z0-9])")


def unsupported_message_numbers(message, evidence_json):
    known = [abs(value) for _, value, _ in numbers_in(evidence_json)]
    bad = []
    for found in NUMBER_RE.finditer(message):
        raw = found.group()
        cleaned = raw.replace(",", "").rstrip(".")
        try:
            value = abs(float(cleaned))
        except ValueError:
            continue
        tolerance = 0.5 * 10 ** -(len(cleaned.split(".")[1]) if "." in cleaned else 0) + 1e-9
        after = WORD_AFTER_RE.match(message, found.end())
        word = after.group(1).lower() if after else ""
        if word in SCALE_WORDS:
            value, tolerance = value * SCALE_WORDS[word], tolerance * SCALE_WORDS[word]
        elif word in WINDOW_LABELS.get(value, ()):
            continue
        if value in ALLOWED_NUMBERS or any(abs(value - k) <= tolerance for k in known):
            continue
        bad.append(raw)
    return bad


def apply_guardrails(draft, valid_titles, evidence):
    """Blocks an alert that breaks a rule. The AI cannot talk its way past this function."""
    final = dict(draft, blocked_reason="")
    canonical = {title.lower(): title for title in valid_titles}
    checked = [canonical.get(s.strip().lower()) for s in draft["sources"]]
    invalid = [s for s, c in zip(draft["sources"], checked) if c is None]
    final["sources"] = list(dict.fromkeys(c for c in checked if c))
    reason = ""
    if draft["alert"]:
        if invalid or not final["sources"]:
            reason = "no valid source: every source must be a real playbook section title"
        elif has_advice_language(draft["message"]):
            reason = "advice language in the message"
        elif not market_summaries(evidence):   # a string, None or list from a tool is not market data (and must not crash this function)
            reason = "no valid market data in this run"
        else:
            breached = {summary.get("coin") for summary in market_summaries(evidence) if summary.get("breaches")}
            without = [coin for coin in draft["coins"] if coin not in breached]
            repeated = recently_alerted_coins(evidence)
            if without:
                reason = f"{', '.join(without)}: no price rank at or above the alert rank in the market data (context never alerts alone)"
            elif repeated is not None and draft["coins"] and all(str(coin).strip().upper() in repeated for coin in draft["coins"]):
                # The playbook's repeat rule, enforced here because the AI does not always follow it (it once alerted BTC again because
                # two other metrics were "new"). An alert that holds at least one coin not alerted recently is left to the AI.
                reason = (f"{', '.join(draft['coins'])}: already alerted within the last {HISTORY_LIMIT} decisions "
                          "(Repeat alerts rule: no alert again for the same coin)")
            else:
                # The alert message may only use numbers that the tools returned. The reasoning is not checked here. A block, never a rewrite.
                invented = list(dict.fromkeys(unsupported_message_numbers(draft["message"], evidence_text(evidence))))
                if invented:
                    reason = f"numbers not found in the evidence: {', '.join(invented)}"
    if reason:
        log(f"GUARDRAIL blocked the alert: {reason}")
        final.update(alert=False, coins=[], message="", blocked_reason=reason)
    return final


def market_summaries(evidence):
    """The get_market_summary results of this run that are real summaries (not errors, not odd types)."""
    return [e["result"] for e in evidence
            if e["tool"] == "get_market_summary" and isinstance(e["result"], dict) and "error" not in e["result"]]


def code_breached_coins(evidence):
    """The coins that the code's own rank check puts at the alert rank in this run's market summaries, as a sorted list.
    It reads the 'breaches' list that add_breaches wrote, the same data the per-coin rule in apply_guardrails uses. No rank is recomputed here."""
    return sorted({s["coin"] for s in market_summaries(evidence) if s.get("breaches") and isinstance(s.get("coin"), str)})


def check_for_missed_breach(draft, evidence):
    """Compares the AI's answer with the evidence and returns (code_breaches, check). It only reports: one ::warning:: line when a
    'no alert' answer meets coins at the alert rank, or when the run gathered no market data to check against. It never changes the decision.
    A coin that the last HISTORY_LIMIT decisions already alerted is not a miss when the history is in the evidence (the repeat rule).
    The comparison itself is breach_comparison, which the review stage (check R7) uses as well."""
    breached, check, missed, unverified = breach_comparison(draft, evidence)
    if unverified:
        log("::warning::no valid market data in this run, so the answer could not be checked against the alert rank.")
    elif missed:
        log(f"::warning::the AI said no alert, but the code finds {', '.join(missed)} at or above the alert rank ({ALERT_RANK:g}) "
            "in this run's market data. Check the run before trusting this calm day.")
    return breached, check


# ==============================================================================================
# 11. PUTTING IT TOGETHER: plan or loop, then the review stage (always), then the guardrails
# ==============================================================================================
def version_name(mode, use_reviewer):
    """The label saved with every row. ' + reviewer' means the AI review was on; the code review runs in every version."""
    return ("Planner" if mode == "planner" else "Agent loop") + (" + reviewer" if use_reviewer else "")


def decide(mode, use_reviewer, tools, now=None):
    """The review stage runs on every run. use_reviewer only decides whether the AI review call is made for a drafted alert.
    `now` is the clock for the freshness check (R6); only tests and the scenarios set it."""
    draft, evidence = run_planner(tools) if mode == "planner" else run_agent_loop(tools)
    # The evidence must not depend on which tools the AI chose to call: the code runs the data tools that it left out (the recent
    # decisions among them, which the repeat rule and the review need). The draft was written without these extra results and is checked
    # against the full evidence; it is not asked for again just because evidence was added.
    added = complete_evidence(tools, evidence)
    draft = review_stage(draft, evidence, use_reviewer, now, added)
    titles = [section["title"] for section in load_playbook()]
    final = apply_guardrails(draft, titles, evidence)
    # Extra keys for the log line only (code_breaches, check, review_status, review): deliver() builds the Sheet row from named fields.
    final["code_breaches"], final["check"] = check_for_missed_breach(draft, evidence)
    final = apply_review_outcome(final)
    log_review_summary(final)
    return final, evidence


def has_valid_market_data(evidence):
    """True when at least one get_market_summary result in the evidence is real data: a dict with no error.
    Same rule as the 'no valid market data in this run' guardrail, plus a dict check so an odd tool result cannot count."""
    return any(e["tool"] == "get_market_summary" and isinstance(e["result"], dict) and "error" not in e["result"] for e in evidence)


def run_live(mode, use_reviewer, dry_run):
    version = version_name(mode, use_reviewer)
    log(f"Version: {version} | model: {MODEL} | watchlist: {', '.join(WATCHLIST)}")
    decision, evidence = decide(mode, use_reviewer, TOOLS)
    log("Decision: " + scrub(json.dumps(decision)))
    if dry_run:
        log("Dry run: nothing was posted to the Sheet")
    else:
        deliver(decision, version)
        publish_market_data(evidence, version)
    log(f"Run finished: alert={decision['alert']} llm_calls={STATS['llm_calls']}")
    if not has_valid_market_data(evidence):
        # The rows are saved first (the log of what happened stays). The run is then red, so a data outage is not a green day.
        print("ERROR: no valid market data for any coin, see the market rows", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Crypto watch agent (education only, not financial advice).")
    parser.add_argument("--mode", choices=["agent", "planner"], default=DEFAULT_MODE)
    parser.add_argument("--reviewer", choices=["on", "off"], default="on" if USE_REVIEWER else "off",
                        help="on (default): the AI also reviews every drafted alert. off: skip only the AI review call; the code review always runs")
    parser.add_argument("--dry-run", action="store_true", help="do not post to the Sheet")
    parser.add_argument("--test", action="store_true", help="run the scenarios on fake tools")
    parser.add_argument("--only", default="", help="with --test: run only these scenarios, for example 8-16 or 1,3,5")
    parser.add_argument("--publish", action="store_true", help="with --test: also write every scenario result to the scenarios tab")
    parser.add_argument("--measure", action="store_true", help="run all 4 versions several times")
    parser.add_argument("--runs", type=int, default=3, help="runs per version for --measure")
    parser.add_argument("--check-sources", action="store_true",
                        help="call every free data source once and print what came back (no AI, no tokens)")
    args = parser.parse_args(argv)
    use_reviewer = args.reviewer == "on"
    try:
        if args.check_sources:
            lines, failed = sources.check_sources(WATCHLIST, os.environ.get("COINGECKO_API_KEY", "").strip())
            print("\n".join(lines))
            return 1 if failed else 0
        if args.measure:
            import scenarios
            return scenarios.measure(args.runs)
        if args.test:
            import scenarios
            return scenarios.run_all(args.mode, use_reviewer, scenarios.parse_only(args.only), args.publish)["exit_code"]
        return run_live(args.mode, use_reviewer, args.dry_run)
    except AgentError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
