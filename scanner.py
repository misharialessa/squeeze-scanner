import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh
from core.scan import run
from core.metrics import rank_1_to_5

load_dotenv()
import os
for k, v in st.secrets.items(): os.environ.setdefault(k, str(v))   # Streamlit Cloud secrets
cfg = yaml.safe_load(open("config.yaml"))

st.set_page_config(page_title="Squeeze Scanner", layout="wide")
st.title("Momentum / Squeeze Scanner")

with st.sidebar:
    st.header("Filters")
    cfg["crypto"]["enabled"] = st.checkbox("Crypto (Hyperliquid perps)", cfg["crypto"]["enabled"])
    cfg["stocks"]["enabled"] = st.checkbox("US small caps", cfg["stocks"]["enabled"])
    st.subheader("Impulse trigger (5m)")
    cfg["impulse"]["min_move_pct"] = st.number_input("Min move % (1–2 candles)", 0.2, 20.0, float(cfg["impulse"]["min_move_pct"]), 0.1)
    cfg["impulse"]["vol_multiple"] = st.slider("Candle vol ≥ x avg", 2.0, 5.0, float(cfg["impulse"]["vol_multiple"]), 0.5)
    cfg["impulse"]["avg_lookback_days"] = st.slider("Avg over days", 2, 4, int(cfg["impulse"]["avg_lookback_days"]))
    cfg["daily_movers"]["enabled"] = st.checkbox("Also show 24h/day ≥10% movers", cfg["daily_movers"]["enabled"])
    st.caption("CMC: " + ("✅ key set" if os.getenv("CMC_API_KEY") else "not set"))
    min_rvol = st.number_input("Min RVOL to show", 0.0, 20.0, 0.0)
    auto = st.toggle("Auto-refresh", True)
    if auto:
        st_autorefresh(interval=cfg["refresh_seconds"] * 1000, key="tick")
    if st.button("Refresh now"):
        st.cache_data.clear()


if "ledger" not in st.session_state:
    st.session_state.ledger = {}          # rolling mid-price history for impulse pre-detection

diag = {}
try:
    with st.spinner("Scanning…"):
        df = run(cfg, st.session_state.ledger, diag)
except Exception:
    st.error("Scan failed — copy the traceback below and send it to Claude.")
    st.code(traceback.format_exc())
    st.stop()
now_local = datetime.now(ZoneInfo(cfg["timezone"])).strftime("%H:%M:%S %Z")
seeding = len(next(iter(st.session_state.ledger.values()), [])) <= 1
st.caption(f"Last scan {now_local} · {len(df)} hits" + (" · warming up price ledger (first refresh seeds top-volume perps)" if seeding else ""))

with st.expander("Diagnostics — what the scanner checked this refresh", expanded=df.empty):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Crypto candidates", diag.get("crypto_candidates", 0))
    c2.metric("Candles fetched", diag.get("candles_ok", 0))
    c3.metric("Candle errors", diag.get("candles_err", 0))
    c4.metric("Stock candidates", getattr(__import__("core.scan", fromlist=["scan_stocks"]).scan_stocks, "diag", {}).get("stock_candidates", "—"))
    for k in ("last_err", "scan_crypto_error", "scan_stocks_error"):
        if diag.get(k):
            st.code(f"{k}: {diag[k]}")
    nm = pd.DataFrame(diag.get("near_misses", []))
    if len(nm):
        st.caption("Closest to trigger (move over last 2 candles · spike volume ÷ 3-day per-candle avg):")
        st.dataframe(nm.sort_values("move_pct", ascending=False).head(15), hide_index=True, use_container_width=True,
                     column_config={"move_pct": st.column_config.NumberColumn("Move %", format="%.2f%%"),
                                    "vol_x": st.column_config.NumberColumn("Vol ×", format="%.1fx")})

if df.empty:
    st.info("No assets past the trigger right now. Check Diagnostics above: if candles fetched is 0, Hyperliquid is rate-limiting; "
            "if near-misses show small moves, the market is simply quiet.")
    st.stop()

try:
    df = rank_1_to_5(df, cfg["weights"], cfg)
except Exception:
    st.error("Ranking failed — copy the traceback below and send it to Claude.")
    st.code(traceback.format_exc())
    st.stop()
if min_rvol:
    df = df[df["rvol"].fillna(0) >= min_rvol]

act = df[df["act"]]
if len(act):
    st.error("🔥 ACT NOW (potential ≥70): " + ", ".join(f"{t} {s}" for t, s in zip(act["ticker"], act["side"])))
dt = df[df["side"] == "NONE"]
if len(dt):
    st.warning("🚫 DON'T TOUCH: " + ", ".join(dt["ticker"]))
df["ticker"] = df.apply(lambda r: ("🔥 " if r["act"] else "") + r["ticker"], axis=1)

df["flags_txt"] = df["flags"].apply(lambda f: " | ".join(f) if isinstance(f, list) else "")
tz = ZoneInfo(cfg["timezone"])
if "impulse_time" in df:
    df["impulse_time"] = pd.to_datetime(df["impulse_time"], utc=True, errors="coerce").dt.tz_convert(tz).dt.strftime("%H:%M")
cols = ["potential", "side", "ticker", "asset", "impulse_time", "impulse_pct", "impulse_vol_x", "change_pct", "price", "volume",
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
fmt.update({"volume": "{:,.0f}", "float_shares": "{:,.0f}", "market_cap_usd": "{:,.0f}", "liq_above_usd": "{:,.0f}", "liq_below_usd": "{:,.0f}", "impulse_vol_x": "{:.1f}x", "potential": "{:.0f}", "score_pct": "{:.0f}%",
            "funding_8h_pct": "{:+.3f}%", "price": lambda v: f"{v:,.2f}" if v >= 1 else f"{v:.5f}"})
styled = show.style.format({k: v for k, v in fmt.items() if k in show.columns}, na_rep="—")
if "potential" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 70, G), (lambda x: x >= 50, LG), (lambda x: x < 30, "background-color:#eeeeee")]), subset=["potential"])
if "change_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 30, G), (lambda x: x > 0, LG), (lambda x: x <= -30, R), (lambda x: x < 0, "background-color:#f4a6a6")]), subset=["change_pct"])
if "rvol" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 5, G), (lambda x: x >= 3, LG), (lambda x: x < 3, "background-color:#f4a6a6")]), subset=["rvol"])
if "short_risk" in show: styled = styled.map(lambda v: {"EXTREME": R, "HIGH": O, "MED": "background-color:#fff2b3"}.get(v, ""), subset=["short_risk"])
if "side" in show: styled = styled.map(lambda v: R if v == "NONE" else "", subset=["side"])
if "macd_long_ok" in show: styled = styled.map(lambda v: LG if v is True else "", subset=["macd_long_ok"])
if "float_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x < 30, O)]), subset=["float_pct"])
if "funding_8h_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x <= -0.4, G), (lambda x: x <= -0.1, LG), (lambda x: x >= 0.4, R), (lambda x: x >= 0.1, O)]), subset=["funding_8h_pct"])
ext = [c for c in show.columns if c.startswith(("ema9_", "vwap_"))]
if ext: styled = styled.map(lambda v: _c(v, [(lambda x: x > 0, LG), (lambda x: x <= 0, "background-color:#f4a6a6")]), subset=ext)

st.dataframe(
    styled,
    use_container_width=True,
    hide_index=True,
    column_config={
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
    with st.expander(f"{r['ticker']} · {r['side']} · {r['change_pct']:+.1f}% · potential {r['potential']:.0f}"):
        st.caption(r.get("why", ""))
        if r.get("news"):
            st.markdown(f"**News:** [{r['news']}]({r['news_link']})")
        for f in r.get("flags", []):
            st.write(f)
        if r.get("error"):
            st.caption(f"data error: {r['error']}")

st.download_button("Export CSV", df.drop(columns=["flags","flags_txt"], errors="ignore").to_csv(index=False), "scan.csv")
