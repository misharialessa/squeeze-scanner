"""Hyperliquid public info API — no key required."""
import time
import requests
import pandas as pd

API = "https://api.hyperliquid.xyz/info"
_TF_MS = {"5m": 5 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000, "1d": 86_400_000}


def _post(payload: dict, retries: int = 2):
    for i in range(retries + 1):
        r = requests.post(API, json=payload, timeout=15)
        if r.status_code == 429 and i < retries:
            time.sleep(1.5 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()


def universe_snapshot() -> pd.DataFrame:
    """All perps with mark, 24h change, funding (hourly), OI, 24h notional volume."""
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    rows = []
    for asset, ctx in zip(meta["universe"], ctxs):
        mark, prev = float(ctx["markPx"]), float(ctx.get("prevDayPx") or 0)
        rows.append({
            "ticker": asset["name"],
            "price": mark,
            "change_pct": round((mark / prev - 1) * 100, 2) if prev else None,
            "funding_1h_pct": float(ctx["funding"]) * 100,          # hourly, %
            "open_interest": float(ctx["openInterest"]) * mark,      # $ notional
            "vol_24h_usd": float(ctx["dayNtlVlm"]),
        })
    df = pd.DataFrame(rows)
    df["funding_8h_pct"] = (df["funding_1h_pct"] * 8).round(4)      # comparable to Binance convention
    return df


def candles(coin: str, interval: str, bars: int = 300) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - bars * _TF_MS[interval]
    raw = _post({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end}})
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def daily_volumes(coin: str, days: int = 6) -> pd.Series:
    """Prior-day base-asset volumes (excludes the current, partial day)."""
    d = candles(coin, "1d", days + 1)
    return d["volume"].iloc[:-1] if len(d) > 1 else pd.Series(dtype=float)


def all_mids() -> dict:
    """{coin: mid price} — cheapest call; used for the rolling price ledger."""
    return {k: float(v) for k, v in _post({"type": "allMids"}).items() if not k.startswith("@")}


def microstructure(coin: str) -> dict:
    """Book + tape at this instant: spread, depth within 1%, imbalance, taker buy ratio. Cheap calls (weight 2 / 20)."""
    out = {"spread_bps": None, "depth_1pct_usd": None, "book_imbalance": None, "taker_buy_ratio": None, "trades_5m_usd": None}
    try:
        book = _post({"type": "l2Book", "coin": coin})
        bids, asks = book["levels"][0], book["levels"][1]
        bb, ba = float(bids[0]["px"]), float(asks[0]["px"])
        mid = (bb + ba) / 2
        out["spread_bps"] = round((ba - bb) / mid * 1e4, 1)
        bid_d = sum(float(l["px"]) * float(l["sz"]) for l in bids if float(l["px"]) >= mid * 0.99)
        ask_d = sum(float(l["px"]) * float(l["sz"]) for l in asks if float(l["px"]) <= mid * 1.01)
        out["depth_1pct_usd"] = round(bid_d + ask_d)
        out["book_imbalance"] = round((bid_d - ask_d) / (bid_d + ask_d), 3) if bid_d + ask_d else None
    except Exception:
        pass
    try:
        tr = _post({"type": "recentTrades", "coin": coin}) or []
        cutoff = time.time() * 1000 - 5 * 60_000
        tr = [t for t in tr if float(t.get("time", 0)) >= cutoff] or tr[-50:]
        buy = sum(float(t["px"]) * float(t["sz"]) for t in tr if t.get("side") == "B")
        tot = sum(float(t["px"]) * float(t["sz"]) for t in tr)
        out["taker_buy_ratio"] = round(buy / tot, 3) if tot else None
        out["trades_5m_usd"] = round(tot)
    except Exception:
        pass
    return out
