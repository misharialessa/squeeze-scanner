"""US small-cap movers. Candidates from Finviz screener (free, 15-min delayed on free tier),
detail from yfinance. Swap in Polygon/Finnhub later by implementing the same three functions."""
import io
import requests
import pandas as pd
import yfinance as yf

_HDR = {"User-Agent": "Mozilla/5.0"}


def movers(max_price: float = 20, max_float_m: float = 50, min_move: float = 10) -> pd.DataFrame:
    """Gainers AND losers ≥ min_move % with price/float caps."""
    frames = []
    for direction in ("u", "d"):
        url = (f"https://finviz.com/screener.ashx?v=111&f=sh_price_u{int(max_price)},"
               f"sh_float_u{int(max_float_m)},ta_change_{direction}{int(min_move)}&o=-change")
        try:
            html = requests.get(url, headers=_HDR, timeout=15).text
            tables = pd.read_html(io.StringIO(html))
            t = next((x for x in tables if "Ticker" in x.columns), None)
            if t is not None:
                frames.append(t)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    # Finviz renames/reorders columns at times — match by keyword, case-insensitive
    def col(*keys):
        for c in df.columns:
            if any(k in str(c).lower() for k in keys):
                return df[c]
        return None
    tk, pr, ch, vo = col("ticker"), col("price"), col("change"), col("volume")
    if tk is None or ch is None:
        return pd.DataFrame()        # bot-wall or layout change → no stocks this refresh, app keeps running
    out = pd.DataFrame({
        "ticker": tk,
        "price": pd.to_numeric(pr, errors="coerce") if pr is not None else None,
        "change_pct": pd.to_numeric(ch.astype(str).str.rstrip("%").str.replace(",", ""), errors="coerce"),
        "volume": pd.to_numeric(vo.astype(str).str.replace(",", ""), errors="coerce") if vo is not None else None,
    })
    return out.dropna(subset=["change_pct"]).reset_index(drop=True)


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
        "float_shares": fl,
        "float_pct": round(fl / so * 100, 1) if fl and so else None,
        "short_pct_float": info.get("shortPercentOfFloat"),
        "bars_5m": intra,
        "prior_daily_vols": prior_daily,
        "news": news,
    }
