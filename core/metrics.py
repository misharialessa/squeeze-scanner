"""
HOW TO ADD A NEW INDICATOR
--------------------------
1. Write a function  def my_metric(hit: dict, ctx: dict) -> dict  that returns new keys for the row.
   `hit` already contains everything earlier metrics produced; `ctx` has bars_5m, bars_30m, bars_1h, config.
2. Decorate it with @metric("name", asset="crypto" | "stock" | "both").
3. (Optional) add a rank rule in RANK_RULES and a weight in config.yaml → it joins the composite score.
4. (Optional) add a checklist rule in CHECKLIST.
That's it — the scanner picks it up automatically.
"""
import pandas as pd
from core.indicators import extension_pct, macd_state

METRICS: list[tuple[str, str, callable]] = []


def _v(x):
    """None for missing/NaN, else the value."""
    return None if x is None or (isinstance(x, float) and x != x) else x


def metric(name: str, asset: str = "both"):
    def deco(fn):
        METRICS.append((name, asset, fn))
        return fn
    return deco


# ---------- built-in metrics ----------

@metric("extension")
def m_extension(hit, ctx):
    out = {}
    for tf in ctx["config"]["timeframes"]:
        e = extension_pct(ctx.get(f"bars_{tf}"))
        out[f"ema9_{tf}_pct"] = e["ema_pct"]
        out[f"vwap_{tf}_pct"] = e["vwap_pct"]
    vals = [v for k, v in out.items() if v is not None]
    out["extension_avg_pct"] = round(sum(vals) / len(vals), 2) if vals else None
    out["above_all"] = all(v > 0 for v in vals) if vals else False
    out["below_all"] = all(v < 0 for v in vals) if vals else False
    out["tfs_confirming"] = (sum(v > 0 for v in vals) if (sum(v > 0 for v in vals) >= len(vals) / 2) else sum(v < 0 for v in vals)) if vals else 0
    return out


@metric("funding", asset="crypto")
def m_funding(hit, ctx):
    f = hit.get("funding_8h_pct")
    cfg = ctx["config"]["crypto"]
    if f is None:
        return {"funding_label": None}
    side = "shorts pay" if f < 0 else "longs pay"
    mag = "extreme" if abs(f) >= cfg["funding_extreme_pct"] else "high" if abs(f) >= cfg["funding_high_pct"] else "normal"
    return {"funding_label": f"{side} · {mag}"}


@metric("macd")
def m_macd(hit, ctx):
    st = macd_state(ctx.get("bars_5m"), ctx["config"]["macd"])
    st["macd_long_ok"] = bool(st["macd_positive"]) and st["macd_cross_up_ago"] is not None
    return st


@metric("short_risk", asset="crypto")
def m_short_risk(hit, ctx):
    """How dangerous is it to short this? Combines float, funding sign, overhead liquidation clusters."""
    cfg = ctx["config"]["crypto"]
    pts, why = 0, []
    hit = {k: _v(v) for k, v in hit.items()}
    fl, f = hit.get("float_pct"), hit.get("funding_8h_pct")
    if fl is not None and fl < 30: pts += 2; why.append("low float")
    if fl is not None and fl < 15: pts += 1
    if f is not None and f <= -cfg["funding_extreme_pct"]: pts += 2; why.append("shorts paying extreme")
    elif f is not None and f <= -cfg["funding_high_pct"]: pts += 1; why.append("shorts paying")
    la, lb = hit.get("liq_above_pct"), hit.get("liq_below_pct")
    if la is not None and la <= 5: pts += 2; why.append(f"short liqs {la}% overhead")
    if abs(hit.get("change_pct") or 0) >= cfg["heavy_move_pct"]: pts += 1; why.append("parabolic")
    label = "EXTREME" if pts >= 6 else "HIGH" if pts >= 4 else "MED" if pts >= 2 else "LOW"
    return {"short_risk": label, "short_risk_why": ", ".join(why)}


def dont_touch(hit: dict, cfg: dict) -> str | None:
    """SKR pattern: parabolic + low float + shorts paying heavily → shorts get hunted, then longs get flushed."""
    d = cfg["crypto"]["dont_touch"]
    chg, fl, f = _v(hit.get("change_pct")) or 0, _v(hit.get("float_pct")), _v(hit.get("funding_8h_pct"))
    if chg >= d["move_pct"] and fl is not None and fl < d["float_pct"] and f is not None and f <= d["funding_pct"]:
        return "DON'T TOUCH — low-float short hunt: shorts liquidated despite paying, longs late for the flush"
    if chg >= d["move_pct"] and hit.get("short_risk") == "EXTREME":
        return "DON'T TOUCH — parabolic with extreme short risk; no edge either side"
    return None


# ---------- 1–5 ranking ----------
# Each rule: column -> (higher_is_better, transform). Ranked by quintile across the current hit list.

RANK_RULES = {
    "move":        ("change_pct",        lambda s: s.abs()),
    "rvol":        ("rvol",              lambda s: s),
    "extension":   ("extension_avg_pct", lambda s: s),
    "funding":     ("funding_8h_pct",    lambda s: -s),          # more negative = more squeeze fuel = higher rank
    "liquidation": ("liq_above_pct",     lambda s: -s),          # nearer cluster above = higher rank
}


def rank_1_to_5(df: pd.DataFrame, weights: dict, cfg: dict) -> pd.DataFrame:
    if df.empty:
        return df
    score = pd.Series(0.0, index=df.index)
    max_possible = pd.Series(0.0, index=df.index)   # per-row, so stocks (no funding/liq) aren't buried
    for name, (col, tf) in RANK_RULES.items():
        if col not in df or df[col].notna().sum() == 0:
            continue
        s = tf(pd.to_numeric(df[col], errors="coerce"))
        r = s.rank(pct=True).apply(lambda p: min(5, max(1, int(p * 5 + 0.999))) if p == p else None)
        df[f"rank_{name}"] = r
        w = weights.get(name, 1.0)
        score += r.fillna(0) * w
        max_possible += r.notna() * 5 * w
    df["score"] = score.round(2)
    df["score_pct"] = (score / max_possible.replace(0, float("nan")) * 100).round(0)
    pot = pd.DataFrame([potential(r, cfg) for r in df.to_dict("records")], index=df.index)
    df[["side", "potential", "why"]] = pot
    df["act"] = df["potential"] >= 70
    return df.sort_values(["potential", "score_pct"], ascending=False).reset_index(drop=True)


# ---------- absolute trade-potential score (0–100) ----------
# Direction = sign of the move. Points only for evidence aligned with that direction.

def potential(hit: dict, cfg: dict) -> dict:
    hit = {k: _v(v) for k, v in hit.items()}
    chg = hit.get("change_pct") or 0
    side = "LONG" if chg > 0 else "SHORT"
    pts, why = 0, []
    rv = hit.get("rvol")
    if rv is not None:
        p = 25 if rv >= 5 else 15 if rv >= 3 else 5 if rv >= 2 else -10
        pts += p; why.append(f"RVOL {rv}x {p:+d}")
    a = abs(chg)
    p = 15 if 10 <= a < 30 else 10 if a >= 30 else 0
    pts += p; why.append(f"move {a:.0f}% {p:+d}")
    conf = hit.get("above_all") if side == "LONG" else hit.get("below_all")
    n = hit.get("tfs_confirming") or 0
    p = 20 if conf else 8 if n >= 2 else 0
    pts += p; why.append(f"EMA/VWAP {n}/3 TFs {p:+d}")
    f = hit.get("funding_8h_pct")
    if f is not None:
        hi, ex = cfg["crypto"]["funding_high_pct"], cfg["crypto"]["funding_extreme_pct"]
        aligned = -f if side == "LONG" else f          # LONG wants shorts paying (f<0); SHORT wants longs paying
        p = 20 if aligned >= ex else 12 if aligned >= hi else -12 if aligned <= -ex else -6 if aligned <= -hi else 0
        pts += p; why.append(f"funding {f:+.3f}% {p:+d}")
    fl = hit.get("float_pct")
    if fl is not None and fl < 30:
        p = 10 if side == "LONG" else -10
        pts += p; why.append(f"low float {p:+d}")
    liq = hit.get("liq_above_pct") if side == "LONG" else hit.get("liq_below_pct")
    opp = hit.get("liq_below_pct") if side == "LONG" else hit.get("liq_above_pct")
    if liq is not None and (opp is None or liq < opp):
        p = 10 if liq <= 5 else 5
        pts += p; why.append(f"liq cluster {liq}% ahead {p:+d}")
    if hit.get("news_link"):
        pts += 10; why.append("catalyst +10")
    if hit.get("impulse_vol_x"):
        p = 15 if hit["impulse_vol_x"] >= 5 else 10
        pts += p; why.append(f"impulse {hit['impulse_pct']}% on {hit['impulse_vol_x']}x vol {p:+d}")
    if side == "LONG" and hit.get("macd_long_ok"):
        pts += 10; why.append(f"MACD>0 & cross {hit['macd_cross_up_ago']} bars ago +10")
    elif side == "LONG" and hit.get("macd_positive") is False:
        pts -= 5; why.append("MACD<0 -5")
    dt = dont_touch(hit, cfg)
    if dt:
        return {"side": "NONE", "potential": 0, "why": dt}
    return {"side": side, "potential": max(0, min(100, pts)), "why": " · ".join(why)}


# ---------- risk checklist ----------

def checklist(hit: dict, cfg: dict) -> list[str]:
    hit = {k: _v(v) for k, v in hit.items()}
    flags = []
    chg, fl_pct, rv = hit.get("change_pct") or 0, hit.get("float_pct"), hit.get("rvol")
    heavy = abs(chg) >= cfg["crypto"]["heavy_move_pct"]
    if rv is not None and rv < cfg["rvol"]["shortlist_multiple"]:
        flags.append(f"⚠ RVOL {rv}x < {cfg['rvol']['shortlist_multiple']}x — move not volume-backed")
    if fl_pct is not None and fl_pct < 30:
        flags.append("🔴 Low float — squeeze risk for shorts / support for longs" + (" (weighted: heavy move)" if heavy else ""))
    if isinstance(hit.get("short_pct_float"), (int, float)) and hit["short_pct_float"] > 0.2:
        flags.append(f"🔥 Short interest {hit['short_pct_float']*100:.0f}% of float")
    f = hit.get("funding_8h_pct")
    if f is not None and f <= -cfg["crypto"]["funding_high_pct"]:
        flags.append(f"🔥 Negative funding {f:.3f}% — shorts paying, squeeze fuel")
    if f is not None and f >= cfg["crypto"]["funding_extreme_pct"]:
        flags.append(f"⚠ Extreme positive funding {f:.3f}% — longs crowded")
    if hit.get("above_all") is False and hit.get("extension_avg_pct") is not None:
        flags.append("⚠ Not above 9EMA+VWAP on all TFs")
    if not hit.get("news_link"):
        flags.append("⚠ No news catalyst found")
    if hit.get("macd_long_ok"):
        flags.append("✅ MACD positive + bullish cross")
    if hit.get("short_risk") in ("HIGH", "EXTREME"):
        flags.append(f"🚫 Short risk {hit['short_risk']}: {hit.get('short_risk_why')}")
    return flags
