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
from core.indicators import extension_pct, macd_state, context

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


@metric("context")
def m_context(hit, ctx):
    return context(ctx.get("bars_5m"), ctx["config"]["impulse"]["candles"])


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
    """IGNITION score — uses only what is known at the first fire. Weights are hypotheses to be validated by the journal."""
    hit = {k: _v(v) for k, v in hit.items()}
    chg = hit.get("change_pct") or 0
    side = "LONG" if (hit.get("impulse_pct") or chg) > 0 else "SHORT"
    pts, why = 0, []

    def add(p, label):
        nonlocal pts
        pts += p; why.append(f"{label} {p:+d}")

    # 1) where in the range did it fire
    if hit.get("new_24h_high"): add(20, "new 24h high")
    elif hit.get("range_pos_24h") is not None:
        rp = hit["range_pos_24h"]
        add(10 if rp >= 0.8 else -10 if rp < 0.5 else 0, f"range pos {rp:.2f}")
    # 2) compression before the spike
    c = hit.get("compression")
    if c is not None: add(15 if c < 0.6 else 5 if c < 0.9 else -5 if c > 1.3 else 0, f"pre-spike vol {c:.2f}x")
    # 3) is this the first big candle or the 15th
    vr = hit.get("vol_rank_24h")
    if vr is not None: add(15 if vr == 1 else 8 if vr <= 3 else -10 if vr > 10 else 0, f"vol rank #{vr}/24h")
    # 4) stage of the move
    s24 = hit.get("chg_24h_at_fire_pct")
    if s24 is not None: add(10 if abs(s24) < 5 else 0 if abs(s24) < 20 else -10, f"24h stage {s24:+.0f}%")
    # 5) positioning: OI change last hour
    oi = hit.get("oi_chg_1h_pct")
    if oi is not None: add(10 if oi >= 2 else 0 if oi > -2 else -5, f"OI 1h {oi:+.1f}%")
    # 6) idiosyncratic vs beta
    b = hit.get("btc_move_pct")
    if b is not None: add(-10 if abs(b) >= 0.5 else 5, f"BTC same window {b:+.2f}%")
    # 7) trend context (journal: fresh VWAP reclaim = chop, established = works)
    v1 = hit.get("vwap_1h_pct")
    if v1 is not None and side == "LONG": add(10 if v1 > 3 else 3 if v1 > 1 else -10 if v1 > 0 else 0, f"1h VWAP {v1:+.1f}%")
    conf = hit.get("above_all") if side == "LONG" else hit.get("below_all")
    add(8 if conf else 0, f"EMA/VWAP {hit.get('tfs_confirming') or 0}/6")
    # 8) impulse quality
    ip, iv = hit.get("impulse_pct"), hit.get("impulse_vol_x")
    if ip is not None: add(10 if abs(ip) >= 1.5 else 0, f"impulse {ip:.2f}%")
    if iv is not None: add(8 if iv >= 5 else 0, f"on {iv:.1f}x vol")
    # 9) funding aligned
    f = hit.get("funding_8h_pct")
    if f is not None:
        hi_, ex = cfg["crypto"]["funding_high_pct"], cfg["crypto"]["funding_extreme_pct"]
        aligned = -f if side == "LONG" else f
        add(12 if aligned >= ex else 6 if aligned >= hi_ else -8 if aligned <= -ex else 0, f"funding {f:+.3f}%")
    fl = hit.get("float_pct")
    if fl is not None and fl < 30: add(5 if side == "LONG" else -10, "low float")
    if hit.get("news_link"): add(5, "catalyst")
    if hit.get("macd_long_ok"): why.append("MACD✓ (0, tracked)")

    dt = dont_touch(hit, cfg)
    if dt:
        return {"side": "NONE", "potential": 0, "why": dt}
    if side == "SHORT":
        return {"side": "SHORT (watch)", "potential": min(40, max(0, pts)), "why": "watch-only · " + " · ".join(why)}
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
