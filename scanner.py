import time
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
    cfg["crypto"]["move_trigger_pct"] = st.number_input("Crypto move trigger %", 1.0, 100.0, float(cfg["crypto"]["move_trigger_pct"]))
    cfg["stocks"]["move_trigger_pct"] = st.number_input("Stock move trigger %", 1.0, 100.0, float(cfg["stocks"]["move_trigger_pct"]))
    min_rvol = st.number_input("Min RVOL to show", 0.0, 20.0, 0.0)
    auto = st.toggle("Auto-refresh", True)
    if auto:
        st_autorefresh(interval=cfg["refresh_seconds"] * 1000, key="tick")
    if st.button("Refresh now"):
        st.cache_data.clear()


@st.cache_data(ttl=cfg["refresh_seconds"] - 5, show_spinner="Scanning…")
def cached_scan(cfg_json: str):
    return run(yaml.safe_load(cfg_json))


df = cached_scan(yaml.dump(cfg))
st.caption(f"Last scan {time.strftime('%H:%M:%S')} · {len(df)} hits")

if df.empty:
    st.info("No assets past the trigger right now.")
    st.stop()

df = rank_1_to_5(df, cfg["weights"])
if min_rvol:
    df = df[df["rvol"].fillna(0) >= min_rvol]

act = df[df["act"] != ""]
if len(act):
    st.error(f"🔥 ACT NOW: {', '.join(act['ticker'])} — score ≥80%, RVOL ≥3x, above 9EMA+VWAP on all TFs")

df["flags_txt"] = df["flags"].apply(lambda f: " | ".join(f) if isinstance(f, list) else "")
cols = ["act", "score_pct", "asset", "ticker", "change_pct", "price", "volume", "float_pct",
        "funding_8h_pct", "funding_label", "flags_txt", "rvol", "float_shares", "news_link",
        "ema9_5m_pct", "vwap_5m_pct", "ema9_30m_pct", "vwap_30m_pct", "ema9_1h_pct", "vwap_1h_pct",
        "liq_above_pct", "liq_below_pct"]
rank_cols = [c for c in df.columns if c.startswith("rank_")]
show = df[[c for c in cols + rank_cols if c in df.columns]]

def _c(v, rules):
    if v is None or v != v: return ""
    for cond, style in rules:
        if cond(v): return style
    return ""
G, LG, R, O = "background-color:#1b7f3b;color:white", "background-color:#a8dcb5", "background-color:#c62828;color:white", "background-color:#f6b26b"
styled = show.style
if "score_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 80, G), (lambda x: x >= 60, LG), (lambda x: x < 40, "background-color:#eeeeee")]), subset=["score_pct"])
if "change_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 30, G), (lambda x: x > 0, LG), (lambda x: x <= -30, R), (lambda x: x < 0, "background-color:#f4a6a6")]), subset=["change_pct"])
if "rvol" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x >= 5, G), (lambda x: x >= 3, LG), (lambda x: x < 3, "background-color:#f4a6a6")]), subset=["rvol"])
if "float_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x < 30, O)]), subset=["float_pct"])
if "funding_8h_pct" in show: styled = styled.map(lambda v: _c(v, [(lambda x: x <= -0.4, G), (lambda x: x <= -0.1, LG), (lambda x: x >= 0.4, R), (lambda x: x >= 0.1, O)]), subset=["funding_8h_pct"])
ext = [c for c in show.columns if c.startswith(("ema9_", "vwap_"))]
if ext: styled = styled.map(lambda v: _c(v, [(lambda x: x > 0, LG), (lambda x: x <= 0, "background-color:#f4a6a6")]), subset=ext)

st.dataframe(
    styled,
    use_container_width=True,
    hide_index=True,
    column_config={
        "news_link": st.column_config.LinkColumn("News", display_text="open"),
        "act": st.column_config.TextColumn("", width="small"),
        "flags_txt": st.column_config.TextColumn("Flags", width="large"),
        "score_pct": st.column_config.NumberColumn("Score %", format="%.0f%%"),
        "change_pct": st.column_config.NumberColumn("Δ %", format="%.1f%%"),
        "funding_8h_pct": st.column_config.NumberColumn("Funding 8h", format="%.3f%%"),
        "rvol": st.column_config.NumberColumn("RVOL (5d)", format="%.1fx"),
        "float_shares": st.column_config.NumberColumn("Float", format="%.0f"),
        "float_pct": st.column_config.NumberColumn("Float %", format="%.0f%%"),
    },
)

st.subheader("Risk checklist")
for r in df.to_dict("records"):
    with st.expander(f"{r.get('act','')} {r['ticker']} · {r['change_pct']:+.1f}% · score {r['score_pct']:.0f}%"):
        if r.get("news"):
            st.markdown(f"**News:** [{r['news']}]({r['news_link']})")
        for f in r.get("flags", []):
            st.write(f)
        if r.get("error"):
            st.caption(f"data error: {r['error']}")

st.download_button("Export CSV", df.drop(columns=["flags","flags_txt"], errors="ignore").to_csv(index=False), "scan.csv")
