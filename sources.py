"""Free public data sources that add market context to the watch agent (education only).

Every source here is free. Kraken (spot depth and futures), Deribit and alternative.me need no key. CoinGecko needs the free
Demo key in COINGECKO_API_KEY, and without it the CoinGecko fields say "not configured". No paid API is used and no paid
plan is needed.

Rules this module keeps:
- Nothing raises out of fetch_context(). A source that fails gives {"unavailable": "<short reason>"} for that field and the rest still work.
- A reason never contains a URL, a header or a key. The key travels in a header and is never printed.
- This data is context. It never raises an alert (see apply_guardrails in agent.py).
- Attribution: market-wide volume and prices come from CoinGecko, the fear and greed index from alternative.me.
"""
import math
import re
import statistics
import time
from datetime import datetime, timezone

import requests

KRAKEN_DEPTH_URL = "https://api.kraken.com/0/public/Depth"
KRAKEN_FUTURES_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
DERIBIT_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
COINGECKO_URL = "https://api.coingecko.com/api/v3"
FEAR_GREED_URL = "https://api.alternative.me/fng/"

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple"}
FUTURES_BASES = {"BTC": ("XBT", "BTC"), "ETH": ("ETH",), "XRP": ("XRP",)}   # Kraken Futures calls Bitcoin XBT on some contracts
DERIBIT_CURRENCIES = {"BTC", "ETH"}     # Deribit publishes a volatility index (DVOL) for these two only

DEPTH_LEVELS = 500          # order-book levels fetched per side (Kraken allows 1 to 500; the default of 100 did not reach 1% on a live check)
DEPTH_BAND = 0.01           # depth is counted within 1% of the mid price, or within the narrower range both sides of the book cover
PRICE_TOLERANCE_PCT = 1.0   # two live prices that differ by more than this are flagged (a data-quality convention, see playbook.md)
MIN_RANK_DAYS = 90          # fewer completed days than this: no rank is given
VOLUME_WINDOW = 30
RANK_FLAG = 95.0            # same alert rank as the price metrics
MAX_FUNDING_PCT_PER_HOUR = 1.0   # an hourly funding rate above this is not plausible, so it is reported as unavailable
SECONDS_PER_DAY = 86400
TIMEOUT = 15


class SourceError(Exception):
    """A source could not be read. The message is short and never contains a secret."""


def get_json(url, params=None, headers=None, attempts=2):
    """GET and parse JSON. The error text names the kind of problem only, never the URL, headers or key."""
    last = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as error:
            last = type(error).__name__
        else:
            if response.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {response.status_code}"
            elif response.status_code >= 400:
                raise SourceError(f"HTTP {response.status_code}")
            else:
                try:
                    return response.json()
                except ValueError:
                    raise SourceError("reply is not JSON")
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    raise SourceError(last)


def unavailable(reason):
    return {"unavailable": str(reason)[:120]}


def safe(function, *args):
    """Runs one source. Any problem becomes an 'unavailable' value for that field, so one broken source never stops the others."""
    try:
        return function(*args)
    except SourceError as error:
        return unavailable(error)
    except (ValueError, KeyError, TypeError, IndexError, ZeroDivisionError, statistics.StatisticsError) as error:
        return unavailable(f"unexpected reply ({type(error).__name__})")


# Numeric text from a web reply: plain ASCII digits only (D-15), with plain spaces or line breaks around it allowed as before.
# float() alone also accepts "1_000", full-width digits and other Unicode spaces, which are refused.
NUMBER_TEXT_RE = re.compile(r"[ \t\r\n]*[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[ \t\r\n]*\Z")


def number(value, name, minimum=None):
    """A finite float, or ValueError. Booleans and text that is not a plain number (ASCII digits, a decimal point, an exponent, plain spaces around it) are refused."""
    if isinstance(value, bool) or (isinstance(value, str) and not NUMBER_TEXT_RE.match(value)):
        raise ValueError(name)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):   # OverflowError: a JSON integer too large for a float
        raise ValueError(name)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(name)
    return result


def percentile_rank(values, latest):
    """Share of the values below the latest one, ties counting half (the same rule as agent.percentile_rank), 0 to 100."""
    below = sum(1 for v in values if v < latest)
    equal = sum(1 for v in values if v == latest)
    return 100.0 * (below + 0.5 * equal) / len(values)


def volume_ratio_series(volumes, window=VOLUME_WINDOW):
    """Each day's volume divided by the mean of the previous `window` days (None where there is no baseline)."""
    ratios = []
    for i, volume in enumerate(volumes):
        base = volumes[i - window:i] if i >= window else []
        mean = statistics.fmean(base) if len(base) == window else 0.0
        ratios.append(volume / mean if mean > 0 else None)
    return ratios


# ---- Kraken order book: liquidity ---------------------------------------------------------------------------------------
def parse_depth(body):
    """Spread and the order-book depth around the mid price. Reads the one pair in the reply.
    The band is 1% of the mid price, or less when either side of the fetched book does not reach 1%. Both sides always use the same band,
    so bid and ask depth can be compared, and the band is reported."""
    if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("result"), dict) or len(body["result"]) != 1:
        raise SourceError("unexpected order book reply")
    book = next(iter(body["result"].values()))
    bids = [(number(row[0], "bid", 0), number(row[1], "bid size", 0)) for row in book["bids"]]
    asks = [(number(row[0], "ask", 0), number(row[1], "ask size", 0)) for row in book["asks"]]
    if not bids or not asks:
        raise SourceError("empty order book")
    best_bid, best_ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if best_bid <= 0 or best_ask < best_bid:
        raise SourceError("crossed or invalid order book")
    mid = (best_bid + best_ask) / 2.0
    reach = min(mid * DEPTH_BAND, mid - min(p for p, _ in bids), max(p for p, _ in asks) - mid)
    low, high, slack = mid - reach, mid + reach, mid * 1e-9      # slack: the farthest level sits exactly on the edge of its own band
    return {
        "mid_price": round(mid, 6),
        "spread_pct": round((best_ask - best_bid) / mid * 100.0, 4),
        "bid_depth_usd": round(sum(p * v for p, v in bids if p >= low - slack), 2),
        "ask_depth_usd": round(sum(p * v for p, v in asks if p <= high + slack), 2),
        "depth_band_pct": round(reach / mid * 100.0, 3),
    }


def fetch_depth(pair):
    return parse_depth(get_json(KRAKEN_DEPTH_URL, {"pair": pair, "count": DEPTH_LEVELS}))


# ---- Kraken Futures: funding rate and open interest -----------------------------------------------------------------------
def parse_futures(body, coin):
    """Funding rate and open interest of the coin's perpetual. Prefers the linear PF_ contract when there are several."""
    if not isinstance(body, dict) or body.get("result") != "success" or not isinstance(body.get("tickers"), list):
        raise SourceError("unexpected futures reply")
    bases = FUTURES_BASES[coin]
    found = [t for t in body["tickers"] if isinstance(t, dict) and t.get("tag") == "perpetual" and not t.get("suspended")
             and str(t.get("pair", "")).upper() in {f"{base}:USD" for base in bases}]
    if not found:
        raise SourceError(f"no perpetual contract for {coin}")
    found.sort(key=lambda t: not str(t.get("symbol", "")).upper().startswith("PF_"))
    ticker = found[0]
    symbol = str(ticker.get("symbol", ""))
    mark = number(ticker.get("markPrice"), "mark price", 0)
    linear = symbol.upper().startswith("PF_")
    if ticker.get("relativeFundingRate") is not None:
        funding_pct = number(ticker["relativeFundingRate"], "relative funding rate") * 100.0
    elif linear and ticker.get("fundingRate") is not None and mark > 0:
        funding_pct = number(ticker["fundingRate"], "funding rate") / mark * 100.0   # absolute rate over the mark price
    else:
        raise SourceError("funding rate unit unclear")
    if abs(funding_pct) > MAX_FUNDING_PCT_PER_HOUR:
        raise SourceError("implausible funding rate")
    open_interest = number(ticker.get("openInterest"), "open interest", 0)
    return {
        "symbol": symbol,
        "funding_rate_pct_per_hour": round(funding_pct, 6),
        "funding_pays": "longs pay shorts" if funding_pct > 0 else "shorts pay longs" if funding_pct < 0 else "flat",
        "open_interest": round(open_interest, 4),
        # only the linear contract is sized in the coin, so only there is a dollar value worked out
        "open_interest_usd_approx": round(open_interest * mark, 0) if linear else None,
    }


def fetch_futures_body():
    return get_json(KRAKEN_FUTURES_URL)


# ---- Deribit: implied volatility (DVOL, BTC and ETH only) ------------------------------------------------------------------------
def parse_dvol(body, now):
    """The latest DVOL value and its rank against the completed days of the past year."""
    result = body.get("result") if isinstance(body, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or not data:
        raise SourceError("unexpected volatility index reply")
    candles = [(number(c[0], "time", 0) / 1000.0, number(c[4], "close", 0)) for c in data]
    candles.sort()
    today = datetime.fromtimestamp(now, timezone.utc).date()
    completed = [close for t, close in candles if datetime.fromtimestamp(t, timezone.utc).date() < today]
    latest = candles[-1][1]
    out = {"implied_vol_pct": round(latest, 2), "history_days": len(completed)}
    if len(completed) >= MIN_RANK_DAYS:
        out["rank_1y"] = round(percentile_rank(completed, latest), 1)
    else:
        out["rank_1y"] = None
    return out


def fetch_dvol(currency, now):
    params = {"currency": currency, "start_timestamp": int((now - 366 * SECONDS_PER_DAY) * 1000),
              "end_timestamp": int(now * 1000), "resolution": "1D"}
    return parse_dvol(get_json(DERIBIT_URL, params), now)


# ---- CoinGecko (free Demo key): market-wide volume and a second price ---------------------------------------------------------------
def coingecko_headers(key):
    return {"x-cg-demo-api-key": key, "accept": "application/json"}


def parse_markets(body):
    """coin id -> price, 24-hour volume, market cap, rank and 24-hour change. Reads the fields by name."""
    if not isinstance(body, list):
        raise SourceError("unexpected markets reply")
    out = {}
    for item in body:
        if not isinstance(item, dict) or "id" not in item:
            continue
        rank = item.get("market_cap_rank")
        out[item["id"]] = {
            "price_usd": number(item.get("current_price"), "price", 0),
            "volume_24h_usd": number(item.get("total_volume"), "volume", 0),
            "market_cap_usd": number(item.get("market_cap"), "market cap", 0),
            "market_cap_rank": int(rank) if isinstance(rank, (int, float)) and not isinstance(rank, bool) else None,
            "change_24h_pct": round(number(item.get("price_change_percentage_24h"), "24h change"), 2),
        }
    if not out:
        raise SourceError("no coins in the markets reply")
    return out


def parse_volume_history(body, now):
    """Market-wide volume today against the mean of the previous 30 days, ranked against that ratio's own history.
    Points of the current UTC day are dropped, because that day is not finished."""
    points = body.get("total_volumes") if isinstance(body, dict) else None
    if not isinstance(points, list) or not points:
        raise SourceError("unexpected volume history reply")
    today = datetime.fromtimestamp(now, timezone.utc).date()
    by_day = {}
    for stamp, volume in points:
        day = datetime.fromtimestamp(number(stamp, "time", 0) / 1000.0, timezone.utc).date()
        if day < today:
            by_day[day] = number(volume, "volume", 0)        # the last point of a day wins
    volumes = [by_day[d] for d in sorted(by_day)]
    ratios = volume_ratio_series(volumes)
    valid = [r for r in ratios if r is not None]
    if len(valid) < MIN_RANK_DAYS:
        raise SourceError(f"only {len(valid)} days of volume history")
    return {"market_volume_ratio_30d": round(valid[-1], 3), "market_volume_rank": round(percentile_rank(valid, valid[-1]), 1),
            "history_days": len(volumes), "as_of": sorted(by_day)[-1].isoformat()}


def fetch_markets(key, coins):
    ids = ",".join(COINGECKO_IDS[c] for c in coins)
    return parse_markets(get_json(f"{COINGECKO_URL}/coins/markets", {"vs_currency": "usd", "ids": ids, "per_page": len(coins)},
                                  coingecko_headers(key)))


def fetch_volume_history(key, coin, now):
    body = get_json(f"{COINGECKO_URL}/coins/{COINGECKO_IDS[coin]}/market_chart", {"vs_currency": "usd", "days": 365},
                    coingecko_headers(key))
    return parse_volume_history(body, now)


# ---- alternative.me: fear and greed index (market-wide) ----------------------------------------------------------------------------------
def parse_fear_greed(body):
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) < 2:
        raise SourceError("unexpected fear and greed reply")
    values = [number(item["value"], "value", 0) for item in data]
    if max(values) > 100:
        raise SourceError("fear and greed value out of range")
    label = str(data[0].get("value_classification", ""))[:20]
    return {"value": int(values[0]), "label": label, "change_vs_yesterday": int(values[0] - values[1]),
            "mean_prior_30d": round(statistics.fmean(values[1:31]), 1)}


def fetch_fear_greed():
    return parse_fear_greed(get_json(FEAR_GREED_URL, {"limit": 31, "format": "json"}))


# ---- price check and assembly --------------------------------------------------------------------------------------------------------------
def price_check(kraken_mid, other_price):
    """Two live prices of the same coin from two sources. A gap above the tolerance is flagged, never used to block an alert."""
    # rounded to 6 places before the comparison, so a gap of exactly 1% is not pushed over the line by floating-point noise
    difference = round((kraken_mid / other_price - 1.0) * 100.0, 6)
    return {"kraken_mid_usd": round(kraken_mid, 6), "coingecko_price_usd": round(other_price, 6),
            "difference_pct": round(difference, 3), "agree": abs(difference) <= PRICE_TOLERANCE_PCT}


def is_data(piece):
    return isinstance(piece, dict) and "unavailable" not in piece


def build_context(coins, depth, futures, dvol, markets, volume, fear_greed, as_of):
    """Puts the already-read pieces together. Pure code, so tests can feed it sample replies."""
    out = {"as_of": as_of, "coins": {}, "sentiment": fear_greed, "flags": []}
    for coin in coins:
        market = markets.get(COINGECKO_IDS[coin]) if is_data(markets) else markets
        if is_data(markets) and market is None:
            market = unavailable("coin missing from the markets reply")
        wide = None
        if is_data(market):
            wide = dict(market)
            history = volume.get(coin, unavailable("not read"))
            wide.update(history if is_data(history) else {"volume_history": history["unavailable"]})
        piece = {
            "liquidity": depth.get(coin, unavailable("not read")),
            "derivatives": futures.get(coin, unavailable("not read")),
            "implied_volatility": dvol.get(coin, unavailable("not read")),
            "market_wide": wide if wide is not None else market,
        }
        mid = piece["liquidity"].get("mid_price") if is_data(piece["liquidity"]) else None
        other = market.get("price_usd") if is_data(market) else None
        piece["price_check"] = price_check(mid, other) if mid and other else unavailable("needs both prices")
        out["coins"][coin] = piece
        iv = piece["implied_volatility"]
        if is_data(iv) and iv.get("rank_1y") is not None and iv["rank_1y"] >= RANK_FLAG:
            out["flags"].append(f"implied volatility {coin}")
        if is_data(wide) and wide.get("market_volume_rank") is not None and wide["market_volume_rank"] >= RANK_FLAG:
            out["flags"].append(f"market volume {coin}")
        if is_data(piece["price_check"]) and not piece["price_check"]["agree"]:
            out["flags"].append(f"price sources {coin} disagree")
    if is_data(fear_greed) and (fear_greed["value"] <= 24 or fear_greed["value"] >= 76):
        out["flags"].append(f"sentiment {fear_greed['label'].lower()}")
    return out


def fetch_context(pairs, now, coingecko_key=""):
    """Reads every free source for the watchlist. `pairs` maps a coin to its Kraken pair. Never raises."""
    coins = list(pairs)
    depth = {coin: safe(fetch_depth, pairs[coin]) for coin in coins}
    try:
        futures_body = fetch_futures_body()
    except SourceError as error:
        futures = {coin: unavailable(error) for coin in coins}
    else:
        futures = {coin: safe(parse_futures, futures_body, coin) for coin in coins}
    dvol = {coin: (safe(fetch_dvol, coin, now) if coin in DERIBIT_CURRENCIES
                   else unavailable(f"Deribit publishes no volatility index for {coin}")) for coin in coins}
    if coingecko_key:
        markets = safe(fetch_markets, coingecko_key, coins)
        volume = {coin: safe(fetch_volume_history, coingecko_key, coin, now) for coin in coins} if is_data(markets) else {}
    else:
        markets = unavailable("not configured (COINGECKO_API_KEY)")
        volume = {}
    fear_greed = safe(fetch_fear_greed)
    as_of = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return build_context(coins, depth, futures, dvol, markets, volume, fear_greed, as_of)


# ---- the data check: a manual run that shows what each source really returns (no AI, no tokens) --------------------------------------------------
def check_sources(pairs, coingecko_key=""):
    """Reads every source once and returns printable lines. A line never contains a key. Used by `agent.py --check-sources`."""
    now = time.time()
    lines, failed = [], 0

    def step(name, function, *args):
        nonlocal failed
        started = time.time()
        result = safe(function, *args)
        took = int((time.time() - started) * 1000)
        ok = is_data(result)
        if not ok:
            failed += 1
        lines.append(f"{'OK  ' if ok else 'FAIL'} {name} ({took} ms): {str(result)[:300]}")
        return result

    for coin, pair in pairs.items():
        step(f"Kraken order book {coin}", fetch_depth, pair)
    try:
        body = fetch_futures_body()
        for coin in pairs:
            raw = [t for t in body.get("tickers", []) if isinstance(t, dict) and t.get("tag") == "perpetual"
                   and str(t.get("pair", "")).upper() in {f"{b}:USD" for b in FUTURES_BASES[coin]}]
            shown = "; ".join(f"{t.get('symbol')} mark={t.get('markPrice')} oi={t.get('openInterest')} fundingRate={t.get('fundingRate')} "
                              f"relativeFundingRate={t.get('relativeFundingRate')}" for t in raw[:3])
            lines.append(f"INFO Kraken Futures raw {coin}: {shown or 'no perpetual'}")
            step(f"Kraken Futures {coin}", parse_futures, body, coin)
    except SourceError as error:
        failed += 1
        lines.append(f"FAIL Kraken Futures: {error}")
    for coin in pairs:
        if coin in DERIBIT_CURRENCIES:
            step(f"Deribit DVOL {coin}", fetch_dvol, coin, now)
        else:
            lines.append(f"SKIP Deribit DVOL {coin}: no volatility index exists for this coin")
    step("Fear and greed", fetch_fear_greed)
    if coingecko_key:
        step("CoinGecko markets", fetch_markets, coingecko_key, list(pairs))
        for coin in pairs:
            step(f"CoinGecko volume history {coin}", fetch_volume_history, coingecko_key, coin, now)
    else:
        lines.append("SKIP CoinGecko: COINGECKO_API_KEY is not set (free Demo key)")
    lines.append(f"Required sources failed: {failed}")
    return lines, failed
