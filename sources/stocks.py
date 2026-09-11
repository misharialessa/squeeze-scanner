"""US small-cap movers. Candidates from Finviz screener (free, 15-min delayed on free tier),
detail from yfinance. Swap in Polygon/Finnhub later by implementing the same three functions."""
import io
import requests
import pandas as pd
import yfinance as yf

_HDR = {"User-Agent": "Mozilla/5.0"}


def movers(cap: str = "midunder", min_move: float = 2, max_candidates: int = 40, min_cap_usd: float = 5e7, max_cap_usd: float = 1e10) -> tuple[pd.DataFrame, dict]:
    """Candidates: US equities up ≥min_move% today, micro→mid cap, sorted by % change.
    Source 1: Yahoo screener via yfinance (works from cloud IPs). Source 2: Finviz (often blocked)."""
    info = {"stock_source": None, "stock_error": None}
    # --- Yahoo custom screen ---
    try:
        q = yf.EquityQuery("and", [
            yf.EquityQuery("gt", ["percentchange", float(min_move)]),
            yf.EquityQuery("btwn", ["intradaymarketcap", float(min_cap_usd), float(max_cap_usd)]),
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("gt", ["dayvolume", 200000]),
        ])
        res = yf.screen(q, size=min(250, max(25, max_candidates * 3)), sortField="percentchange", sortAsc=False)
        quotes = res.get("quotes", []) if isinstance(res, dict) else []
        rows = [{"ticker": x.get("symbol"), "price": x.get("regularMarketPrice"), "change_pct": x.get("regularMarketChangePercent"),
                 "volume": x.get("regularMarketVolume"), "market_cap": x.get("marketCap")} for x in quotes if x.get("symbol")]
        df = pd.DataFrame(rows).dropna(subset=["ticker", "change_pct"])
        if len(df):
            info["stock_source"] = f"yahoo screener ({len(df)} matches)"
            return df.sort_values("change_pct", ascending=False).head(max_candidates).reset_index(drop=True), info
        info["stock_error"] = "yahoo screener returned 0"
    except Exception as e:
        info["stock_error"] = f"yahoo: {str(e)[:120]}"
    # --- Yahoo predefined screens (no filters, but never empty in-session) ---
    try:
        rows = []
        for name in ("day_gainers", "small_cap_gainers", "aggressive_small_caps"):
            res = yf.screen(name, size=100)
            rows += [{"ticker": x.get("symbol"), "price": x.get("regularMarketPrice"), "change_pct": x.get("regularMarketChangePercent"),
                      "volume": x.get("regularMarketVolume"), "market_cap": x.get("marketCap")} for x in (res.get("quotes", []) if isinstance(res, dict) else [])]
        df = pd.DataFrame(rows).dropna(subset=["ticker", "change_pct"]).drop_duplicates("ticker")
        df = df[(df["change_pct"] >= min_move) & (df["market_cap"].fillna(0).between(min_cap_usd, max_cap_usd))]
        if len(df):
            info["stock_source"] = f"yahoo predefined ({len(df)} matches)"
            return df.sort_values("change_pct", ascending=False).head(max_candidates).reset_index(drop=True), info
    except Exception as e:
        info["stock_error"] = (info["stock_error"] or "") + f" | predefined: {str(e)[:100]}"
    # --- Finviz fallback ---
    try:
        url = f"https://finviz.com/screener.ashx?v=111&f=cap_{cap},ta_change_u{int(min_move)}&o=-change"
        html = requests.get(url, headers=_HDR, timeout=15).text
        tables = pd.read_html(io.StringIO(html))
        t = next((x for x in tables if any("ticker" in str(c).lower() for c in x.columns)), None)
        if t is not None:
            col = lambda k: next((t[c] for c in t.columns if k in str(c).lower()), None)
            df = pd.DataFrame({"ticker": col("ticker"), "price": pd.to_numeric(col("price"), errors="coerce"),
                               "change_pct": pd.to_numeric(col("change").astype(str).str.rstrip("%"), errors="coerce"),
                               "volume": pd.to_numeric(col("volume").astype(str).str.replace(",", ""), errors="coerce")}).dropna(subset=["change_pct"])
            info["stock_source"] = f"finviz ({len(df)} matches)"
            return df.head(max_candidates).reset_index(drop=True), info
        info["stock_error"] = (info["stock_error"] or "") + " | finviz: no table (blocked?)"
    except Exception as e:
        info["stock_error"] = (info["stock_error"] or "") + f" | finviz: {str(e)[:80]}"
    return pd.DataFrame(), info


def detail(ticker: str) -> dict:
    """Float, shares outstanding, short interest, 5m intraday bars, prior daily volumes, news."""
    t = yf.Ticker(ticker)
    info = t.info or {}
    intra = t.history(period="5d", interval="5m", prepost=True)
    daily = t.history(period="10d", interval="1d")
    intra = intra.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]] if len(intra) else pd.DataFrame()
    prior_daily = daily["Volume"].iloc[:-1].tail(5) if len(daily) > 1 else pd.Series(dtype=float)
    fl, so = info.get("floatShares"), info.get("sharesOutstanding")
    news = [(n.get("title"), n.get("link")) for n in (t.news or [])[:3]]
    return {
        "market_cap": info.get("marketCap"),
        "float_shares": fl,
        "float_pct": round(fl / so * 100, 1) if fl and so else None,
        "short_pct_float": info.get("shortPercentOfFloat"),
        "bars_5m": intra,
        "prior_daily_vols": prior_daily,
        "news": news,
    }


def last_prices(tickers: list[str]) -> dict:
    """Latest price for a batch of tickers (one download)."""
    if not tickers:
        return {}
    try:
        data = yf.download(tickers, period="1d", interval="5m", prepost=True, progress=False, group_by="ticker", threads=True)
        out = {}
        for t in tickers:
            try:
                s = data[t]["Close"] if len(tickers) > 1 else data["Close"]
                out[t] = float(s.dropna().iloc[-1])
            except Exception:
                pass
        return out
    except Exception:
        return {}
