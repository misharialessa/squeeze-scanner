import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from core.scan import run
from core.metrics import rank_1_to_5
from core import tracker, persist, fit
from sources import hyperliquid as hl, stocks as stock_src

load_dotenv()
import os
for k, v in st.secrets.items(): os.environ.setdefault(k, str(v))   # Streamlit Cloud secrets
cfg = yaml.safe_load(open("config.yaml"))

@st.cache_resource
def price_ledger() -> dict:
    return {}       # rolling mid-price history; lives in the app process, shared across devices/tabs


@st.cache_resource
def models_store() -> dict:
    return {"models": fit.load(), "report": None}


@st.cache_resource
def hit_store() -> dict:
    return tracker.load()       # {(asset, ticker): row}; journaled to disk

def _wait_and_rerun():
    """Native auto-refresh: sleep then rerun. Widget clicks interrupt the sleep, so the sidebar stays responsive."""
    if auto:
        time.sleep(cfg["refresh_seconds"])
        st.rerun()
    st.stop()


SCAN_MODE = os.getenv("SCAN_MODE", "app").strip().strip('"').lower()          # "cron" = GitHub Actions scans; app only displays

st.set_page_config(page_title="Squeeze Scanner", layout="wide")
st.title("Momentum / Squeeze Scanner")

with st.sidebar:
    st.header("Display Filters (app-controlled)")
    st.caption("⚙️ GitHub collects all impulses; you control what to see here")

    st.subheader("Asset class")
    show_crypto = st.checkbox("Crypto (Hyperliquid perps)", True)
    show_stocks = st.checkbox("US small caps (optional)", False)

    st.subheader("Signal quality (display filter)")
    min_rvol = st.number_input("Min RVOL to show", 0.0, 1000.0, 0.0, 0.5)
    min_potential = st.number_input("Min entry score to show", 0, 100, 0, 5)
    cfg["retain_hours"] = int(st.number_input("Keep on screen (hours)", 1, 720, int(cfg["retain_hours"]), 6))

    st.subheader("Crypto: Impulse criteria (for reference)")
    st.caption(f"🛰 **GitHub uses these to collect signals** — adjust to refine what's collected")
    cfg["impulse"]["min_move_pct"] = st.number_input("Min move % / 2 candles", 0.05, 100.0, float(cfg["impulse"]["min_move_pct"]), 0.1, format="%.2f")
    cfg["impulse"]["vol_multiple"] = st.number_input("Spike volume ≥ × avg", 1.0, 100.0, float(cfg["impulse"]["vol_multiple"]), 0.5)
    cfg["impulse"]["avg_lookback_days"] = int(st.number_input("Avg volume lookback (days)", 1, 15, int(cfg["impulse"]["avg_lookback_days"]), help="How far back to average volume"))

    if show_stocks:
        st.subheader("Stocks: Impulse criteria")
        si = cfg["stock_impulse"]
        si["min_move_pct"] = st.number_input("Stocks: min move %", 0.05, 100.0, float(si["min_move_pct"]), 0.1, format="%.2f", key="s_mv")
        si["vol_multiple"] = st.number_input("Stocks: spike vol ≥ × avg", 1.0, 100.0, float(si["vol_multiple"]), 0.5, key="s_vx")
        cfg["stocks"]["min_market_cap_usd"] = st.number_input("Stocks: min market cap ($M)", 0, 100000, int(cfg["stocks"]["min_market_cap_usd"] / 1e6), key="s_mc") * 1e6
        cfg["stocks"]["min_day_change_pct"] = st.number_input("Stocks: day change ≥ % (pre-filter)", 0.0, 100.0, float(cfg["stocks"]["min_day_change_pct"]), 0.5)

    st.subheader("Advanced")
    cfg["daily_movers"]["enabled"] = st.checkbox("Also show 24h/day movers", cfg["daily_movers"]["enabled"])
    if cfg["daily_movers"]["enabled"]:
        cfg["daily_movers"]["move_pct"] = st.number_input("Daily move ≥ %", 0.5, 500.0, float(cfg["daily_movers"]["move_pct"]), 0.5)
    auto = st.toggle("Auto-refresh", True)

    st.divider()
    st.subheader("Journal")
    st.caption(persist.status())
    st.caption(f"Mode: **{SCAN_MODE}** · {os.getenv('GITHUB_REPO') or '—'}")
    st.caption("CMC: " + ("✅ key set" if os.getenv("CMC_API_KEY") else "not set"))
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Push now", use_container_width=True):
            err = tracker.save(hit_store(), force_remote=True)
            st.success("✅ Pushed") if not err else st.error(err)
    with c2:
        if st.button("Refresh", use_container_width=True):
            st.cache_data.clear(); st.rerun()
    if st.button("Clear journal", use_container_width=True):
        hit_store().clear(); tracker.save(hit_store(), force_remote=True); st.success("Cleared")



diag = {}
try:
    if SCAN_MODE == "cron":
        remote = tracker.load()                        # fresh copy from the journal branch
        if remote:
            hit_store().clear(); hit_store().update(remote)
        new = pd.DataFrame()
        job = persist.load_remote("last_run.json") or {}
        if job:
            diag.update(job.get("diag", {}))
            diag["near"], diag["stock_near"] = job.get("near", []), job.get("stock_near", [])
            diag["job_ts"], diag["job_new"], diag["job_push_err"] = job.get("ts"), job.get("new"), job.get("push_err")
        rep = persist.load_remote("fit_report.json")
        if rep: models_store()["report"] = pd.DataFrame(rep)
        mw = persist.load_remote(fit.MODEL_FILE)
        if mw:
            import json as _json; _json.dump(mw, open(fit.MODEL_FILE, "w")); models_store()["models"] = fit.load()
    else:
        with st.spinner("Scanning…"):
            new = run(cfg, price_ledger(), diag)
except Exception:
    st.error("Scan failed — copy the traceback below and send it to Claude.")
    st.code(traceback.format_exc())
    _wait_and_rerun()
# ---- journal: ingest new signals, then mark everything to market ----
store, now_ts = hit_store(), time.time()
new_rows = [] if new.empty else new.to_dict("records")
if new_rows:
    for r in new_rows:                                   # distinct-impulse count so far (incl. this one) feeds the score
        prev = store.get((r.get("asset"), r.get("ticker")), {})
        r["impulses"] = len(set(prev.get("impulse_times") or []) | ({str(r["impulse_time"])} if r.get("impulse_time") is not None else set()))
    new_ranked = rank_1_to_5(pd.DataFrame(new_rows), cfg["weights"], cfg)
    new_rows = new_ranked.to_dict("records")
if SCAN_MODE != "cron":
    tracker.ingest(store, new_rows, now_ts)
# entry_* stays frozen at the FIRST fire — no upgrades from later information
prices, btc_now = {}, None
try:
    mids = hl.all_mids()
    btc_now = mids.get("BTC")
    prices.update({k: mids[k[1]] for k in store if k[0] == "crypto" and k[1] in mids})
except Exception:
    pass
prices.update({("stock", t): p for t, p in stock_src.last_prices([k[1] for k in store if k[0] == "stock"]).items()})
if SCAN_MODE != "cron":
    tracker.mark(store, prices, now_ts, cfg["retain_hours"], btc_now)
    tracker.save(store)
df_all = pd.DataFrame(list(store.values()))
if not df_all.empty and "archived" not in df_all:
    df_all["archived"] = False
if not df_all.empty:
    df_all["archived"] = df_all["archived"].apply(lambda v: bool(v) if v is not None and v == v else False).astype(bool)
df = df_all[~df_all["archived"]].copy() if not df_all.empty else df_all
n_archived = int(df_all["archived"].sum()) if not df_all.empty else 0
if not df.empty:
    df["live"] = (now_ts - df["last_seen"]) < cfg["refresh_seconds"] * 1.5
    tz_ = ZoneInfo(cfg["timezone"])
    df["first_seen_local"] = pd.to_datetime(df["first_seen"], unit="s", utc=True).dt.tz_convert(tz_).dt.strftime("%d %b %H:%M")
    df["age_min"] = ((now_ts - df["first_seen"]) / 60).round(0)
    df["flags"] = df["flags"].apply(lambda f: f if isinstance(f, list) else [])
    df["score_pct"] = df.get("score_pct")

now_local = datetime.now(ZoneInfo(cfg["timezone"])).strftime("%H:%M:%S %Z")
seeding = len(next(iter(price_ledger().values()), [])) <= 1
st.caption(("🛰 scanning via GitHub Actions · " if SCAN_MODE == "cron" else "") + f"Last scan {now_local} · {len(new) if not new.empty else 0} new/refired · {len(df)} on screen (last {cfg['retain_hours']}h) · {n_archived} archived · {len(df_all)} total in journal" + (" · warming up price ledger (first refresh seeds top-volume perps)" if seeding else ""))

if SCAN_MODE == "cron":
    _age = (now_ts - diag["job_ts"]) / 60 if diag.get("job_ts") else None
    if _age is None:
        st.error("🛰 No scanner status yet — the GitHub job has not reported, or the app cannot read the journal repo. "
                 f"last_run.json: {persist.load_status('last_run.json')} · journal: {persist.load_status()}")
    elif _age > 15:
        st.error(f"🛰 Scanner last reported {_age:.0f} min ago — it looks stopped. Runs hand off every ~6h; the hourly watchdog restarts it.")
    else:
        st.success(f"🛰 Scanner running on GitHub · last cycle {_age:.0f} min ago · {diag.get('job_new', 0)} new this cycle"
                   + (f" · push error: {diag['job_push_err']}" if diag.get("job_push_err") else ""))

with st.expander("Diagnostics — what the scanner checked this refresh", expanded=df.empty):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Crypto candidates", diag.get("crypto_candidates", 0))
    c2.metric("Candles fetched", diag.get("candles_ok", 0))
    c3.metric("Candle errors", diag.get("candles_err", 0))
    c4.metric("Stock candidates", diag.get("stock_candidates", "—"))
    st.caption(f"Stocks: source = {diag.get('stock_source') or 'none'} · bars checked {diag.get('stock_checked', 0)} · impulses {diag.get('stock_impulses', 0)}"
               + (" · US market closed (16:30–23:00 Kuwait)" if not (13 <= datetime.now(ZoneInfo('UTC')).hour < 20) else ""))
    for k in ("last_err", "scan_crypto_error", "scan_stocks_error", "stock_error"):
        if diag.get(k):
            st.code(f"{k}: {diag[k]}")
    sn = pd.DataFrame(diag.get("stock_near", []))
    if len(sn):
        st.caption("Stocks closest to trigger:")
        st.dataframe(sn.sort_values("move_pct", ascending=False).head(10), hide_index=True, width="stretch",
                     column_config={"move_pct": st.column_config.NumberColumn("Move %", format="%.2f%%"), "vol_x": st.column_config.NumberColumn("Vol ×", format="%.1fx")})
    nm = pd.DataFrame(diag.get("near_misses", []))
    if len(nm):
        st.caption("Closest to trigger (move over last 2 candles · spike volume ÷ 3-day per-candle avg):")
        st.dataframe(nm.sort_values("move_pct", ascending=False).head(15), hide_index=True, width="stretch",
                     column_config={"move_pct": st.column_config.NumberColumn("Move %", format="%.2f%%"),
                                    "vol_x": st.column_config.NumberColumn("Vol ×", format="%.1fx")})

if df.empty and not df_all.empty:
    with st.expander("📊 Scanner effectiveness (all history)", expanded=True):
        st.dataframe(tracker.effectiveness(df_all), hide_index=True, width="stretch")
        st.download_button("Download full journal CSV", df_all.drop(columns=["flags"], errors="ignore").to_csv(index=False), "signals_journal.csv", key="dl_empty")
if df.empty:
    st.info("Nothing on screen in the last retention window. Check Diagnostics above: if candles fetched is 0, Hyperliquid is rate-limiting; "
            "if near-misses show small moves, the market is simply quiet.")
    _wait_and_rerun()

df["potential"] = pd.to_numeric(df["entry_potential"], errors="coerce")
df["side"] = df["entry_side"]
df["act"] = (df["potential"] >= 40) & (df["side"] == "LONG")
df["confirmed"] = pd.to_numeric(df.get("impulses", 1), errors="coerce").fillna(1) >= 2

# Apply sidebar filters
if not df.empty:
    # Asset class filter
    if show_crypto and not show_stocks:
        df = df[df["asset"] == "crypto"]
    elif show_stocks and not show_crypto:
        df = df[df["asset"] == "stock"]
    # Quality filters
    if min_rvol > 0:
        df = df[pd.to_numeric(df.get("rvol", 0), errors="coerce") >= min_rvol]
    if min_potential > 0:
        df = df[pd.to_numeric(df["potential"], errors="coerce") >= min_potential]

df = df.sort_values(["live", "first_seen"], ascending=[False, False]).reset_index(drop=True)

# ---- model fit (validated models only ever reach the score) ----
ms = models_store()
with st.expander("🧪 Model fit — day-blocked cross-validation on the full journal", expanded=False):
    st.caption("A model is used live only if its out-of-sample AUC beats the shuffled-target ceiling with ≥250 signals. "
               "Everything else is reported and ignored, by design.")
    if st.button("Run fit now (30–90 s)"):
        with st.spinner("Fitting…"):
            try:
                rep, val = fit.run(df_all, targets=("win", "tp"))
                ms["report"] = rep
                if val:
                    fit.save(val); ms["models"] = val
            except Exception:
                st.code(traceback.format_exc())
    if ms.get("report") is not None:
        st.dataframe(ms["report"], hide_index=True, width="stretch")
    if ms.get("models"):
        st.success("Live models: " + ", ".join(f"{k} (AUC {v['auc']:.2f}, n={v['n']})" for k, v in ms["models"].items()))
    else:
        st.info("No validated model yet — score uses the three hand rules (VWAP reclaim, ≥10x vol, OI inflow).")
df = fit.predict(ms.get("models", {}), df)
LIVE_H = next((h for h in ("1h", "4h", "15m") if f"model_p_{h}" in df and ms.get("models", {}).get(f"{h}:win")), None)
if LIVE_H:
    df["potential"] = (pd.to_numeric(df[f"model_p_{LIVE_H}"], errors="coerce") * 100).round(0)
    df.loc[df["side"] == "NONE", "potential"] = 0
    df["act"] = (df["potential"] >= 65) & (df["side"] == "LONG")
    st.caption(f"Ranking = validated {LIVE_H} model probability (AUC {ms['models'][f'{LIVE_H}:win']['auc']:.2f}, n={ms['models'][f'{LIVE_H}:win']['n']}). IGNITION at ≥65%.")

# ---- effectiveness ----
with st.expander("📊 Scanner effectiveness (signed: + means the original call was right)", expanded=False):
    eff = tracker.effectiveness(df_all)          # ALL history, archived included
    if eff.empty:
        st.caption("Needs signals that have aged past the first checkpoint (15 min).")
    else:
        st.dataframe(eff, hide_index=True, width="stretch")
    st.download_button("Download full journal CSV", df_all.drop(columns=["flags"], errors="ignore").to_csv(index=False), "signals_journal.csv")
if min_rvol:
    df = df[df["rvol"].fillna(0) >= min_rvol]
if min_potential:
    df = df[df["potential"].fillna(0) >= min_potential]

act = df[df["act"]]
if len(act):
    st.error(("🔥 IGNITION (model P(win) ≥65%): " if LIVE_H else "🔥 IGNITION (score ≥40 = clean signal + ≥10x vol or OI inflow; 15m–1h horizon): ") + ", ".join(f"{t} {s}" for t, s in zip(act["ticker"], act["side"])))
dt = df[df["side"] == "NONE"]
if len(dt):
    st.warning("🚫 DON'T TOUCH: " + ", ".join(dt["ticker"]))
df["ticker"] = df.apply(lambda r: ("🔥 " if r["act"] else "") + r["ticker"], axis=1)

df["flags_txt"] = df["flags"].apply(lambda f: " | ".join(f) if isinstance(f, list) else "")
tz = ZoneInfo(cfg["timezone"])
if "impulse_time" in df:
    df["impulse_time"] = pd.to_datetime(df["impulse_time"], utc=True, errors="coerce").dt.tz_convert(tz).dt.strftime("%H:%M")
df["ticker"] = df.apply(lambda r: ("🟢 " if r.get("live") else "") + str(r["ticker"]), axis=1)
model_cols = [c for c in df.columns if c.startswith("model_p_")]
cols = ["potential", *model_cols, "side", "ticker", "asset", "first_seen_local", "age_min", "entry_price", "cur_price", "perf_pct",
        "alpha_pct", "mfe_pct", "mae_pct", "first_to_2pct", "perf_15m", "perf_1h", "perf_4h", "perf_24h", "confirmed", "impulses", "impulse_time",
        "breadth_pct", "btc_4h_pct", "concurrent_signals", "taker_buy_ratio", "book_imbalance", "spread_bps",
        "new_24h_high", "compression", "vol_rank_24h", "chg_24h_at_fire_pct", "oi_chg_1h_pct", "btc_move_pct", "impulse_pct", "impulse_vol_x", "change_pct", "price", "volume",
        "float_pct", "funding_8h_pct", "funding_label", "short_risk", "macd_long_ok", "flags_txt", "why",
        "rvol", "market_cap_usd", "top_markets", "score_pct", "float_shares", "news_link",
        "ema9_5m_pct", "vwap_5m_pct", "ema9_30m_pct", "vwap_30m_pct", "ema9_1h_pct", "vwap_1h_pct"]
rank_cols = [c for c in df.columns if c.startswith("rank_")]
show = df[[c for c in cols + rank_cols if c in df.columns]]

def _c(v, rules):
    if v is None or v != v: return ""
    for cond, style in rules:
        if cond(v): return style
    return ""
G, LG, R, O = "background-color:#1b7f3b;color:white", "background-color:#a8dcb5", "background-color:#c62828;color:white", "background-color:#f6b26b"
PCT = [c for c in show.columns if c.endswith("_pct")]
fmt = {c: "{:.1f}%" for c in PCT}
fmt.update({"model_p_15m": "{:.0%}", "model_p_1h": "{:.0%}", "model_p_4h": "{:.0%}", "model_p_24h": "{:.0%}",
            "taker_buy_ratio": "{:.0%}", "book_imbalance": "{:+.2f}", "spread_bps": "{:.0f}", "alpha_pct": "{:+.1f}%", "breadth_pct": "{:.0f}%", "btc_4h_pct": "{:+.1f}%", "compression": "{:.2f}x", "vol_rank_24h": "#{:.0f}", "entry_price": lambda v: f"{v:,.2f}" if v >= 1 else f"{v:.5f}", "cur_price": lambda v: f"{v:,.2f}" if v >= 1 else f"{v:.5f}",
            "perf_pct": "{:+.1f}%", "mfe_pct": "{:+.1f}%", "mae_pct": "{:+.1f}%", "perf_15m": "{:+.1f}%", "perf_1h": "{:+.1f}%", "perf_4h": "{:+.1f}%", "perf_24h": "{:+.1f}%",
            "volume": "{:,.0f}", "float_shares": "{:,.0f}", "market_cap_usd": "{:,.0f}", "liq_above_usd": "{:,.0f}", "liq_below_usd": "{:,.0f}", "impulse_vol_x": "{:.1f}x", "potential": "{:.0f}", "score_pct": "{:.0f}%",
            "funding_8h_pct": "{:+.3f}%", "price": lambda v: f"{v:,.2f}" if v >= 1 else f"{v:.5f}"})
styled = show.style.format({k: v for k, v in fmt.items() if k in show.columns}, na_rep="—")
if "potential" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 70, G), (lambda x: x >= 50, LG), (lambda x: x < 30, "background-color:#eeeeee")]), subset=["potential"])
if "change_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 30, G), (lambda x: x > 0, LG), (lambda x: x <= -30, R), (lambda x: x < 0, "background-color:#f4a6a6")]), subset=["change_pct"])
if "rvol" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 5, G), (lambda x: x >= 3, LG), (lambda x: x < 3, "background-color:#f4a6a6")]), subset=["rvol"])
if model_cols: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 0.65, G), (lambda x: x >= 0.55, LG), (lambda x: x < 0.45, "background-color:#f4a6a6")]), subset=model_cols)
PERF = [c for c in ("perf_pct", "alpha_pct", "perf_15m", "perf_1h", "perf_4h", "perf_24h", "mfe_pct", "mae_pct") if c in show.columns]
if PERF: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 5, G), (lambda x: x > 0, LG), (lambda x: x <= -5, R), (lambda x: x < 0, "background-color:#f4a6a6")]), subset=PERF)
if "short_risk" in show: styled = styled.map(lambda v: {"EXTREME": R, "HIGH": O, "MED": "background-color:#fff2b3"}.get(v, ""), subset=["short_risk"])
if "side" in show: styled = styled.map(lambda v: R if v == "NONE" else "background-color:#eeeeee" if str(v).startswith("SHORT") else "", subset=["side"])
if "new_24h_high" in show: styled = styled.map(lambda v: LG if v is True else "", subset=["new_24h_high"])
if "confirmed" in show: styled = styled.map(lambda v: LG if v is True else "", subset=["confirmed"])
if "compression" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x < 0.6, G), (lambda x: x > 1.3, "background-color:#f4a6a6")]), subset=["compression"])
if "vol_rank_24h" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x == 1, G), (lambda x: x <= 3, LG), (lambda x: x > 10, "background-color:#f4a6a6")]), subset=["vol_rank_24h"])
if "macd_long_ok" in show: styled = styled.map(lambda v: LG if v is True else "", subset=["macd_long_ok"])
if "float_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x < 30, O)]), subset=["float_pct"])
if "funding_8h_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x <= -0.4, G), (lambda x: x <= -0.1, LG), (lambda x: x >= 0.4, R), (lambda x: x >= 0.1, O)]), subset=["funding_8h_pct"])
ext = [c for c in show.columns if c.startswith(("ema9_", "vwap_"))]
if ext: styled = styled.map(lambda v: _c(v, [(lambda x: x > 0, LG), (lambda x: x <= 0, "background-color:#f4a6a6")]), subset=ext)

st.dataframe(
    styled,
    width="stretch",
    hide_index=True,
    column_config={
        "entry_price": "Entry px", "cur_price": "Now px", "perf_pct": "Perf (signed)", "mfe_pct": "MFE", "mae_pct": "MAE",
        "perf_15m": "@15m", "perf_1h": "@1h", "perf_4h": "@4h", "perf_24h": "@24h",
        "model_p_15m": "P(win 15m)", "model_p_1h": "P(win 1h)", "model_p_4h": "P(win 4h)", "model_p_24h": "P(win 24h)",
        "taker_buy_ratio": "Taker buy %", "book_imbalance": "Book imb.", "spread_bps": "Spread bps", "concurrent_signals": "Fired together",
        "alpha_pct": "Alpha vs BTC", "first_to_2pct": "±2% first", "breadth_pct": "Breadth", "btc_4h_pct": "BTC 4h",
        "impulses": "Impulses", "confirmed": "2nd impulse", "new_24h_high": "New 24h hi", "compression": "Pre-spike vol",
        "vol_rank_24h": "Vol rank", "chg_24h_at_fire_pct": "24h @fire", "oi_chg_1h_pct": "OI 1h", "btc_move_pct": "BTC same win",
        "first_seen_local": "First seen", "age_min": st.column_config.NumberColumn("Age (min)", format="%.0f"), "fires": "Fires",
        "impulse_time": "Impulse @", "impulse_pct": "Impulse %", "impulse_vol_x": "Impulse vol", "short_risk": "Short risk",
        "macd_long_ok": "MACD ✓", "top_exchange": "Heaviest OI", "liq_exchange": "Liq map exch.", "market_cap_usd": "Mkt cap",
        "top_markets": "Top markets", "liq_above_usd": "Liq $ above", "liq_below_usd": "Liq $ below",
        "potential": "Potential", "side": "Side", "ticker": "Ticker", "asset": "Asset", "change_pct": "Δ %",
        "price": "Price", "volume": "Volume", "float_pct": "Float %", "float_shares": "Float (shares)",
        "funding_8h_pct": "Funding 8h", "funding_label": "Funding", "rvol": "RVOL (5d)", "score_pct": "Rel. score",
        "why": st.column_config.TextColumn("Why", width="large"),
        "flags_txt": st.column_config.TextColumn("Flags", width="large"),
        "news_link": st.column_config.LinkColumn("News", display_text="open"),
        "ema9_5m_pct": "EMA9 5m", "vwap_5m_pct": "VWAP 5m", "ema9_30m_pct": "EMA9 30m", "vwap_30m_pct": "VWAP 30m",
        "ema9_1h_pct": "EMA9 1h", "vwap_1h_pct": "VWAP 1h", "liq_above_pct": "Liq above", "liq_below_pct": "Liq below",
    },
)

st.subheader("Risk checklist")
for r in df.to_dict("records"):
    with st.expander(f"{r['ticker']} · {r['side']} · entry {r.get('entry_price')} → now {r.get('cur_price')} · perf {r.get('perf_pct') if r.get('perf_pct') is not None else 0:+.1f}% · potential {r['potential']:.0f}"):
        st.caption(r.get("why", ""))
        if r.get("news"):
            st.markdown(f"**News:** [{r['news']}]({r['news_link']})")
        for f in r.get("flags", []):
            st.write(f)
        if r.get("error"):
            st.caption(f"data error: {r['error']}")

_wait_and_rerun()
