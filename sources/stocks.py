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
    if fk:
        base = "https://finnhub.io/api/v1"
        # 1) try the movers endpoint (premium on some plans) — tolerate empty/non-JSON
        try:
            r = requests.get(f"{base}/stock/market-movers", params={"exchange": "US", "token": fk}, timeout=20)
            data = r.json() if r.status_code == 200 and r.text.strip() else None
            pool = (data.get("gainers") if isinstance(data, dict) else data) or []
            rows = [{"ticker": x.get("symbol"), "price": x.get("price") or x.get("last"),
                     "change_pct": x.get("changePercent") or x.get("perc"), "volume": x.get("volume"),
                     "market_cap": x.get("marketCap")} for x in pool]
            df = _norm(rows, min_move, min_cap_usd, max_cap_usd, max_candidates)
            if len(df):
                info["stock_source"] = f"finnhub movers ({len(df)})"; return df, info
        except Exception:
            pass
        # 2) free-tier path: quote a cached small/mid-cap universe, keep gainers
        try:
            global _FH_UNIV
            if "_FH_UNIV" not in globals() or not _FH_UNIV:
                syms = requests.get(f"{base}/stock/symbol", params={"exchange": "US", "token": fk}, timeout=25).json()
                _FH_UNIV = [x["symbol"] for x in syms if x.get("type") == "Common Stock" and x.get("symbol") and "." not in x["symbol"]]
            import concurrent.futures as cf
            def q(sym):
                try:
                    d = requests.get(f"{base}/quote", params={"symbol": sym, "token": fk}, timeout=8).json()
                    return {"ticker": sym, "price": d.get("c"), "change_pct": d.get("dp"), "volume": None, "market_cap": None} if d.get("dp") is not None else None
                except Exception:
                    return None
            universe = _FH_UNIV[:1200]                     # ~40s at 60 req/s; covers most active names
            rows = []
            with cf.ThreadPoolExecutor(max_workers=25) as ex:
                for res in ex.map(q, universe):
                    if res and res["change_pct"] is not None and res["change_pct"] >= min_move:
                        rows.append(res)
            df = pd.DataFrame(rows).sort_values("change_pct", ascending=False).head(max_candidates).reset_index(drop=True) if rows else pd.DataFrame()
            if len(df):
                info["stock_source"] = f"finnhub quotes ({len(df)})"; return df, info
            info["stock_error"] = "finnhub: no gainers ≥ threshold (market closed?)"
        except Exception as e:
            info["stock_error"] = f"finnhub quotes: {str(e)[:110]}"
    else:
        info["stock_error"] = "no FINNHUB_API_KEY set"
    # --- Yahoo predefined screens    # --- Yahoo predefined screens (no auth crumb needed) ---
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


def _fh(path, **params):
    fk = os.getenv("FINNHUB_API_KEY")
    if not fk:
        raise RuntimeError("no FINNHUB_API_KEY")
    r = requests.get(f"https://finnhub.io/api/v1/{path}", params={**params, "token": fk}, timeout=12)
    if r.status_code != 200 or not r.text.strip():
        raise RuntimeError(f"finnhub {path} {r.status_code}")
    return r.json()


def _detail_finnhub(ticker: str) -> dict:
    """Finnhub free tier: 5m candles (30 days), daily candles, profile (market cap, shares out), news. No float/short %."""
    import time as _t
    now = int(_t.time())
    c5 = _fh("stock/candle", symbol=ticker, resolution="5", **{"from": now - 6 * 86400, "to": now})
    if c5.get("s") != "ok":
        raise RuntimeError("no 5m candles")
    intra = pd.DataFrame({"open": c5["o"], "high": c5["h"], "low": c5["l"], "close": c5["c"], "volume": c5["v"]},
                         index=pd.to_datetime(c5["t"], unit="s", utc=True).tz_convert("America/New_York"))
    cd = _fh("stock/candle", symbol=ticker, resolution="D", **{"from": now - 20 * 86400, "to": now})
    prior_daily = pd.Series(cd["v"][:-1][-5:], dtype=float) if cd.get("s") == "ok" and len(cd.get("v", [])) > 1 else pd.Series(dtype=float)
    prof = {}
    try: prof = _fh("stock/profile2", symbol=ticker)
    except Exception: pass
    news = []
    try:
        d0 = _t.strftime("%Y-%m-%d", _t.gmtime(now - 3 * 86400)); d1 = _t.strftime("%Y-%m-%d", _t.gmtime(now))
        news = [(n.get("headline"), n.get("url")) for n in _fh("company-news", symbol=ticker, **{"from": d0, "to": d1})[:3]]
    except Exception: pass
    mc = prof.get("marketCapitalization"); so = prof.get("shareOutstanding")
    return {"market_cap": mc * 1e6 if mc else None, "float_shares": None, "float_pct": None, "short_pct_float": None,
            "shares_outstanding": so * 1e6 if so else None, "bars_5m": intra, "prior_daily_vols": prior_daily, "news": news}


def _detail_yf(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info or {}
    intra = t.history(period="5d", interval="5m", prepost=True)
    daily = t.history(period="10d", interval="1d")
    intra = intra.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]] if len(intra) else pd.DataFrame()
    prior_daily = daily["Volume"].iloc[:-1].tail(5) if len(daily) > 1 else pd.Series(dtype=float)
    fl, so = info.get("floatShares"), info.get("sharesOutstanding")
    news = [(n.get("title"), n.get("link")) for n in (t.news or [])[:3]]
    return {"market_cap": info.get("marketCap"), "float_shares": fl, "float_pct": round(fl / so * 100, 1) if fl and so else None,
            "short_pct_float": info.get("shortPercentOfFloat"), "bars_5m": intra, "prior_daily_vols": prior_daily, "news": news}


def detail(ticker: str) -> dict:
    """Float, shares, 5m bars, prior daily volumes, news. Finnhub first (works from cloud IPs); yfinance fallback."""
    try:
        return _detail_finnhub(ticker)
    except Exception:
        return _detail_yf(ticker)


def last_prices(tickers: list[str]) -> dict:
    """Latest price for a batch of tickers. Finnhub quotes (parallel) first; yfinance batch fallback."""
    if not tickers:
        return {}
    if os.getenv("FINNHUB_API_KEY"):
        import concurrent.futures as cf
        def q(t):
            try:
                d = _fh("quote", symbol=t); return (t, float(d["c"])) if d.get("c") else None
            except Exception:
                return None
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            out = dict(x for x in ex.map(q, tickers) if x)
        if out:
            return out
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
