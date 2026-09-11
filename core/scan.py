import time
import pandas as pd
from core.indicators import resample, rvol, impulse
from core.metrics import METRICS, checklist
from sources import hyperliquid as hl, stocks, crypto_extras, cmc


def _run_metrics(hit, asset, ctx):
    for name, scope, fn in METRICS:
        if scope in ("both", asset):
            try:
                hit.update(fn(hit, ctx))
            except Exception as e:
                hit[f"{name}_error"] = str(e)[:80]
    return hit


def _impulse(b5, cfg, asset="crypto"):
    i = cfg["stock_impulse"] if asset == "stock" else cfg["impulse"]
    return impulse(b5, i["min_move_pct"], i["candles"], i["vol_multiple"], i["avg_lookback_days"])


def regime(snap: pd.DataFrame) -> dict:
    """Market context at fire time — tracked on every signal so we can condition on it later."""
    out = {"breadth_pct": None, "btc_24h_pct": None, "btc_4h_pct": None, "btc_px": None}
    try:
        out["breadth_pct"] = round((snap["change_pct"] > 0).mean() * 100, 1)
        out["btc_24h_pct"] = float(snap.loc[snap["ticker"] == "BTC", "change_pct"].iloc[0])
        out["btc_px"] = float(snap.loc[snap["ticker"] == "BTC", "price"].iloc[0])
        b = hl.candles("BTC", "1h", 6)
        out["btc_4h_pct"] = round((float(b["close"].iloc[-1]) / float(b["open"].iloc[-5]) - 1) * 100, 2) if len(b) >= 5 else None
    except Exception:
        pass
    return out


# ---------------- crypto ----------------

def _oi_change(ledger, coin, oi_now, now):
    """OI % change vs ~1h ago from the ledger's OI trail."""
    trail = ledger.setdefault(("oi", coin), [])
    trail.append((now, oi_now))
    ledger[("oi", coin)] = [(t, v) for t, v in trail if now - t <= 3900]
    old = [(t, v) for t, v in ledger[("oi", coin)] if now - t >= 3000]
    return round((oi_now / old[0][1] - 1) * 100, 2) if old and old[0][1] else None


def _btc_move(ledger, horizon):
    hist = ledger.get("BTC") or []
    if len(hist) < 2: return None
    return round((hist[-1][1] / hist[0][1] - 1) * 100, 2)


def crypto_candidates(cfg, ledger: dict, snap: pd.DataFrame) -> list[str]:
    """Assets whose mid moved ≥min_move% in the last ~10 min per the price ledger, plus optional daily movers.
    ledger: {coin: [(ts, price), ...]} maintained by the app across refreshes."""
    now = time.time()
    mids = hl.all_mids()
    cands = set()
    horizon = cfg["impulse"]["candles"] * 300 + 60
    for coin, px in mids.items():
        hist = ledger.setdefault(coin, [])
        hist.append((now, px))
        ledger[coin] = [(t, p) for t, p in hist if now - t <= horizon]
        old = min((p for t, p in ledger[coin]), default=px)
        if old and (px / old - 1) * 100 >= cfg["impulse"]["min_move_pct"] * 0.8:   # slight slack; candles confirm
            cands.add(coin)
    for _, r in snap.iterrows():
        _oi_change(ledger, r["ticker"], r["open_interest"], now)
    if len(ledger.get(next(iter(mids)), [])) <= 1:                                     # cold start → seed
        cands |= set(snap.nlargest(cfg["impulse"]["cold_start_assets"], "vol_24h_usd")["ticker"])
    if cfg["daily_movers"]["enabled"]:
        cands |= set(snap[snap["change_pct"].abs() >= cfg["daily_movers"]["move_pct"]]["ticker"])
    return sorted(cands)


def _near_miss(b5, cfg):
    """Best move over the last 1..N candles and the spike volume multiple, regardless of thresholds."""
    i = cfg["impulse"]
    n = int(i["avg_lookback_days"] * 288)
    if b5 is None or len(b5) < 30:
        return None
    hist = b5.iloc[-(n + i["candles"]):-i["candles"]]
    avg = float(hist["volume"].mean()) or 1
    seg = b5.iloc[-i["candles"]:]
    return {"move_pct": round((float(seg["close"].iloc[-1]) / float(seg["open"].iloc[0]) - 1) * 100, 2),
            "vol_x": round(float(seg["volume"].max()) / avg, 1)}


def scan_crypto(cfg, ledger: dict, diag: dict) -> list[dict]:
    snap = hl.universe_snapshot().set_index("ticker")
    reg = regime(snap.reset_index())
    cands = crypto_candidates(cfg, ledger, snap.reset_index())
    diag.update(crypto_candidates=len(cands), candles_ok=0, candles_err=0, last_err=None, near_misses=[])
    hits = []
    for coin in cands:
        if coin not in snap.index:
            continue
        try:
            b5 = hl.candles(coin, "5m", int(cfg["impulse"]["avg_lookback_days"] * 288) + 10)
            diag["candles_ok"] += 1
            time.sleep(0.25)                                  # stay under HL rate limit
        except Exception as e:
            diag["candles_err"] += 1; diag["last_err"] = str(e)[:160]
            continue
        nm = _near_miss(b5, cfg)
        if nm:
            diag["near_misses"].append({"ticker": coin, **nm})
        imp = _impulse(b5, cfg)
        daily_hit = cfg["daily_movers"]["enabled"] and abs(snap.loc[coin, "change_pct"] or 0) >= cfg["daily_movers"]["move_pct"]
        if not imp and not daily_hit:
            continue
        hit = snap.loc[coin].to_dict(); hit["ticker"] = coin; hit["asset"] = "crypto"; hit["trigger"] = "impulse" if imp else "daily"
        hit.update(imp or {}); hit.update(reg)
        trail = ledger.get(("oi", coin)) or []
        old = [(t, v) for t, v in trail if time.time() - t >= 3000]
        hit["oi_chg_1h_pct"] = round((trail[-1][1] / old[0][1] - 1) * 100, 2) if trail and old and old[0][1] else None
        hit["btc_move_pct"] = _btc_move(ledger, cfg["impulse"]["candles"] * 300)
        ctx = {"config": cfg, "bars_5m": b5, "bars_30m": resample(b5, "30min"), "bars_1h": resample(b5, "1h")}
        today = b5[b5.index.date == b5.index[-1].date()]["volume"].sum()
        hit["volume"] = round(float(today), 2)
        try:
            hit["rvol"] = rvol(today, hl.daily_volumes(coin, cfg["rvol"]["lookback_days"]))
        except Exception:
            hit["rvol"] = None
        hit.update(cmc.asset_info(coin))
        n = crypto_extras.news(coin)
        hit["news"], hit["news_link"] = (n[0][0], n[0][1]) if n else (None, None)
        hits.append(_run_metrics(hit, "crypto", ctx))
    return hits


# ---------------- stocks ----------------

def scan_stocks(cfg) -> list[dict]:
    s = cfg["stocks"]
    cands = stocks.movers(s["finviz_cap"], s["min_day_change_pct"], s["max_candidates"])
    hits = []
    scan_stocks.diag = {"stock_candidates": len(cands)}
    for _, row in cands.iterrows():
        hit = row.to_dict(); hit["asset"] = "stock"
        try:
            d = stocks.detail(hit["ticker"])
        except Exception:
            continue
        b5 = d["bars_5m"]
        imp = _impulse(b5, cfg, "stock") if len(b5) else None
        daily_hit = cfg["daily_movers"]["enabled"] and abs(hit.get("change_pct") or 0) >= cfg["daily_movers"]["move_pct"]
        if not imp and not daily_hit:
            continue
        if (d.get("market_cap") or 0) and d["market_cap"] < s["min_market_cap_usd"]:
            continue
        hit["market_cap_usd"] = d.get("market_cap")
        hit["trigger"] = "impulse" if imp else "daily"; hit.update(imp or {})
        ctx = {"config": cfg, "bars_5m": b5, "bars_30m": resample(b5, "30min"), "bars_1h": resample(b5, "1h")}
        hit["rvol"] = rvol(hit.get("volume") or 0, d["prior_daily_vols"])
        hit.update({k: d[k] for k in ("float_shares", "float_pct", "short_pct_float")})
        hit["news"], hit["news_link"] = (d["news"][0][0], d["news"][0][1]) if d["news"] else (None, None)
        hits.append(_run_metrics(hit, "stock", ctx))
    return hits


def run(cfg, ledger: dict, diag: dict) -> pd.DataFrame:
    rows = []
    for enabled, fn, args in ((cfg["crypto"]["enabled"], scan_crypto, (cfg, ledger, diag)), (cfg["stocks"]["enabled"], scan_stocks, (cfg,))):
        if enabled:
            try:
                rows += fn(*args)
            except Exception as e:
                diag[f"{fn.__name__}_error"] = str(e)[:200]
                rows.append({"asset": fn.__name__.split("_")[1], "ticker": "SOURCE ERROR", "change_pct": 0, "error": str(e)[:120]})
    df = pd.DataFrame(rows)
    if not df.empty:
        flags = []
        for r in df.to_dict("records"):
            try:
                flags.append(checklist(r, cfg))
            except Exception as e:
                flags.append([f"checklist error: {e}"])
        df["flags"] = flags
    return df
