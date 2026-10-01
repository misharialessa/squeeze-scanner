# Squeeze Scanner — setup (≈10 min)

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and add keys (all optional — scanner works with zero keys; CoinGlass adds liquidation map, CryptoPanic adds crypto news).
3. `streamlit run scanner.py` → browser opens. Auto-refreshes every 60s (config.yaml).

## Data sources
| Need | Source | Key? |
|---|---|---|
| Crypto movers, funding, OI, candles | Hyperliquid public API | no |
| Crypto float (circulating/total) | CoinGecko | no |
| Liquidation clusters | CoinGlass v4 | paid |
| Crypto news | CryptoPanic | free |
| Stock candidates (<$20, float <50M, ±10%) | Finviz screener | no (15-min delay) |
| Stock bars, float, short %, news | yfinance | no |

## Adding an indicator
See the docstring at the top of `core/metrics.py` — one decorated function, done.

## Known limits
- Finviz free data is ~15 min delayed; for true real-time stocks swap `sources/stocks.py` to Polygon (paid).
- CoinGlass response shape is inferred from docs; if the liquidation columns stay empty, send me one raw JSON response and I'll fix the parser.
- "Crossing 10%" alerting is a refresh-by-refresh check; next version can add a Telegram push on first cross.

## Two-repo layout (public code, private journal)
- `squeeze-scanner` (public): the code + workflow. Unlimited Actions minutes.
- `squeeze-journal` (private): `journal` branch holds signals_journal.json, ledger.json, fit_report.json, model_weights.json.
Both the workflow (`JOURNAL_REPO` secret) and Streamlit (`GITHUB_REPO` secret) point at the private repo.

## Continuous scanning with GitHub Actions (recommended)
The Streamlit app only runs while a browser tab is open. `scan_job.py` + `.github/workflows/scan.yml`
run the same scan on GitHub's servers every 5 minutes and write to the `journal` branch.
1. Repo → Settings → Secrets and variables → Actions → New repository secret:
   `JOURNAL_TOKEN` (your fine-grained PAT), plus `FINNHUB_API_KEY`, `CMC_API_KEY`, `CRYPTOPANIC_API_KEY` if you use them.
2. Repo → Actions tab → enable workflows → "scan" → Run workflow (first run seeds the ledger).
3. Streamlit Cloud → Settings → Secrets → add `SCAN_MODE = "cron"` so the app displays instead of scanning.
Free minutes: unlimited on public repos; ~2,000/month on private (≈1.5 min × 288 runs/day exceeds it → use `*/15` cron or make the repo public).
The job also re-runs the model fit daily at 03:00 UTC and publishes `fit_report.json` / `model_weights.json` to the branch.
