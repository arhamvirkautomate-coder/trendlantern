#!/usr/bin/env python3
"""
Turns the private engine output into the PUBLIC, research-only data set.

The public site shows market data, trend scores, technical levels, news and
historical backtests. It does NOT publish buy/sell instructions, position
sizes, "what to do" advice or the live paper-trading account.

Usage: python3 publicize.py ENGINE_OUT_DIR SITE_DATA_DIR
"""
import json, os, re, sys

LABELS = {"Strong Buy": "Strong uptrend", "Buy": "Uptrend", "Hold": "Neutral",
          "Reduce": "Weakening", "Sell / Avoid": "Downtrend"}

STANCE = {"Risk-on": "Risk-on conditions: most trends are up",
          "Neutral": "Mixed conditions: trends are uneven",
          "Risk-off": "Risk-off conditions: most trends are down"}

REGIME_TEXT = {
    "bull": "Bitcoin is above its 50- and 200-day averages. Historically this has been a supportive backdrop for the wider market.",
    "neutral": "Bitcoin is mixed around its long-term averages. Trend signals across the market tend to be less reliable in this state.",
    "bear": "Bitcoin is below its 200-day average. Historically this has been a weak backdrop with higher downside risk.",
}

# reason texts that read like instructions -> neutral descriptions
REWRITES = [
    (r"Capped at Buy: waiting for momentum confirmation \(RSI 48–72 and MACD rising\)", "Momentum not yet confirmed (RSI 48–72 and MACD rising), so the score is capped below Strong"),
    (r": contrarian accumulation window", ": historically a contrarian signal"),
    (r": be patient", ": euphoric sentiment"),
    (r": no rush to buy", ": late-stage sentiment"),
    (r": running hot, better entries likely on a dip", ": running hot"),
    (r"\(chasing risk\)", "(extended move)"),
    (r"\(falling knife\)", "(no sign of a bottom yet)"),
    (r"these rules made money on", "the rules were profitable on"),
    (r"these rules lost money on", "the rules were unprofitable on"),
]


def rw(t):
    for a, b in REWRITES:
        t = re.sub(a, b, t)
    return t


def pub_coin(c):
    sh, lg = c["short"], c["long"]
    sp, lp = sh["plan"], lg["plan"]
    return {
        "sym": c["sym"], "name": c.get("name"), "rank": c.get("rank"), "meme": c.get("meme"),
        "price": c["price"], "ch1h": c.get("ch1h"), "ch24h": c.get("ch24h"), "ch7d": c.get("ch7d"), "ch30d": c.get("ch30d"),
        "mcap": c.get("mcap"), "vol24": c.get("vol24"), "ath_dist": c.get("ath_dist"),
        "news_n": c.get("news_n"), "news_sent": c.get("news_sent"), "risk": c["risk"], "ind": c["ind"], "bt": c.get("bt"),
        "short": {
            "score": sh["score"], "label": LABELS.get(sh["label"], sh["label"]),
            "reasons": [{"t": rw(r["t"]), "s": r["s"]} for r in sh["reasons"]],
            "levels": {  # technical reference levels, not instructions
                "pullback": sp["zone"], "vstop": sp["stop"], "vstop_pct": sp["stop_pct"],
                "r15": sp["tp1"], "r15_pct": sp["tp1_pct"], "r3": sp["tp2"], "r3_pct": sp["tp2_pct"],
                "resistance": sp["resistance"],
            },
        },
        "long": {
            "score": lg["score"], "label": LABELS.get(lg["label"], lg["label"]),
            "reasons": [{"t": rw(r["t"]), "s": r["s"]} for r in lg["reasons"]],
            "levels": {"band": lp["zone"], "trend_break": lp["invalidation"], "hi1y": lp["target1"]},
        },
    }


# ---------------------------------------------------------------- public report card ("receipts")
H4, DAY = 4 * 3600000, 86400000
CHECKS = (("d1", DAY), ("d3", 3 * DAY), ("d7", 7 * DAY))


def update_ledger(path, coins, charts, now_ms):
    """Logs every new 'Strong uptrend' / 'Downtrend' label as a call and grades it 1, 3 and 7 days later
    against the 4h closes. Nothing is ever deleted from the stats, wrong calls included."""
    try:
        L = json.load(open(path))
    except Exception:  # noqa
        L = {"started_ms": now_ms, "calls": [], "stats": {}}
    series = charts.get("series", {})
    btc = series.get("BTC", {})

    def px_at(sym, t):
        s = series.get(sym) or {}
        h, t0 = s.get("h4") or [], s.get("t4")
        if not h or t0 is None:
            return None
        # h4[i] is the close of the candle opened at t0 + i*4h, i.e. the price at t0 + (i+1)*4h
        i = round((t - H4 - t0) / H4)
        return h[i] if 0 <= i < len(h) else None

    # grade open calls
    for c in L["calls"]:
        if c.get("graded"):
            continue
        for key, dt in CHECKS:
            if key not in c and now_ms >= c["t"] + dt:
                p = px_at(c["sym"], c["t"] + dt)
                b = px_at("BTC", c["t"] + dt)
                if p is None:
                    if now_ms > c["t"] + dt + 6 * DAY:
                        c[key] = None  # data gap: recorded as missing, never silently dropped
                    continue
                c[key] = round((p / c["price"] - 1) * 100, 2)
                if b and c.get("btc"):
                    c[key + "_btc"] = round((b / c["btc"] - 1) * 100, 2)
        if all(k in c for k, _ in CHECKS):
            c["graded"] = True
            m = c.get("d7")
            c["right"] = None if m is None else (m > 0 if c["dir"] == "up" else m < 0)

    # log new calls
    recent = {(c["sym"], c["dir"]) for c in L["calls"] if now_ms - c["t"] < 7 * DAY}
    btc_px = next((c["price"] for c in coins if c["sym"] == "BTC"), None)
    for c in coins:
        lab = c["short"]["label"]
        d = "up" if lab == "Strong uptrend" else "down" if lab == "Downtrend" else None
        if not d or (c["sym"], d) in recent:
            continue
        L["calls"].append({"id": f"{c['sym']}-{now_ms}", "sym": c["sym"], "dir": d, "t": now_ms, "price": c["price"],
                           "score": c["short"]["score"], "risk": c["risk"], "meme": c.get("meme"), "btc": btc_px})

    # stats (computed over every graded call ever kept)
    g = [c for c in L["calls"] if c.get("graded") and c.get("right") is not None]
    def grp(cs):
        if not cs:
            return {"n": 0}
        right = sum(1 for c in cs if c["right"])
        rel = [c["d7"] - c["d7_btc"] for c in cs if c.get("d7_btc") is not None]
        return {"n": len(cs), "right": right, "pct": round(right / len(cs) * 100),
                "avg7": round(sum(c["d7"] for c in cs) / len(cs), 2),
                "vs_btc": round(sum(rel) / len(rel), 2) if rel else None}
    L["stats"] = {"all": grp(g), "up": grp([c for c in g if c["dir"] == "up"]), "down": grp([c for c in g if c["dir"] == "down"]),
                  "open": sum(1 for c in L["calls"] if not c.get("graded")), "total": len(L["calls"])}
    L["calls"] = L["calls"][-600:]
    L["updated_ms"] = now_ms
    json.dump(L, open(path, "w"), separators=(",", ":"))
    return L


def main(src, dst):
    os.makedirs(dst, exist_ok=True)
    snap = json.load(open(os.path.join(src, "snapshot.json")))
    m = snap["market"]
    stance_key = m["stance"].split(":")[0]
    reg = dict(m.get("regime") or {})
    reg["text"] = REGIME_TEXT.get(reg.get("regime"), reg.get("text", ""))
    coins = [pub_coin(c) for c in snap["coins"]]
    by = {c["sym"]: c for c in coins}
    elig = [c for c in coins if c["risk"] != "Extreme"]
    leaders = {
        "short": [c["sym"] for c in sorted(elig, key=lambda c: -c["short"]["score"]) if c["short"]["score"] >= 60][:6],
        "long": [c["sym"] for c in sorted(elig, key=lambda c: -c["long"]["score"]) if c["long"]["score"] >= 60][:6],
        "weak": [c["sym"] for c in sorted(coins, key=lambda c: c["short"]["score"] + c["long"]["score"])
                 if c["short"]["score"] < 40 and c["long"]["score"] < 45][:6],
        "meme": [c["sym"] for c in sorted([c for c in coins if c["meme"]], key=lambda c: -c["short"]["score"])][:5],
    }
    meta = {k: snap["meta"][k] for k in ("generated", "generated_ms", "version", "next_update_ms", "coins", "sources_ok", "sources_failed", "runtime_s")}
    meta["edition"] = "public-research"
    public = {"meta": meta, "fx": snap["fx"], "market": {**m, "stance": STANCE.get(stance_key, m["stance"]), "regime": reg},
              "leaders": leaders, "backtest": snap.get("backtest"), "coins": coins}
    out = {
        "snapshot.json": public,
        "charts.json": json.load(open(os.path.join(src, "charts.json"))),
        "news.json": json.load(open(os.path.join(src, "news.json"))),
        "history.json": json.load(open(os.path.join(src, "history.json"))),
    }
    for name, obj in out.items():
        with open(os.path.join(dst, name), "w") as f:
            json.dump(obj, f, separators=(",", ":"))
    led = update_ledger(os.path.join(dst, "ledger.json"), coins, out["charts.json"], snap["meta"]["generated_ms"])
    print(f"ledger: {led['stats']['total']} calls, {led['stats']['open']} open")
    # guard: nothing instruction-like may leak into the public files
    blob = json.dumps(public)
    for bad in ("sell_rules", "size_usd", "Strong Buy", "Sell / Avoid", "Take profit", "Stop-loss:"):
        assert bad not in blob, f"public data still contains '{bad}'"
    print(f"public data written: {len(coins)} coins -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
