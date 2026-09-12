"""
Fit predictive models on the full journal and validate them honestly.

- Features: only entry_* fields (known at the first fire) + time/asset context.
- Validation: GroupKFold by calendar day, so a regime never predicts itself.
- Ceiling: 95th percentile AUC of 40 shuffled-target fits. A model is "validated" only if
  its AUC beats that ceiling AND n >= MIN_N. Only validated models are saved and used live.
"""
import json, os, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
MODEL_FILE = "model_weights.json"
MIN_N = 250
HORIZONS = ["15m", "1h", "4h", "24h"]

FEATURES = ["entry_impulse_pct", "entry_impulse_vol_x", "impulse_candles", "entry_vwap_1h_pct", "entry_ema9_5m_pct",
            "entry_vwap_5m_pct", "entry_ema9_30m_pct", "entry_vwap_30m_pct", "entry_ema9_1h_pct", "entry_tfs_confirming",
            "entry_funding_8h_pct", "entry_float_pct", "entry_change_pct", "entry_new_24h_high", "entry_range_pos_24h",
            "entry_compression", "entry_vol_rank_24h", "entry_chg_24h_at_fire_pct", "entry_oi_chg_1h_pct", "entry_oi_chg_15m_pct",
            "entry_btc_move_pct", "entry_breadth_pct", "entry_btc_4h_pct", "entry_macd_long_ok", "entry_spread_bps",
            "entry_depth_1pct_usd", "entry_book_imbalance", "entry_taker_buy_ratio", "entry_trades_5m_usd", "entry_prior_1h_ret_pct",
            "entry_prior_4h_ret_pct", "entry_prior_24h_ret_pct", "entry_rv_24h_pct", "entry_range_pos_3d", "entry_concurrent_signals",
            "entry_open_interest", "entry_vol_24h_usd", "hour", "dow", "is_stock"]
LOG_COLS = {"entry_depth_1pct_usd", "entry_trades_5m_usd", "entry_open_interest", "entry_vol_24h_usd"}


def design(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    d = df[df["entry_side"].astype(str).str.startswith("LONG")].copy()
    t = pd.to_datetime(d["first_seen"], unit="s", utc=True)
    d["hour"], d["dow"], d["is_stock"] = t.dt.hour, t.dt.dayofweek, (d["asset"] == "stock").astype(int)
    X = pd.DataFrame(index=d.index)
    for f in FEATURES:
        col = d[f] if f in d else pd.Series(np.nan, index=d.index)
        col = col.map({True: 1, False: 0}).fillna(col) if col.dtype == object else col
        col = pd.to_numeric(col, errors="coerce")
        X[f] = np.log1p(col.clip(lower=0)) if f in LOG_COLS else col
    return X, t.dt.date.astype(str)


def _pipe(kind):
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import GradientBoostingClassifier
    if kind == "logit":
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=0.1, max_iter=3000))
    return make_pipeline(SimpleImputer(strategy="median"),
                         GradientBoostingClassifier(n_estimators=120, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0))


def run(df: pd.DataFrame, targets=("win",), n_null: int = 40) -> tuple[pd.DataFrame, dict]:
    """Returns (report table, validated models dict). targets: 'win' (perf>0) and/or 'tp' (mfe_h>=2 and mae_h>-2)."""
    from sklearn.model_selection import GroupKFold, cross_val_predict
    from sklearn.metrics import roc_auc_score
    X, groups = design(df)
    d = df.loc[X.index]
    rows, validated = [], {}
    for h in HORIZONS:
        for tgt in targets:
            if tgt == "win":
                y = (pd.to_numeric(d.get(f"perf_{h}"), errors="coerce") > 0)
                mask = pd.to_numeric(d.get(f"perf_{h}"), errors="coerce").notna()
            else:
                mfe, mae = pd.to_numeric(d.get(f"mfe_{h}"), errors="coerce"), pd.to_numeric(d.get(f"mae_{h}"), errors="coerce")
                y = (mfe >= 2) & (mae > -2); mask = mfe.notna() & mae.notna()
            y = y[mask].astype(int); Xh, g = X[mask], groups[mask]
            if len(y) < 40 or g.nunique() < 3 or y.nunique() < 2:
                rows.append({"horizon": h, "target": tgt, "n": len(y), "note": "not enough data"}); continue
            cv = GroupKFold(n_splits=min(g.nunique(), 6))
            best = None
            for kind in ("logit", "gbm"):
                p = cross_val_predict(_pipe(kind), Xh, y, cv=cv, groups=g, method="predict_proba")[:, 1]
                auc = roc_auc_score(y, p); top = p >= np.quantile(p, 0.7)
                if best is None or auc > best[1]: best = (kind, auc, y[top].mean(), p)
            rng = np.random.default_rng(0); nulls = []
            for _ in range(n_null):
                yp = pd.Series(rng.permutation(y.values), index=y.index)
                p = cross_val_predict(_pipe("logit"), Xh, yp, cv=cv, groups=g, method="predict_proba")[:, 1]
                nulls.append(roc_auc_score(yp, p))
            ceiling = float(np.quantile(nulls, 0.95))
            ok = best[1] > ceiling and len(y) >= MIN_N
            rows.append({"horizon": h, "target": tgt, "n": len(y), "base_win": round(y.mean(), 2), "model": best[0],
                         "oos_auc": round(best[1], 3), "chance_ceiling": round(ceiling, 3), "top30_win": round(best[2], 2),
                         "validated": "✅" if ok else "—"})
            if ok:
                mdl = _pipe(best[0]).fit(Xh, y)
                validated[f"{h}:{tgt}"] = {"kind": best[0], "auc": best[1], "n": int(len(y)), "features": FEATURES,
                                          "medians": Xh.median().to_dict(), "model": mdl}
    return pd.DataFrame(rows), validated


def save(validated: dict):
    import pickle, base64
    out = {k: {"kind": v["kind"], "auc": v["auc"], "n": v["n"], "features": v["features"], "medians": v["medians"],
               "blob": base64.b64encode(pickle.dumps(v["model"])).decode()} for k, v in validated.items()}
    json.dump(out, open(MODEL_FILE, "w"))


def load() -> dict:
    import pickle, base64
    if not os.path.exists(MODEL_FILE):
        return {}
    try:
        raw = json.load(open(MODEL_FILE))
        return {k: {**v, "model": pickle.loads(base64.b64decode(v["blob"]))} for k, v in raw.items()}
    except Exception:
        return {}


def predict(models: dict, rows: pd.DataFrame) -> pd.DataFrame:
    """Add model_p_<horizon> columns to live rows using validated models only."""
    if not models or rows.empty:
        return rows
    tmp = rows.copy()
    for f in FEATURES:                                  # live rows carry un-prefixed names
        base = f[len("entry_"):] if f.startswith("entry_") else f
        if f not in tmp and base in tmp: tmp[f] = tmp[base]
    if "entry_side" not in tmp: tmp["entry_side"] = tmp.get("side", "LONG")
    X, _ = design(tmp)
    for key, m in models.items():
        h = key.split(":")[0]
        try:
            rows.loc[X.index, f"model_p_{h}"] = m["model"].predict_proba(X[m["features"]])[:, 1].round(2)
        except Exception:
            pass
    return rows
