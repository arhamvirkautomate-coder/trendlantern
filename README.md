# TrendLantern: public website

A free, self-updating crypto research website. Every 4 hours GitHub runs the analysis engine, rebuilds the data and publishes the site. There's no server to rent and nothing to keep running.

```
engine/engine.py      analysis engine (prices, candles, news, scores, backtests)
engine/publicize.py   turns engine output into the research-only public data (no buy/sell advice)
engine/brief.py       optional AI market brief via the Anthropic API
site/                 the website (index.html, legal.html, favicon, data/)
.github/workflows/refresh.yml   the 4-hourly automation
```

## One-time setup (about 20 minutes)

1. **Create a GitHub account** at github.com (free).
2. **Create a new repository** called `trendlantern`. Set it to **Public** (GitHub Pages is free for public repos) and don't add a README.
3. **Upload the files.** In the empty repo, click *uploading an existing file* and drag in everything from this folder, then click *Commit changes*.
   - The `.github` folder is hidden on Mac. Press **Cmd + Shift + .** in Finder to show it before dragging.
   - If it still doesn't upload, choose *Add file → Create new file*, type the name `.github/workflows/refresh.yml`, paste the contents of that file, and commit.
4. **Turn on Pages:** *Settings → Pages → Build and deployment → Source: **GitHub Actions***.
5. **Allow the automation to save data:** *Settings → Actions → General → Workflow permissions: **Read and write permissions** → Save*.
6. **Start the first run.** The upload in step 3 already started one automatic run. It fails because Pages wasn't switched on yet, which is expected.
   Now: open the *Actions* tab. If asked, click *I understand… enable workflows*. Then choose *Refresh data and deploy → Run workflow*. After about 5 minutes the site is live at `https://YOUR-USERNAME.github.io/trendlantern/`.

From then on it refreshes itself every 4 hours.

## The AI market brief (optional, about $2–4 a month)

Without this step the site works fully, just without the written brief.

1. Create an API key at **console.anthropic.com**, add a few dollars of credit, and **set a monthly spend limit** (e.g. $5).
2. In GitHub: *Settings → Secrets and variables → Actions → New repository secret*. Name: `ANTHROPIC_API_KEY`, value: your key.
3. Optional, on the *Variables* tab:
   - `BRIEF_EVERY_HOURS`: default `12`, so two briefs a day. Use `4` to write one every run, at about three times the cost.
   - `BRIEF_MODEL`: default `claude-sonnet-5-5`. Use `claude-haiku-4-5-20251001` to halve the cost.

The brief is instructed never to recommend buying or selling. `brief.py` also rejects any reply that uses advice wording and keeps the previous brief instead.

## Your own domain

1. Buy a domain from a registrar such as Cloudflare Registrar (sells at cost), Porkbun or Namecheap. For reference, on 1 Oct 2026 `cryptopulse.com`, `.app`, `.net`, `.xyz` and `.live` were already registered.
2. In GitHub: *Settings → Pages → Custom domain*. Enter `www.yourdomain.com` and save.
3. At your registrar, add these DNS records:
   - `CNAME` record: `www` → `YOUR-USERNAME.github.io`
   - for the bare domain, `A` records `@` → `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
4. Back in *Settings → Pages*, tick **Enforce HTTPS** once it becomes available (can take up to a day).
5. Recommended: verify the domain under your GitHub account's *Settings → Pages → Verified domains*, so nobody else can point it at their site.

## Before you announce it: checklist

- [ ] Fill in `site/legal.html`: your name (or business name), contact email, the governing-law state/country and the "last updated" date. Search for `[`.
- [ ] Put the same email in `site/index.html`: search for `contactEmail: ""`.
- [ ] **Get the disclaimer and the idea itself checked by a lawyer** where you live and where your audience is. In Australia, general advice about financial products needs an AFSL. Many crypto tokens are not financial products, but some are. Pakistan's Virtual Assets Act 2026 lists "advisory services" as a licensed activity. The site is built to publish research, not recommendations, but a lawyer should confirm this for your situation.
- [ ] Open the site on your phone and a laptop, in light and dark mode.
- [ ] Don't promote the site with performance claims ("our signals made 50%"). That turns research into promotion.

## Everyday running

- **Watch it:** the *Actions* tab shows every run. A red run means a data source failed. When the engine can't run, the site keeps the previous data and shows its age.
- **Scheduled runs pausing:** GitHub pauses scheduled runs in repos with no activity for 60 days. If that happens you'll get an email. Click *Enable workflow* in the Actions tab.
- **Changing the engine:** edit `engine/engine.py`, and test it offline with `python3 engine/engine.py --demo --out /tmp/x`. Commit the change and the next run uses it.
- **Cost:** GitHub Pages and Actions are free for public repos. The domain is about $10–15 a year. The AI brief is optional at about $2–4 a month.

## How it differs from the private dashboard

The private claude.ai dashboard keeps the Buy/Sell signals, the position sizes, the paper bot and the AI analyst. The public site shows only:

- trend labels: *Strong uptrend, Uptrend, Neutral, Weakening, Downtrend*
- neutral technical levels
- news
- backtests
- a watchlist that stays in the visitor's own browser

`publicize.py` refuses to publish if any advice wording slips into the public data.
