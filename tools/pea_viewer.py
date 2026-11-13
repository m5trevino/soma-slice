#!/usr/bin/env python3
"""PEA log deck — browse today's manifest, pull files on demand.

The engine publishes one manifest per day:

    https://herbert.save-aichats.com/pea/<YYYY-MM-DD>-manifest.json

A new one appears after midnight on the first API call of the day. The
manifest is the index; nothing is listed from disk. Every file it carries is
visitable or downloadable, and this tool never pulls in the background:

  * Pressing Read on a file scp's it into the local archive, then shows it.
  * Pressing Pull All downloads every file in the day, one at a time.
  * A file already saved locally is never transferred again.

So the manifest is the source of truth for *what exists*, and the local
archive is a cache of *what has been read*.

Usage:
    python3 tools/pea_viewer.py
    python3 tools/pea_viewer.py --no-open --port 5199
    python3 tools/pea_viewer.py --day 2026-11-02
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

MANIFEST_BASE = os.environ.get(
    "PEA_MANIFEST_BASE",
    "https://herbert.save-aichats.com/pea",
)
# Matches the ssh alias pea_sync.py already uses.
REMOTE = os.environ.get("PEA_REMOTE", "engine")
REMOTE_ROOT = "/root/hetzner/ai-engine/pea_sessions"

ARCHIVE = Path(
    os.environ.get("PEA_ROOT", Path.home() / "pea-sessions")
).expanduser().resolve()

DEFAULT_PORT = int(os.environ.get("PEA_VIEWER_PORT", "9893"))

# Set by --day. When empty the viewer follows the calendar and rolls over at
# midnight; when set, every endpoint reads and pulls that day's manifest.
PINNED_DAY = ""

# Manifest cache. The day's manifest is rewritten as runs land, all day long,
# so the copy is only kept long enough to absorb rapid clicks. A manual
# refresh still forces a refetch.
MANIFEST_TTL = 30

FILE_RE = re.compile(
    r"^(?P<stamp>\d{2}-\d{2}-\d{2}-\d{2}\d{2}(?:AM|PM))"
    r"-(?P<stage>[A-Za-z0-9]+)"
    r"-(?P<status>SUCCESS|FAIL)"
    r"(?:-(?P<dup>\d+))?\.txt$"
)

app = FastAPI(title="PEA Log Deck", docs_url=None, redoc_url=None)

_state: dict = {"day": None, "manifest": None, "fetched": 0.0}
_lock = threading.Lock()
_pull = {"running": False, "done": 0, "total": 0, "ok": 0, "fail": 0,
         "current": "", "log": [], "finished": False, "active": False}


# ── manifest ─────────────────────────────────────────────────────────────

def _today() -> str:
    return _dt.date.today().isoformat()


def _day(day: str = "") -> str:
    """The day a request is about: explicit, else --day, else today."""
    day = day or PINNED_DAY or _today()
    try:
        _dt.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad day {day!r}, want YYYY-MM-DD")
    return day


def _manifest_url(day: str) -> str:
    return f"{MANIFEST_BASE}/{day}-manifest.json"


def fetch_manifest(day: str | None = None, force: bool = False) -> dict:
    """Load a day's manifest, caching it in memory for MANIFEST_TTL."""
    day = day or _today()
    with _lock:
        fresh = (
            _state["day"] == day
            and _state["manifest"] is not None
            and (time.time() - _state["fetched"]) < MANIFEST_TTL
        )
        if fresh and not force:
            return _state["manifest"]

    url = _manifest_url(day)
    try:
        req = urllib.request.Request(
            url, headers={"Cache-Control": "no-cache", "User-Agent": "pea-deck"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read())
    except Exception as exc:
        # A missing manifest before midnight is normal, not an error state:
        # the file for today only exists once the first call has happened.
        raise HTTPException(
            status_code=503,
            detail=f"No manifest for {day} yet ({type(exc).__name__}). "
                   f"A new one appears after midnight on the first API call.",
        )

    files = raw.get("files") or []
    for f in files:
        name = f.get("file", "")
        meta = _parse_name(name)
        f["status"] = meta["status"]
        f["stage"] = meta["stage"]
        f["stamp"] = meta["stamp"]
        f["pretty"] = meta["pretty"]
        f["dir_name"] = (f.get("dir") or "").rstrip("/").split("/")[-1]
        f["local"] = str(ARCHIVE / f["dir_name"] / name)
        f["saved"] = Path(f["local"]).is_file()
        f["sort_at"] = _file_sort_at(f)

    with _lock:
        _state.update({"day": day, "manifest": raw, "fetched": time.time()})
    return raw


def _parse_name(name: str) -> dict:
    m = FILE_RE.match(name)
    if not m:
        return {"stamp": "", "pretty": name, "stage": "unknown", "status": "UNKNOWN"}
    d = m.groupdict()
    return {
        "stamp": d["stamp"],
        "pretty": _pretty(d["stamp"]),
        "stage": d["stage"],
        "status": "OK" if d["status"] == "SUCCESS" else "FAIL",
    }


def _pretty(stamp: str) -> str:
    """11-03-26-0340AM -> 2026-11-03 03:40 AM"""
    m = re.match(r"^(\d{2})-(\d{2})-(\d{2})-(\d{2})(\d{2})(AM|PM)$", stamp)
    if not m:
        return stamp
    mo, dy, yr, hh, mi, ap = m.groups()
    return f"20{yr}-{mo}-{dy} {hh}:{mi} {ap}"


# ── ordering ─────────────────────────────────────────────────────────────
#
# Stamps are 12-hour clock text, so they cannot be sorted as strings: 0100PM
# would land before 0200AM. Every file and run gets a sort_at instead, an ISO
# local time that does sort as a string. Names that do not carry a stamp fall
# back to the manifest's created time, then to the saved copy's mtime.

def _stamp_dt(stamp: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(stamp, "%m-%d-%y-%I%M%p")
    except ValueError:
        return None


def _file_sort_at(f: dict) -> str:
    at = _stamp_dt(f.get("stamp", ""))
    if at is None and f.get("created"):
        try:
            created = _dt.datetime.fromisoformat(f["created"].replace("Z", "+00:00"))
            at = created.astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    if at is None and f.get("saved"):
        at = _dt.datetime.fromtimestamp(Path(f["local"]).stat().st_mtime)
    return at.isoformat(timespec="seconds") if at else ""


def _file_key(f: dict) -> tuple:
    # created breaks ties inside a minute, so retries stay in the order made.
    return (f.get("sort_at", ""), f.get("created", ""), f.get("file", ""))


def _run_sort_at(dir_name: str, files: list[dict]) -> str:
    """A run is ordered by the stamp in its directory name, else its newest file."""
    m = re.match(r"^(\d{2}-\d{2}-\d{2}-\d{4}(?:AM|PM))-", dir_name)
    at = _stamp_dt(m.group(1)) if m else None
    if at:
        return at.isoformat(timespec="seconds")
    return max((f.get("sort_at", "") for f in files), default="")


def _latest_run(files: list[dict]) -> str:
    runs: dict[str, list[dict]] = {}
    for f in files:
        runs.setdefault(f["dir_name"], []).append(f)
    return max(runs, key=lambda d: (_run_sort_at(d, runs[d]), d))


# ── pulling ──────────────────────────────────────────────────────────────

def _scp(remote_path: str, local: Path) -> tuple[bool, str]:
    """Copy one file from the engine. Never overwrites a saved file."""
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.is_file():
        return True, "already saved"
    started = time.monotonic()
    result = subprocess.run(
        ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         f"{REMOTE}:{remote_path}", str(local)],
        capture_output=True, text=True, timeout=120,
    )
    took = time.monotonic() - started
    # Wall time per transfer, printed to the viewer's terminal, so first-read
    # latency can be measured before anyone changes how files are fetched.
    size = local.stat().st_size if local.is_file() else 0
    print(f"  scp {took:5.2f}s rc={result.returncode} {size}B {local.parent.name}/{local.name}",
          flush=True)
    if result.returncode == 0 and local.is_file():
        return True, f"pulled in {took:.1f}s"
    err = (result.stderr or "").strip().splitlines()
    return False, (err[-1] if err else "scp failed")[:200]


def _log(msg: str) -> None:
    _pull["log"].append(msg)
    if len(_pull["log"]) > 400:
        del _pull["log"][:-400]


def _pull_one(entry: dict) -> tuple[bool, str]:
    local = Path(entry["local"])
    ok, note = _scp(entry["path"], local)
    entry["saved"] = local.is_file()
    return ok, note


def _pull_all(entries: list[dict]) -> None:
    _pull.update({"running": True, "active": True, "finished": False,
                  "done": 0, "total": len(entries), "ok": 0, "fail": 0,
                  "current": "", "log": []})
    _log(f"pulling {len(entries)} file(s) from {REMOTE}")
    for entry in entries:
        _pull["current"] = entry["file"]
        ok, note = _pull_one(entry)
        _pull["done"] += 1
        if ok:
            _pull["ok"] += 1
            _log(f"✓ {entry['dir_name']}/{entry['file']} — {note}")
        else:
            _pull["fail"] += 1
            _log(f"✗ {entry['dir_name']}/{entry['file']} — {note}")
    _pull["running"] = False
    _pull["finished"] = True
    _pull["current"] = ""
    _log(f"done — {_pull['ok']} saved, {_pull['fail']} failed")


# ── API ──────────────────────────────────────────────────────────────────

@app.get("/api/manifest")
def api_manifest(day: str = "", refresh: bool = False):
    """The day's manifest with run grouping and local-saved state."""
    day = _day(day)
    raw = fetch_manifest(day, force=refresh)

    files = raw.get("files", [])
    runs: dict[str, dict] = {}
    for f in files:
        key = f["dir_name"]
        r = runs.setdefault(key, {
            "run": key, "dir": f.get("dir", ""), "files": [],
            "ok": 0, "fail": 0, "saved": 0, "first": f.get("created", ""),
        })
        r["files"].append(f)
        if f["status"] == "OK":
            r["ok"] += 1
        elif f["status"] == "FAIL":
            r["fail"] += 1
        if f["saved"]:
            r["saved"] += 1

    for r in runs.values():
        r["files"].sort(key=_file_key, reverse=True)
        r["total"] = len(r["files"])
        r["label"] = _run_label(r["run"])
        r["sort_at"] = _run_sort_at(r["run"], r["files"])

    ordered = sorted(runs.values(), key=lambda r: (r["sort_at"], r["run"]), reverse=True)
    flat = sorted(files, key=_file_key, reverse=True)

    return {
        "day": day,
        "pinned": bool(PINNED_DAY),
        "updated": raw.get("updated", ""),
        "count": raw.get("count", len(files)),
        "runs": ordered,
        "files": flat,
        "saved_total": sum(1 for f in files if f["saved"]),
        "archive": str(ARCHIVE),
        "remote": REMOTE,
    }


def _run_label(dir_name: str) -> str:
    """11-03-26-0342AM-250612-1096 -> "03:42 AM · 250612-1096"."""
    m = re.match(r"^(\d{2}-\d{2}-\d{2}-\d{2}\d{2}(?:AM|PM))-(.+)$", dir_name)
    if not m:
        return dir_name
    stamp, rest = m.groups()
    p = _pretty(stamp)
    return f"{p[11:]} · {rest}"


@app.get("/api/file")
def api_file(path: str = "", dir: str = "", file: str = ""):
    """Read one file, pulling it first if it is not already saved."""
    if not file:
        raise HTTPException(status_code=400, detail="file required")
    dir_name = (dir or "").rstrip("/").split("/")[-1] or "unknown"
    local = ARCHIVE / dir_name / file

    if not local.is_file():
        remote = path or f"{REMOTE_ROOT}/{dir_name}/{file}"
        ok, note = _scp(remote, local)
        if not ok:
            raise HTTPException(status_code=502, detail=f"pull failed: {note}")
    else:
        note = "already saved"

    text = local.read_text(errors="ignore")
    return JSONResponse({
        "file": file,
        "dir": dir_name,
        "local": str(local),
        "bytes": local.stat().st_size,
        "note": note,
        "text": text,
    })


@app.get("/api/pull", )
def api_pull(dir: str = "", day: str = ""):
    """Start pulling a whole run (all its files). Returns immediately."""
    if _pull["running"]:
        return {"started": False, "reason": "already running"}
    raw = fetch_manifest(_day(day))
    entries = [f for f in raw.get("files", [])
               if not dir or f["dir_name"] == dir]
    if not entries:
        raise HTTPException(status_code=404, detail="nothing to pull")
    threading.Thread(target=_pull_all, args=(entries,), daemon=True).start()
    return {"started": True, "total": len(entries)}


@app.get("/api/pull-all")
def api_pull_all(latest: bool = False, day: str = ""):
    """Pull every file in the day, or only the newest run's files."""
    if _pull["running"]:
        return {"started": False, "reason": "already running"}
    raw = fetch_manifest(_day(day))
    files = raw.get("files", [])
    if latest and files:
        newest = _latest_run(files)
        files = [f for f in files if f["dir_name"] == newest]
    if not files:
        raise HTTPException(status_code=404, detail="nothing to pull")
    threading.Thread(target=_pull_all, args=(files,), daemon=True).start()
    return {"started": True, "total": len(files)}


@app.get("/api/pull/status")
def api_pull_status():
    return {
        "running": _pull["running"],
        "finished": _pull["finished"],
        "done": _pull["done"],
        "total": _pull["total"],
        "ok": _pull["ok"],
        "fail": _pull["fail"],
        "current": _pull["current"],
        "log": _pull["log"][-120:],
    }


@app.get("/api/download")
def api_download(path: str = "", dir: str = "", file: str = ""):
    """Copy a file into the archive if needed and hand back its local path."""
    if not file:
        raise HTTPException(status_code=400, detail="file required")
    dir_name = (dir or "").rstrip("/").split("/")[-1] or "unknown"
    local = ARCHIVE / dir_name / file
    if not local.is_file():
        ok, note = _scp(path or f"{REMOTE_ROOT}/{dir_name}/{file}", local)
        if not ok:
            raise HTTPException(status_code=502, detail=f"pull failed: {note}")
    return {"local": str(local), "bytes": local.stat().st_size}


@app.post("/api/open")
def api_open(body: dict):
    """Open a saved file with the desktop default handler."""
    local = Path(body.get("local", ""))
    if not local.is_file():
        raise HTTPException(status_code=404, detail="not saved locally")
    subprocess.Popen(["xdg-open", str(local)])
    return {"opened": str(local)}


# ── page ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>PEA Log Deck</title>
<style>
/* ══════════════════════════════════════════════════════════════════════
   PEA LOG DECK — brushed aluminium, leather sides, amber LEDs
   ══════════════════════════════════════════════════════════════════════ */
:root {
  --ink: #e8edf2; --dim: #8b949e;
  --orange: #ff5a00; --orange-hot: #ff8c1a; --amber: #ffb020;
  --green: #3ddc84; --red: #ff3b30;
  --leather: #2b1f16; --leather-hi: #3a2a1f;
  --alu: linear-gradient(180deg,#d0d3d7 0%,#b9bdc2 40%,#a2a7ad 70%,#8a8f96 100%);
  --grain: repeating-linear-gradient(0deg,
      rgba(255,255,255,.022) 0 1px, rgba(0,0,0,.012) 1px 2px,
      rgba(255,255,255,.010) 2px 3px);
}
* { box-sizing: border-box; }
html, body {
  margin: 0; min-height: 100%; color: var(--ink);
  font-family: "Inter", -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 13px; background: #0a0b0d;
}
body { padding: 18px 30px 40px; }
body::before {
  content: ""; position: fixed; inset: 0; z-index: -2;
  background: var(--grain), var(--alu);
}
body::after {
  content: ""; position: fixed; top: 0; bottom: 0; left: 0; width: 24px; z-index: -1;
  background:
    repeating-linear-gradient(38deg, rgba(0,0,0,.22) 0 1px, transparent 1px 4px),
    linear-gradient(90deg, var(--leather-hi), var(--leather));
  box-shadow: inset 0 0 0 1px rgba(0,0,0,.6);
}

.shell { max-width: 1500px; margin: 0 auto; }

/* ── Faceplate ── */
.plate {
  border-radius: 10px; padding: 14px 18px; margin-bottom: 14px;
  border: 1px solid #05070a;
  background:
    repeating-linear-gradient(90deg, rgba(255,255,255,.025) 0 1px, transparent 1px 3px),
    linear-gradient(180deg,#343c46 0%,#232a32 100%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.13), 0 4px 14px rgba(0,0,0,.5);
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
}
.brand h1 {
  margin: 0; font-size: 13px; letter-spacing: .28em; text-transform: uppercase;
  font-weight: 800; color: #dfe6ee;
  text-shadow: 0 1px 0 #000, 0 0 14px rgba(255,90,0,.25);
}
.brand .sub {
  margin-top: 4px; font-size: 9.5px; letter-spacing: .1em; text-transform: uppercase;
  color: #7a838e; font-family: "SF Mono","Consolas",monospace;
}

/* ── LCD ── */
.lcd {
  display: flex; gap: 20px; padding: 9px 16px; border-radius: 6px;
  border: 1px solid #05070a; font-family: "SF Mono","Consolas",monospace;
  background:
    repeating-linear-gradient(0deg, rgba(120,255,190,.02) 0 1px, transparent 1px 3px),
    linear-gradient(180deg,#08110d,#050a08);
  box-shadow: inset 0 3px 12px rgba(0,0,0,.95), 0 1px 0 rgba(255,255,255,.12);
}
.lcd-item { display: flex; flex-direction: column; gap: 2px; }
.lcd-label { font-size: 8px; letter-spacing: .18em; text-transform: uppercase; color: #4c6b5b; }
.lcd-value {
  font-size: 15px; font-weight: 800; font-variant-numeric: tabular-nums;
  color: var(--amber); text-shadow: 0 0 10px rgba(255,176,32,.55);
}
.lcd-value.cool { color: #6fe3b0; text-shadow: 0 0 10px rgba(61,220,132,.45); }
.spacer { flex: 1; }

/* ── Physical rocker switch ── */
.rocker {
  display: inline-flex; padding: 3px; border-radius: 8px; border: 1px solid #05070a;
  background: linear-gradient(180deg,#101419,#1c222a);
  box-shadow: inset 0 2px 6px rgba(0,0,0,.9), 0 1px 0 rgba(255,255,255,.09);
}
.rocker-slot {
  position: relative; border-radius: 6px; padding: 2px; display: flex;
  background: linear-gradient(180deg,#05070a,#0d1116);
}
.rocker-knob {
  position: absolute; top: 2px; bottom: 2px; width: calc(50% - 2px); border-radius: 5px;
  background: linear-gradient(180deg,#6a7480 0%,#495462 45%,#39424e 52%,#232a33 100%);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.45),
    inset 0 -2px 3px rgba(0,0,0,.5),
    0 2px 5px rgba(0,0,0,.65);
  transition: transform .18s cubic-bezier(.4,0,.2,1), filter .18s;
  pointer-events: none;
}
.rocker-knob.right { transform: translateX(100%); }
.rocker button {
  position: relative; z-index: 1; width: 116px; padding: 8px 10px;
  font-size: 9.5px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase;
  color: #6f7b88; background: none; border: none; cursor: pointer;
  transition: color .18s;
}
.rocker button.on { color: #0f1317; text-shadow: 0 1px 0 rgba(255,255,255,.35); }

/* ── Keys ── */
.key {
  display: inline-flex; align-items: center; gap: 7px; padding: 9px 15px;
  font-size: 10px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase;
  cursor: pointer; color: #1a1d21; border-radius: 4px; border: 1px solid #4c5158;
  background: linear-gradient(180deg,#f2f4f6 0%,#d3d7dc 45%,#b7bcc3 52%,#9aa0a8 100%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.9), inset 0 -2px 3px rgba(0,0,0,.16),
              0 2px 5px rgba(0,0,0,.45);
  text-shadow: 0 1px 0 rgba(255,255,255,.65);
}
.key:hover { filter: brightness(1.07); }
.key:active { transform: translateY(1px); box-shadow: inset 0 2px 4px rgba(0,0,0,.3); }
.key:disabled { filter: grayscale(.75) brightness(.75); cursor: default; transform: none; }
.key.lit {
  color: #2b0805; border-color: #7a1a12;
  background: linear-gradient(180deg,#ff9a92,#ff5a4f 45%,#e0362b 52%,#a51f16);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.45), 0 2px 6px rgba(0,0,0,.5),
              0 0 14px rgba(255,59,48,.35);
}
select.key { appearance: none; padding-right: 26px;
  background-image: linear-gradient(45deg, transparent 50%, #1a1d21 50%),
                    linear-gradient(135deg, #1a1d21 50%, transparent 50%),
                    linear-gradient(180deg,#f2f4f6 0%,#d3d7dc 45%,#b7bcc3 52%,#9aa0a8 100%);
  background-position: calc(100% - 13px) 50%, calc(100% - 9px) 50%, 0 0;
  background-size: 4px 4px, 4px 4px, 100% 100%; background-repeat: no-repeat; }
.key.orange {
  color: #2b1200; border-color: #7a2c00;
  background: linear-gradient(180deg,#ffa34d,#ff7a1a 45%,#f06000 52%,#b84600);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.45), 0 2px 6px rgba(0,0,0,.5),
              0 0 16px rgba(255,90,0,.3);
}

/* ── Gauge ── */
.gauge {
  height: 10px; border-radius: 5px; border: 1px solid #05070a; overflow: hidden;
  background: #05070a; box-shadow: inset 0 2px 5px rgba(0,0,0,.95);
}
.gauge-fill {
  height: 100%; width: 0%; transition: width .25s ease, background .25s;
  background: linear-gradient(180deg,#ffc874,#ff8c1a 50%,#e05f00);
  box-shadow: 0 0 14px rgba(255,140,26,.6);
}
.gauge-fill.done-ok { background: linear-gradient(180deg,#8ef7bd,#3ddc84 50%,#1a9c58); box-shadow: 0 0 14px rgba(61,220,132,.6); }
.gauge-fill.done-fail { background: linear-gradient(180deg,#ff9a92,#ff3b30 50%,#b81f16); box-shadow: 0 0 14px rgba(255,59,48,.6); }

/* ── Tape well (pull log) ── */
.well {
  margin-top: 12px; border-radius: 8px; border: 1px solid #05070a;
  background: linear-gradient(180deg,#0a0f0d,#060907);
  box-shadow: inset 0 4px 14px rgba(0,0,0,.95), 0 1px 0 rgba(255,255,255,.1);
  overflow: hidden;
}
.well-head {
  padding: 7px 12px; font-size: 9px; letter-spacing: .16em; text-transform: uppercase;
  color: #7f8b99; border-bottom: 1px solid #05070a;
  background: linear-gradient(180deg,#20262e,#171b21);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.08);
  display: flex; justify-content: space-between;
}
.well-body {
  max-height: 190px; overflow-y: auto; padding: 9px 12px;
  font-family: "SF Mono","Consolas",monospace; font-size: 10.5px; line-height: 1.65;
  color: #7fbfa0;
}
.well-body .ok { color: #6ef2a8; }
.well-body .bad { color: #ff7a72; }

/* ── Cards (By run) ── */
.card {
  border-radius: 8px; margin-bottom: 10px; overflow: hidden;
  border: 1px solid #05070a;
  background: linear-gradient(180deg,#20262e,#161b21);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.07), 0 3px 10px rgba(0,0,0,.5);
}
.card-head {
  display: flex; align-items: center; gap: 12px; padding: 11px 14px; cursor: pointer;
  border-left: 3px solid rgba(255,255,255,.07);
  transition: background .14s, border-left-color .14s;
}
.card-head:hover { background: rgba(255,90,0,.05); }
.card.open .card-head { border-left-color: var(--orange); background: rgba(255,90,0,.08); }
.caret {
  width: 15px; height: 15px; flex-shrink: 0; display: grid; place-items: center;
  color: #6f7b88; font-size: 9px;
  transition: transform .2s cubic-bezier(.4,0,.2,1), color .2s;
}
.card.open .caret { transform: rotate(90deg); color: var(--orange-hot); text-shadow: 0 0 9px rgba(255,90,0,.6); }
.card-name {
  flex: 1; min-width: 0; font-family: "SF Mono","Consolas",monospace; font-size: 11.5px;
  color: #cfd8e3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.card-meta { display: flex; gap: 9px; align-items: center; font-size: 9.5px; color: #7a838e; font-variant-numeric: tabular-nums; }
.card-body { display: none; border-top: 1px solid #05070a; background: #0b0e12; }
.card.open .card-body { display: block; }

/* ── Rows ── */
.row {
  display: flex; align-items: center; gap: 10px; padding: 7px 14px;
  border-bottom: 1px solid rgba(0,0,0,.55); font-family: "SF Mono","Consolas",monospace;
  font-size: 11px; color: #a9b8c4;
}
.row:hover { background: rgba(255,90,0,.05); }
.row .time { width: 92px; color: #8b949e; font-size: 10px; flex-shrink: 0; }
.row .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .dir { width: 210px; color: #5f6a75; font-size: 9.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
.row .path { width: 260px; color: #4e565f; font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
.badge {
  font-size: 8.5px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase;
  padding: 2px 7px; border-radius: 3px; border: 1px solid; flex-shrink: 0; min-width: 62px; text-align: center;
}
.badge.SUCCESS { color: #6ef2a8; border-color: rgba(61,220,132,.45); background: rgba(61,220,132,.12); }
.badge.FAIL { color: #ff7a72; border-color: rgba(255,59,48,.45); background: rgba(255,59,48,.12); }
.badge.UNKNOWN { color: #8b949e; border-color: rgba(139,148,158,.35); background: rgba(139,148,158,.1); }
.saved-dot { width: 6px; height: 6px; border-radius: 50%; background: #1f2a33; flex-shrink: 0; }
.saved-dot.on { background: var(--green); box-shadow: 0 0 7px rgba(61,220,132,.8); }
.read {
  padding: 4px 10px; font-size: 9px; font-weight: 800; letter-spacing: .08em;
  text-transform: uppercase; cursor: pointer; border-radius: 3px; color: #cfe0ee;
  border: 1px solid #05070a; background: linear-gradient(180deg,#333b45,#1c2228);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.14);
  flex-shrink: 0;
}
.read:hover { filter: brightness(1.3); }
.read:disabled { filter: grayscale(.8) brightness(.7); cursor: default; }

/* ── Viewer sheet ── */
.sheet {
  position: fixed; inset: 0; z-index: 60; display: none;
  background: rgba(4,6,8,.82); backdrop-filter: blur(3px);
  padding: 40px;
}
.sheet.show { display: flex; }
.sheet-inner {
  flex: 1; display: flex; flex-direction: column; border-radius: 10px; overflow: hidden;
  border: 1px solid #05070a; background: linear-gradient(180deg,#0a0f0d,#060907);
  box-shadow: inset 0 4px 18px rgba(0,0,0,.95), 0 20px 60px rgba(0,0,0,.8);
}
.sheet-head {
  display: flex; align-items: center; gap: 12px; padding: 10px 14px;
  background: linear-gradient(180deg,#252d36,#1a2027);
  border-bottom: 1px solid #05070a; box-shadow: inset 0 1px 0 rgba(255,255,255,.1);
}
.sheet-title { flex: 1; min-width: 0; font-family: "SF Mono","Consolas",monospace; font-size: 11.5px; color: #cfd8e3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sheet pre {
  flex: 1; margin: 0; padding: 14px 18px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  font-family: "SF Mono","Consolas",monospace; font-size: 11.5px; line-height: 1.6; color: #9fd8bd;
}
.empty { padding: 40px 20px; text-align: center; color: #5c6a63; font-size: 11px; letter-spacing: .06em; line-height: 1.9; }
.hidden { display: none !important; }

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: #05070a; }
::-webkit-scrollbar-thumb { border-radius: 5px; border: 1px solid #05070a;
  background: linear-gradient(180deg,#4a5561,#262d35); }
</style>
</head>
<body>
<div class="shell">

  <div class="plate">
    <div class="brand">
      <h1>PEA Log Deck</h1>
      <div class="sub" id="dayLabel">—</div>
    </div>

    <div class="lcd">
      <div class="lcd-item"><span class="lcd-label">Files</span><span class="lcd-value" id="lcdFiles">—</span></div>
      <div class="lcd-item"><span class="lcd-label">Runs</span><span class="lcd-value" id="lcdRuns">—</span></div>
      <div class="lcd-item"><span class="lcd-label">Saved</span><span class="lcd-value cool" id="lcdSaved">—</span></div>
      <div class="lcd-item"><span class="lcd-label">Updated</span><span class="lcd-value cool" id="lcdUpdated" style="font-size:10px">—</span></div>
    </div>

    <div class="rocker" role="tablist">
      <div class="rocker-slot">
        <div class="rocker-knob" id="knob"></div>
        <button id="vRuns" class="on">By run</button>
        <button id="vFiles">All files</button>
      </div>
    </div>

    <button class="key" id="failOnly" title="show only FAIL files">Failures only</button>
    <select class="key" id="stage" title="show one stage"><option value="">All stages</option></select>

    <div class="spacer"></div>
    <button class="key" id="refresh">Refresh</button>
  </div>

  <div class="plate" style="gap:12px">
    <button class="key orange" id="pullAll">Pull all runs</button>
    <button class="key" id="pullLatest">Pull latest run</button>
    <div style="flex:1"></div>
    <div style="min-width:260px">
      <div class="gauge"><div class="gauge-fill" id="gaugeFill"></div></div>
    </div>
    <div style="font-family:'SF Mono',Consolas,monospace;font-size:10.5px;color:#9fb0bd;min-width:150px;text-align:right" id="gaugeText">idle</div>
  </div>

  <div class="well" id="well" style="display:none">
    <div class="well-head"><span>Pull log</span><span id="wellCount"></span></div>
    <div class="well-body" id="wellBody"></div>
  </div>

  <div id="runView"></div>
  <div id="fileView" class="hidden"></div>
</div>

<div class="sheet" id="sheet">
  <div class="sheet-inner">
    <div class="sheet-head">
      <span class="sheet-title" id="sheetTitle">—</span>
      <span id="sheetNote" style="font-size:9.5px;color:#6f7b88"></span>
      <button class="key" id="sheetOpen">Open local copy</button>
      <button class="key" id="sheetClose">Close</button>
    </div>
    <pre id="sheetBody">—</pre>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const state = { data: null, view: "runs", open: new Set(), pulling: false,
                failOnly: false, stage: "", seeded: false };

/* Filters, then failures ahead of everything else. Array sort is stable, so
   the server's newest-first order holds inside each group. */
function visible(files) {
  return files
    .filter((f) => (!state.failOnly || f.status === "FAIL")
                && (!state.stage || f.stage === state.stage))
    .sort((a, b) => (b.status === "FAIL") - (a.status === "FAIL"));
}
function filtering() { return state.failOnly || !!state.stage; }
function noMatch() {
  return `<div class="empty">${filtering() ? "No files match the filter." : "No files in this manifest yet."}</div>`;
}
function withDay(url) {
  const day = state.data && state.data.day;
  return day ? url + (url.includes("?") ? "&" : "?") + "day=" + encodeURIComponent(day) : url;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function badge(status) {
  const cls = status === "OK" ? "SUCCESS" : status === "FAIL" ? "FAIL" : "UNKNOWN";
  const label = status === "OK" ? "SUCCESS" : status === "FAIL" ? "FAIL" : "UNKNOWN";
  return `<span class="badge ${cls}">${label}</span>`;
}

async function load(refresh) {
  try {
    const r = await fetch(`/api/manifest${refresh ? "?refresh=true" : ""}`);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${r.status}`);
    }
    state.data = await r.json();
    // First load only: open the newest run so its failures are on screen.
    if (!state.seeded && state.data.runs.length) {
      state.open.add(state.data.runs[0].run);
      state.seeded = true;
    }
    render();
  } catch (err) {
    $("runView").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    $("fileView").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    $("dayLabel").textContent = "manifest unavailable";
  }
}

function render() {
  const d = state.data;
  if (!d) return;
  $("dayLabel").textContent = `${d.day}${d.pinned ? " (pinned)" : ""} · ${d.archive.replace(/^.*\//, "~/")} ← ${d.remote}`;

  const stages = [...new Set(d.files.map((f) => f.stage))].sort();
  if (state.stage && !stages.includes(state.stage)) stages.push(state.stage);
  $("stage").innerHTML = `<option value="">All stages</option>` + stages.map((s) =>
    `<option value="${esc(s)}"${s === state.stage ? " selected" : ""}>${esc(s)}</option>`).join("");
  $("lcdFiles").textContent = d.count.toLocaleString();
  $("lcdRuns").textContent = d.runs.length.toLocaleString();
  $("lcdSaved").textContent = `${d.saved_total}/${d.count}`;
  $("lcdUpdated").textContent = (d.updated || "").replace("T", " ").replace("Z", " UTC");

  renderRuns();
  renderFiles();
}

function rowHtml(f, showDir) {
  return `
    <div class="row" data-file="${esc(f.file)}" data-dir="${esc(f.dir_name)}">
      <span class="saved-dot ${f.saved ? "on" : ""}" title="${f.saved ? "saved locally" : "not downloaded"}"></span>
      <span class="time">${esc(f.pretty.slice(11))}</span>
      ${badge(f.status)}
      <span class="name" title="${esc(f.file)}">${esc(f.file)}</span>
      ${showDir ? `<span class="dir" title="${esc(f.dir_name)}">${esc(f.dir_name)}</span>` : ""}
      <span class="path" title="${esc(f.path)}">${esc(f.path)}</span>
      <button class="read" data-read="1">Read</button>
    </div>`;
}

function renderRuns() {
  const d = state.data;
  $("runView").innerHTML = d.runs.map((r) => {
    const files = visible(r.files);
    if (filtering() && !files.length) return "";
    const open = state.open.has(r.run);
    return `
      <div class="card ${open ? "open" : ""}" data-run="${esc(r.run)}">
        <div class="card-head" data-toggle="1">
          <span class="caret">▶</span>
          <span class="card-name">${esc(r.run)}</span>
          <span class="card-meta">
            <span>${r.total} files</span>
            <span style="color:#6ef2a8">${r.ok} ok</span>
            <span style="color:#ff7a72">${r.fail} fail</span>
            <span>${r.saved}/${r.total} saved</span>
          </span>
          <button class="key" data-pullrun="1" style="padding:5px 10px;font-size:9px">Pull run</button>
        </div>
        <div class="card-body">${files.map((f) => rowHtml(f, false)).join("")}</div>
      </div>`;
  }).join("") || noMatch();
}

function renderFiles() {
  const d = state.data;
  $("fileView").innerHTML = visible(d.files).map((f) => rowHtml(f, true)).join("")
    || noMatch();
}

/* ── filters ── */
$("failOnly").onclick = () => {
  state.failOnly = !state.failOnly;
  $("failOnly").classList.toggle("lit", state.failOnly);
  if (state.data) render();
};
$("stage").onchange = () => {
  state.stage = $("stage").value;
  if (state.data) render();
};

/* ── rocker ── */
function setView(v) {
  state.view = v;
  $("knob").classList.toggle("right", v === "files");
  $("vRuns").classList.toggle("on", v === "runs");
  $("vFiles").classList.toggle("on", v === "files");
  $("runView").classList.toggle("hidden", v !== "runs");
  $("fileView").classList.toggle("hidden", v !== "files");
}
$("vRuns").onclick = () => setView("runs");
$("vFiles").onclick = () => setView("files");

/* ── interactions ── */
document.addEventListener("click", async (e) => {
  const toggle = e.target.closest("[data-toggle]");
  if (toggle) {
    const card = toggle.closest(".card");
    const run = card.dataset.run;
    if (state.open.has(run)) state.open.delete(run); else state.open.add(run);
    card.classList.toggle("open");
    return;
  }

  const pullRun = e.target.closest("[data-pullrun]");
  if (pullRun) {
    e.stopPropagation();
    const run = pullRun.closest(".card").dataset.run;
    await startPull(withDay(`/api/pull?dir=${encodeURIComponent(run)}`));
    return;
  }

  const read = e.target.closest("[data-read]");
  if (read) {
    const row = read.closest(".row");
    read.disabled = true;
    read.textContent = "…";
    try {
      const p = new URLSearchParams({ file: row.dataset.file, dir: row.dataset.dir });
      const r = await fetch(`/api/file?${p}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      openSheet(d);
    } catch (err) {
      alert(err.message);
    } finally {
      read.disabled = false;
      read.textContent = "Read";
    }
    return;
  }
});

function openSheet(d) {
  $("sheetTitle").textContent = `${d.dir}/${d.file}`;
  $("sheetNote").textContent = `${(d.bytes / 1024).toFixed(1)} KB · ${d.note}`;
  $("sheetBody").textContent = d.text;
  $("sheetOpen").onclick = async () => {
    await fetch("/api/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ local: d.local }),
    });
  };
  $("sheet").classList.add("show");
}
$("sheetClose").onclick = () => $("sheet").classList.remove("show");
$("sheet").addEventListener("click", (e) => {
  if (e.target === $("sheet")) $("sheet").classList.remove("show");
});

$("refresh").onclick = () => load(true);
$("pullAll").onclick = () => startPull(withDay("/api/pull-all"));
$("pullLatest").onclick = () => startPull(withDay("/api/pull-all?latest=true"));

/* ── pull progress ── */
async function startPull(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (!d.started) { alert(d.reason || "could not start"); return; }
  $("well").style.display = "block";
  $("wellBody").innerHTML = "";
  poll();
}

let pollTimer = null;
async function poll() {
  clearTimeout(pollTimer);
  const r = await fetch("/api/pull/status");
  const s = await r.json();

  const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
  const fill = $("gaugeFill");
  fill.style.width = pct + "%";
  fill.className = "gauge-fill" + (s.finished ? (s.fail ? " done-fail" : " done-ok") : "");

  $("gaugeText").textContent = s.running
    ? `${s.done}/${s.total} · ${pct}%`
    : s.finished
      ? `done · ${s.ok} saved · ${s.fail} failed`
      : "idle";

  $("wellCount").textContent = s.running ? `${s.done}/${s.total}` : "";
  $("wellBody").innerHTML = s.log.map((line) =>
    `<div class="${line.startsWith("✓") ? "ok" : line.startsWith("✗") ? "bad" : ""}">${esc(line)}</div>`
  ).join("");
  $("wellBody").scrollTop = $("wellBody").scrollHeight;

  if (s.running) pollTimer = setTimeout(poll, 700);
  else if (s.finished) load(true);
}

setView("runs");
load(false);
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="PEA log deck")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--day", default="",
                    help="show this day's manifest (YYYY-MM-DD) instead of today's")
    args = ap.parse_args()

    global PINNED_DAY
    if args.day:
        try:
            _dt.date.fromisoformat(args.day)
        except ValueError:
            ap.error(f"--day wants YYYY-MM-DD, got {args.day!r}")
        PINNED_DAY = args.day

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    url = f"http://{args.host}:{args.port}"
    print(f"  PEA log deck  {url}", flush=True)
    if PINNED_DAY:
        print(f"  day           {PINNED_DAY} (pinned)", flush=True)
    print(f"  manifest      {MANIFEST_BASE}/<day>-manifest.json", flush=True)
    print(f"  archive       {ARCHIVE}", flush=True)
    print(f"  transport     scp {REMOTE}:{REMOTE_ROOT}", flush=True)

    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
