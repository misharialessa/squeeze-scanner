"""Pure pandas indicator helpers. Bars must have columns: open, high, low, close, volume (DatetimeIndex)."""
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def vwap(bars: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP: resets each calendar day (UTC for crypto, exchange day for stocks)."""
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3
    day = bars.index.date
    pv = (tp * bars["volume"]).groupby(day).cumsum()
    vv = bars["volume"].groupby(day).cumsum().replace(0, float("nan"))
    return pv / vv


def extension_pct(bars: pd.DataFrame, ema_len: int = 9) -> dict:
    """% distance of last close above 9-EMA and VWAP."""
    if bars is None or len(bars) < ema_len + 1:
        return {"ema_pct": None, "vwap_pct": None}
    last = float(bars["close"].iloc[-1])
    e = float(ema(bars["close"], ema_len).iloc[-1])
    v = float(vwap(bars).iloc[-1])
    return {
        "ema_pct": round((last / e - 1) * 100, 2) if e else None,
        "vwap_pct": round((last / v - 1) * 100, 2) if v == v and v else None,
    }


def resample(bars_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return bars_5m.resample(rule).agg(agg).dropna()


def rvol(today_vol: float, daily_vols: pd.Series) -> float | None:
    """today's volume ÷ mean of prior N daily volumes."""
    if daily_vols is None or len(daily_vols) == 0:
        return None
    avg = float(daily_vols.mean())
    return round(today_vol / avg, 2) if avg else None
