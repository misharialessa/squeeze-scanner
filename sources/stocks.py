"""US small-cap movers. Candidates from Finviz screener (free, 15-min delayed on free tier),
detail from yfinance. Swap in Polygon/Finnhub later by implementing the same three functions."""
import io
import os
import requests
import pandas as pd
import yfinance as yf

_HDR = {"User-Agent": "Mozilla/5.0"}


def _norm(rows, min_move, min_cap, max_cap, max_n):
    df = pd.DataFrame(rows).dropna(subset=["ticker", "change_pct"])
    if "market_cap" in df:
        df = df[df["market_cap"].fillna(min_cap).between(min_cap, max_cap)]
    df = df[df["change_pct"] >= min_move]
    return df.sort_values("change_pct", ascending=False).head(max_n).reset_index(drop=True)


def movers(cap="midunder", min_move=2, max_candidates=40, min_cap_usd=5e7, max_cap_usd=1e10):
    """US gainers, micro→mid cap. Finnhub → Yahoo predefined → Finviz. Returns (df, diag)."""
    info = {"stock_source": None, "stock_error": None}
    fk = os.getenv("FINNHUB_API_KEY")
    # --- Finnhub: one call for the whole US market's daily change ---
    if fk:
        try:
            j = requests.get("https://finnhub.io/api/v1/stock/market-movers",
                             params={"exchange": "US", "token": fk}, timeout=20)
            if j.status_code == 403:                       # endpoint not on free tier → fall through
                info["stock_error"] = "finnhub: movers endpoint not on your plan"
            else:
                data = j.json()
                pool = (data.get("gainers") or []) if isinstance(data, dict) else (data or [])
                rows = [{"ticker": x.get("symbol"), "price": x.get("price") or x.get("last"),
                         "change_pct": x.get("changePercent") or x.get("change_percent") or x.get("perc"),
                         "volume": x.get("volume"), "market_cap": x.get("marketCap")} for x in pool]
                df = _norm(rows, min_move, min_cap_usd, max_cap_usd, max_candidates)
                if len(df):
                    info["stock_source"] = f"finnhub ({len(df)})"
                    return df, info
                if not info["stock_error"]:
                    info["stock_error"] = "finnhub: 0 gainers matched"
        except Exception as e:
            info["stock_error"] = f"finnhub: {str(e)[:110]}"
    else:
        info["stock_error"] = "no FINNHUB_API_KEY set"
    # --- Yahoo predefined screens (no auth crumb needed) ---
    try:
        rows = []
        for name in ("day_gainers", "small_cap_gainers"):
            res = yf.screen(name, size=100)
            rows += [{"ticker": x.get("symbol"), "price": x.get("regularMarketPrice"),
                      "change_pct": x.get("regularMarketChangePercent"), "volume": x.get("regularMarketVolume"),
                      "market_cap": x.get("marketCap")} for x in (res.get("quotes", []) if isinstance(res, dict) else [])]
        df = _norm(pd.DataFrame(rows).drop_duplicates("ticker").to_dict("records"), min_move, min_cap_usd, max_cap_usd, max_candidates)
        if len(df):
            info["stock_source"] = f"yahoo predefined ({len(df)})"
            return df, info
    except Exception as e:
        info["stock_error"] = (info["stock_error"] or "") + f" | yahoo: {str(e)[:90]}"
    # --- Finviz ---
    try:
        url = f"https://finviz.com/screener.ashx?v=111&f=cap_{cap},ta_change_u{int(min_move)}&o=-change"
        html = requests.get(url, headers=_HDR, timeout=15).text
        t = next((x for x in pd.read_html(io.StringIO(html)) if any("ticker" in str(c).lower() for c in x.columns)), None)
        if t is not None:
            col = lambda k: next((t[c] for c in t.columns if k in str(c).lower()), None)
            df = pd.DataFrame({"ticker": col("ticker"), "price": pd.to_numeric(col("price"), errors="coerce"),
                               "change_pct": pd.to_numeric(col("change").astype(str).str.rstrip("%"), errors="coerce"),
                               "volume": pd.to_numeric(col("volume").astype(str).str.replace(",", ""), errors="coerce")}).dropna(subset=["change_pct"])
            info["stock_source"] = f"finviz ({len(df)})"
            return df.head(max_candidates).reset_index(drop=True), info
        info["stock_error"] = (info["stock_error"] or "") + " | finviz: blocked"
    except Exception as e:
        info["stock_error"] = (info["stock_error"] or "") + f" | finviz: {str(e)[:70]}"
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
