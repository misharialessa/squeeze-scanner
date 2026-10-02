"""Journal persistence to a GitHub branch (survives Streamlit Cloud redeploys). Needs GITHUB_TOKEN + GITHUB_REPO."""
import base64, json, os, time, requests

BRANCH = "journal"
PATH = "signals_journal.json"
_last_push = {"t": 0.0}


def _cfg():
    tok, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO")
    return (tok, repo) if tok and repo else (None, None)


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}


def _ensure_branch(tok, repo):
    api = f"https://api.github.com/repos/{repo}"
    if requests.get(f"{api}/git/ref/heads/{BRANCH}", headers=_hdr(tok), timeout=15).status_code == 200:
        return
    main = requests.get(f"{api}", headers=_hdr(tok), timeout=15).json().get("default_branch", "main")
    sha = requests.get(f"{api}/git/ref/heads/{main}", headers=_hdr(tok), timeout=15).json()["object"]["sha"]
    requests.post(f"{api}/git/refs", headers=_hdr(tok), json={"ref": f"refs/heads/{BRANCH}", "sha": sha}, timeout=15)


_last_load: dict = {}          # path -> human-readable result of the last load attempt (for diagnostics)


def load_remote(path: str = PATH) -> dict | None:
    tok, repo = _cfg()
    if not tok:
        _last_load[path] = "no token/repo configured"; return None
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", params={"ref": BRANCH}, headers=_hdr(tok), timeout=15)
        if r.status_code != 200:
            _last_load[path] = f"HTTP {r.status_code} for {repo}@{BRANCH}/{path}: {r.text[:120]}"; return None
        meta = r.json()
        if meta.get("content"):
            data = json.loads(base64.b64decode(meta["content"]))
        else:                                              # >1 MB: API omits inline content → use the raw download URL
            raw = requests.get(meta["download_url"], headers=_hdr(tok), timeout=30)
            raw.raise_for_status(); data = raw.json()
        _last_load[path] = f"ok ({meta.get('size', '?')} bytes)"
        return data
    except Exception as e:
        _last_load[path] = f"error: {str(e)[:150]}"; return None


def load_status(path: str = PATH) -> str:
    return _last_load.get(path, "not attempted")


def push_remote(data: dict, min_interval: int = 300, force: bool = False, path: str = PATH) -> str | None:
    """Commit a JSON file to the journal branch; throttled unless force. Returns an error string or None."""
    tok, repo = _cfg()
    if not tok:
        return "no GITHUB_TOKEN/GITHUB_REPO"
    if not force and path == PATH and time.time() - _last_push["t"] < min_interval:
        return None
    try:
        _ensure_branch(tok, repo)
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        cur = requests.get(url, params={"ref": BRANCH}, headers=_hdr(tok), timeout=15)
        body = {"message": f"journal {time.strftime('%Y-%m-%d %H:%M')}", "branch": BRANCH,
                "content": base64.b64encode(json.dumps(data).encode()).decode()}
        if cur.status_code == 200:
            body["sha"] = cur.json()["sha"]
        r = requests.put(url, headers=_hdr(tok), json=body, timeout=20)
        r.raise_for_status()
        if path == PATH: _last_push["t"] = time.time()
        return None
    except Exception as e:
        return str(e)[:160]


def status() -> str:
    tok, repo = _cfg()
    if not tok:
        return "GitHub journal: not configured (journal resets on redeploy)"
    age = time.time() - _last_push["t"]
    return f"GitHub journal: {repo}@{BRANCH} · last push {int(age/60)} min ago" if _last_push["t"] else f"GitHub journal: {repo}@{BRANCH} · not pushed yet"
