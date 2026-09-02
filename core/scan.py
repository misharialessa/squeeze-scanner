import pandas as pd
from core.indicators import resample, rvol
from core.metrics import METRICS, checklist
from sources import hyperliquid as hl, stocks, crypto_extras


def _run_metrics(hit: dict, asset: str, ctx: dict) -> dict:
    for name, scope, fn in METRICS:
        if scope in ("both", asset):
            try:
                hit.update(fn(hit, ctx))
            except Exception as e:  # one broken metric must not kill the row
                hit[f"{name}_error"] = str(e)[:80]
    return hit


def scan_crypto(cfg: dict) -> list[dict]:
    c = cfg["crypto"]
    snap = hl.universe_snapshot()
    snap = snap[snap["change_pct"].abs() >= c["move_trigger_pct"]]
    hits = []
    for _, row in snap.iterrows():
        hit = row.to_dict()
        hit["asset"] = "crypto"
        try:
            b5 = hl.candles(hit["ticker"], "5m", 300)
            ctx = {"config": cfg, "bars_5m": b5, "bars_30m": resample(b5, "30min"), "bars_1h": resample(b5, "1h")}
            today = b5[b5.index.date == b5.index[-1].date()]["volume"].sum() if len(b5) else 0
            hit["volume"] = round(float(today), 2)
            hit["rvol"] = rvol(today, hl.daily_volumes(hit["ticker"], cfg["rvol"]["lookback_days"]))
        except Exception as e:
            ctx = {"config": cfg}
            hit["error"] = str(e)[:80]
        hit.update(crypto_extras.supply(hit["ticker"]))
        hit.update(crypto_extras.liquidation_clusters(hit["ticker"], hit["price"]))
        n = crypto_extras.news(hit["ticker"])
        hit["news"], hit["news_link"] = (n[0][0], n[0][1]) if n else (None, None)
        hits.append(_run_metrics(hit, "crypto", ctx))
    return hits


def scan_stocks(cfg: dict) -> list[dict]:
    s = cfg["stocks"]
    cands = stocks.movers(s["max_price"], s["max_float_m"], s["move_trigger_pct"])
    hits = []
    for _, row in cands.iterrows():
        hit = row.to_dict()
        hit["asset"] = "stock"
        try:
            d = stocks.detail(hit["ticker"])
            b5 = d["bars_5m"]
            ctx = {"config": cfg, "bars_5m": b5,
                   "bars_30m": resample(b5, "30min") if len(b5) else None,
                   "bars_1h": resample(b5, "1h") if len(b5) else None}
            hit["rvol"] = rvol(hit.get("volume") or 0, d["prior_daily_vols"])
            hit.update({k: d[k] for k in ("float_shares", "float_pct", "short_pct_float")})
            hit["news"], hit["news_link"] = (d["news"][0][0], d["news"][0][1]) if d["news"] else (None, None)
        except Exception as e:
            ctx = {"config": cfg}
            hit["error"] = str(e)[:80]
        hits.append(_run_metrics(hit, "stock", ctx))
    return hits


def run(cfg: dict) -> pd.DataFrame:
    rows = []
    for enabled, fn in ((cfg["crypto"]["enabled"], scan_crypto), (cfg["stocks"]["enabled"], scan_stocks)):
        if enabled:
            try:
                rows += fn(cfg)
            except Exception as e:      # one source down must never take the dashboard down
                rows.append({"asset": fn.__name__.split("_")[1], "ticker": "SOURCE ERROR", "change_pct": 0, "error": str(e)[:120]})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["flags"] = [checklist(r, cfg) for r in df.to_dict("records")]
    return df
