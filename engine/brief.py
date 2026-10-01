#!/usr/bin/env python3
"""
Writes the AI market brief for the public site with the Anthropic API.

Needs the ANTHROPIC_API_KEY environment variable (a GitHub Actions secret).
Skips quietly when the key is missing or the current brief is still fresh,
so the site keeps working without it.

Env:
  ANTHROPIC_API_KEY   required to write a brief
  BRIEF_MODEL         default: claude-sonnet-5-5
  BRIEF_EVERY_HOURS   default: 12  (two briefs a day keeps the cost to ~$2-4/month)

Usage: python3 brief.py SITE_DATA_DIR
"""
import json, os, sys, time, urllib.request

MODEL = os.environ.get("BRIEF_MODEL", "claude-sonnet-5-5")
EVERY_H = float(os.environ.get("BRIEF_EVERY_HOURS", "12"))

INSTRUCTIONS = """You write the market brief for TrendLantern, a free public crypto RESEARCH website.
Audience: beginners worldwide. Tone: calm, plain English, short sentences.

HARD RULES (legal):
- General information only. Never tell the reader to buy, sell, hold, enter, exit, take profit or size a position.
- Never say "you should", "consider buying/selling", "good time to buy", "accumulate", "avoid this coin".
- Describe conditions, trends, risks and events instead (e.g. "LINK has the strongest short-term trend score",
  "a Fed hike would historically weigh on risk assets").
- Use only numbers that appear in the data below. Do not invent prices, dates or statistics.
- If the data is thin, say less.

Reply with ONLY a JSON object with these keys:
  headline: one sentence
  summary: two short paragraphs separated by a blank line (what the market is doing, and the main drivers)
  events: 3-6 objects {"title": short, "detail": one or two sentences} drawn from the headlines
  short_outlook: 2-3 sentences on conditions for the next few days (which trends are strongest/weakest, what could change them)
  long_outlook: 2-3 sentences on the longer-term trend picture
  risks: 3-5 short strings
  calendar: upcoming dated events mentioned in the headlines (strings), or []
  coin_notes: object mapping up to 8 coin symbols (leaders or coins in the news) to one or two neutral sentences
"""


def build_prompt(d):
    s = json.load(open(os.path.join(d, "snapshot.json")))
    n = json.load(open(os.path.join(d, "news.json")))["items"][:40]
    m = s["market"]
    lines = [f"DATA TIME: {s['meta']['generated']}",
             f"MARKET: score {m['score']}/100, {m['stance']}. BTC regime {m['regime'].get('regime')}. "
             f"Fear&Greed {m['fng'].get('v')} ({m['fng'].get('l')}). BTC dominance {m.get('btc_dom')}%. "
             f"Total cap 24h change {m.get('mcap_ch24')}%. Breadth: {m['breadth'].get('st')}% above 4h trend, "
             f"{m['breadth'].get('lt')}% above 200-day. Stablecoins 30d {round((m.get('stables') or {}).get('ch30', 0), 1)}%. "
             f"DeFi TVL 30d {round((m.get('tvl') or {}).get('ch30', 0), 1)}%.",
             f"LEADERS: short-term {s['leaders']['short']}, long-term {s['leaders']['long']}, weakest {s['leaders']['weak']}",
             "COINS (sym price 24h% 7d% | short-term trend | long-term trend | risk):"]
    for c in s["coins"]:
        lines.append(f"{c['sym']} {c['price']} {c['ch24h']} {c['ch7d']} | {c['short']['label']} {c['short']['score']} | "
                     f"{c['long']['label']} {c['long']['score']} | {c['risk']}")
    lines.append("HEADLINES (category, sentiment -1..1, source):")
    for x in n:
        lines.append(f"- [{x['cat']}, {x['sent']}] {x['title']} ({x['source']})")
    return s, "\n".join(lines)


def call(prompt):
    body = json.dumps({"model": MODEL, "max_tokens": 2000,
                       "messages": [{"role": "user", "content": INSTRUCTIONS + "\n\n" + prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1])


BANNED = ["you should", "consider buying", "consider selling", "good time to buy", "buy now", "sell now", "take profit", "accumulate"]


def main(d):
    path = os.path.join(d, "brief.json")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("brief: no ANTHROPIC_API_KEY, skipping"); return
    try:
        old = json.load(open(path))
        if time.time() * 1000 - old.get("generated_ms", 0) < EVERY_H * 3600e3:
            print("brief: still fresh, skipping"); return
    except Exception:  # noqa
        pass
    snap, prompt = build_prompt(d)
    try:
        b = call(prompt)
    except Exception as e:  # noqa
        print(f"brief: API call failed ({e}); keeping the previous brief"); return
    need = {"headline", "summary", "events", "short_outlook", "long_outlook", "risks"}
    if not need.issubset(b):
        print("brief: reply missing keys; keeping the previous brief"); return
    txt = json.dumps(b).lower()
    hits = [w for w in BANNED if w in txt]
    if hits:
        print(f"brief: reply used advice wording {hits}; keeping the previous brief"); return
    b["generated_ms"] = snap["meta"]["generated_ms"]
    b["model"] = MODEL
    json.dump(b, open(path, "w"), separators=(",", ":"))
    print("brief: written")


if __name__ == "__main__":
    main(sys.argv[1])
