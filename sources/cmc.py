"""CoinMarketCap — supply/float, market cap, top markets. Falls back to CoinGecko when no key."""
import os
import requests
from sources.crypto_extras import supply as _gecko_supply

BASE = "https://pro-api.coinmarketcap.com"


def _get(path, **params):
    key = os.getenv("CMC_API_KEY")
    if not key:
        return None
    r = requests.get(f"{BASE}{path}", params=params, headers={"X-CMC_PRO_API_KEY": key}, timeout=15)
    r.raise_for_status()
    return r.json().get("data")


def asset_info(symbol: str) -> dict:
    sym = symbol.upper().lstrip("k")
    out = {"float_shares": None, "float_pct": None, "market_cap_usd": None, "cmc_vol_24h_usd": None, "top_markets": None}
    try:
        d = _get("/v2/cryptocurrency/quotes/latest", symbol=sym)
        if d and d.get(sym):
            c = d[sym][0]
            circ, tot = c.get("circulating_supply"), c.get("total_supply") or c.get("max_supply")
            q = c["quote"]["USD"]
            out.update(float_shares=circ, float_pct=round(circ / tot * 100, 1) if circ and tot else None,
                       market_cap_usd=q.get("market_cap"), cmc_vol_24h_usd=q.get("volume_24h"))
            try:  # market pairs is on higher CMC plans — best effort
                mp = _get("/v2/cryptocurrency/market-pairs/latest", symbol=sym, limit=5, sort="volume_24h_strict")
                pairs = (mp.get(sym) or [{}])[0].get("market_pairs", []) if isinstance(mp, dict) else []
                out["top_markets"] = ", ".join(f"{p['exchange']['name']} {p['market_pair']}" for p in pairs[:3]) or None
            except Exception:
                pass
            return out
    except Exception:
        pass
    out.update(_gecko_supply(symbol))
    return out
