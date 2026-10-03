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


def macd(close: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def macd_state(bars: pd.DataFrame, cfg: dict) -> dict:
    if bars is None or len(bars) < cfg["slow"] + cfg["signal"] + 2:
        return {"macd": None, "macd_positive": None, "macd_cross_up_ago": None}
    m = macd(bars["close"], cfg["fast"], cfg["slow"], cfg["signal"])
    above = (m["macd"] > m["signal"]).astype(int)
    crosses = above.diff()
    recent = crosses.iloc[-cfg["cross_lookback"]:]
    up_idx = [i for i, v in enumerate(reversed(recent.tolist())) if v == 1]
    return {
        "macd": round(float(m["macd"].iloc[-1]), 6),
        "macd_positive": bool(m["macd"].iloc[-1] > 0),
        "macd_cross_up_ago": up_idx[0] if up_idx else None,   # 0 = this candle
    }


def impulse(bars_5m: pd.DataFrame, min_move_pct: float, candles: int, vol_multiple: float, lookback_days: int,
            scan_back: int = 1) -> dict | None:
    """Sudden move: ≥min_move% over 1..candles consecutive bars AND spike-candle volume ≥ multiple × avg per-candle volume.
    scan_back: how many bar-endings to test (1 = only the latest bars; 12 = any window ending in the last hour, so a move
    that happened while the collector was restarting is still caught, stamped with its real impulse_time and late_min)."""
    n = int(lookback_days * 24 * 12)
    if bars_5m is None or len(bars_5m) < 30:
        return None
    hist = bars_5m.iloc[-(n + candles):-candles] if len(bars_5m) > n + candles else bars_5m.iloc[:-candles]
    avg_vol = float(hist["volume"].mean()) if len(hist) else 0
    if not avg_vol:
        return None
    best = None
    for back in range(0, min(scan_back, len(bars_5m) - candles)):
        last = bars_5m.iloc[len(bars_5m) - candles - back: len(bars_5m) - back]
        for k in range(1, candles + 1):
            seg = last.iloc[-k:]
            move = (float(seg["close"].iloc[-1]) / float(seg["open"].iloc[0]) - 1) * 100
            vmult = float(seg["volume"].max()) / avg_vol
            if move >= min_move_pct and vmult >= vol_multiple and (best is None or move > best["impulse_pct"]):
                best = {"impulse_pct": round(move, 2), "impulse_candles": k, "impulse_vol_x": round(vmult, 1),
                        "impulse_time": seg.index[0], "late_min": int(back * 5)}
        if best is not None and back == 0:
            break                                     # live fire wins; only look back when the latest bars are quiet
    return best


def context(bars_5m: pd.DataFrame, impulse_candles: int = 2) -> dict:
    """Entry-time context, all computed from bars available at the moment of the fire (no lookahead)."""
    out = {"range_pos_24h": None, "pct_from_24h_high": None, "new_24h_high": None, "compression": None,
           "vol_rank_24h": None, "chg_24h_at_fire_pct": None}
    if bars_5m is None or len(bars_5m) < 300:
        return out
    day = bars_5m.iloc[-288:]
    spike = bars_5m.iloc[-impulse_candles:]
    pre = bars_5m.iloc[:-impulse_candles]
    last = float(spike["close"].iloc[-1])
    hi, lo = float(day["high"].max()), float(day["low"].min())
    prior_hi = float(pre.iloc[-288:]["high"].max())
    out["range_pos_24h"] = round((last - lo) / (hi - lo), 2) if hi > lo else None
    out["pct_from_24h_high"] = round((last / hi - 1) * 100, 2)
    out["new_24h_high"] = bool(last >= prior_hi)
    rng = (bars_5m["high"] - bars_5m["low"]) / bars_5m["close"]
    pre_hour = float(rng.iloc[-impulse_candles - 12:-impulse_candles].mean())
    norm = float(rng.iloc[-864:-impulse_candles].mean())
    out["compression"] = round(pre_hour / norm, 2) if norm else None          # <0.6 = coiled, >1.3 = already noisy
    out["vol_rank_24h"] = int((day["volume"] > float(spike["volume"].max())).sum() + 1)
    out["chg_24h_at_fire_pct"] = round((last / float(day["open"].iloc[0]) - 1) * 100, 2)
    return out


def history_context(bars_5m: pd.DataFrame, impulse_candles: int = 2) -> dict:
    """The coin's own recent path before the fire: prior returns, realised vol, 3-day range position."""
    out = {"prior_1h_ret_pct": None, "prior_4h_ret_pct": None, "prior_24h_ret_pct": None, "rv_24h_pct": None, "range_pos_3d": None}
    if bars_5m is None or len(bars_5m) < 300:
        return out
    pre = bars_5m.iloc[:-impulse_candles]
    c = pre["close"]; last = float(c.iloc[-1])
    for name, n in (("prior_1h_ret_pct", 12), ("prior_4h_ret_pct", 48), ("prior_24h_ret_pct", 288)):
        if len(c) > n: out[name] = round((last / float(c.iloc[-n - 1]) - 1) * 100, 2)
    r = c.iloc[-288:].pct_change().dropna()
    out["rv_24h_pct"] = round(float(r.std()) * (288 ** 0.5) * 100, 2) if len(r) > 20 else None
    hi, lo = float(pre["high"].max()), float(pre["low"].min())
    out["range_pos_3d"] = round((last - lo) / (hi - lo), 2) if hi > lo else None
    return out
