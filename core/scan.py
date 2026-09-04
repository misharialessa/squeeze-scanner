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


def _impulse(b5, cfg):
    i = cfg["impulse"]
    return impulse(b5, i["min_move_pct"], i["candles"], i["vol_multiple"], i["avg_lookback_days"])


# ---------------- crypto ----------------

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
    if len(ledger.get(next(iter(mids)), [])) <= 1:                                     # cold start → seed
        cands |= set(snap.nlargest(cfg["impulse"]["cold_start_assets"], "vol_24h_usd")["ticker"])
    if cfg["daily_movers"]["enabled"]:
        cands |= set(snap[snap["change_pct"].abs() >= cfg["daily_movers"]["move_pct"]]["ticker"])
    return sorted(cands)


def scan_crypto(cfg, ledger: dict) -> list[dict]:
    snap = hl.universe_snapshot().set_index("ticker")
    hits = []
    for coin in crypto_candidates(cfg, ledger, snap.reset_index()):
        if coin not in snap.index:
            continue
        try:
            b5 = hl.candles(coin, "5m", int(cfg["impulse"]["avg_lookback_days"] * 288) + 10)
        except Exception:
            continue
        imp = _impulse(b5, cfg)
        daily_hit = cfg["daily_movers"]["enabled"] and abs(snap.loc[coin, "change_pct"] or 0) >= cfg["daily_movers"]["move_pct"]
        if not imp and not daily_hit:
            continue
        hit = snap.loc[coin].to_dict(); hit["ticker"] = coin; hit["asset"] = "crypto"; hit["trigger"] = "impulse" if imp else "daily"
        hit.update(imp or {})
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
    for _, row in cands.iterrows():
        hit = row.to_dict(); hit["asset"] = "stock"
        try:
            d = stocks.detail(hit["ticker"])
        except Exception:
            continue
        b5 = d["bars_5m"]
        imp = _impulse(b5, cfg) if len(b5) else None
        daily_hit = cfg["daily_movers"]["enabled"] and abs(hit.get("change_pct") or 0) >= cfg["daily_movers"]["move_pct"]
        if not imp and not daily_hit:
            continue
        hit["trigger"] = "impulse" if imp else "daily"; hit.update(imp or {})
        ctx = {"config": cfg, "bars_5m": b5, "bars_30m": resample(b5, "30min"), "bars_1h": resample(b5, "1h")}
        hit["rvol"] = rvol(hit.get("volume") or 0, d["prior_daily_vols"])
        hit.update({k: d[k] for k in ("float_shares", "float_pct", "short_pct_float")})
        hit["news"], hit["news_link"] = (d["news"][0][0], d["news"][0][1]) if d["news"] else (None, None)
        hits.append(_run_metrics(hit, "stock", ctx))
    return hits


def run(cfg, ledger: dict) -> pd.DataFrame:
    rows = []
    for enabled, fn, args in ((cfg["crypto"]["enabled"], scan_crypto, (cfg, ledger)), (cfg["stocks"]["enabled"], scan_stocks, (cfg,))):
        if enabled:
            try:
                rows += fn(*args)
            except Exception as e:
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
