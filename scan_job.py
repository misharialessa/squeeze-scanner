"""Headless scan — run by GitHub Actions every 5 minutes. Persists ledger + journal to the `journal` branch.
Needs GITHUB_TOKEN, GITHUB_REPO; optional CMC/CRYPTOPANIC/FINNHUB keys."""
import json, os, sys, time
import yaml, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import persist, tracker, fit
from core.scan import run
from core.metrics import rank_1_to_5
from sources import hyperliquid as hl, stocks as stock_src

LEDGER_PATH = "ledger.json"


def load_ledger() -> dict:
    raw = persist.load_remote(LEDGER_PATH) or {}
    return {(tuple(k.split("|")) if "|" in k else k): [tuple(x) for x in v] for k, v in raw.items()}


def save_ledger(ledger: dict):
    now = time.time()
    raw = {("|".join(k) if isinstance(k, tuple) else k): [(round(t), p) for t, p in v if now - t <= 4000] for k, v in ledger.items()}
    err = persist.push_remote(raw, force=True, path=LEDGER_PATH)
    if err: print("ledger push:", err)


STATUS_PATH = "last_run.json"


def cycle(cfg, store, ledger, push: bool) -> dict:
    """One scan cycle (one app refresh). Commits journal/ledger/status only when push=True."""
    diag, t0 = {}, time.time()
    new = run(cfg, ledger, diag)
    now = time.time()
    rows = [] if new.empty else new.to_dict("records")
    if rows:
        for r in rows:
            prev = store.get((r.get("asset"), r.get("ticker")), {})
            r["impulses"] = len(set(prev.get("impulse_times") or []) | ({str(r["impulse_time"])} if r.get("impulse_time") is not None else set()))
        rows = rank_1_to_5(pd.DataFrame(rows), cfg["weights"], cfg).to_dict("records")
    tracker.ingest(store, rows, now)
    prices, btc = {}, None
    try:
        mids = hl.all_mids(); btc = mids.get("BTC")
        prices.update({k: mids[k[1]] for k in store if k[0] == "crypto" and k[1] in mids})
    except Exception as e:
        print("mids:", e)
    try:
        live_stocks = [k[1] for k in store if k[0] == "stock" and not store[k].get("archived")]
        prices.update({("stock", t): p for t, p in stock_src.last_prices(live_stocks).items()})
    except Exception as e:
        print("stock prices:", e)
    tracker.mark(store, prices, now, cfg["retain_hours"], btc)
    status = {"ts": now, "new": len(rows), "journal": len(store), "secs": round(time.time() - t0),
              "diag": {k: v for k, v in diag.items() if not isinstance(v, list)},
              "near": (diag.get("near") or [])[:10], "stock_near": (diag.get("stock_near") or [])[:10], "push_err": None}
    if push:
        status["push_err"] = tracker.save(store, force_remote=True)
        save_ledger(ledger)
        persist.push_remote(status, force=True, path=STATUS_PATH)
    else:
        tracker.save(store)
    print(json.dumps({k: v for k, v in status.items() if k not in ("near", "stock_near")}, default=str), flush=True)
    return status


def daily_fit(store, now):
    if time.gmtime(now).tm_hour == 3 and time.gmtime(now).tm_min < 2 and len(store) >= 100:
        try:
            rep, val = fit.run(pd.DataFrame(list(store.values())), targets=("win", "tp"))
            print(rep.to_string(index=False))
            persist.push_remote(json.loads(rep.to_json(orient="records")), force=True, path="fit_report.json")
            if val:
                fit.save(val)
                persist.push_remote(json.load(open(fit.MODEL_FILE)), force=True, path=fit.MODEL_FILE)
                print("validated:", list(val))
        except Exception as e:
            print("fit:", e)


def main():
    cfg = yaml.safe_load(open("config.yaml"))
    minutes = float(os.getenv("RUN_MINUTES", "345"))          # under GitHub's 6h job limit
    every = int(os.getenv("CYCLE_SECONDS", str(cfg.get("refresh_seconds", 60))))
    store, ledger = tracker.load(), load_ledger()
    t_end, i = time.time() + minutes * 60, 0
    while time.time() < t_end:
        t0 = time.time()
        try:
            cycle(cfg, store, ledger, push=(i % 5 == 0))      # commit every ~5 min
            daily_fit(store, time.time())
        except Exception as e:
            print("cycle error:", repr(e), flush=True)
        i += 1
        time.sleep(max(5, every - (time.time() - t0)))
    try:
        cycle(cfg, store, ledger, push=True)                  # final commit before hand-off
    except Exception as e:
        print("final cycle error:", repr(e), flush=True)
    tok, repo = os.getenv("ACTIONS_TOKEN"), os.getenv("CODE_REPO")
    if tok and repo:                                          # start the next run right away; hourly schedule is the backup
        import requests
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/scan.yml/dispatches",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                          json={"ref": "main"}, timeout=20)
        print("hand-off:", r.status_code, flush=True)


if __name__ == "__main__":
    main()
