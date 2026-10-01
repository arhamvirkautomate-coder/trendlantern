#!/usr/bin/env python3
"""
CryptoPulse analysis engine (arhamvirkautomate project)
-------------------------------------------------------
Standard-library only. Pulls free public data, computes technical /
on-chain / sentiment signals, builds trade plans (entry, stop, targets,
when-to-sell), updates a simulated $300 paper portfolio, and writes JSON
documents that the dashboard reads.

Usage:
  python3 engine.py --out OUT_DIR [--prev PREV_DIR] [--demo]

  --prev  folder holding the previous run's paper.json and history.json
          (so the paper portfolio + history carry forward)
  --demo  synthetic data for offline testing only (never publish demo output)

Outputs (in OUT_DIR): snapshot.json, charts.json, news.json, paper.json,
history.json, run_log.json
"""
import argparse, bisect, json, math, os, random, re, ssl, sys, time, html
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VERSION = "1.0.0"

# ---------------------------------------------------------------- config
CFG = {
    "capital_usd": 300.0,          # default paper/live capital
    "risk_per_trade": 0.015,       # 1.5% of capital risked per trade
    "max_open": 3,
    "fee": 0.001,                  # Binance spot 0.1% per side
    "universe_top": 40,            # top-N majors by market cap
    "meme_top": 10,                # plus top-N meme coins (flagged high risk)
    "min_quote_vol": 15_000_000,   # min 24h USDT volume on Binance
    "meme_cap_frac": 0.10,         # max 10% of capital in any one meme coin
}

KNOWN_MEMES = {"DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "TRUMP", "PENGU", "PUMP", "FARTCOIN", "BRETT", "POPCAT",
               "MEME", "NEIRO", "TURBO", "BOME", "MOG", "SPX", "1000SATS", "PEOPLE", "DOGS", "MEW", "BABYDOGE", "ACT", "PNUT"}
STABLES = {"USDT", "USDC", "FDUSD", "DAI", "TUSD", "USDE", "USDP", "PYUSD", "USDD", "BUSD",
           "EURC", "EUR", "AEUR", "USD1", "XUSD", "BFUSD", "RLUSD", "USDS", "USDX", "GUSD",
           "FRAX", "LUSD", "USDB", "USD0", "SUSDE", "USTC", "PAXG", "XAUT", "USDG", "USYC"}
WRAPPED = {"WBTC", "WETH", "STETH", "WSTETH", "WBETH", "BETH", "CBBTC", "WEETH", "RETH",
           "CBETH", "METH", "EZETH", "RSETH", "LBTC", "SOLVBTC", "BTCB", "WBNB", "JITOSOL",
           "MSOL", "BNSOL", "LSETH", "OSETH", "TBTC", "CLBTC", "SAVAX", "STKAAVE", "WTRX"}

UA = {"User-Agent": "Mozilla/5.0 (CryptoPulse personal dashboard)", "Accept": "*/*"}
LOG = {"ok": [], "fail": []}


# ---------------------------------------------------------------- http
def _ctx():
    cafile = os.environ.get("SSL_CERT_FILE") or ("/root/.ccr/ca-bundle.crt" if os.path.exists("/root/.ccr/ca-bundle.crt") else None)
    try:
        return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    except Exception:
        return ssl.create_default_context()

CTX = _ctx()

def get(url, tries=3, timeout=25, raw=False, sleep=1.5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                data = r.read()
            return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 418):
                time.sleep(sleep * (i + 2) * 4)
            elif e.code in (400, 403, 404, 451):
                break
            else:
                time.sleep(sleep * (i + 1))
        except Exception as e:  # noqa
            last = str(e)[:120]
            time.sleep(sleep * (i + 1))
    raise RuntimeError(f"{url[:90]} -> {last}")

def src(name, fn, *a, **k):
    try:
        v = fn(*a, **k)
        LOG["ok"].append(name)
        return v
    except Exception as e:  # noqa
        LOG["fail"].append(f"{name}: {str(e)[:160]}")
        return None


# ---------------------------------------------------------------- math
def ema(vals, n):
    if not vals:
        return []
    k = 2 / (n + 1)
    out, e = [], vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def sma(vals, n):
    return sum(vals[-n:]) / min(n, len(vals)) if vals else 0

def rsi(closes, n=14):
    if len(closes) < n + 2:
        return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out = [50.0] * (n + 1)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        rs = ag / al if al > 0 else 999
        out.append(100 - 100 / (1 + rs))
    return out

def macd_hist(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema(line, 9)
    return [a - b for a, b in zip(line, sig)]

def atr(highs, lows, closes, n=14):
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i]); continue
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    out, a = [], trs[0]
    for t in trs:
        a = (a * (n - 1) + t) / n
        out.append(a)
    return out

def stdev(v):
    if len(v) < 2:
        return 0
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))

def sig(x, d=5):
    """round to d significant digits (handles meme-coin prices)"""
    if x is None or x == 0 or not math.isfinite(x):
        return 0 if x is None or not math.isfinite(x or 0) else x
    return round(x, -int(math.floor(math.log10(abs(x)))) + (d - 1))

def usd(x):
    """human price text for rule sentences"""
    if x is None: return "—"
    if x >= 1000: return f"${x:,.0f}"
    if x >= 1: return "$" + f"{x:,.3f}".rstrip("0").rstrip(".")
    v = sig(x, 4)
    return "$" + f"{v:.12f}".rstrip("0").rstrip(".")

def pct(a, b):
    return (a / b - 1) * 100 if b else 0

def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- data: market list
def cg_markets(category=None, per_page=150):
    q = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": per_page, "page": 1,
         "price_change_percentage": "1h,24h,7d,30d", "sparkline": "false"}
    if category:
        q["category"] = category
    return get("https://api.coingecko.com/api/v3/coins/markets?" + urllib.parse.urlencode(q), tries=4, sleep=4)

def binance_tickers():
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            d = get(host + "/api/v3/ticker/24hr", tries=2)
            return host, {t["symbol"]: t for t in d if t["symbol"].endswith("USDT")}
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(f"binance tickers unavailable: {last}")

def binance_klines(host, symbol, interval, limit):
    d = get(f"{host}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}", tries=2, sleep=0.5)
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
             "v": float(k[7])} for k in d]

def cg_candles(cg_id):
    """fallback when Binance is unreachable: hourly->4h (90d) and daily (365d) from CoinGecko"""
    h = get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=usd&days=90", sleep=6)
    time.sleep(2.5)
    d = get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=usd&days=365&interval=daily", sleep=6)
    time.sleep(2.5)
    def bucket(prices, vols, ms):
        out, cur = [], None
        vmap = {int(t // ms): v for t, v in vols}
        for t, p in prices:
            b = int(t // ms)
            if cur is None or cur["b"] != b:
                if cur: out.append(cur)
                cur = {"b": b, "t": b * ms, "o": p, "h": p, "l": p, "c": p, "v": vmap.get(b, 0) / (ms / 3.6e6 * 1 if ms < 8.64e7 else 1)}
            cur["h"] = max(cur["h"], p); cur["l"] = min(cur["l"], p); cur["c"] = p
        if cur: out.append(cur)
        return out
    k4 = bucket(h["prices"], h.get("total_volumes", []), 4 * 3600 * 1000)
    kd = bucket(d["prices"], d.get("total_volumes", []), 24 * 3600 * 1000)
    return k4, kd


# ---------------------------------------------------------------- data: macro / sentiment
def fear_greed():
    d = get("https://api.alternative.me/fng/?limit=60")["data"]
    return [{"v": int(x["value"]), "l": x["value_classification"], "t": int(x["timestamp"])} for x in d]

def cg_global():
    return get("https://api.coingecko.com/api/v3/global", tries=3, sleep=4)["data"]

def fx_rates():
    d = get("https://open.er-api.com/v6/latest/USD")
    r = d["rates"]
    return {"USD": 1.0, **{k: r[k] for k in ("PKR", "GBP", "EUR", "AUD", "INR", "AED", "CAD") if k in r}}

def stablecoins():
    d = get("https://stablecoins.llama.fi/stablecoincharts/all")
    def tot(x):
        return float(x.get("totalCirculatingUSD", {}).get("peggedUSD", 0))
    now, ago = tot(d[-1]), tot(d[-31]) if len(d) > 31 else tot(d[0])
    return {"total": now, "ch30": pct(now, ago)}

def defi_tvl():
    d = get("https://api.llama.fi/v2/historicalChainTvl")
    now, ago = d[-1]["tvl"], d[-31]["tvl"] if len(d) > 31 else d[0]["tvl"]
    return {"total": now, "ch30": pct(now, ago)}


# ---------------------------------------------------------------- news
FEEDS = [
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Google News · Crypto", "https://news.google.com/rss/search?q=crypto+OR+bitcoin+OR+ethereum+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News · Macro", "https://news.google.com/rss/search?q=%22federal+reserve%22+OR+inflation+OR+tariffs+OR+%22interest+rates%22+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News · Regulation", "https://news.google.com/rss/search?q=crypto+(SEC+OR+regulation+OR+ETF+OR+PVARA+OR+ASIC)+when:2d&hl=en-US&gl=US&ceid=US:en"),
]

POS = {"approve": 1, "approved": 1.2, "approval": 1, "etf inflow": 1.2, "inflows": 0.8, "inflow": 0.8, "surge": 0.8,
       "surges": 0.8, "soar": 0.8, "soars": 0.8, "rally": 0.7, "rallies": 0.7, "record high": 1, "all-time high": 1,
       "partnership": 0.6, "adoption": 0.6, "adopts": 0.6, "launch": 0.3, "launches": 0.3, "upgrade": 0.5,
       "bullish": 0.8, "buys": 0.5, "accumulate": 0.6, "accumulating": 0.6, "breakout": 0.7, "rate cut": 0.8,
       "rate cuts": 0.8, "reserve": 0.4, "listing": 0.4, "lists": 0.3, "gains": 0.5, "jumps": 0.6, "wins": 0.5,
       "dismissed": 0.6, "clarity": 0.5, "legalize": 0.6, "license": 0.4, "licence": 0.4, "rebound": 0.6, "recover": 0.4}
NEG = {"hack": -1.2, "hacked": -1.2, "exploit": -1.2, "exploited": -1.2, "drained": -1.2, "lawsuit": -0.8,
       "sues": -0.8, "charged": -0.8, "fraud": -1, "scam": -1, "ban": -0.9, "bans": -0.9, "delist": -1,
       "delisting": -1, "outflow": -0.8, "outflows": -0.8, "crash": -1, "crashes": -1, "plunge": -0.9,
       "plunges": -0.9, "tumble": -0.8, "tumbles": -0.8, "slump": -0.7, "liquidation": -0.6, "liquidations": -0.6,
       "bearish": -0.8, "sell-off": -0.8, "selloff": -0.8, "dump": -0.7, "investigation": -0.7, "probe": -0.6,
       "unlock": -0.4, "unlocks": -0.4, "rate hike": -0.8, "inflation rises": -0.6, "tariff": -0.5, "tariffs": -0.5,
       "recession": -0.7, "warning": -0.4, "falls": -0.5, "drops": -0.5, "declines": -0.5, "insolvent": -1.2,
       "bankrupt": -1.2, "bankruptcy": -1.2, "halt": -0.6, "halts": -0.6, "outage": -0.5, "rug": -1.2}
CATS = [("Regulation", ["sec", "regulat", "law", "bill", "licen", "pvara", "asic", "cftc", "mica", "court", "ban", "tax"]),
        ("Macro", ["fed", "federal reserve", "inflation", "cpi", "rate", "tariff", "recession", "dollar", "treasury", "jobs", "gdp"]),
        ("ETF / Institutions", ["etf", "blackrock", "fidelity", "institution", "treasury company", "strategy", "saylor", "fund"]),
        ("Security", ["hack", "exploit", "drain", "scam", "phishing", "rug", "vulnerab"]),
        ("Exchange", ["binance", "coinbase", "kraken", "okx", "bybit", "exchange", "listing", "delist"]),
        ("Tech / Upgrades", ["upgrade", "mainnet", "fork", "launch", "layer", "protocol", "network"])]
ALIASES = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", " eth "], "BNB": ["bnb", "binance coin"],
           "SOL": ["solana", " sol "], "XRP": ["xrp", "ripple"], "DOGE": ["dogecoin", "doge"], "ADA": ["cardano"],
           "TRX": ["tron", " trx"], "AVAX": ["avalanche", "avax"], "LINK": ["chainlink"], "DOT": ["polkadot"],
           "TON": ["toncoin", " ton "], "SHIB": ["shiba"], "PEPE": ["pepe"], "SUI": [" sui"], "LTC": ["litecoin"],
           "BCH": ["bitcoin cash"], "NEAR": ["near protocol"], "APT": ["aptos"], "UNI": ["uniswap"], "HBAR": ["hedera"],
           "XLM": ["stellar"], "ARB": ["arbitrum"], "OP": ["optimism"], "WIF": ["dogwifhat"], "BONK": ["bonk"],
           "TRUMP": ["trump coin", "$trump"], "HYPE": ["hyperliquid"], "ENA": ["ethena"], "AAVE": ["aave"],
           "FET": ["fetch.ai", "artificial superintelligence"], "RENDER": ["render network"], "INJ": ["injective"],
           "ICP": ["internet computer"], "FIL": ["filecoin"], "ATOM": ["cosmos"], "ETC": ["ethereum classic"],
           "TAO": ["bittensor"], "WLD": ["worldcoin"], "SEI": [" sei "], "FLOKI": ["floki"], "ONDO": ["ondo"]}

def parse_feed(name, url):
    raw = get(url, tries=2, raw=True, timeout=20)
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pd = it.findtext("pubDate") or ""
        srcname = it.findtext("source") or name
        ts = 0
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                ts = int(datetime.strptime(pd.strip(), fmt).timestamp()); break
            except Exception:  # noqa
                pass
        if t:
            if name.startswith("Google News") and " - " in t:
                t, srcname = t.rsplit(" - ", 1)
            items.append({"title": html.unescape(t), "link": link, "source": srcname.strip(), "feed": name, "ts": ts})
    return items[:40]

def score_text(t):
    s = " " + t.lower() + " "
    v = 0.0
    for k, w in POS.items():
        if k in s: v += w
    for k, w in NEG.items():
        if k in s: v += w
    return clamp(v / 2, -1, 1)

def tag_news(items, symbols):
    out, seen = [], set()
    for it in sorted(items, key=lambda x: -x["ts"]):
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        s = " " + it["title"].lower().replace(",", " ").replace(":", " ") + " "
        coins = []
        for sym in symbols:
            al = ALIASES.get(sym, []) + [f" {sym.lower()} ", f"${sym.lower()}"]
            if any(a in s for a in al):
                coins.append(sym)
        cat = next((c for c, kws in CATS if any(re.search(r"\b" + re.escape(k), s) for k in kws)), "Market")
        sent = score_text(it["title"])
        impact = "high" if (cat in ("Regulation", "Macro", "Security", "ETF / Institutions") and abs(sent) >= 0.3) or \
                 any(c in ("BTC", "ETH") for c in coins) and abs(sent) >= 0.4 else ("medium" if abs(sent) >= 0.25 or coins else "low")
        out.append({**it, "coins": coins, "cat": cat, "sent": round(sent, 2), "impact": impact})
    return out[:60]


# ---------------------------------------------------------------- analysis
def short_rules(close, e20, e50, e50_prev, r, mh, mh_prev, vol_ratio, up_candle, bb_up, d_close, d_e50, d_e200, regime):
    """Scores the 4h swing setup. Returns ([(points, reason)], momentum_ok). Used live AND in the backtest."""
    out = []
    out.append((1, "Price above 4h EMA50 (short-term uptrend)") if close > e50 else (-1, "Price below 4h EMA50 (short-term downtrend)"))
    out.append((1, "EMA20 above EMA50 (momentum aligned)") if e20 > e50 else (-0.5, "EMA20 below EMA50 (momentum weak)"))
    slope = pct(e50, e50_prev)
    out.append((0.5 if slope > 0 else -0.5, f"4h EMA50 sloping {'up' if slope > 0 else 'down'} ({slope:+.1f}% / 2 days)"))
    if 50 <= r <= 68: out.append((1, f"RSI {r:.0f}: healthy bullish momentum"))
    elif 68 < r <= 75: out.append((0.25, f"RSI {r:.0f}: strong but getting stretched"))
    elif r > 75: out.append((-1, f"RSI {r:.0f}: overbought, pullback risk"))
    elif 40 <= r < 50: out.append((0, f"RSI {r:.0f}: neutral"))
    elif 30 <= r < 40: out.append((-0.5, f"RSI {r:.0f}: weak momentum"))
    elif d_e200 is not None and d_close > d_e200: out.append((0.25, f"RSI {r:.0f}: oversold dip inside a long-term uptrend"))
    else: out.append((-0.5, f"RSI {r:.0f}: oversold in a downtrend (falling knife)"))
    if mh > 0 and mh > mh_prev: out.append((1, "MACD histogram positive and rising"))
    elif mh > 0: out.append((0.25, "MACD positive but fading"))
    elif mh > mh_prev: out.append((0, "MACD negative but improving"))
    else: out.append((-1, "MACD negative and falling"))
    if vol_ratio > 1.8: out.append((0.5 if up_candle else -0.5, f"Volume spike x{vol_ratio:.1f} on a {'green' if up_candle else 'red'} candle"))
    if close > bb_up: out.append((-0.5, "Closed above upper Bollinger Band (stretched)"))
    if d_e50 is not None:
        out.append((0.5, "Daily close above EMA50") if d_close > d_e50 else (-0.5, "Daily close below EMA50"))
    if regime == "bear": out.append((-1.5, "Market filter: BTC below its 200-day average (bear regime)"))
    elif regime == "neutral": out.append((-0.5, "Market filter: BTC regime neutral / choppy"))
    else: out.append((0.25, "Market filter: BTC in bull regime"))
    return out, (48 <= r <= 72 and mh > mh_prev)


def trade_levels(price, atr_v, swing_low):
    stop = max(price - 2 * atr_v, swing_low * 0.995)
    sp = pct(price, stop)
    if sp < 1.5: stop = price * 0.985
    if sp > 12: stop = price * 0.88
    rv = price - stop
    return stop, price + 1.5 * rv, price + 3 * rv


def regime_series(kd):
    """BTC regime for each daily candle (known at that day's close)."""
    cd = [k["c"] for k in kd]; e50, e200 = ema(cd, 50), ema(cd, 200)
    out = {}
    for i, k in enumerate(kd):
        if i < 200: out[k["t"]] = "neutral"; continue
        p = cd[i]
        out[k["t"]] = "bull" if p > e200[i] and e50[i] > e200[i] else "bear" if p < e200[i] and e50[i] < e200[i] else "neutral"
    return out


def backtest_short(k4, kd, btc_reg, fee=0.001, warmup=210):
    """Replays the short-term rules bar by bar over the 4h history (no look-ahead: each decision uses
    only candles closed at that time). Entry at the signal candle's close; exits by stop / TP1 (half, stop
    to entry) / TP2 / 60-bar time stop. Returns per-coin stats. News is not included (no history)."""
    n = len(k4)
    if n < warmup + 30 or len(kd) < 60:
        return None
    c = [k["c"] for k in k4]; h = [k["h"] for k in k4]; l = [k["l"] for k in k4]; v = [k["v"] for k in k4]
    e20, e50, r4, mh, a4 = ema(c, 20), ema(c, 50), rsi(c), macd_hist(c), atr(h, l, c)
    cd = [k["c"] for k in kd]; ed50, ed200 = ema(cd, 50), ema(cd, 200)
    day_ms = 86400000
    dts = [k["t"] for k in kd]
    trades, i = [], warmup
    while i < n - 1:
        # latest daily candle fully closed at this 4h close
        t_close = k4[i]["t"] + 4 * 3600000
        di = bisect.bisect_right(dts, t_close - day_ms) - 1   # last daily candle closed by now
        if di < 1:
            i += 1; continue
        dk = dts[di]
        win = c[i - 19:i + 1]; bm = sum(win) / 20; bb_up = bm + 2 * stdev(win)
        vr = v[i] / (sum(v[i - 20:i]) / 20) if sum(v[i - 20:i]) > 0 else 1
        rules, mom = short_rules(c[i], e20[i], e50[i], e50[i - 12], r4[i], mh[i], mh[i - 1], vr, c[i] >= k4[i]["o"],
                                 bb_up, cd[di], ed50[di] if di >= 200 else None, ed200[di] if di >= 200 else None,
                                 btc_reg.get(dk, "neutral"))
        score = 50 + sum(x for x, _ in rules) * 7
        if not (score >= 72 and mom):
            i += 1; continue
        entry = c[i]
        stop, tp1, tp2 = trade_levels(entry, a4[i], min(l[i - 19:i + 1]))
        qty, realized, tp1_hit, exit_i, reason = 1.0, 0.0, False, None, None
        cost = entry * (1 + fee)
        for j in range(i + 1, min(n, i + 61)):
            if l[j] <= stop:
                realized += qty * stop * (1 - fee); exit_i, reason = j, ("stop" if not tp1_hit else "breakeven"); qty = 0; break
            if not tp1_hit and h[j] >= tp1:
                realized += 0.5 * tp1 * (1 - fee); qty = 0.5; tp1_hit = True; stop = entry
            if tp1_hit and h[j] >= tp2:
                realized += qty * tp2 * (1 - fee); exit_i, reason = j, "tp2"; qty = 0; break
        if qty > 0:
            j = min(n - 1, i + 60)
            if j == n - 1 and i + 60 > n - 1:
                break  # trade still open at the end of the data: not counted
            realized += qty * c[j] * (1 - fee); exit_i, reason = j, "time"
        ret = (realized - cost) / cost * 100
        risk_pct = (entry - trade_levels(entry, a4[i], min(l[i - 19:i + 1]))[0]) / entry * 100
        trades.append({"ret": ret, "r": ret / risk_pct if risk_pct else 0, "reason": reason, "bars": exit_i - i})
        i = exit_i + 1
    days = (k4[-1]["t"] - k4[warmup]["t"]) / day_ms
    hold = pct(c[-1], c[warmup])
    if not trades:
        return {"n": 0, "days": round(days), "hold": round(hold, 1)}
    wins = [t for t in trades if t["ret"] > 0]
    gross_w = sum(t["ret"] for t in wins); gross_l = -sum(t["ret"] for t in trades if t["ret"] <= 0)
    comp = 1.0
    for t in trades:
        comp *= 1 + t["ret"] / 100 * 0.33   # one-third position size, as the live rules use
    return {"n": len(trades), "win": round(len(wins) / len(trades) * 100), "avg": round(sum(t["ret"] for t in trades) / len(trades), 2),
            "avg_r": round(sum(t["r"] for t in trades) / len(trades), 2),
            "pf": round(gross_w / gross_l, 2) if gross_l > 0 else None,
            "acct": round((comp - 1) * 100, 2), "hold": round(hold, 1), "days": round(days),
            "avg_bars": round(sum(t["bars"] for t in trades) / len(trades), 1)}


def analyse(sym, meta, k4, kd, btc_ctx, fng_now, news_for_coin):
    c4 = [k["c"] for k in k4]; h4 = [k["h"] for k in k4]; l4 = [k["l"] for k in k4]; v4 = [k["v"] for k in k4]
    cd = [k["c"] for k in kd]; hd = [k["h"] for k in kd]; ld = [k["l"] for k in kd]
    price = meta.get("price") or c4[-1]
    e20, e50 = ema(c4, 20), ema(c4, 50)
    r4 = rsi(c4); mh = macd_hist(c4); a4 = atr(h4, l4, c4)
    ed50, ed200 = ema(cd, 50), ema(cd, 200)
    rd = rsi(cd)
    bb_m = sma(c4, 20); bb_sd = stdev(c4[-20:]); bb_up, bb_lo = bb_m + 2 * bb_sd, bb_m - 2 * bb_sd
    vol_ratio = (v4[-1] / (sum(v4[-21:-1]) / 20)) if len(v4) > 21 and sum(v4[-21:-1]) > 0 else 1
    rets = [math.log(cd[i] / cd[i - 1]) for i in range(max(1, len(cd) - 30), len(cd)) if cd[i - 1] > 0]
    vol_ann = stdev(rets) * math.sqrt(365) * 100 if rets else 0
    hi365 = max(hd[-365:]) if hd else price
    dd365 = pct(price, hi365)
    ret90 = pct(cd[-1], cd[-91]) if len(cd) > 91 else pct(cd[-1], cd[0])
    rs_btc = ret90 - btc_ctx.get("ret90", 0) if sym != "BTC" else 0
    atr_pct = a4[-1] / price * 100 if price else 0
    swing_low = min(l4[-20:]); swing_high = max(h4[-50:])
    enough_daily = len(cd) >= 200

    # ---------- short-term (4h swing: days to ~2 weeks) — shared rules, also used by the backtest
    R = []  # reasons
    s = 0.0
    def add(v, t):
        nonlocal s
        s += v
        R.append({"t": t, "s": 1 if v > 0 else (-1 if v < 0 else 0)})
    rules, momentum_ok = short_rules(
        c4[-1], e20[-1], e50[-1], e50[-13] if len(e50) > 13 else e50[0], r4[-1], mh[-1], mh[-2], vol_ratio,
        k4[-1]["c"] >= k4[-1]["o"], bb_up, cd[-1], ed50[-1] if enough_daily else None,
        ed200[-1] if enough_daily else None, btc_ctx.get("regime", "neutral"))
    for v, t in rules:
        add(v, t)
    ns = news_for_coin["sent"]
    if news_for_coin["n"]:
        add(clamp(ns * 0.75, -0.75, 0.75), f"News flow {'positive' if ns > 0.1 else 'negative' if ns < -0.1 else 'mixed'} ({news_for_coin['n']} headlines)")
    ch7 = meta.get("ch7d") or 0
    if ch7 > 40: add(-0.75, f"Already +{ch7:.0f}% in 7 days (chasing risk)")
    bt = meta.get("bt") or {}
    if bt.get("n", 0) >= 6 and bt.get("pf") is not None:
        if bt["pf"] >= 1.5: add(0.5, f"Track record: these rules made money on {sym} before ({bt['n']} past trades, {bt['win']}% wins)")
        elif bt["pf"] < 0.8: add(-0.5, f"Track record: these rules lost money on {sym} before ({bt['n']} past trades, {bt['win']}% wins)")
    st_score = int(clamp(50 + s * 7, 0, 100))

    # ---------- long-term (weeks to months)
    LR = []; ls = 0.0
    def ladd(v, t):
        nonlocal ls
        ls += v
        LR.append({"t": t, "s": 1 if v > 0 else (-1 if v < 0 else 0)})
    if enough_daily:
        ladd(1.5 if cd[-1] > ed200[-1] else -1.5, "Above 200-day average (long-term uptrend)" if cd[-1] > ed200[-1] else "Below 200-day average (long-term downtrend)")
        ladd(1 if ed50[-1] > ed200[-1] else -0.5, "Golden cross (50d > 200d)" if ed50[-1] > ed200[-1] else "Death cross (50d < 200d)")
        sl200 = pct(ed200[-1], ed200[-21])
        ladd(0.5 if sl200 > 0 else -0.5, f"200-day average {'rising' if sl200 > 0 else 'falling'}")
    else:
        ladd(-0.5, "Less than 200 days of history (young asset)")
    if sym != "BTC":
        if rs_btc > 10: ladd(0.75, f"Outperforming BTC by {rs_btc:.0f}% over 90d")
        elif rs_btc < -20: ladd(-0.75, f"Underperforming BTC by {abs(rs_btc):.0f}% over 90d")
    if -60 <= dd365 <= -20 and enough_daily and cd[-1] > ed200[-1]: ladd(0.5, f"{abs(dd365):.0f}% below 1-year high while trend is up (discount)")
    if dd365 < -80: ladd(-1, f"{abs(dd365):.0f}% below 1-year high (broken chart)")
    rank = meta.get("rank") or 999
    if rank <= 10: ladd(1, f"Top-10 asset (rank #{rank}): deep liquidity")
    elif rank <= 25: ladd(0.5, f"Large cap (rank #{rank})")
    if meta.get("meme"): ladd(-1, "Meme coin: driven by hype, no cash flows")
    if vol_ann > 120: ladd(-0.5, f"Very volatile ({vol_ann:.0f}% annualised)")
    if fng_now is not None:
        if fng_now <= 25: ladd(0.5, f"Fear & Greed {fng_now} (extreme fear): contrarian accumulation window")
        elif fng_now >= 80: ladd(-0.5, f"Fear & Greed {fng_now} (extreme greed): be patient")
    if rd[-1] > 80: ladd(-0.75, f"Daily RSI {rd[-1]:.0f}: overheated")
    elif rd[-1] > 70: ladd(-0.25, f"Daily RSI {rd[-1]:.0f}: running hot, better entries likely on a dip")
    if enough_daily and price > ed200[-1] * 1.5: ladd(-0.75, f"{pct(price, ed200[-1]):.0f}% above the 200-day average (overextended)")
    if fng_now is not None and 65 <= fng_now < 80: ladd(-0.25, f"Fear & Greed {fng_now} (greed): no rush to buy")
    if news_for_coin["n"]:
        ladd(clamp(ns * 0.5, -0.5, 0.5), "Recent news sentiment " + ("positive" if ns > 0.1 else "negative" if ns < -0.1 else "mixed"))
    lt_score = int(clamp(50 + ls * 6.5, 0, 100))

    def label(x):
        return "Strong Buy" if x >= 72 else "Buy" if x >= 60 else "Hold" if x >= 45 else "Reduce" if x >= 32 else "Sell / Avoid"
    # Strong Buy on the short-term view needs momentum confirmation
    if st_score >= 72 and not momentum_ok:
        st_score = 71
        R.append({"t": "Capped at Buy: waiting for momentum confirmation (RSI 48–72 and MACD rising)", "s": 0})

    # ---------- risk grade
    if meta.get("meme") or vol_ann > 150: risk = "Extreme"
    elif rank <= 10 and vol_ann < 70: risk = "Low"
    elif rank <= 30 and vol_ann < 110: risk = "Medium"
    else: risk = "High"

    # ---------- short-term trade plan
    stop, tp1, tp2 = trade_levels(price, a4[-1], swing_low)
    stop_pct = (1 - stop / price) * 100
    lo = e20[-1] if e20[-1] < price else price - 0.5 * a4[-1]
    zone = [max(lo, price - a4[-1]), price]
    cap = CFG["capital_usd"]
    risk_usd = cap * CFG["risk_per_trade"]
    size = min(risk_usd / (stop_pct / 100), cap / CFG["max_open"])
    if meta.get("meme"): size = min(size, cap * CFG["meme_cap_frac"])
    short_plan = {
        "entry": sig(price), "zone": [sig(zone[0]), sig(zone[1])], "stop": sig(stop), "stop_pct": round(stop_pct, 2),
        "tp1": sig(tp1), "tp2": sig(tp2), "tp1_pct": round(pct(tp1, price), 2), "tp2_pct": round(pct(tp2, price), 2),
        "resistance": sig(swing_high), "horizon": "3–14 days",
        "size_usd": round(size, 2), "risk_usd": round(size * stop_pct / 100, 2),
        "sell_rules": [
            f"Stop-loss: sell everything if price trades below {usd(stop)} (−{stop_pct:.1f}%).",
            f"Take profit 1: sell 50% at {usd(tp1)} (+{pct(tp1, price):.1f}%), then move the stop to your entry price.",
            f"Take profit 2: sell the rest at {usd(tp2)} (+{pct(tp2, price):.1f}%), or earlier if a 4h candle closes below the EMA20 after TP1.",
            "Exit early if a 4h candle closes below the EMA50 while MACD is negative (trend broken).",
            "Time stop: if neither target nor stop is hit within 10 days, close the trade and free up the capital.",
        ],
    }
    # ---------- long-term plan
    if enough_daily and cd[-1] > ed200[-1]:
        lz = [sig(min(ed50[-1], ed200[-1])), sig(max(ed50[-1], ed200[-1]))]
        lz_text = f"Accumulate on dips between {usd(lz[0])} and {usd(lz[1])} (50/200-day averages)."
    elif enough_daily:
        lz = [sig(ed200[-1]), sig(ed200[-1] * 1.05)]
        lz_text = f"Wait: only start buying after a daily close back above the 200-day average ({usd(ed200[-1])})."
    else:
        lz = [sig(price * 0.85), sig(price)]
        lz_text = "Young asset: buy only in small pieces (DCA) and keep the position small."
    inval = ed200[-1] * 0.92 if enough_daily else price * 0.7
    t1 = hi365 if hi365 > price * 1.05 else price * 1.3
    long_plan = {
        "zone": lz, "zone_text": lz_text, "invalidation": sig(inval), "target1": sig(t1), "target2": sig(t1 * 1.35),
        "horizon": "3–12 months",
        "sell_rules": [
            f"Exit if a weekly candle closes below {usd(inval)} (long-term trend broken).",
            f"Trim 25% near {usd(t1)} (previous 1-year high / major resistance).",
            "Trim another 25% each time the daily RSI goes above 80 or Fear & Greed hits 85+ (euphoria).",
            "Keep a core position while price stays above the 200-day average.",
        ],
    }
    ind = {
        "rsi4h": round(r4[-1], 1), "rsi1d": round(rd[-1], 1), "macdh4h": sig(mh[-1], 3),
        "macd_rising": mh[-1] > mh[-2], "ema20_4h": sig(e20[-1]), "ema50_4h": sig(e50[-1]),
        "ema50_1d": sig(ed50[-1]) if enough_daily else None, "ema200_1d": sig(ed200[-1]) if enough_daily else None,
        "atr4h_pct": round(atr_pct, 2), "vol_ratio": round(vol_ratio, 2),
        "bb_pos": round((c4[-1] - bb_lo) / (bb_up - bb_lo) * 100, 0) if bb_up > bb_lo else 50,
        "vol_ann": round(vol_ann, 1), "dd365": round(dd365, 1), "rs_btc90": round(rs_btc, 1), "ret90": round(ret90, 1),
        "hi365": sig(hi365), "support": sig(swing_low), "resistance": sig(swing_high),
    }
    return {
        "short": {"score": st_score, "label": label(st_score), "reasons": R, "plan": short_plan},
        "long": {"score": lt_score, "label": label(lt_score), "reasons": LR, "plan": long_plan},
        "risk": risk, "ind": ind,
    }


def btc_context(kd):
    cd = [k["c"] for k in kd]
    e50, e200 = ema(cd, 50), ema(cd, 200)
    p = cd[-1]
    if p > e200[-1] and e50[-1] > e200[-1]: regime = "bull"
    elif p < e200[-1] and e50[-1] < e200[-1]: regime = "bear"
    else: regime = "neutral"
    ret90 = pct(cd[-1], cd[-91]) if len(cd) > 91 else 0
    txt = {"bull": "BTC is above its 50 and 200-day averages: buying dips is favoured, full position sizes allowed.",
           "neutral": "BTC is mixed around its long-term averages: be selective, smaller sizes, take profits sooner.",
           "bear": "BTC is below its 200-day average: capital protection first. Mostly stay in USDT; only the strongest setups."}[regime]
    return {"regime": regime, "price": sig(p), "ema50": sig(e50[-1]), "ema200": sig(e200[-1]), "ret90": ret90, "text": txt}


# ---------------------------------------------------------------- paper portfolio
def update_paper(prev, coins, klines4h, now_ms, regime):
    """Simulated $300 account that follows the Strong Buy rules, including the kill-switch and daily loss limit."""
    cap = CFG["capital_usd"]
    st = prev or {"start_capital": cap, "cash": cap, "open": [], "closed": [], "equity": [], "started": now_ms}
    fee, H4 = CFG["fee"], 4 * 3600000
    events, still = [], []
    last_px = {s: k[-1]["c"] for s, k in klines4h.items() if k}
    for c in coins:
        last_px[c["sym"]] = c["price"]

    def sell(p, q, px):
        st["cash"] += q * px * (1 - fee)
        p["realized"] = p.get("realized", 0) + q * px * (1 - fee) - p["cost"] * q / p["qty0"]
        p["qty"] -= q

    for p in st["open"]:
        closed = False
        for k in klines4h.get(p["sym"], []):
            k_close = k["t"] + H4
            if k_close <= p.get("checked", p["opened"]) or k["t"] < p["opened"] - H4:
                continue
            if k["t"] < p["opened"]:
                continue  # the entry candle: its range before the entry is unknown, skip it (conservative)
            if k["l"] <= p["stop"]:  # stop is checked first (conservative)
                sell(p, p["qty"], p["stop"])
                reason = "Stop-loss" if not p.get("tp1_hit") else "Stopped at breakeven"
                st["closed"].append({**p, "exit": p["stop"], "closed": k["t"], "reason": reason, "pnl": round(p["realized"], 2)})
                events.append(f"Closed {p['sym']} at {sig(p['stop'])} ({reason.lower()})")
                closed = True; break
            if not p.get("tp1_hit") and k["h"] >= p["tp1"]:
                sell(p, p["qty"] / 2, p["tp1"]); p["tp1_hit"] = True; p["stop"] = p["entry"]
                events.append(f"{p['sym']} hit TP1 {sig(p['tp1'])}: sold 50%, stop moved to entry")
            if p.get("tp1_hit") and k["h"] >= p["tp2"]:
                sell(p, p["qty"], p["tp2"])
                st["closed"].append({**p, "exit": p["tp2"], "closed": k["t"], "reason": "Take profit 2", "pnl": round(p["realized"], 2)})
                events.append(f"Closed {p['sym']} at TP2 {sig(p['tp2'])}")
                closed = True; break
            if k_close <= now_ms and k_close - p["opened"] > 10 * 86400000:
                sell(p, p["qty"], k["c"])
                st["closed"].append({**p, "exit": k["c"], "closed": k["t"], "reason": "Time stop (10 days)", "pnl": round(p["realized"], 2)})
                events.append(f"Closed {p['sym']} on the 10-day time stop")
                closed = True; break
            if k_close <= now_ms:
                p["checked"] = k_close  # only fully closed candles are marked as processed
        if not closed:
            still.append(p)
    st["open"] = still

    def equity_now():
        return st["cash"] + sum(p["qty"] * last_px.get(p["sym"], p["entry"]) for p in st["open"])

    eq = equity_now()
    peak = max([e["v"] for e in st["equity"]] + [st["start_capital"], eq])
    day_ago = [e["v"] for e in st["equity"] if e["t"] <= now_ms - 86400000]
    ref = day_ago[-1] if day_ago else st["start_capital"]
    halted = None
    if eq < peak * 0.85:
        halted = "Kill-switch: equity is more than 15% below its peak. No new trades until reviewed."
    elif eq < ref * 0.97:
        halted = "Daily loss limit: down more than 3% in 24h. No new trades for now."
    if halted:
        events.append(halted)
    else:
        cands = sorted([c for c in coins if c["short"]["label"] == "Strong Buy" and c["risk"] != "Extreme"
                        and c["sym"] not in {p["sym"] for p in st["open"]}], key=lambda c: -c["short"]["score"])
        if regime == "bear":
            cands = [c for c in cands if c["short"]["score"] >= 80]
        for c in cands:
            if len(st["open"]) >= CFG["max_open"]:
                break
            pl, e = c["short"]["plan"], equity_now()
            size = min(e * CFG["risk_per_trade"] / (pl["stop_pct"] / 100), e / CFG["max_open"], st["cash"] * 0.98 - e * 0.10)
            if size < 10:
                break
            qty = size / c["price"]
            st["cash"] -= size * (1 + fee)
            st["open"].append({"sym": c["sym"], "entry": c["price"], "qty": qty, "qty0": qty, "cost": size * (1 + fee),
                               "stop": pl["stop"], "tp1": pl["tp1"], "tp2": pl["tp2"], "opened": now_ms, "checked": now_ms,
                               "score": c["short"]["score"]})
            events.append(f"Opened {c['sym']} ${size:.0f} at {sig(c['price'])} (score {c['short']['score']})")
    eq = equity_now()
    st["equity"].append({"t": now_ms, "v": round(eq, 2)})
    st["equity"] = st["equity"][-540:]
    st["closed"] = st["closed"][-100:]
    wins = [x for x in st["closed"] if x["pnl"] > 0]
    run_peak, max_dd = 0, 0
    for e in st["equity"]:
        run_peak = max(run_peak, e["v"]); max_dd = min(max_dd, pct(e["v"], run_peak))
    st["stats"] = {"equity": round(eq, 2), "return_pct": round(pct(eq, st["start_capital"]), 2),
                   "trades": len(st["closed"]), "win_rate": round(len(wins) / len(st["closed"]) * 100, 1) if st["closed"] else None,
                   "open": len(st["open"]), "cash": round(st["cash"], 2), "max_dd": round(max_dd, 2),
                   "realized": round(sum(x["pnl"] for x in st["closed"]), 2), "halted": halted}
    st["last_events"] = events
    return st


# ---------------------------------------------------------------- demo data
def demo_series(n, start, drift, vol, step_ms, now_ms):
    out, p = [], start
    for i in range(n):
        o = p
        p = max(p * math.exp(random.gauss(drift, vol)), 1e-9)
        h = max(o, p) * (1 + abs(random.gauss(0, vol / 2))); l = min(o, p) * (1 - abs(random.gauss(0, vol / 2)))
        out.append({"t": now_ms - (n - i) * step_ms, "o": o, "h": h, "l": l, "c": p, "v": random.uniform(5e6, 5e7)})
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--prev", default=None)
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    def load_prev(name):
        if a.prev and os.path.exists(os.path.join(a.prev, name)):
            try:
                d = json.load(open(os.path.join(a.prev, name)))
                return d.get("state", d) if name == "paper.json" else d
            except Exception:  # noqa
                return None
        return None

    # ---- universe
    if a.demo:
        random.seed(7)
        demo = [("BTC", "Bitcoin", 1, 64000, False), ("ETH", "Ethereum", 2, 3100, False), ("BNB", "BNB", 4, 590, False),
                ("SOL", "Solana", 5, 150, False), ("XRP", "XRP", 6, 0.6, False), ("DOGE", "Dogecoin", 8, 0.12, True),
                ("ADA", "Cardano", 9, 0.4, False), ("LINK", "Chainlink", 14, 13, False), ("PEPE", "Pepe", 30, 0.0000095, True),
                ("AVAX", "Avalanche", 12, 27, False)]
        markets = [{"symbol": s.lower(), "name": n, "id": n.lower(), "market_cap_rank": r, "current_price": p,
                    "market_cap": p * 1e9 / r, "total_volume": 5e8 / r, "price_change_percentage_1h_in_currency": random.uniform(-1, 1),
                    "price_change_percentage_24h_in_currency": random.uniform(-5, 5), "price_change_percentage_7d_in_currency": random.uniform(-12, 12),
                    "price_change_percentage_30d_in_currency": random.uniform(-25, 25), "ath_change_percentage": random.uniform(-80, -5),
                    "_meme": m} for s, n, r, p, m in demo]
        memes = []
        host, tick = "demo", {f"{s}USDT": {"quoteVolume": "9e8"} for s, *_ in demo}
    else:
        markets = src("CoinGecko markets", cg_markets, None, 150) or []
        time.sleep(2)
        memes = src("CoinGecko meme category", cg_markets, "meme-token", 30) or []
        bt = src("Binance tickers", binance_tickers)
        host, tick = bt if bt else (None, {})

    if not markets and tick:
        LOG["fail"].append("CoinGecko unavailable: using last known market-cap ranks + Binance volume")
        prev_snap = load_prev("snapshot.json") or {}
        prev_rank = {c["sym"]: c.get("rank") for c in prev_snap.get("coins", []) if c.get("rank")}
        ranked = sorted(tick.values(), key=lambda t: (prev_rank.get(t["symbol"][:-4], 10_000), -float(t.get("quoteVolume", 0))))
        markets = [{"symbol": t["symbol"][:-4].lower(), "name": t["symbol"][:-4], "id": t["symbol"][:-4].lower(),
                    "market_cap_rank": prev_rank.get(t["symbol"][:-4]) or (i + 1), "current_price": float(t["lastPrice"]), "market_cap": None,
                    "total_volume": float(t["quoteVolume"]),
                    "price_change_percentage_24h_in_currency": float(t["priceChangePercent"])} for i, t in enumerate(ranked[:120])]
    meme_ids = {m["id"] for m in memes}
    universe, seen = [], set()
    def eligible(m):
        s = m["symbol"].upper()
        return re.fullmatch(r"[A-Z0-9]{2,12}", s) is not None and s not in STABLES and s not in WRAPPED and not s.endswith(("UP", "DOWN", "BULL", "BEAR")) and s not in seen
    for m in markets:
        if len([u for u in universe if not u["meme"]]) >= CFG["universe_top"]:
            break
        s = m["symbol"].upper()
        if not eligible(m):
            continue
        if tick and (s + "USDT") not in tick:
            continue
        if tick and float(tick[s + "USDT"].get("quoteVolume", 0)) < CFG["min_quote_vol"]:
            continue
        seen.add(s)
        universe.append({"m": m, "meme": m["id"] in meme_ids or m.get("_meme", False) or s in KNOWN_MEMES})
    for m in memes[:25]:
        if len([u for u in universe if u["meme"]]) >= CFG["meme_top"]:
            break
        s = m["symbol"].upper()
        if not eligible(m) or (tick and (s + "USDT") not in tick):
            continue
        if tick and float(tick[s + "USDT"].get("quoteVolume", 0)) < CFG["min_quote_vol"] / 3:
            continue
        seen.add(s)
        universe.append({"m": m, "meme": True})
    if not universe:
        raise SystemExit("No market data available — check network access. Sources failed: " + "; ".join(LOG["fail"]))

    # ---- candles
    K4, KD = {}, {}
    fallback_n = 0
    for u in universe:
        s = u["m"]["symbol"].upper()
        if a.demo:
            p = u["m"]["current_price"]
            kd = demo_series(400, p * 0.6, 0.0012, 0.03, 86400000, now_ms)
            scale = p / kd[-1]["c"]
            for k in kd:
                for f in "ohlc": k[f] *= scale
            k4 = demo_series(500, kd[-84]["c"], 0.0003, 0.012, 14400000, now_ms)
            sc = p / k4[-1]["c"]
            for k in k4:
                for f in "ohlc": k[f] *= sc
            K4[s], KD[s] = k4, kd
            continue
        try:
            if host:
                K4[s] = binance_klines(host, s + "USDT", "4h", 1000)
                KD[s] = binance_klines(host, s + "USDT", "1d", 400)
            else:
                if fallback_n >= 20:
                    continue
                K4[s], KD[s] = cg_candles(u["m"]["id"]); fallback_n += 1
        except Exception as e:  # noqa
            LOG["fail"].append(f"candles {s}: {str(e)[:100]}")
    prev_paper = load_prev("paper.json")
    for p in (prev_paper or {}).get("open", []):
        if p["sym"] not in K4 and host and host != "demo":
            try:
                K4[p["sym"]] = binance_klines(host, p["sym"] + "USDT", "4h", 200)
            except Exception as e:  # noqa
                LOG["fail"].append(f"paper position candles {p['sym']}: {str(e)[:80]}")
    # indicators use CLOSED candles only (the still-forming candle makes signals flicker); price stays live
    H4, D1 = 4 * 3600000, 86400000
    K4c = {k: [x for x in v if x["t"] + H4 <= now_ms] for k, v in K4.items()}
    KDc = {k: [x for x in v if x["t"] + D1 <= now_ms] for k, v in KD.items()}
    if host:
        LOG["ok"].append(f"Binance candles ({len(K4)} coins)")
    elif K4:
        LOG["ok"].append(f"CoinGecko candles fallback ({len(K4)} coins)")

    # ---- macro & sentiment
    if a.demo:
        fng = [{"v": 60 - i // 3, "l": "Greed", "t": int(now.timestamp()) - i * 86400} for i in range(60)]
        glob = {"total_market_cap": {"usd": 2.3e12}, "market_cap_change_percentage_24h_usd": 1.2,
                "market_cap_percentage": {"btc": 56.1, "eth": 13.2}}
        fx = {"USD": 1.0, "PKR": 280.5, "GBP": 0.75}
        stab = {"total": 2.6e11, "ch30": 2.1}; tvl = {"total": 1.1e11, "ch30": -3.2}
        raw_news = [{"title": "Bitcoin ETF inflows surge as rate cut hopes grow", "link": "#", "source": "Demo", "feed": "Demo", "ts": int(now.timestamp()) - 3600},
                    {"title": "DeFi protocol exploited, $20M drained", "link": "#", "source": "Demo", "feed": "Demo", "ts": int(now.timestamp()) - 7200},
                    {"title": "Solana network upgrade launches on mainnet", "link": "#", "source": "Demo", "feed": "Demo", "ts": int(now.timestamp()) - 9000}]
    else:
        fng = src("Fear & Greed", fear_greed) or []
        glob = src("CoinGecko global", cg_global) or {}
        fx = src("FX rates", fx_rates) or {"USD": 1.0, "PKR": None, "GBP": None}
        stab = src("DefiLlama stablecoins", stablecoins)
        tvl = src("DefiLlama TVL", defi_tvl)
        raw_news = []
        for name, url in FEEDS:
            r = src(f"News: {name}", parse_feed, name, url)
            if r:
                raw_news += r
    syms = [u["m"]["symbol"].upper() for u in universe]
    news = tag_news(raw_news, syms)
    fng_now = fng[0]["v"] if fng else None

    # ---- BTC regime
    btc_ctx = btc_context(KDc["BTC"]) if "BTC" in KDc else {"regime": "neutral", "text": "BTC data unavailable", "ret90": 0}

    btc_reg = regime_series(KDc["BTC"]) if "BTC" in KDc else {}
    # ---- analyse coins
    coins, charts = [], {}
    for u in universe:
        m = u["m"]; s = m["symbol"].upper()
        if s not in K4c or s not in KDc or len(K4c[s]) < 60 or len(KDc[s]) < 30:
            continue
        cn = [n for n in news if s in n["coins"]]
        nf = {"n": len(cn), "sent": (sum(n["sent"] for n in cn) / len(cn)) if cn else 0}
        meta = {"price": K4[s][-1]["c"] if host or a.demo else m.get("current_price"), "rank": m.get("market_cap_rank"),
                "meme": u["meme"], "ch7d": m.get("price_change_percentage_7d_in_currency")}
        try:
            meta["bt"] = backtest_short(K4c[s], KDc[s], btc_reg)
            an = analyse(s, meta, K4c[s], KDc[s], btc_ctx, fng_now, nf)
            an["bt"] = meta["bt"]
        except Exception as e:  # noqa
            LOG["fail"].append(f"analyse {s}: {e}")
            continue
        price = meta["price"]
        cdl = KD[s]
        if m.get("price_change_percentage_7d_in_currency") is None and len(cdl) > 8:
            m["price_change_percentage_7d_in_currency"] = pct(cdl[-1]["c"], cdl[-8]["c"])
        if m.get("price_change_percentage_30d_in_currency") is None and len(cdl) > 31:
            m["price_change_percentage_30d_in_currency"] = pct(cdl[-1]["c"], cdl[-31]["c"])
        if m.get("price_change_percentage_24h_in_currency") is None and s + "USDT" in tick:
            m["price_change_percentage_24h_in_currency"] = float(tick[s + "USDT"]["priceChangePercent"])
        coins.append({
            "sym": s, "name": m.get("name"), "id": m.get("id"), "rank": m.get("market_cap_rank"), "meme": u["meme"],
            "price": sig(price, 6), "ch1h": round(m.get("price_change_percentage_1h_in_currency") or 0, 2),
            "ch24h": round(m.get("price_change_percentage_24h_in_currency") or 0, 2),
            "ch7d": round(m.get("price_change_percentage_7d_in_currency") or 0, 2),
            "ch30d": round(m.get("price_change_percentage_30d_in_currency") or 0, 2),
            "mcap": m.get("market_cap"), "vol24": float(tick.get(s + "USDT", {}).get("quoteVolume", 0)) or m.get("total_volume"),
            "ath_dist": round(m.get("ath_change_percentage") or 0, 1),
            "news_n": nf["n"], "news_sent": round(nf["sent"], 2), **an,
        })
        c4 = [k["c"] for k in K4[s]][-84:]   # 14 days of 4h
        cd = [k["c"] for k in KD[s]][-120:]  # 120 days
        charts[s] = {"h4": [sig(x, 5) for x in c4], "d1": [sig(x, 5) for x in cd],
                     "t4": K4[s][-84]["t"] if len(K4[s]) >= 84 else K4[s][0]["t"], "td": KD[s][-len(cd)]["t"]}

    # ---- market overview
    tot = (glob.get("total_market_cap") or {}).get("usd")
    lt_up = [c for c in coins if c["ind"]["ema200_1d"] and c["price"] > c["ind"]["ema200_1d"]]
    st_up = [c for c in coins if c["price"] > c["ind"]["ema50_4h"]]
    ranked = sorted(coins, key=lambda c: -c["ch24h"])
    market = {
        "total_mcap": tot, "mcap_ch24": round(glob.get("market_cap_change_percentage_24h_usd") or 0, 2),
        "btc_dom": round((glob.get("market_cap_percentage") or {}).get("btc", 0), 2),
        "eth_dom": round((glob.get("market_cap_percentage") or {}).get("eth", 0), 2),
        "fng": {"v": fng_now, "l": fng[0]["l"] if fng else None, "hist": [x["v"] for x in fng[:60]][::-1]},
        "regime": btc_ctx, "breadth": {"st": round(len(st_up) / len(coins) * 100) if coins else None,
                                       "lt": round(len(lt_up) / len(coins) * 100) if coins else None},
        "stables": stab, "tvl": tvl,
        "gainers": [{"sym": c["sym"], "ch": c["ch24h"]} for c in ranked[:5]],
        "losers": [{"sym": c["sym"], "ch": c["ch24h"]} for c in ranked[-5:][::-1]],
        "news_mood": round(sum(n["sent"] for n in news[:40]) / max(1, len(news[:40])), 2) if news else None,
    }
    # overall market score 0-100
    ms = 50
    ms += {"bull": 15, "neutral": 0, "bear": -15}[btc_ctx.get("regime", "neutral")]
    if market["breadth"]["st"] is not None: ms += (market["breadth"]["st"] - 50) * 0.2
    if market["breadth"]["lt"] is not None: ms += (market["breadth"]["lt"] - 50) * 0.2
    if stab: ms += clamp(stab["ch30"] * 2, -6, 6)
    if market["news_mood"] is not None: ms += market["news_mood"] * 10
    market["score"] = int(clamp(ms, 0, 100))
    market["stance"] = ("Risk-on: favourable for swing longs" if market["score"] >= 62 else
                        "Neutral: be selective, smaller sizes" if market["score"] >= 45 else
                        "Risk-off: protect capital, mostly USDT")

    # ---- top picks
    elig = [c for c in coins if c["risk"] != "Extreme"]
    picks = {
        "short": [c["sym"] for c in sorted(elig, key=lambda c: -c["short"]["score"]) if c["short"]["score"] >= 60][:5],
        "long": [c["sym"] for c in sorted(elig, key=lambda c: -c["long"]["score"]) if c["long"]["score"] >= 60][:5],
        "avoid": [c["sym"] for c in sorted(coins, key=lambda c: c["short"]["score"] + c["long"]["score"]) if c["short"]["score"] < 40 and c["long"]["score"] < 45][:5],
        "meme_watch": [c["sym"] for c in sorted([c for c in coins if c["meme"]], key=lambda c: -c["short"]["score"]) if c["short"]["score"] >= 60][:3],
    }

    # ---- paper portfolio + history
    paper = update_paper(prev_paper, coins, K4, now_ms, btc_ctx.get("regime"))
    # ---- backtest summary across all coins
    bts = [c["bt"] for c in coins if c.get("bt") and c["bt"].get("n")]
    allt = sum(b["n"] for b in bts)
    backtest = {"coins": len(bts), "trades": allt,
                "win": round(sum(b["win"] * b["n"] for b in bts) / allt) if allt else None,
                "avg": round(sum(b["avg"] * b["n"] for b in bts) / allt, 2) if allt else None,
                "avg_r": round(sum(b["avg_r"] * b["n"] for b in bts) / allt, 2) if allt else None,
                "days": max([b["days"] for b in bts] or [0]),
                "beat_hold": sum(1 for b in bts if b["acct"] > b["hold"] * 0.33),
                "note": "Short-term rules replayed on past 4h candles without news. Past results do not guarantee future results."}
    hist = load_prev("history.json") or {"points": []}
    hist["points"].append({"t": now_ms, "fng": fng_now, "mcap": tot, "score": market["score"], "btc": btc_ctx.get("price"),
                           "breadth": market["breadth"]["st"]})
    hist["points"] = hist["points"][-1100:]

    meta = {"generated": now.isoformat(timespec="seconds"), "generated_ms": now_ms, "version": VERSION,
            "demo": bool(a.demo), "next_update_ms": now_ms + 4 * 3600 * 1000, "coins": len(coins),
            "sources_ok": LOG["ok"], "sources_failed": LOG["fail"], "runtime_s": round(time.time() - t0, 1),
            "config": CFG}
    snapshot = {"meta": meta, "fx": fx, "market": market, "picks": picks, "backtest": backtest,
                "coins": sorted(coins, key=lambda c: c["rank"] or 999)}
    out = {"snapshot.json": snapshot, "charts.json": {"meta": {"generated_ms": now_ms}, "series": charts},
           "news.json": {"meta": {"generated_ms": now_ms}, "items": news},
           "paper.json": {"meta": {"generated_ms": now_ms}, "state": paper},
           "history.json": hist, "run_log.json": meta}
    for name, obj in out.items():
        with open(os.path.join(a.out, name), "w") as f:
            json.dump(obj, f, separators=(",", ":"), default=lambda o: None)
    sizes = {n: os.path.getsize(os.path.join(a.out, n)) for n in out}
    print(json.dumps({"coins": len(coins), "news": len(news), "ok": len(LOG["ok"]), "failed": LOG["fail"],
                      "sizes_kb": {k: round(v / 1024, 1) for k, v in sizes.items()}, "market_score": market["score"],
                      "regime": btc_ctx.get("regime"), "picks": picks, "paper_events": paper["last_events"]}, indent=1))
    big = [n for n, v in sizes.items() if v > 105 * 1024]  # stored size is ~2x compact JSON; db limit 256 KiB
    if big:
        print("WARNING: documents over 240KB:", big, file=sys.stderr)


if __name__ == "__main__":
    main()
