"""Checks that the data fetched from Kraken is correct and authentic, and says exactly what was checked.

    python validate_data.py              prints a PASS / WARN / FAIL table and writes data_export/data_quality.csv
    python validate_data.py --out DIR    writes the CSV to DIR
    python validate_data.py --no-coingecko   skips the independent price comparison

What is checked, for each of BTC, ETH and XRP (every check has a plain-language line in COVERS below):
  the reply        Kraken answered, with the pair that was asked for and no error
  the timestamps   whole seconds, plausible, at 00:00 UTC, in order, no duplicate, no missing day, newest completed day is yesterday
  the values       prices are positive numbers, high is the highest and low the lowest of the day, vwap lies between them,
                   volume is not negative, the trade count is a whole number
  warnings         zero-volume days, daily moves above 30%, an open more than 5% away from the previous close
  independence     the latest closes against CoinGecko's price, a second and unrelated source (needs the free Demo key)
and once across the coins:
  alignment        BTC, ETH and XRP end on the same day, so a cross-asset number never compares different days

FAIL means the data is wrong and must not be trusted. WARN means unusual but possible (a real crash looks like this).
SKIP means the check could not be made (no key, source down, or an earlier FAIL made it meaningless). The exit code is 1 when any
check is FAIL, 0 otherwise. The thresholds (30%, 5%, 1% and 3%) are my conventions, listed below, not Kraken rules.

No AI and no tokens. Kraken's public OHLC endpoint needs no key; CoinGecko needs the optional free Demo key (COINGECKO_API_KEY).
"""

import argparse
import csv
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import agent
import sources

DAY = 86400
EARLIEST = int(datetime(2013, 1, 1, tzinfo=timezone.utc).timestamp())   # before Kraken's first candle
BIG_MOVE_PCT = 30.0         # a daily close-to-close move above this is a WARN (real crashes exist; a data error looks like this too)
GAP_TO_PREVIOUS_CLOSE_PCT = 5.0   # an open further than this from the previous close is a WARN
AGREE_PCT = 1.0             # Kraken against CoinGecko: up to this is PASS ...
DISAGREE_PCT = 3.0          # ... above this is FAIL, in between is WARN
COMPARE_DAYS = 14           # how many of the latest days are compared with CoinGecko
TOLERANCE = 1e-9            # relative slack for "low <= close" style comparisons (floating point)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# check name -> what kind of error it catches. A test checks that every check that is run has a line here.
COVERS = {
    "kraken_reply": "Kraken did not answer, answered with an error, or answered for another pair than the one asked for",
    "row_shape": "A candle that does not have exactly 8 values (the reply format changed or was cut off)",
    "timestamp_valid": "A time that is text, a decimal, a boolean, milliseconds instead of seconds, before 2013 or in the future",
    "timestamp_midnight": "A daily candle that does not start at 00:00:00 UTC (a shifted or timezone-converted time)",
    "timestamp_order": "Candles out of order or the same day twice",
    "no_missing_days": "A day missing from the series",
    "latest_day_is_yesterday": "Stale data: the newest completed candle is older than yesterday (UTC)",
    "enough_history": "Fewer completed days than the 90 the agent needs to rank today against its own history",
    "price_values": "A price (open, high, low, close) that is zero, negative, not a number, NaN or infinite; a vwap that is not a number",
    "volume_trades_values": "A negative or non-numeric volume; a trade count that is negative, decimal or not a number",
    "high_low_consistent": "A day whose high is below its open or close, or whose low is above them",
    "vwap_in_range": "A volume-weighted average price outside the day's low to high range",
    "zero_volume_days": "A completed day with no trading at all (a gap in the exchange data)",
    "extreme_daily_moves": f"A close more than {BIG_MOVE_PCT:g}% away from the previous close (a real crash, or a wrong price)",
    "open_vs_previous_close": f"An open more than {GAP_TO_PREVIOUS_CLOSE_PCT:g}% away from the previous close (a missing or wrong candle)",
    "coins_same_dates": "One coin ending on a different day than the others (the coins would be compared on the wrong dates)",
    "coingecko_agreement": f"Kraken closes that disagree with CoinGecko by more than {AGREE_PCT:g}% (WARN) or {DISAGREE_PCT:g}% (FAIL): wrong pair, wrong unit or wrong value",
}


def result(coin, check, status, detail):
    return {"coin": coin, "check": check, "status": status, "detail": detail}


def num(value):
    """A finite float from a number or numeric text, else None. Booleans, NaN, infinity, words and text that is not plain ASCII digits
    ("1_000", full-width digits) are refused. The same rule as agent.as_number, so the gate and the agent never disagree."""
    return agent.as_number(value)


def is_whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def label(stamp):
    return agent.format_date(stamp) if is_whole(stamp) and EARLIEST <= stamp <= 4_102_444_800 else repr(stamp)


def examples(items, limit=3):
    shown = ", ".join(str(item) for item in items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def verdict(coin, check, bad, ok_detail, bad_detail, status=FAIL):
    """PASS when `bad` is empty, otherwise `status` with the first examples."""
    if not bad:
        return result(coin, check, PASS, ok_detail)
    return result(coin, check, status, f"{len(bad)} {bad_detail}: {examples(bad)}")


def check_rows(coin, rows, now):
    """All checks that need only the Kraken rows. Never raises, whatever the rows hold."""
    out = []
    today = int(now.timestamp()) // DAY * DAY
    if not isinstance(rows, list) or not rows:
        return [result(coin, "row_shape", FAIL, "no candles at all")]
    shaped = [r for r in rows if isinstance(r, (list, tuple)) and len(r) == 8]
    bad_shape = [i for i, r in enumerate(rows) if not (isinstance(r, (list, tuple)) and len(r) == 8)]
    out.append(verdict(coin, "row_shape", [f"row {i}" for i in bad_shape], f"all {len(rows)} candles have 8 values", "candle(s) without 8 values"))
    if not shaped:
        return out

    # ---- timestamps
    valid = [r for r in shaped if is_whole(r[0]) and EARLIEST <= r[0] <= int(now.timestamp())]
    bad_stamp = [label(r[0]) for r in shaped if not (is_whole(r[0]) and EARLIEST <= r[0] <= int(now.timestamp()))]
    out.append(verdict(coin, "timestamp_valid", bad_stamp, "every time is a whole number of seconds between 2013 and now",
                       "time value(s) that are not whole seconds between 2013 and now"))
    stamps = [r[0] for r in valid]
    off = [label(t) for t in stamps if t % DAY]
    out.append(verdict(coin, "timestamp_midnight", off, "every candle starts at 00:00 UTC", "candle(s) not starting at 00:00 UTC"))
    disorder = [label(b) for a, b in zip(stamps, stamps[1:]) if b <= a]
    out.append(verdict(coin, "timestamp_order", disorder, "strictly increasing, no day twice", "candle(s) out of order or repeated"))
    ordered = sorted(set(stamps))
    gaps = [f"{label(a)} to {label(b)}" for a, b in zip(ordered, ordered[1:]) if b - a != DAY and (b - a) % DAY == 0]
    gaps += [f"{label(a)} to {label(b)} (not whole days)" for a, b in zip(ordered, ordered[1:]) if (b - a) % DAY]
    out.append(verdict(coin, "no_missing_days", gaps, f"{len(ordered)} consecutive days", "gap(s)"))
    completed = [t for t in ordered if t < today]
    if completed and completed[-1] == today - DAY:
        out.append(result(coin, "latest_day_is_yesterday", PASS, f"newest completed candle is {label(completed[-1])}"))
    elif completed:
        out.append(result(coin, "latest_day_is_yesterday", FAIL, f"newest completed candle is {label(completed[-1])}, expected {label(today - DAY)}"))
    else:
        out.append(result(coin, "latest_day_is_yesterday", FAIL, "no completed candle"))
    out.append(result(coin, "enough_history", PASS if len(completed) >= agent.MIN_HISTORY_DAYS else FAIL,
                      f"{len(completed)} completed days, the agent needs {agent.MIN_HISTORY_DAYS}"))

    # ---- values (every shaped row, including today's unfinished one)
    price_bad, volume_bad, high_low_bad, vwap_bad = [], [], [], []
    for r in shaped:
        when = label(r[0])
        prices = [num(r[i]) for i in (1, 2, 3, 4)]
        volume, vwap, trades = num(r[6]), num(r[5]), r[7]
        if any(p is None or p <= 0 for p in prices) or vwap is None or vwap < 0 or (volume and volume > 0 and vwap <= 0):
            price_bad.append(when)
        if volume is None or volume < 0 or not is_whole(trades) or trades < 0:
            volume_bad.append(when)
        if any(p is None or p <= 0 for p in prices):
            continue
        open_, high, low, close = prices
        slack = TOLERANCE * high
        if high + slack < max(open_, close, low) or low - slack > min(open_, close, high):
            high_low_bad.append(when)
        if vwap is not None and volume and volume > 0 and (vwap > high + slack or vwap < low - slack):
            vwap_bad.append(when)
    out.append(verdict(coin, "price_values", price_bad, "open, high, low, close and vwap are positive numbers", "candle(s) with a bad price"))
    out.append(verdict(coin, "volume_trades_values", volume_bad, "volume is a number of zero or more and trades a whole number",
                       "candle(s) with a bad volume or trade count"))
    out.append(verdict(coin, "high_low_consistent", high_low_bad, "high is the highest and low the lowest of open, high, low and close",
                       "candle(s) where high or low contradicts open or close"))
    out.append(verdict(coin, "vwap_in_range", vwap_bad, "vwap lies between low and high on every day with trades", "candle(s) with vwap outside low to high"))

    # ---- warnings (completed days only)
    done = sorted((r for r in shaped if is_whole(r[0]) and EARLIEST <= r[0] < today), key=lambda r: r[0])
    zero = [label(r[0]) for r in done if num(r[6]) == 0]
    out.append(verdict(coin, "zero_volume_days", zero, "every completed day has trading", "completed day(s) with zero volume", status=WARN))
    moves, gaps_open = [], []
    for before, after in zip(done, done[1:]):
        if after[0] - before[0] != DAY:
            continue
        previous, close, open_ = num(before[4]), num(after[4]), num(after[1])
        if previous and close and abs(close / previous - 1) * 100 > BIG_MOVE_PCT:
            moves.append(f"{label(after[0])} ({(close / previous - 1) * 100:+.1f}%)")
        if previous and open_ and abs(open_ / previous - 1) * 100 > GAP_TO_PREVIOUS_CLOSE_PCT:
            gaps_open.append(f"{label(after[0])} ({(open_ / previous - 1) * 100:+.1f}%)")
    out.append(verdict(coin, "extreme_daily_moves", moves, f"no daily move above {BIG_MOVE_PCT:g}%", "daily move(s) above the limit", status=WARN))
    out.append(verdict(coin, "open_vs_previous_close", gaps_open, f"every open is within {GAP_TO_PREVIOUS_CLOSE_PCT:g}% of the previous close",
                       "open(s) far from the previous close", status=WARN))
    return out


def check_coins_aligned(rows_by_coin, now):
    """All coins must end on the same completed day. Reported once, under the name ALL."""
    today = int(now.timestamp()) // DAY * DAY
    newest = {}
    for coin, rows in rows_by_coin.items():
        stamps = [r[0] for r in rows if isinstance(rows, list) and isinstance(r, (list, tuple)) and len(r) == 8
                  and is_whole(r[0]) and EARLIEST <= r[0] < today and r[0] % DAY == 0]
        if stamps:
            newest[coin] = max(stamps)
    if len(newest) < 2:
        return result("ALL", "coins_same_dates", SKIP, "fewer than two coins have usable completed candles to compare")
    if len(set(newest.values())) == 1:
        return result("ALL", "coins_same_dates", PASS, f"all {len(newest)} coins end on {label(next(iter(newest.values())))}")
    return result("ALL", "coins_same_dates", FAIL, "coins end on different days: " + ", ".join(f"{coin} {label(end)}" for coin, end in newest.items()))


def coingecko_prices(body):
    """{UTC date text: price} for CoinGecko points stamped exactly 00:00 UTC, which is the price at the end of the day before.
    Anything that is not a list of [milliseconds, price] pairs is skipped, never guessed."""
    points = body.get("prices") if isinstance(body, dict) else None
    out = {}
    if not isinstance(points, list):
        return out
    for point in points:
        if isinstance(point, list) and len(point) == 2 and is_whole(point[0]) and point[0] % (DAY * 1000) == 0:
            price = num(point[1])
            if price and price > 0:
                out[agent.format_date(point[0] // 1000)] = price
    return out


def check_against_coingecko(coin, rows, cg_prices, now, days=COMPARE_DAYS):
    """Kraken's close of day D-1 against CoinGecko's 00:00 UTC point of day D (the same moment, two unrelated sources)."""
    if cg_prices is None:
        return result(coin, "coingecko_agreement", SKIP, "no CoinGecko data (no key, or the source did not answer)")
    today = int(now.timestamp()) // DAY * DAY
    closes = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, (list, tuple)) and len(r) == 8 and is_whole(r[0]) and EARLIEST <= r[0] < today and r[0] % DAY == 0 and num(r[4]):
            closes[r[0]] = num(r[4])
    gaps = []
    for stamp in sorted(closes, reverse=True):
        other = cg_prices.get(agent.format_date(stamp + DAY))
        if other:
            gaps.append((agent.format_date(stamp), (closes[stamp] / other - 1) * 100))
        if len(gaps) == days:
            break
    if not gaps:
        return result(coin, "coingecko_agreement", SKIP, "no day in common with CoinGecko's 00:00 UTC points")
    worst = max(gaps, key=lambda g: abs(g[1]))
    median = statistics.median(abs(g[1]) for g in gaps)
    detail = f"{len(gaps)} latest days compared, largest gap {worst[1]:+.2f}% on {worst[0]}, median gap {median:.2f}%"
    size = abs(worst[1])
    status = PASS if size <= AGREE_PCT else WARN if size <= DISAGREE_PCT else FAIL
    return result(coin, "coingecko_agreement", status, detail)


def fetch_coingecko(coin, key):
    """CoinGecko's daily prices for one coin, or None when there is no key or the source does not answer (the check is then SKIP)."""
    if not key:
        return None
    try:
        body = sources.get_json(f"{sources.COINGECKO_URL}/coins/{sources.COINGECKO_IDS[coin]}/market_chart",
                                {"vs_currency": "usd", "days": 365}, sources.coingecko_headers(key))
    except Exception:  # noqa: BLE001 - a source that is down must never stop the validation
        return None
    return coingecko_prices(body)


def validate(raw_by_coin, now, coingecko_by_coin=None, errors=None):
    """The full report: a list of results. raw_by_coin is {coin: rows}; errors is {coin: text} for coins Kraken did not answer."""
    report = []
    for coin in agent.WATCHLIST:
        if errors and coin in errors:
            report.append(result(coin, "kraken_reply", FAIL, errors[coin]))
            continue
        rows = raw_by_coin.get(coin)
        report.append(result(coin, "kraken_reply", PASS, f"{agent.WATCHLIST[coin]} answered with the expected pair key"))
        report.extend(check_rows(coin, rows, now))
        report.append(check_against_coingecko(coin, rows, (coingecko_by_coin or {}).get(coin), now))
    report.append(check_coins_aligned({c: r for c, r in raw_by_coin.items() if not (errors and c in errors)}, now))
    return report


def summary(report):
    counts = {status: sum(1 for r in report if r["status"] == status) for status in (PASS, WARN, FAIL, SKIP)}
    return counts, f"{len(report)} checks: {counts[PASS]} PASS, {counts[WARN]} WARN, {counts[FAIL]} FAIL, {counts[SKIP]} SKIP"


def write_report(path, report):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["coin", "check", "status", "detail", "what_it_catches"])
        for r in report:
            writer.writerow([r["coin"], r["check"], r["status"], r["detail"], COVERS.get(r["check"], "")])


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description="Check that the data fetched from Kraken is correct.")
    parser.add_argument("--out", default="data_export", help="folder for data_quality.csv (default: data_export)")
    parser.add_argument("--no-coingecko", action="store_true", help="skip the comparison with CoinGecko")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    key = "" if args.no_coingecko else os.environ.get("COINGECKO_API_KEY", "").strip()
    raw, errors, cg = {}, {}, {}
    for coin, pair in agent.WATCHLIST.items():
        try:
            raw[coin] = agent.fetch_rows(pair)
        except RuntimeError as error:
            errors[coin] = str(error)[:200]
            continue
        cg[coin] = fetch_coingecko(coin, key)
    report = validate(raw, now, cg, errors)
    for r in report:
        print(f"{r['status']:<5} {r['coin']:<4} {r['check']:<26} {r['detail']}")
    counts, line = summary(report)
    print(line)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_report(out / "data_quality.csv", report)
    print(f"wrote {out / 'data_quality.csv'}")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
