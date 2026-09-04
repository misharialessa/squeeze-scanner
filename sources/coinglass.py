"""CoinGlass v4. Two calls per hit: where is OI heaviest → liquidation map on that exchange.
Endpoint paths/response shapes follow the public docs; if a field is missing the function returns Nones."""
import os
import requests

BASE = "https://open-api-v4.coinglass.com/api"


def _get(path, **params):
    key = os.getenv("COINGLASS_API_KEY")
    if not key:
        return None
    r = requests.get(f"{BASE}{path}", params=params, headers={"CG-API-KEY": key, "accept": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json().get("data")


def heaviest_exchange(symbol: str) -> dict:
    """Exchange with the most open interest for this coin (proxy for 'where it's most traded')."""
    try:
        data = _get("/futures/open-interest/exchange-list", symbol=symbol) or []
        rows = [d for d in data if str(d.get("exchange", "")).lower() not in ("all", "")]
        best = max(rows, key=lambda d: float(d.get("open_interest_usd") or d.get("openInterest") or 0), default=None)
        return {"top_exchange": best.get("exchange"), "top_exchange_oi_usd": float(best.get("open_interest_usd") or 0)} if best else {"top_exchange": None}
    except Exception:
        return {"top_exchange": None}


def liquidation_clusters(symbol: str, price: float, exchange: str | None) -> dict:
    """Nearest heavy cluster above/below, total $ at risk each side, and the single biggest cluster."""
    empty = {"liq_above_pct": None, "liq_below_pct": None, "liq_above_usd": None, "liq_below_usd": None,
             "liq_bias": None, "liq_exchange": exchange}
    if not exchange:
        return empty
    try:
        data = _get("/futures/liquidation/map", symbol=f"{symbol}USDT", exchange=exchange, range="1d") or []
        pts = []
        for x in data:                                  # tolerate list-of-lists or list-of-dicts
            if isinstance(x, dict):
                p = float(x.get("price") or 0); v = float(x.get("liq_usd") or x.get("long_liq_usd", 0) or 0) + float(x.get("short_liq_usd", 0) or 0)
            else:
                p = float(x[0]); v = sum(float(y) for y in x[1:3])
            if p and v:
                pts.append((p, v))
        if not pts:
            return empty
        cut = sorted(v for _, v in pts)[int(len(pts) * 0.9)]
        above = [(p, v) for p, v in pts if p > price]
        below = [(p, v) for p, v in pts if p < price]
        heavy_a = [p for p, v in above if v >= cut]
        heavy_b = [p for p, v in below if v >= cut]
        a = round((min(heavy_a) / price - 1) * 100, 2) if heavy_a else None
        b = round((1 - max(heavy_b) / price) * 100, 2) if heavy_b else None
        bias = None
        if a is not None or b is not None:
            bias = "clusters nearer ABOVE → long magnet" if (b is None or (a is not None and a < b)) else "clusters nearer BELOW → short magnet"
        return {"liq_above_pct": a, "liq_below_pct": b,
                "liq_above_usd": round(sum(v for _, v in above)), "liq_below_usd": round(sum(v for _, v in below)),
                "liq_bias": bias, "liq_exchange": exchange}
    except Exception:
        return empty
