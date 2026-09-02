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
