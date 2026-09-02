"""Optional enrichments for crypto hits. Each function returns None-filled dict on failure so the scan never dies."""
import os
import requests
from functools import lru_cache

_CG = "https://api.coingecko.com/api/v3"


@lru_cache(maxsize=1)
def _coingecko_index() -> dict:
    """symbol(upper) -> coingecko id. Cached for the process lifetime."""
    try:
        coins = requests.get(f"{_CG}/coins/list", timeout=20).json()
        idx = {}
        for c in coins:
            idx.setdefault(c["symbol"].upper(), c["id"])   # first match wins; good enough for majors
        return idx
    except Exception:
        return {}


def supply(symbol: str) -> dict:
    """Circulating vs total supply = crypto 'float'."""
    cid = _coingecko_index().get(symbol.upper().lstrip("k"))
    if not cid:
        return {"float_shares": None, "float_pct": None}
    try:
        d = requests.get(f"{_CG}/coins/{cid}",
                         params={"localization": "false", "tickers": "false", "community_data": "false",
                                 "developer_data": "false"}, timeout=15).json()["market_data"]
        circ, tot = d.get("circulating_supply"), d.get("total_supply") or d.get("max_supply")
        return {"float_shares": circ, "float_pct": round(circ / tot * 100, 1) if circ and tot else None}
    except Exception:
        return {"float_shares": None, "float_pct": None}


def liquidation_clusters(symbol: str, price: float, exchange: str = "Binance") -> dict:
    """Nearest heavy liquidation cluster above/below price via CoinGlass v4 (paid key)."""
    key = os.getenv("COINGLASS_API_KEY")
    empty = {"liq_above_pct": None, "liq_below_pct": None, "liq_bias": None}
    if not key:
        return empty
    try:
        r = requests.get("https://open-api-v4.coinglass.com/api/futures/liquidation/map",
                         params={"symbol": f"{symbol}USDT", "exchange": exchange, "range": "1d"},
                         headers={"CG-API-KEY": key}, timeout=15).json()
        levels = r.get("data", [])   # expected: list of [price, long_liq_usd, short_liq_usd, ...]
        pts = [(float(x[0]), float(x[1]) + float(x[2])) for x in levels if len(x) >= 3]
        if not pts:
            return empty
        cut = sorted(v for _, v in pts)[int(len(pts) * 0.9)]           # top-10% clusters = "heavy"
        above = [p for p, v in pts if p > price and v >= cut]
        below = [p for p, v in pts if p < price and v >= cut]
        a = round((min(above) / price - 1) * 100, 2) if above else None
        b = round((1 - max(below) / price) * 100, 2) if below else None
        bias = None
        if a is not None or b is not None:
            bias = "long (clusters above nearer)" if (b is None or (a is not None and a < b)) else "short (clusters below nearer)"
        return {"liq_above_pct": a, "liq_below_pct": b, "liq_bias": bias}
    except Exception:
        return empty


def news(symbol: str) -> list[tuple[str, str]]:
    key = os.getenv("CRYPTOPANIC_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://cryptopanic.com/api/v1/posts/",
                         params={"auth_token": key, "currencies": symbol, "kind": "news"}, timeout=15).json()
        return [(p["title"], p["url"]) for p in r.get("results", [])[:3]]
    except Exception:
        return []
