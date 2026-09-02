"""Hyperliquid public info API — no key required."""
import time
import requests
import pandas as pd

API = "https://api.hyperliquid.xyz/info"
_TF_MS = {"5m": 5 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000, "1d": 86_400_000}


def _post(payload: dict):
    r = requests.post(API, json=payload, timeout=15)
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
