"""Signal journal: freezes the original call, then tracks performance, excursions and checkpoints."""
import json, os, time
import pandas as pd

JOURNAL = "signals_journal.json"
CHECKPOINTS = {"15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
FROZEN = ["side", "potential", "price", "change_pct", "impulse_pct", "impulse_vol_x", "funding_8h_pct", "float_pct",
          "rvol", "macd_long_ok", "short_risk", "trigger", "why"]


def load() -> dict:
    if os.path.exists(JOURNAL):
        try:
            return {tuple(k.split("|", 1)): v for k, v in json.load(open(JOURNAL)).items()}
        except Exception:
            pass
    return {}


def save(store: dict):
    try:
        json.dump({"|".join(k): _jsonable(v) for k, v in store.items()}, open(JOURNAL, "w"))
    except Exception:
        pass


def _jsonable(row):
    out = {}
    for k, v in row.items():
        if isinstance(v, (list, dict, str, int, float, bool)) or v is None:
            out[k] = v if not (isinstance(v, float) and v != v) else None
        else:
            out[k] = str(v)
    return out


def signed(side, entry, px):
    if not entry or not px:
        return None
    raw = (px / entry - 1) * 100
    return round(-raw if side == "SHORT" else raw, 2)


def ingest(store: dict, new_rows: list[dict], now: float):
    """Add new signals; re-fires update live fields but never the frozen entry_* snapshot."""
    for r in new_rows:
        if r.get("ticker") == "SOURCE ERROR":
            continue
        key = (r.get("asset"), r.get("ticker"))
        if key in store:
            row = store[key]
            row.update({k: v for k, v in r.items() if k not in ("first_seen",)})
            row["fires"] = row.get("fires", 0) + 1
            row["last_seen"] = now
        else:
            row = dict(r)
            row.update({f"entry_{k}": r.get(k) for k in FROZEN})
            row.update(first_seen=now, last_seen=now, fires=1, mfe_pct=0.0, mae_pct=0.0, high=r.get("price"), low=r.get("price"))
            store[key] = row


def mark(store: dict, prices: dict, now: float, retain_hours: float):
    """Update current price, signed perf, MFE/MAE, checkpoints; prune expired."""
    for key in list(store):
        row = store[key]
        if now - row["first_seen"] > retain_hours * 3600:
            del store[key]; continue
        px = prices.get(key)
        if not px:
            continue
        side, entry = row.get("entry_side"), row.get("entry_price")
        row["cur_price"] = px
        row["perf_pct"] = signed(side, entry, px)
        row["raw_move_pct"] = round((px / entry - 1) * 100, 2) if entry else None
        row["high"] = max(row.get("high") or px, px); row["low"] = min(row.get("low") or px, px)
        row["mfe_pct"] = signed(side, entry, row["high"] if side != "SHORT" else row["low"])
        row["mae_pct"] = signed(side, entry, row["low"] if side != "SHORT" else row["high"])
        age = now - row["first_seen"]
        for name, secs in CHECKPOINTS.items():
            if age >= secs and row.get(f"perf_{name}") is None:
                row[f"perf_{name}"] = row["perf_pct"]


def effectiveness(df: pd.DataFrame) -> pd.DataFrame:
    """Win rate / avg return per checkpoint, by side, potential bucket, MACD."""
    if df.empty or "entry_side" not in df:
        return pd.DataFrame()
    d = df[df["entry_side"].isin(["LONG", "SHORT"])].copy()
    if d.empty:
        return pd.DataFrame()
    d["bucket"] = pd.cut(pd.to_numeric(d["entry_potential"], errors="coerce"), [-1, 49, 69, 100], labels=["<50", "50–69", "≥70"])
    d["macd"] = d["entry_macd_long_ok"].map({True: "MACD ✓", False: "MACD ✗"}).fillna("MACD ✗")
    groups = [("All", d), *[(f"Side {s}", g) for s, g in d.groupby("entry_side")],
              *[(f"Potential {b}", g) for b, g in d.groupby("bucket", observed=True)],
              *[(m, g) for m, g in d.groupby("macd")]]
    rows = []
    for name, g in groups:
        r = {"segment": name, "n": len(g)}
        for cp in ["15m", "1h", "4h", "24h"]:
            v = pd.to_numeric(g[f"perf_{cp}"], errors="coerce").dropna() if f"perf_{cp}" in g else pd.Series(dtype=float)
            r[f"win {cp}"] = f"{(v > 0).mean() * 100:.0f}%" if len(v) else "—"
            r[f"avg {cp}"] = f"{v.mean():+.1f}%" if len(v) else "—"
        for k in ("mfe_pct", "mae_pct"):
            v = pd.to_numeric(g[k], errors="coerce").dropna() if k in g else pd.Series(dtype=float)
            r["avg " + k[:3].upper()] = f"{v.mean():+.1f}%" if len(v) else "—"
        rows.append(r)
    return pd.DataFrame(rows)
