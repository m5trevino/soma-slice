#!/usr/bin/env python3
"""SOMA debug dashboard — skeuomorphic "machine room" edition.

Drop-in replacement for debug_dashboard.py. Same public API, same HTTP
endpoints, same SSE/JSONL/checkpoint plumbing — only the front end changes.

    from machinetalk.debug_dashboard_skeu import start_dashboard
    start_dashboard("/tmp/soma_debug", port=9876, auto_open=True)

Style: brushed graphite chassis, glass-domed status orbs, amber LEDs,
inset CRT readouts, physical keycaps.
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List

# ── Global state ────────────────────────────────────────────────────────

_jsonl_file: Path | None = None
_jsonl_lock = threading.Lock()

# In-memory ring buffer (last N events for live updates)
_events: List[Dict[str, Any]] = []
_events_lock = threading.Lock()
MAX_EVENTS = 5000  # keep last 5000 in memory

# Dashboard config
DEBUG_DIR = Path("/tmp/soma_debug")
SOMA_DIR = None  # Set when dashboard starts

# Current session tracking (set from pipeline or auto-detected)
_current_session_id: str | None = None
_current_session_title: str | None = None
_current_session_chars: int = 0


def set_current_session(session_id: str | None, title: str | None = None, chars: int = 0) -> None:
    """Set the session currently being shown in the dashboard header."""
    global _current_session_id, _current_session_title, _current_session_chars
    _current_session_id = session_id
    _current_session_title = title
    _current_session_chars = chars


def get_current_session():
    """Return current session info as dict."""
    return {
        "session_id": _current_session_id,
        "title": _current_session_title,
        "chars": _current_session_chars,
    }


def set_soma_dir(path: str | Path | None):
    global SOMA_DIR
    if path:
        SOMA_DIR = Path(path)


def _query_int(path: str, key: str, default: int) -> int:
    """Read an integer query parameter, falling back to ``default``."""
    from urllib.parse import parse_qs, urlparse
    try:
        values = parse_qs(urlparse(path).query).get(key)
        return int(values[0]) if values else default
    except (ValueError, TypeError, IndexError):
        return default


# ── Stage rows ──────────────────────────────────────────────────────────

ROW_KEY_SEP = ":"
MISSING_CELL = "-"


def row_key(event: Dict[str, Any] | None) -> str:
    """Row key for one stage event: ``session_code`` + ":" + ``stage_code``.

    Re-reading the same JSONL therefore replaces a row instead of appending a
    duplicate. Events written before TB-OBS-005 carry neither code; those fall
    back to the chatset and stage they were keyed on before, so an older line
    still gets its own stable key. ``rowKey()`` in the page is the same rule.
    """
    e = event if isinstance(event, dict) else {}
    session_code = e.get("session_code")
    stage_code = e.get("stage_code")
    if session_code and stage_code:
        return f"{session_code}{ROW_KEY_SEP}{stage_code}"
    set_key = e.get("chatset_id") or (
        f"{e.get('session_id') or ''}{ROW_KEY_SEP}{e.get('chatset_number') or ''}")
    return f"{set_key}{ROW_KEY_SEP}{e.get('stage') or ''}"


def stage_rows(events: List[Any]) -> List[Dict[str, Any]]:
    """Collapse events to one row per key, the last read winning.

    A JSONL line can parse to something that is not an event object; those
    lines are dropped rather than becoming a row with nothing in it.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for event in events:
        if isinstance(event, dict):
            rows[row_key(event)] = event
    return list(rows.values())


def load_event_rows(path: Path) -> List[Dict[str, Any]]:
    """Read one JSONL file into stage rows, skipping unreadable lines."""
    events: List[Dict[str, Any]] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return stage_rows(events)


def append_event(event: Dict[str, Any]) -> None:
    """Called by the pipeline when each stage call completes."""
    with _events_lock:
        _events.append(event)
        if len(_events) > MAX_EVENTS:
            del _events[:len(_events) - MAX_EVENTS]
    # Also write to JSONL file if debug dir is set
    if _jsonl_file:
        with _jsonl_lock:
            try:
                with open(_jsonl_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception:
                pass


def start_dashboard(debug_dir: str, port: int = 9876, auto_open: bool = True,
                    soma_dir: str | None = None) -> None:
    """Start the live HTML dashboard server on the given port."""
    global _jsonl_file, DEBUG_DIR, SOMA_DIR

    p = Path(debug_dir)
    p.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR = p

    jsonl_files = sorted(p.glob("*.jsonl"))
    if jsonl_files:
        _jsonl_file = jsonl_files[-1]
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _jsonl_file = p / f"{ts}.jsonl"

    if soma_dir:
        SOMA_DIR = Path(soma_dir)

    # Kill anything on the port
    import subprocess
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    time.sleep(0.3)

    # ThreadingHTTPServer, not HTTPServer: /events is an unbounded SSE loop,
    # and a single-threaded server can only serve one request at a time. One
    # open dashboard tab would block /data and the page itself forever.
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.daemon_threads = True
    server.jsonl_path = _jsonl_file
    server.debug_dir = p
    print(f"  Debug dir: {_jsonl_file}")
    print(f"  Dashboard: http://127.0.0.1:{port}  (skeuomorphic)")

    if auto_open:
        threading.Timer(1, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    server.serve_forever()


# ── Dashboard Handler ───────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path == "/events":
            self._sse_handler()
        elif path == "/data":
            self._data_handler()
        elif path == "/queue":
            self._queue_handler()
        elif path == "/progress":
            self._progress_handler()
        elif path == "/session":
            self._session_handler()
        else:
            self._html_handler()

    def _html_handler(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DASHBOARD_HTML.encode("utf-8"))

    def _data_handler(self):
        """Return events as JSON, complete and untruncated.

        ``?since=N`` returns only events after index N, which is what the page
        uses. A full read without ``since`` returns the whole history because
        that is what the log is — nothing is cut, ever.
        """
        since = _query_int(self.path, "since", 0)

        with _events_lock:
            total = len(_events)
            window = list(_events[since:]) if since else list(_events)

        # Nothing in memory (fresh process): serve the newest file whole.
        if not window and _jsonl_file and _jsonl_file.exists():
            try:
                with open(_jsonl_file) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                window.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                total = len(window)
                window = window[since:]
            except Exception:
                pass

        body = json.dumps({"events": window, "total": total},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _queue_handler(self):
        """Return the current session queue."""
        queue_data = {"selected": [], "count": 0}

        if SOMA_DIR:
            qp = SOMA_DIR / "data" / "queue.json"
            if qp.exists():
                try:
                    queue_data = json.loads(qp.read_text())
                except Exception:
                    pass

            # Also check for existing queue in parent dir
            if not queue_data.get("selected"):
                qp = SOMA_DIR / "queue.json"
                if qp.exists():
                    try:
                        queue_data = json.loads(qp.read_text())
                    except Exception:
                        pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(queue_data, ensure_ascii=False).encode("utf-8"))

    def _progress_handler(self):
        """Return processing progress for queued sessions."""
        queue_data = {"selected": [], "count": 0}
        if SOMA_DIR:
            qp = SOMA_DIR / "data" / "queue.json"
            if qp.exists():
                try:
                    queue_data = json.loads(qp.read_text())
                except Exception:
                    pass

        selected = queue_data.get("selected", [])
        if not selected:
            # Try to find sessions from events
            with _events_lock:
                sids = set()
                for ev in _events:
                    sid = ev.get("session_id")
                    if sid:
                        sids.add(sid)
                selected = list(sids)[:50]

        sessions = []
        total_sets_all = 0
        done_sets_all = 0

        for path in selected:
            sid = Path(path).name
            status = "queued"
            set_done = 0
            set_total = 0
            set_current = 0
            title = sid

            # Load session.json for metadata
            sj_path = Path(path) / "session.json"
            if sj_path.exists():
                try:
                    sj = json.loads(sj_path.read_text())
                    title = sj.get("title") or sj.get("name", sid)
                    set_total = sj.get("total_sets", 0)
                except Exception:
                    pass

            # Check checkpoints for status
            if SOMA_DIR:
                for sd in SOMA_DIR.glob("*/"):
                    cp = sd / "checkpoints" / f"{sid}.chunks.checkpoint.json"
                    if not cp.exists():
                        cp = sd / "checkpoints" / f"{sid}.checkpoint.json"
                    if cp.exists():
                        try:
                            cp_data = json.loads(cp.read_text())
                            cs = cp_data.get("chatsets", {})
                            set_total = len(cs)
                            set_done = sum(
                                1 for c in cs.values()
                                if all(
                                    s.get("status") in ("DONE", "SKIPPED")
                                    for s in c.get("stages", {}).values()
                                )
                            )
                            set_current = sum(
                                1 for c in cs.values()
                                if any(
                                    s.get("status") == "RUNNING"
                                    for s in c.get("stages", {}).values()
                                )
                            )
                        except Exception:
                            pass
                        break

            # Fall back to counting from events
            if set_total == 0:
                with _events_lock:
                    cs_sets = set()
                    cs_done = 0
                    for ev in _events:
                        if ev.get("session_id") == sid:
                            n = ev.get("chatset_number")
                            if n is not None:
                                cs_sets.add(str(n))
                                st = (ev.get("status") or "").lower()
                                if st in ("done",):
                                    cs_done += 1
                    set_total = len(cs_sets)
                    set_done = cs_done

            if set_total > 0 and set_done >= set_total:
                status = "done"
            elif set_current > 0 or set_done > 0:
                status = "running"

            total_sets_all += set_total
            done_sets_all += set_done

            sessions.append({
                "path": path,
                "id": sid,
                "title": title,
                "status": status,
                "sets_done": set_done,
                "sets_total": set_total,
                "sets_current": set_current,
                "progress": round(set_done / set_total * 100, 1) if set_total > 0 else 0,
            })

        overall_pct = round(done_sets_all / total_sets_all * 100, 1) if total_sets_all > 0 else 0

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "sessions": sessions,
            "total": len(sessions),
            "overall_pct": overall_pct,
            "total_sets": total_sets_all,
            "done_sets": done_sets_all,
        }, ensure_ascii=False).encode("utf-8"))

    def _session_handler(self):
        """Return current session context for header display."""
        info = get_current_session()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(info, ensure_ascii=False).encode("utf-8"))

    def _sse_handler(self):
        """Server-Sent Events stream."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                time.sleep(1)
                with _events_lock:
                    for ev in _events[-100:]:
                        self.wfile.write(
                            f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    _events.clear()
                if hasattr(self.wfile, "flush"):
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass  # suppress access logs


# ── Dashboard HTML ──────────────────────────────────────────────────────

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SOMA · Machine Room</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --void: #05080b;
  --chassis: #141b22;
  --chassis-deep: #0c1116;
  --plate: #1b242d;
  --plate-hi: #2a3642;
  --well: #080c10;
  --bone: #e9e4d7;
  --text: #d7dee5;
  --dim: #8d9aa4;
  --amber: #f2a93b;
  --amber-hi: #ffd28c;
  --amber-deep: #8a4f12;
  --green: #4ade80;
  --red: #ef5350;
  --yellow: #f6c453;
  --edge: rgba(255,255,255,.10);
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --sans: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { min-height: 100%; }
body {
  background:
    radial-gradient(1100px 640px at 50% -12%, rgba(242,169,59,.09), transparent 62%),
    radial-gradient(900px 520px at 104% 104%, rgba(70,130,180,.08), transparent 60%),
    linear-gradient(158deg, #04070a 0%, #0a1015 42%, #06090c 100%);
  color: var(--bone);
  font-family: var(--sans);
  font-size: 13px;
  padding: 22px 20px 34px;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}
/* film grain over the whole room */
body::before {
  content: "";
  position: fixed; inset: 0;
  pointer-events: none;
  opacity: .38;
  mix-blend-mode: overlay;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.82' numOctaves='3' stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter><rect width='180' height='180' filter='url(%23n)' opacity='0.62'/></svg>");
}

/* ══════════════ CHASSIS ══════════════ */
.deck {
  position: relative;
  z-index: 1;
  max-width: 1500px;
  margin: 0 auto;
  padding: 16px 18px 22px;
  border-radius: 26px;
  background:
    linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,0) 20%),
    repeating-linear-gradient(90deg, rgba(255,255,255,.013) 0 1px, transparent 1px 3px),
    linear-gradient(152deg, #1b242d 0%, #121921 44%, #0d1319 100%);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.20),
    inset 0 -1px 0 rgba(0,0,0,.85),
    inset 0 0 0 1px rgba(0,0,0,.65),
    0 1px 0 rgba(255,255,255,.05),
    0 34px 74px rgba(0,0,0,.66),
    0 12px 24px rgba(0,0,0,.48);
}
.deck::before { /* glass sheen */
  content: "";
  position: absolute; inset: 0;
  border-radius: 26px;
  pointer-events: none;
  background: linear-gradient(118deg, rgba(255,255,255,.10) 0%, rgba(255,255,255,.02) 18%, transparent 42%, transparent 74%, rgba(255,255,255,.035) 100%);
  mix-blend-mode: screen;
}
.deck::after { /* inner edge ring */
  content: "";
  position: absolute; inset: 7px;
  border-radius: 20px;
  pointer-events: none;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.07), inset 0 0 0 1px rgba(255,255,255,.025);
}
.screw {
  position: absolute;
  width: 13px; height: 13px;
  border-radius: 50%;
  background: radial-gradient(circle at 34% 28%, #77838f, #333d47 58%, #171d23 100%);
  box-shadow: inset 0 1px 1px rgba(255,255,255,.55), inset 0 -1px 2px rgba(0,0,0,.75), 0 1px 2px rgba(0,0,0,.85);
  z-index: 3;
}
.screw::after {
  content: "";
  position: absolute; inset: 0; margin: auto;
  width: 8px; height: 1.5px;
  background: #0a0e12;
  transform: rotate(34deg);
  box-shadow: 0 -1px 0 rgba(255,255,255,.16);
}
.screw.tl { top: 14px; left: 14px; }
.screw.tr { top: 14px; right: 14px; }
.screw.bl { bottom: 14px; left: 14px; }
.screw.br { bottom: 14px; right: 14px; }

/* ══════════════ MASTHEAD ══════════════ */
.masthead {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  flex-wrap: wrap;
  padding: 10px 22px 18px;
  border-bottom: 1px solid rgba(0,0,0,.62);
  box-shadow: 0 1px 0 rgba(255,255,255,.05);
}
.brand { display: flex; align-items: center; gap: 16px; }
.dial {
  position: relative;
  width: 54px; height: 54px;
  border-radius: 50%;
  flex: 0 0 auto;
  background:
    radial-gradient(circle at 50% 50%, #232c36 0 46%, transparent 47%),
    conic-gradient(from 0deg,
      #55616d, #1b232b 7%, #55616d 14%, #1b232b 21%, #55616d 28%, #1b232b 35%,
      #55616d 42%, #1b232b 49%, #55616d 56%, #1b232b 63%, #55616d 70%, #1b232b 77%,
      #55616d 84%, #1b232b 91%, #55616d 100%);
  box-shadow:
    0 7px 12px rgba(0,0,0,.72),
    inset 0 1px 1px rgba(255,255,255,.22),
    0 0 0 2px #090d11;
}
.dial span {
  position: absolute; inset: 11px;
  border-radius: 50%;
  background: radial-gradient(circle at 38% 30%, #3d4956, #141b22 72%);
  box-shadow: inset 0 2px 5px rgba(0,0,0,.85), inset 0 -1px 0 rgba(255,255,255,.10);
}
.dial::after {
  content: "";
  position: absolute; left: 50%; top: 7px;
  width: 3px; height: 15px; margin-left: -1.5px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--amber-hi), var(--amber-deep));
  box-shadow: 0 0 9px rgba(242,169,59,.85);
}
.brand-text h1 {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: .18em;
  color: #ded8c9;
  text-shadow: 0 1px 0 rgba(255,255,255,.20), 0 -1px 1px rgba(0,0,0,.85), 0 0 22px rgba(242,169,59,.10);
  line-height: 1.2;
}
.brand-text h1 em {
  font-style: normal;
  color: var(--amber);
  text-shadow: 0 0 12px rgba(242,169,59,.55), 0 -1px 1px rgba(0,0,0,.8);
}
.brand-text .sub {
  margin-top: 5px;
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: .26em;
  text-transform: uppercase;
  color: #7d8a95;
}
.brand-text .sub .sep { color: #46535e; padding: 0 4px; }
#link-state { color: #93a0aa; }
#link-state.on { color: var(--amber); text-shadow: 0 0 10px rgba(242,169,59,.7); }

/* LED bank */
.led-bank { display: flex; gap: 8px; flex-wrap: wrap; }
.led {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 13px;
  border-radius: 9px;
  background: linear-gradient(180deg, #1c252e, #11181e);
  border: 1px solid rgba(0,0,0,.65);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.10), inset 0 -2px 4px rgba(0,0,0,.55), 0 3px 7px rgba(0,0,0,.45);
}
.led .lbl {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: .18em;
  color: #7f8c96;
}
.led b {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 600;
  color: #e7eef4;
  text-shadow: 0 0 10px rgba(255,255,255,.12);
}
.led b.ok { color: var(--green); text-shadow: 0 0 10px rgba(74,222,128,.45); }
.led b.bad { color: var(--red); text-shadow: 0 0 10px rgba(239,83,80,.45); }
.led b.warn { color: var(--yellow); text-shadow: 0 0 10px rgba(246,196,83,.45); }
.lamp {
  width: 9px; height: 9px;
  border-radius: 50%;
  background: #151c22;
  box-shadow: inset 0 1px 1px rgba(255,255,255,.16), inset 0 -1px 1px rgba(0,0,0,.9), 0 0 0 1px #04070a;
  flex: 0 0 auto;
}
.lamp-cool  { background: #7fb7d8; box-shadow: 0 0 10px rgba(127,183,216,.75), inset 0 1px 1px rgba(255,255,255,.7), 0 0 0 1px #04070a; }
.lamp-green { background: var(--green); box-shadow: 0 0 10px rgba(74,222,128,.8), inset 0 1px 1px rgba(255,255,255,.7), 0 0 0 1px #04070a; }
.lamp-red   { background: var(--red);   box-shadow: 0 0 10px rgba(239,83,80,.8), inset 0 1px 1px rgba(255,255,255,.7), 0 0 0 1px #04070a; }
.lamp-amber { background: var(--amber); box-shadow: 0 0 11px rgba(242,169,59,.85), inset 0 1px 1px rgba(255,255,255,.7), 0 0 0 1px #04070a; }

/* ══════════════ SESSION PLATE ══════════════ */
.session-plate {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 15px 22px 4px;
  flex-wrap: wrap;
}
.plate-label {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: .22em;
  color: #707d87;
  writing-mode: vertical-rl;
  transform: rotate(180deg);
  padding: 4px 0;
  white-space: nowrap;
}
.lcd {
  position: relative;
  flex: 1;
  min-width: 280px;
  border-radius: 9px;
  padding: 11px 15px;
  overflow: hidden;
  background: linear-gradient(180deg, #0a120c, #050a07 55%, #071009);
  border: 1px solid #000;
  box-shadow:
    inset 0 2px 9px rgba(0,0,0,.95),
    inset 0 0 34px rgba(74,222,128,.05),
    0 1px 0 rgba(255,255,255,.06);
}
.lcd::after {
  content: "";
  position: absolute; inset: 0;
  pointer-events: none;
  background: repeating-linear-gradient(0deg, rgba(0,0,0,.20) 0 1px, transparent 1px 3px);
  opacity: .7;
}
.lcd-row {
  display: flex;
  align-items: baseline;
  gap: 26px;
  flex-wrap: wrap;
  font-family: var(--mono);
  font-size: 11px;
  color: #8fb99a;
  text-shadow: 0 0 9px rgba(100,240,150,.22);
}
.lcd-name {
  font-size: 13px;
  font-weight: 600;
  color: #b9f5cb;
  letter-spacing: .06em;
  text-shadow: 0 0 12px rgba(120,255,160,.45);
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lcd-item { white-space: nowrap; }
.lcd-item b { color: #d9ffe4; font-weight: 600; margin-left: 5px; }
.link-lamp { display: flex; align-items: center; gap: 8px; }
.link-lamp span {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: .2em;
  color: #78858f;
}
#link-lamp {
  width: 10px; height: 10px;
  border-radius: 50%;
  background: #1a2a1f;
  box-shadow: inset 0 1px 1px rgba(255,255,255,.15), 0 0 0 1px #04070a;
  transition: background .18s, box-shadow .18s;
}
#link-lamp.on {
  background: var(--green);
  box-shadow: 0 0 13px rgba(74,222,128,.9), inset 0 1px 1px rgba(255,255,255,.75), 0 0 0 1px #04070a;
}

/* ══════════════ TABS ══════════════ */
.tab-bar { display: flex; gap: 10px; padding: 16px 22px 2px; }
.tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  padding: 11px 24px;
  border: 1px solid rgba(0,0,0,.68);
  border-radius: 10px;
  cursor: pointer;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: #8b98a3;
  background: linear-gradient(180deg, #25313c, #18212a 56%, #121920);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.15),
    inset 0 -2px 3px rgba(0,0,0,.7),
    0 3px 0 #090d11,
    0 6px 12px rgba(0,0,0,.5);
  transition: color .16s, transform .08s;
}
.tab .tab-lamp {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #232c34;
  box-shadow: inset 0 1px 1px rgba(255,255,255,.12), 0 0 0 1px #04070a;
}
.tab:hover { color: #c3cdd5; }
.tab:active { transform: translateY(2px); box-shadow: inset 0 2px 6px rgba(0,0,0,.85), 0 1px 0 #090d11; }
.tab.active {
  color: var(--amber-hi);
  background: linear-gradient(180deg, #10171d, #161f27);
  box-shadow:
    inset 0 3px 9px rgba(0,0,0,.88),
    inset 0 -1px 0 rgba(255,255,255,.06),
    0 1px 0 rgba(255,255,255,.03);
}
.tab.active .tab-lamp {
  background: var(--amber);
  box-shadow: 0 0 11px rgba(242,169,59,.9), inset 0 1px 1px rgba(255,255,255,.7), 0 0 0 1px #04070a;
}
.tab-content { display: none; }
.tab-content.active { display: block; }

/* ══════════════ RAIL / KEYCAPS ══════════════ */
.rail {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 12px 22px 0;
  padding: 12px 14px;
  border-radius: 13px;
  background: linear-gradient(180deg, #0e141a, #0a0f14);
  box-shadow:
    inset 0 2px 7px rgba(0,0,0,.85),
    inset 0 -1px 0 rgba(255,255,255,.05),
    0 1px 0 rgba(255,255,255,.045);
}
.rail-sep {
  width: 1px;
  height: 22px;
  margin: 0 4px;
  background: linear-gradient(180deg, transparent, rgba(255,255,255,.14), transparent);
  box-shadow: 1px 0 0 rgba(0,0,0,.6);
}
.rail-note {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: .16em;
  color: #6f7c86;
}
.key {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .13em;
  text-transform: uppercase;
  color: #c8d2da;
  padding: 9px 15px;
  border-radius: 8px;
  border: 1px solid rgba(0,0,0,.68);
  background: linear-gradient(180deg, #2b3742, #1a232c 55%, #131a21);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.18),
    inset 0 -2px 4px rgba(0,0,0,.55),
    0 3px 0 #090d11,
    0 6px 11px rgba(0,0,0,.5);
  cursor: pointer;
  transition: filter .15s, transform .07s, color .15s;
}
.key:hover { filter: brightness(1.12); }
.key:active, .key.pressed {
  transform: translateY(2px);
  box-shadow: inset 0 2px 6px rgba(0,0,0,.85), 0 1px 0 #090d11;
}
.key.pressed {
  color: var(--amber-hi);
  text-shadow: 0 0 10px rgba(242,169,59,.55);
  background: linear-gradient(180deg, #1a2119, #161d1a);
}
.key-mini { padding: 9px 12px; }
.key-wide { width: 100%; margin-top: 10px; padding: 7px 10px; font-size: 9px; letter-spacing: .18em; }
.search-wrap { display: flex; align-items: center; gap: 8px; }
.search {
  width: 210px;
  padding: 9px 12px;
  border-radius: 8px;
  border: 1px solid #000;
  background: linear-gradient(180deg, #060a0e, #0b1116);
  color: #d6e2ea;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .06em;
  box-shadow: inset 0 2px 7px rgba(0,0,0,.92), 0 1px 0 rgba(255,255,255,.06);
  outline: none;
}
.search::placeholder { color: #56636d; letter-spacing: .14em; }
.search:focus { box-shadow: inset 0 2px 7px rgba(0,0,0,.92), 0 0 0 2px rgba(242,169,59,.35), 0 1px 0 rgba(255,255,255,.06); }
.page-info {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--amber-hi);
  letter-spacing: .1em;
  padding: 6px 12px;
  border-radius: 6px;
  background: #070b0f;
  box-shadow: inset 0 2px 6px rgba(0,0,0,.9), 0 1px 0 rgba(255,255,255,.05);
  text-shadow: 0 0 10px rgba(242,169,59,.4);
}

/* ══════════════ ORBS ══════════════ */
.orb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(106px, 1fr));
  gap: 24px;
  padding: 30px 26px 34px;
  max-width: 1320px;
  margin: 0 auto;
}
.orb {
  --ga: 159, 224, 255;
  --gb: 31, 111, 150;
  position: relative;
  width: 86px; height: 86px;
  margin: 10px auto;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border: 0;
  color: #f3f8fc;
  font-family: var(--mono);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: .04em;
  background:
    radial-gradient(circle at 33% 25%, rgba(255,255,255,.72), rgba(255,255,255,0) 30%),
    radial-gradient(circle at 50% 64%, rgb(var(--ga)), rgb(var(--gb)) 68%, #07161e 100%);
  box-shadow:
    0 0 0 5px #0b1116,
    0 0 0 6px rgba(133,152,168,.34),
    0 0 0 7px rgba(255,255,255,.05),
    0 13px 20px rgba(0,0,0,.7),
    0 3px 6px rgba(0,0,0,.65),
    inset 0 -10px 16px rgba(0,0,0,.55),
    inset 0 9px 13px rgba(255,255,255,.20),
    0 0 34px rgba(var(--gb), .40);
  text-shadow: 0 1px 2px rgba(0,0,0,.7), 0 0 14px rgba(255,255,255,.35);
  transition: transform .18s cubic-bezier(.2,.9,.3,1.35), box-shadow .2s, filter .15s;
  outline: none;
}
.orb::after { /* specular arc */
  content: "";
  position: absolute;
  left: 15%; top: 8%;
  width: 55%; height: 34%;
  border-radius: 50%;
  background: linear-gradient(180deg, rgba(255,255,255,.58), rgba(255,255,255,.05));
  filter: blur(1px);
  transform: rotate(-16deg);
  pointer-events: none;
}
.orb .orb-led {
  position: absolute;
  right: 18%; top: 15%;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: rgba(255,255,255,.55);
  box-shadow: 0 0 8px rgba(255,255,255,.55);
  pointer-events: none;
}
.orb .orb-sub {
  position: absolute;
  bottom: 12px;
  font-size: 7px;
  letter-spacing: .18em;
  font-weight: 500;
  color: rgba(255,255,255,.55);
  text-shadow: 0 1px 1px rgba(0,0,0,.6);
  pointer-events: none;
}
.orb:hover { transform: translateY(-3px) scale(1.035); filter: brightness(1.06); }
.orb:active { transform: translateY(0) scale(.985); }
.orb:focus-visible { box-shadow: 0 0 0 5px #0b1116, 0 0 0 7px rgba(242,169,59,.75), 0 13px 20px rgba(0,0,0,.7); }

.orb.done {
  --ga: 172, 246, 197;
  --gb: 26, 122, 67;
  box-shadow:
    0 0 0 5px #0b1116,
    0 0 0 6px rgba(133,152,168,.34),
    0 0 0 7px rgba(255,255,255,.05),
    0 13px 20px rgba(0,0,0,.7),
    inset 0 -10px 16px rgba(0,0,0,.55),
    inset 0 9px 13px rgba(255,255,255,.22),
    0 0 36px rgba(74,222,128,.42);
}
.orb.running {
  --ga: 255, 227, 166;
  --gb: 184, 116, 28;
  box-shadow:
    0 0 0 5px #0b1116,
    0 0 0 6px rgba(133,152,168,.34),
    0 0 0 7px rgba(255,255,255,.05),
    0 13px 20px rgba(0,0,0,.7),
    inset 0 -10px 16px rgba(0,0,0,.55),
    inset 0 9px 13px rgba(255,255,255,.24),
    0 0 40px rgba(242,169,59,.55);
  animation: orb-pulse 1.7s ease-in-out infinite;
}
.orb.failed {
  --ga: 255, 183, 170;
  --gb: 142, 36, 29;
  box-shadow:
    0 0 0 5px #0b1116,
    0 0 0 6px rgba(180,120,115,.34),
    0 0 0 7px rgba(255,255,255,.05),
    0 13px 20px rgba(0,0,0,.7),
    inset 0 -10px 16px rgba(0,0,0,.55),
    inset 0 9px 13px rgba(255,255,255,.20),
    0 0 38px rgba(239,83,80,.48);
}
.orb.failed .orb-led { background: #ffd0c9; box-shadow: 0 0 10px rgba(255,110,100,.9); }
.orb.running .orb-led { background: #fff3d6; box-shadow: 0 0 11px rgba(255,210,140,.95); animation: orb-pulse 1.7s ease-in-out infinite; }
.orb.done .orb-led { background: #d8ffe6; box-shadow: 0 0 10px rgba(140,255,180,.9); }
@keyframes orb-pulse {
  0%, 100% { filter: brightness(1); }
  50% { filter: brightness(1.13); }
}

.grid-empty {
  grid-column: 1 / -1;
  text-align: center;
  padding: 58px 20px;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .28em;
  color: #6d7a84;
  text-transform: uppercase;
}
.grid-empty span {
  display: block;
  margin-top: 10px;
  font-size: 9px;
  letter-spacing: .16em;
  color: #4e5a64;
  text-transform: none;
}

/* ══════════════ POPUP ══════════════ */
.popup {
  display: none;
  position: fixed; inset: 0;
  z-index: 60;
  padding: 22px;
  background:
    radial-gradient(1000px 600px at 50% 0%, rgba(242,169,59,.06), transparent 60%),
    rgba(3,6,9,.82);
  backdrop-filter: blur(9px);
  -webkit-backdrop-filter: blur(9px);
}
.popup.open { display: flex; }
.popup-shell {
  position: relative;
  width: min(1320px, 100%);
  height: 100%;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 20px;
  background:
    linear-gradient(180deg, rgba(255,255,255,.05), transparent 18%),
    repeating-linear-gradient(90deg, rgba(255,255,255,.012) 0 1px, transparent 1px 3px),
    linear-gradient(155deg, #1a232c, #111820 50%, #0d1319);
  border: 1px solid rgba(0,0,0,.7);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.18),
    inset 0 -1px 0 rgba(0,0,0,.85),
    0 40px 90px rgba(0,0,0,.7);
  animation: panel-in .22s cubic-bezier(.2,.9,.3,1.1);
}
@keyframes panel-in {
  from { opacity: 0; transform: translateY(10px) scale(.995); }
  to { opacity: 1; transform: none; }
}
.popup-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 18px;
  border-bottom: 1px solid rgba(0,0,0,.65);
  box-shadow: 0 1px 0 rgba(255,255,255,.05);
  background: linear-gradient(180deg, rgba(255,255,255,.035), transparent);
}
.popup-nav { display: flex; align-items: center; gap: 10px; }
.popup-title {
  margin-left: 8px;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: #ded8c9;
  text-shadow: 0 1px 0 rgba(255,255,255,.16), 0 -1px 1px rgba(0,0,0,.8);
}
.numkey {
  width: 64px;
  padding: 9px 8px;
  text-align: center;
  border-radius: 7px;
  border: 1px solid #000;
  background: linear-gradient(180deg, #05090c, #0a1015);
  color: var(--amber-hi);
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  box-shadow: inset 0 2px 7px rgba(0,0,0,.9), 0 1px 0 rgba(255,255,255,.06);
  outline: none;
}
.numkey:focus { box-shadow: inset 0 2px 7px rgba(0,0,0,.9), 0 0 0 2px rgba(242,169,59,.4); }
.key-close { padding: 9px 14px; color: #b9c3cb; }
.popup-body { flex: 1; overflow: auto; padding: 22px 24px 32px; }

.stage-group { margin-bottom: 24px; }
.group-label {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 12px;
  padding: 6px 14px;
  border-radius: 5px;
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: .24em;
  text-transform: uppercase;
  color: #f3cd92;
  background: linear-gradient(180deg, #46331c, #241708 60%, #1b1107);
  border: 1px solid rgba(0,0,0,.65);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.22),
    inset 0 -2px 3px rgba(0,0,0,.6),
    0 3px 6px rgba(0,0,0,.5);
  text-shadow: 0 1px 0 rgba(0,0,0,.6), 0 0 10px rgba(242,169,59,.25);
}
.group-label i {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--amber);
  box-shadow: 0 0 8px rgba(242,169,59,.85);
}
.stage-strip { display: flex; gap: 14px; flex-wrap: wrap; }
.stage-card {
  position: relative;
  min-width: 196px;
  flex: 1 1 196px;
  max-width: 300px;
  padding: 15px 14px 13px;
  border-radius: 12px;
  cursor: pointer;
  border: 1px solid rgba(0,0,0,.65);
  background:
    linear-gradient(180deg, rgba(255,255,255,.045), transparent 30%),
    linear-gradient(165deg, #242f39, #161e26 55%, #10161c);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.14),
    inset 0 -2px 5px rgba(0,0,0,.7),
    0 7px 15px rgba(0,0,0,.55),
    0 1px 0 rgba(255,255,255,.04);
  transition: transform .16s, box-shadow .2s, filter .16s;
}
.stage-card::before {
  content: "";
  position: absolute;
  left: 13px; right: 13px; top: 0;
  height: 3px;
  border-radius: 0 0 3px 3px;
  background: #2b353f;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.18);
}
.stage-card.done::before { background: linear-gradient(90deg, #2ecc71, #a7f3c6); box-shadow: 0 0 13px rgba(74,222,128,.8); }
.stage-card.running::before { background: linear-gradient(90deg, var(--amber-deep), var(--amber-hi)); box-shadow: 0 0 13px rgba(242,169,59,.85); }
.stage-card.failed::before { background: linear-gradient(90deg, #8e241d, #ff9d92); box-shadow: 0 0 13px rgba(239,83,80,.85); }
.stage-card:hover { transform: translateY(-2px); filter: brightness(1.04); }
.stage-card:active { transform: translateY(0); }
.stage-top { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.stage-lamp {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #232c34;
  box-shadow: inset 0 1px 1px rgba(255,255,255,.12), 0 0 0 1px #04070a;
  flex: 0 0 auto;
}
.stage-lamp.done { background: var(--green); box-shadow: 0 0 9px rgba(74,222,128,.8), inset 0 1px 1px rgba(255,255,255,.6), 0 0 0 1px #04070a; }
.stage-lamp.running { background: var(--amber); box-shadow: 0 0 10px rgba(242,169,59,.85), inset 0 1px 1px rgba(255,255,255,.6), 0 0 0 1px #04070a; animation: orb-pulse 1.7s ease-in-out infinite; }
.stage-lamp.failed { background: var(--red); box-shadow: 0 0 9px rgba(239,83,80,.85), inset 0 1px 1px rgba(255,255,255,.6), 0 0 0 1px #04070a; }
.stage-card h3 {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: #ccd6de;
  text-shadow: 0 1px 0 rgba(0,0,0,.7);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meta {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 0;
  border-bottom: 1px dashed rgba(255,255,255,.07);
  font-family: var(--mono);
  font-size: 10px;
}
.meta:last-of-type { border-bottom: 0; }
.meta span { color: #6f7d88; letter-spacing: .14em; font-size: 8.5px; }
.meta b { color: #e2eaf0; font-weight: 500; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 62%; }
.retry {
  position: absolute;
  top: 8px; right: 8px;
  padding: 2px 6px;
  border-radius: 7px;
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 600;
  color: #ffe4df;
  background: linear-gradient(180deg, #c23b32, #7d1e18);
  box-shadow: 0 0 10px rgba(239,83,80,.55), inset 0 1px 0 rgba(255,255,255,.35), 0 2px 4px rgba(0,0,0,.5);
  cursor: pointer;
  z-index: 3;
}
.token-tooltip {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  padding: 8px 11px;
  border-radius: 7px;
  border: 1px solid rgba(0,0,0,.7);
  background: linear-gradient(180deg, #202a34, #131a21);
  box-shadow: 0 10px 22px rgba(0,0,0,.65), inset 0 1px 0 rgba(255,255,255,.12);
  font-family: var(--mono);
  font-size: 9.5px;
  color: #cbd5dd;
  white-space: nowrap;
  pointer-events: none;
  z-index: 200;
  display: none;
}
.stage-card:hover .token-tooltip { display: block; }
.token-tooltip .warn { color: var(--yellow); }
.token-tooltip .ok { color: var(--green); }
.retry-cascade {
  display: none;
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  min-width: 210px;
  padding: 9px;
  border-radius: 8px;
  border: 1px solid rgba(0,0,0,.7);
  background: linear-gradient(180deg, #202a34, #111820);
  box-shadow: 0 12px 26px rgba(0,0,0,.7), inset 0 1px 0 rgba(255,255,255,.12);
  font-family: var(--mono);
  font-size: 9.5px;
  z-index: 200;
}
.retry-cascade.open { display: block; }
.retry-cascade .attempt { padding: 2px 0; color: #cbd5dd; }
.retry-cascade .attempt .fail { color: var(--red); }
.retry-cascade .attempt .pass { color: var(--green); }
.output-toggle {
  display: none;
  margin-top: 9px;
  padding: 10px;
  border-radius: 8px;
  max-height: 220px;
  overflow: auto;
  background: linear-gradient(180deg, #050a07, #071009);
  border: 1px solid #000;
  box-shadow: inset 0 2px 8px rgba(0,0,0,.92), 0 1px 0 rgba(255,255,255,.05);
  color: #9df0b4;
  font-family: var(--mono);
  font-size: 9.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  text-shadow: 0 0 7px rgba(90,240,140,.25);
}
.output-toggle.show { display: block; }

/* ══════════════ SESSIONS ══════════════ */
.sessions-panel { padding: 18px 22px 10px; }
.console {
  position: relative;
  padding: 18px 20px 20px;
  border-radius: 15px;
  background:
    linear-gradient(180deg, rgba(255,255,255,.04), transparent 26%),
    linear-gradient(160deg, #1c2630, #121a22 60%, #0f161c);
  border: 1px solid rgba(0,0,0,.65);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.13),
    inset 0 -2px 5px rgba(0,0,0,.65),
    0 8px 18px rgba(0,0,0,.5);
  margin-bottom: 18px;
}
.console-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: .24em;
  text-transform: uppercase;
  color: #8b98a3;
}
.console-sub { color: #55626c; letter-spacing: .14em; }
.readouts { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
.readout {
  flex: 1 1 130px;
  padding: 11px 14px;
  border-radius: 9px;
  background: linear-gradient(180deg, #05090c, #0a1014);
  border: 1px solid #000;
  box-shadow: inset 0 2px 8px rgba(0,0,0,.92), 0 1px 0 rgba(255,255,255,.05);
}
.readout .lbl {
  display: block;
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: .2em;
  color: #5f6d77;
  margin-bottom: 6px;
}
.readout b {
  font-family: var(--mono);
  font-size: 21px;
  font-weight: 600;
  color: #eef4f8;
  text-shadow: 0 1px 0 rgba(0,0,0,.6);
}
.readout b.ok { color: var(--green); text-shadow: 0 0 12px rgba(74,222,128,.4); }
.meter { padding-top: 2px; }
.progress-track {
  position: relative;
  height: 24px;
  padding: 4px;
  border-radius: 8px;
  background: linear-gradient(180deg, #04070a, #0a1014);
  box-shadow: inset 0 2px 9px rgba(0,0,0,.95), 0 1px 0 rgba(255,255,255,.05);
  overflow: hidden;
}
.progress-fill {
  position: relative;
  height: 100%;
  width: 0;
  border-radius: 5px;
  background: linear-gradient(90deg, #1f7a45, #4ade80 60%, #a7f3c6);
  box-shadow: 0 0 18px rgba(74,222,128,.45), inset 0 1px 0 rgba(255,255,255,.45);
  transition: width .5s ease;
}
.progress-fill::after {
  content: "";
  position: absolute; inset: 0;
  background: repeating-linear-gradient(90deg, transparent 0 10px, rgba(0,0,0,.5) 10px 13px);
  border-radius: 5px;
}
.progress-fill.running {
  background: linear-gradient(90deg, #8a4f12, #f2a93b 60%, #ffd28c);
  box-shadow: 0 0 18px rgba(242,169,59,.45), inset 0 1px 0 rgba(255,255,255,.45);
  animation: orb-pulse 1.7s ease-in-out infinite;
}
.sessions-table { width: 100%; border-collapse: separate; border-spacing: 0 8px; }
.sessions-table th {
  text-align: left;
  padding: 6px 14px;
  font-family: var(--mono);
  font-size: 8.5px;
  font-weight: 600;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: #6f7c86;
}
.sessions-table td {
  padding: 12px 14px;
  font-size: 12px;
  color: #d3dce3;
  background: linear-gradient(180deg, #1a232c, #121920);
  border-top: 1px solid rgba(255,255,255,.055);
  border-bottom: 1px solid rgba(0,0,0,.62);
}
.sessions-table tbody tr td:first-child { border-radius: 10px 0 0 10px; border-left: 1px solid rgba(255,255,255,.05); }
.sessions-table tbody tr td:last-child { border-radius: 0 10px 10px 0; border-right: 1px solid rgba(0,0,0,.5); }
.sessions-table tbody tr:hover td { filter: brightness(1.07); }
.status-dot {
  display: inline-block;
  width: 9px; height: 9px;
  border-radius: 50%;
  margin-right: 9px;
  vertical-align: middle;
  background: #232c34;
  box-shadow: inset 0 1px 1px rgba(255,255,255,.12), 0 0 0 1px #04070a;
}
.status-dot.queued { background: #5d6a75; }
.status-dot.running { background: var(--amber); box-shadow: 0 0 10px rgba(242,169,59,.85), inset 0 1px 1px rgba(255,255,255,.6), 0 0 0 1px #04070a; animation: orb-pulse 1.7s ease-in-out infinite; }
.status-dot.done { background: var(--green); box-shadow: 0 0 10px rgba(74,222,128,.85), inset 0 1px 1px rgba(255,255,255,.6), 0 0 0 1px #04070a; }
.status-dot.failed { background: var(--red); box-shadow: 0 0 10px rgba(239,83,80,.85), inset 0 1px 1px rgba(255,255,255,.6), 0 0 0 1px #04070a; }
.mini-bar-track {
  width: 92px;
  height: 9px;
  padding: 2px;
  border-radius: 4px;
  display: inline-block;
  vertical-align: middle;
  background: linear-gradient(180deg, #04070a, #0a1014);
  box-shadow: inset 0 2px 5px rgba(0,0,0,.9), 0 1px 0 rgba(255,255,255,.05);
  overflow: hidden;
}
.mini-bar-fill {
  position: relative;
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, #1f7a45, #4ade80);
  box-shadow: 0 0 9px rgba(74,222,128,.4);
}
.mini-bar-fill::after {
  content: "";
  position: absolute; inset: 0;
  background: repeating-linear-gradient(90deg, transparent 0 5px, rgba(0,0,0,.5) 5px 7px);
}
.mini-bar-fill.running {
  background: linear-gradient(90deg, #8a4f12, #f2a93b);
  box-shadow: 0 0 9px rgba(242,169,59,.4);
}
.session-id {
  font-family: var(--mono);
  font-size: 11px;
  color: #9fadb8;
}
.empty-state {
  text-align: center;
  padding: 46px 20px;
  color: #7b8994;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .16em;
  text-transform: uppercase;
}
.empty-state .icon {
  width: 54px; height: 54px;
  margin: 0 auto 16px;
  border-radius: 50%;
  background: radial-gradient(circle at 38% 30%, #2e3a45, #131a21 70%);
  box-shadow: inset 0 2px 6px rgba(0,0,0,.8), inset 0 -1px 0 rgba(255,255,255,.08), 0 0 0 1px rgba(0,0,0,.6);
}
.empty-state p { font-size: 10px; }
.empty-state p + p { margin-top: 8px; font-size: 9px; letter-spacing: .1em; color: #5d6a74; text-transform: none; }
code { font-family: var(--mono); color: var(--amber); }

/* ══════════════ MODAL ══════════════ */
.modal {
  display: none;
  position: fixed; inset: 0;
  z-index: 100;
  padding: 24px;
  align-items: center;
  justify-content: center;
  background: rgba(3,6,9,.8);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
}
.modal.open { display: flex; }
.modal-panel {
  width: min(920px, 92%);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 18px;
  border: 1px solid rgba(0,0,0,.7);
  background:
    linear-gradient(180deg, rgba(255,255,255,.05), transparent 22%),
    linear-gradient(158deg, #1c2630, #111820);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.16),
    0 40px 90px rgba(0,0,0,.72);
  animation: panel-in .2s cubic-bezier(.2,.9,.3,1.1);
}
.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 15px 20px;
  border-bottom: 1px solid rgba(0,0,0,.65);
  box-shadow: 0 1px 0 rgba(255,255,255,.05);
}
.modal-head h2 {
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: #ded8c9;
  text-shadow: 0 1px 0 rgba(255,255,255,.16), 0 -1px 1px rgba(0,0,0,.8);
}
.modal-close {
  padding: 7px 12px;
  border-radius: 7px;
  border: 1px solid rgba(0,0,0,.68);
  background: linear-gradient(180deg, #2b3742, #1a232c);
  color: #b9c3cb;
  font-family: var(--mono);
  font-size: 10px;
  cursor: pointer;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.16), 0 2px 0 #090d11;
}
.modal-close:active { transform: translateY(2px); box-shadow: inset 0 2px 5px rgba(0,0,0,.8); }
.modal-body { padding: 20px; overflow: auto; }
.modal-body pre {
  padding: 12px;
  border-radius: 8px;
  background: linear-gradient(180deg, #050a07, #071009);
  border: 1px solid #000;
  box-shadow: inset 0 2px 8px rgba(0,0,0,.92);
  color: #9df0b4;
  font-family: var(--mono);
  font-size: 10px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 270px;
  overflow: auto;
  text-shadow: 0 0 7px rgba(90,240,140,.22);
}
.section { margin-bottom: 18px; }
.section h3 {
  margin-bottom: 8px;
  font-family: var(--mono);
  font-size: 8.5px;
  font-weight: 600;
  letter-spacing: .22em;
  text-transform: uppercase;
  color: #7b8994;
}

/* ══════════════ REFRESH + FOOTER ══════════════ */
.refresh-btn {
  position: fixed;
  right: 24px; bottom: 24px;
  width: 54px; height: 54px;
  border-radius: 50%;
  z-index: 90;
  cursor: pointer;
  color: #d7e0e8;
  font-size: 19px;
  line-height: 1;
  border: 1px solid rgba(0,0,0,.7);
  background:
    radial-gradient(circle at 36% 28%, #3c4956, #1a232c 62%, #10171d 100%);
  box-shadow:
    inset 0 2px 2px rgba(255,255,255,.22),
    inset 0 -4px 7px rgba(0,0,0,.6),
    0 4px 0 #090d11,
    0 10px 22px rgba(0,0,0,.6);
  transition: transform .08s, filter .15s;
}
.refresh-btn:hover { filter: brightness(1.12); }
.refresh-btn:active { transform: translateY(3px); box-shadow: inset 0 3px 8px rgba(0,0,0,.85), 0 1px 0 #090d11; }
.foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 18px 24px 4px;
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: #55626c;
}
.foot .foot-lamp {
  width: 6px; height: 6px;
  border-radius: 50%;
  display: inline-block;
  margin-right: 8px;
  background: var(--amber);
  box-shadow: 0 0 9px rgba(242,169,59,.8);
  vertical-align: middle;
}

/* ══════════════ RESPONSIVE ══════════════ */
@media (max-width: 820px) {
  body { padding: 12px 10px 26px; }
  .deck { padding: 12px 10px 18px; border-radius: 20px; }
  .masthead { padding: 8px 12px 14px; }
  .led-bank { width: 100%; }
  .led { flex: 1 1 auto; justify-content: center; }
  .session-plate { padding: 13px 12px 2px; }
  .plate-label { display: none; }
  .lcd-name { max-width: 100%; }
  .tab-bar, .rail, .sessions-panel { padding-left: 12px; padding-right: 12px; }
  .rail { margin-left: 12px; margin-right: 12px; }
  .search { width: 150px; }
  .orb-grid { grid-template-columns: repeat(auto-fill, minmax(92px, 1fr)); gap: 20px; padding: 22px 14px 26px; }
  .orb { width: 74px; height: 74px; font-size: 13px; }
  .popup { padding: 10px; }
  .popup-body { padding: 16px 14px 24px; }
  .stage-card { max-width: none; }
}
</style>
</head>
<body>
<div class="deck">
  <span class="screw tl"></span><span class="screw tr"></span>
  <span class="screw bl"></span><span class="screw br"></span>

  <header class="masthead">
    <div class="brand">
      <div class="dial" aria-hidden="true"><span></span></div>
      <div class="brand-text">
        <h1>SOMA <em>//</em> MACHINETALK</h1>
        <div class="sub">DEBUG CONSOLE <span class="sep">·</span> MACHINE ROOM <span class="sep">·</span> <span id="link-state">LINK IDLE</span></div>
      </div>
    </div>
    <div class="led-bank">
      <div class="led"><i class="lamp lamp-cool"></i><span class="lbl">EVENTS</span><b id="s-events">0</b></div>
      <div class="led"><i class="lamp lamp-green"></i><span class="lbl">DONE</span><b class="ok" id="s-done">0</b></div>
      <div class="led"><i class="lamp lamp-red"></i><span class="lbl">FAILED</span><b class="bad" id="s-fail">0</b></div>
      <div class="led"><i class="lamp lamp-amber"></i><span class="lbl">RUNNING</span><b class="warn" id="s-run">0</b></div>
    </div>
  </header>

  <section class="session-plate">
    <div class="plate-label">ACTIVE SESSION</div>
    <div class="lcd" id="session-lcd">
      <div class="lcd-row">
        <span class="lcd-name" id="session-name">NO SESSION</span>
        <span class="lcd-item">CHATSETS <b id="h-total-cs">0</b></span>
        <span class="lcd-item">CURRENT <b id="h-current-cs">0</b></span>
        <span class="lcd-item">CHARS <b id="h-total-chars">0</b></span>
        <span class="lcd-item">SET CHARS <b id="h-current-chars">0</b></span>
      </div>
    </div>
    <div class="link-lamp"><i id="link-lamp"></i><span>LIVE</span></div>
  </section>

  <nav class="tab-bar">
    <button class="tab active" data-tab="orbs"><i class="tab-lamp"></i>ORBS</button>
    <button class="tab" data-tab="sessions"><i class="tab-lamp"></i>SESSIONS</button>
  </nav>

  <!-- ═══════════════ ORBS ═══════════════ -->
  <section class="tab-content active" id="tab-orbs">
    <div class="rail">
      <button class="key key-mini" id="prev" title="Previous page">◀</button>
      <span class="page-info" id="page-info">1 / 1</span>
      <button class="key key-mini" id="next" title="Next page">▶</button>
      <span class="rail-sep"></span>
      <button class="key" id="failed-only">FAILED ONLY</button>
      <div class="search-wrap">
        <input type="text" id="search" class="search" placeholder="FILTER SETS…" autocomplete="off" spellcheck="false">
        <button class="key key-mini" id="search-clear" title="Clear filter" style="display:none">✕</button>
      </div>
      <span class="rail-note" id="orb-count">—</span>
    </div>
    <div class="orb-grid" id="orbs"></div>
  </section>

  <!-- ═══════════════ SESSIONS ═══════════════ -->
  <section class="tab-content" id="tab-sessions">
    <div class="sessions-panel">
      <div class="console">
        <div class="console-head">
          <span>BATCH TELEMETRY</span>
          <span class="console-sub">/progress · 3s poll</span>
        </div>
        <div class="readouts">
          <div class="readout"><span class="lbl">SESSIONS</span><b id="ses-total">—</b></div>
          <div class="readout"><span class="lbl">CHAT SETS</span><b id="sets-total">—</b></div>
          <div class="readout"><span class="lbl">DONE</span><b class="ok" id="sets-done">—</b></div>
          <div class="readout"><span class="lbl">COMPLETION</span><b class="ok" id="overall-pct">—</b></div>
        </div>
        <div class="meter">
          <div class="progress-track"><div class="progress-fill" id="progress-fill" style="width:0%"></div></div>
        </div>
      </div>
      <table class="sessions-table">
        <thead>
          <tr><th>Status</th><th>Session</th><th>Chat Sets</th><th>Progress</th></tr>
        </thead>
        <tbody id="sessions-body">
          <tr><td colspan="4" class="empty-state">
            <div class="icon"></div>
            <p>No session queue loaded.</p>
            <p>Run <code>run_sessions.py</code> to start a batch.</p>
          </td></tr>
        </tbody>
      </table>
      <div class="console">
        <div class="console-head">
          <span>STAGE TELEMETRY</span>
          <span class="console-sub">one row per code · last read wins</span>
        </div>
        <table class="sessions-table">
          <thead>
            <tr><th>Code</th><th>Status</th><th>Cache Hit</th><th>Cache Miss</th></tr>
          </thead>
          <tbody id="stages-body">
            <tr><td colspan="4" class="empty-state">
              <div class="icon"></div>
              <p>No stage events recorded.</p>
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <div class="foot">
    <span><i class="foot-lamp"></i>SOMA TELEMETRY BUS</span>
    <span id="foot-clock">—</span>
  </div>
</div>

<!-- ═══════════════ POPUP ═══════════════ -->
<div class="popup" id="popup">
  <div class="popup-shell">
    <div class="popup-head">
      <div class="popup-nav">
        <button class="key key-mini" id="pop-prev" title="Previous chatset">◀</button>
        <input type="text" id="pop-num" class="numkey" value="1">
        <button class="key key-mini" id="pop-next" title="Next chatset">▶</button>
        <span class="popup-title" id="pop-title">Chatset</span>
      </div>
      <button class="key key-close" id="pop-close" title="Close">✕</button>
    </div>
    <div class="popup-body" id="popup-body"></div>
  </div>
</div>

<!-- ═══════════════ MODAL ═══════════════ -->
<div class="modal" id="modal">
  <div class="modal-panel">
    <div class="modal-head">
      <h2 id="modal-title">Stage</h2>
      <button class="modal-close" id="modal-close">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<button class="refresh-btn" id="refresh-btn" title="Refresh">↻</button>

<script>
// ── State ────────────────────────────────────────────────────────
const STAGES = [
  ['Ingestion', ['distiller','introbeaver','planbeaver','outrobeaver']],
  ['Blueprint', ['nexus','spark','falcon']],
  ['Build',     ['eagle','drafter','scout']],
  ['Audit',     ['oc']],
  ['Harden',    ['olc']],
  ['Terminal',  ['ax_hillock','myelinator','terminal']]
];
const MAPPED = new Set(STAGES.flatMap(([, s]) => s));
const PER_PAGE = 50;

let events = [];
let eventCount = 0;   // how many events the page already holds
let rows = {};
let page = 0;
let currentOrb = null;
let failedOnly = false;

// ── Helpers ──────────────────────────────────────────────────────
function esc(v) {
  return String(v === undefined || v === null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function num(v, fallback) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : (fallback || 0);
}
function cell(v) {
  return (v === undefined || v === null || v === '') ? '-' : String(v);
}
// Same rule as row_key() in the Python module: session_code + ':' + stage_code.
// Events older than TB-OBS-005 have no codes; they fall back to the chatset and
// stage they were already keyed on so re-reading the JSONL still replaces.
function rowKey(e) {
  if (e && e.session_code && e.stage_code) return e.session_code + ':' + e.stage_code;
  const setKey = (e && e.chatset_id) || ((e && e.session_id ? e.session_id : '') + ':' + (e && e.chatset_number !== undefined && e.chatset_number !== null ? e.chatset_number : ''));
  return setKey + ':' + (e && e.stage ? e.stage : '');
}
function flashLink() {
  const lamp = document.getElementById('link-lamp');
  const state = document.getElementById('link-state');
  if (!lamp) return;
  lamp.classList.add('on');
  if (state) { state.classList.add('on'); state.textContent = 'LINK OK'; }
  clearTimeout(flashLink._t);
  flashLink._t = setTimeout(() => {
    lamp.classList.remove('on');
    if (state) { state.classList.remove('on'); state.textContent = 'LINK READY'; }
  }, 1100);
}

// ── Header updates ──────────────────────────────────────────────
function updateHeader() {
  const sessionMap = {};
  events.forEach(e => {
    const sid = e.session_id;
    if (!sid) return;
    if (!sessionMap[sid]) {
      sessionMap[sid] = { chatsets: new Set(), chars: 0, running_cs: null, title: null };
    }
    const info = sessionMap[sid];
    const cs = e.chatset_number;
    if (cs !== undefined && cs !== null) info.chatsets.add(cs);
    if (e.chars_in) info.chars += parseInt(e.chars_in) || 0;
    const st = (e.status || '').toLowerCase();
    if (st === 'running') info.running_cs = cs;
    if (e.session_title) info.title = e.session_title;
  });

  if (Object.keys(sessionMap).length === 0) {
    document.getElementById('session-name').textContent = 'NO SESSION';
    document.getElementById('h-total-cs').textContent = '0';
    document.getElementById('h-current-cs').textContent = '0';
    document.getElementById('h-total-chars').textContent = '0';
    document.getElementById('h-current-chars').textContent = '0';
    return;
  }

  let activeSid = null;
  for (const sid of Object.keys(sessionMap)) {
    if (sessionMap[sid].running_cs !== null) { activeSid = sid; break; }
  }
  if (!activeSid) activeSid = Object.keys(sessionMap).slice(-1)[0];

  const info = sessionMap[activeSid];
  const sortedCS = Array.from(info.chatsets).map(Number).sort((a, b) => a - b);
  const totalCS = sortedCS.length;
  const currentCS = sortedCS.length > 0 ? sortedCS[sortedCS.length - 1] : 0;
  const currentChars = events
    .filter(e => e.session_id === activeSid && e.chatset_number === currentCS)
    .reduce((sum, e) => sum + (parseInt(e.chars_in) || 0), 0);

  document.getElementById('session-name').textContent = info.title || activeSid;
  document.getElementById('h-total-cs').textContent = totalCS;
  document.getElementById('h-current-cs').textContent = currentCS;
  document.getElementById('h-total-chars').textContent = info.chars.toLocaleString();
  document.getElementById('h-current-chars').textContent = currentChars.toLocaleString();
}

// ── Tabs ─────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'sessions') loadSessions();
  });
});

// ── Orbs ─────────────────────────────────────────────────────────
function group() {
  rows = {};
  events.forEach(e => {
    const key = e.chatset_id || (e.session_id + ':' + e.chatset_number);
    if (!rows[key]) rows[key] = { id: key, set: e.chatset_number, stages: {} };
    rows[key].stages[e.stage] = e;
  });
}

function orbStatus(r) {
  const sts = Object.values(r.stages).map(s => (s.status || '').toLowerCase());
  if (sts.some(s => s === 'failed')) return 'failed';
  if (sts.some(s => s === 'running')) return 'running';
  if (sts.length && sts.every(s => s === 'done')) return 'done';
  return '';
}

function renderOrbs() {
  group();
  const list = Object.values(rows).sort((a, b) => (a.set || 0) - (b.set || 0));
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = list.filter(r => {
    const matchSearch = !q || String(r.set).includes(q) || r.id.toLowerCase().includes(q);
    const matchFailed = !failedOnly || orbStatus(r) === 'failed';
    return matchSearch && matchFailed;
  });
  const pages = Math.max(1, Math.ceil(filtered.length / PER_PAGE));
  page = Math.min(page, pages - 1);
  const slice = filtered.slice(page * PER_PAGE, (page + 1) * PER_PAGE);

  const grid = document.getElementById('orbs');
  if (slice.length === 0) {
    grid.innerHTML = `<div class="grid-empty">Awaiting telemetry<span>orbs appear as stage events land</span></div>`;
  } else {
    grid.innerHTML = slice.map(r => {
      const st = orbStatus(r);
      return `<button class="orb ${st}" data-id="${esc(r.id)}" title="Chatset ${esc(r.set)}">
        <i class="orb-led"></i>
        ${esc(r.set || '?')}
        <span class="orb-sub">SET</span>
      </button>`;
    }).join('');
  }

  document.getElementById('page-info').textContent = `${page + 1} / ${pages}`;
  document.getElementById('orb-count').textContent = `${filtered.length} SET${filtered.length === 1 ? '' : 'S'}`;
  document.getElementById('s-events').textContent = events.length;
  document.getElementById('s-done').textContent = list.filter(r => orbStatus(r) === 'done').length;
  document.getElementById('s-fail').textContent = list.filter(r => orbStatus(r) === 'failed').length;
  document.getElementById('s-run').textContent = list.filter(r => orbStatus(r) === 'running').length;
}

document.getElementById('failed-only').onclick = function () {
  failedOnly = !failedOnly;
  this.classList.toggle('pressed', failedOnly);
  page = 0;
  renderOrbs();
};
document.getElementById('search').oninput = function () {
  renderOrbs();
  document.getElementById('search-clear').style.display = this.value ? 'inline-block' : 'none';
};
document.getElementById('search-clear').onclick = function () {
  document.getElementById('search').value = '';
  document.getElementById('search-clear').style.display = 'none';
  renderOrbs();
};
document.getElementById('prev').onclick = () => { page = Math.max(0, page - 1); renderOrbs(); };
document.getElementById('next').onclick = () => { page++; renderOrbs(); };
document.getElementById('refresh-btn').onclick = () => { refreshAll(); };

// ── Popup ─────────────────────────────────────────────────────────
function stageCard(stage, ev, r) {
  const st = (ev.status || '').toLowerCase();
  const cls = st === 'done' ? 'done' : st === 'failed' ? 'failed' : st === 'running' ? 'running' : '';
  const attCount = typeof ev.attempts === 'number' ? ev.attempts
    : (Array.isArray(ev.attempts) ? ev.attempts.length : 0);
  const retry = attCount > 1 ? `<span class="retry" data-stage="${esc(stage)}">${attCount}</span>` : '';
  const outputId = 'out-' + stage + '-' + r.set;

  let effHtml = '';
  if (ev.prompt_tokens && ev.prompt_tokens > 0 && ev.chars_in) {
    const promptEff = (ev.chars_in / ev.prompt_tokens).toFixed(1);
    const warn = promptEff < 2;
    const compEff = ev.completion_tokens && ev.raw_response
      ? (String(ev.raw_response).length / ev.completion_tokens).toFixed(1) : '—';
    effHtml = `<div class="token-tooltip"><span class="${warn ? 'warn' : 'ok'}">${promptEff} ch/token${warn ? ' ⚠' : ''}</span> · ${compEff} out/token</div>`;
  }

  let cascadeHtml = '';
  if (attCount > 1) {
    const attList = Array.isArray(ev.attempts) ? ev.attempts : [];
    cascadeHtml = `<div class="retry-cascade" id="cascade-${esc(r.set)}-${esc(stage)}">`;
    attList.forEach((a, i) => {
      const isError = a.status === 'fail' || a.verdict === 'fail';
      cascadeHtml += `<div class="attempt ${isError ? 'fail' : 'pass'}">${i + 1}: ${esc(a.model || '?')} ${isError ? '✗' : '✓'}</div>`;
    });
    cascadeHtml += '</div>';
  }

  const raw = ev.raw_response ? JSON.stringify(ev.raw_response, null, 2).substring(0, 2000) : '—';
  const lat = (ev.latency_ms !== undefined && ev.latency_ms !== null)
    ? `${Math.round(Number(ev.latency_ms))} ms` : null;

  return `<div class="stage-card ${cls}" data-stage="${esc(stage)}">
    ${retry}${effHtml}${cascadeHtml}
    <div class="stage-top"><i class="stage-lamp ${cls}"></i><h3>${esc(stage)}</h3></div>
    <div class="meta"><span>SET</span><b>${esc(r.set)}</b></div>
    <div class="meta"><span>CHARS</span><b>${esc(ev.chars_in || '—')}</b></div>
    <div class="meta"><span>MODEL</span><b>${esc(ev.model || '—')}</b></div>
    ${lat ? `<div class="meta"><span>LATENCY</span><b>${esc(lat)}</b></div>` : ''}
    <button class="key key-wide" data-action="output" data-output="${esc(outputId)}">OUTPUT</button>
    <div class="output-toggle" id="${esc(outputId)}">${esc(raw)}</div>
  </div>`;
}

function openPopup(id) {
  currentOrb = id;
  const r = rows[id];
  if (!r) return;
  document.getElementById('pop-title').textContent = `Chatset ${r.set || id}`;
  document.getElementById('pop-num').value = r.set || '';

  let html = '';
  STAGES.forEach(([groupName, stages]) => {
    const present = stages.filter(s => r.stages[s]);
    if (present.length === 0) return;
    html += `<div class="stage-group"><div class="group-label"><i></i>${esc(groupName)}</div><div class="stage-strip">`;
    present.forEach(stage => html += stageCard(stage, r.stages[stage], r));
    html += '</div></div>';
  });
  if (!html) {
    html = `<div class="grid-empty">No stage telemetry recorded for this chatset</div>`;
  }

  const extra = Object.keys(r.stages).filter(s => !MAPPED.has(s));
  if (extra.length) {
    html += `<div class="stage-group"><div class="group-label"><i></i>Unmapped</div><div class="stage-strip">`;
    extra.forEach(stage => html += stageCard(stage, r.stages[stage] || {}, r));
    html += '</div></div>';
  }

  const bodyEl = document.getElementById('popup-body');
  const scrollY = bodyEl.scrollTop;
  bodyEl.innerHTML = html;
  bodyEl.scrollTop = scrollY;
  document.getElementById('popup').classList.add('open');
}

document.getElementById('orbs').addEventListener('click', e => {
  const orb = e.target.closest('.orb');
  if (orb) openPopup(orb.dataset.id);
});

document.getElementById('popup-body').addEventListener('click', e => {
  const card = e.target.closest('.stage-card');
  if (!card) return;
  const outBtn = e.target.closest('[data-action="output"]');
  if (outBtn) {
    const outEl = document.getElementById(outBtn.dataset.output);
    if (outEl) outEl.classList.toggle('show');
    return;
  }
  const retryBadge = e.target.closest('.retry');
  if (retryBadge) {
    const stage = card.dataset.stage;
    const setId = rows[currentOrb] ? rows[currentOrb].set : '';
    const cascade = document.getElementById(`cascade-${setId}-${stage}`);
    document.querySelectorAll('.retry-cascade.open').forEach(c => { if (c !== cascade) c.classList.remove('open'); });
    if (cascade) cascade.classList.toggle('open');
    return;
  }
  if (!e.target.closest('.retry-cascade')) {
    document.querySelectorAll('.retry-cascade.open').forEach(c => c.classList.remove('open'));
  }
  if (e.target.closest('.output-toggle')) return;
  openModal(card.dataset.stage);
});

document.getElementById('pop-close').onclick = () => document.getElementById('popup').classList.remove('open');
document.getElementById('pop-prev').onclick = () => stepOrb(-1);
document.getElementById('pop-next').onclick = () => stepOrb(1);
document.getElementById('pop-num').onchange = function () {
  const n = parseInt(this.value, 10);
  const list = Object.values(rows).sort((a, b) => (a.set || 0) - (b.set || 0));
  const found = list.find(r => r.set === n);
  if (found) openPopup(found.id);
};

function stepOrb(dir) {
  const list = Object.values(rows).sort((a, b) => (a.set || 0) - (b.set || 0));
  const idx = list.findIndex(r => r.id === currentOrb);
  const next = list[idx + dir];
  if (next) openPopup(next.id);
}

// ── Modal ─────────────────────────────────────────────────────────
function openModal(stage) {
  const r = rows[currentOrb];
  if (!r) return;
  const ev = r.stages[stage] || {};
  document.getElementById('modal-title').textContent = `${stage} · ${ev.model || '—'}`;
  document.getElementById('modal-body').innerHTML = `
    <div class="section"><h3>Gate Error</h3><pre>${esc(ev.errors || 'none')}</pre></div>
    <div class="section"><h3>Raw Response</h3><pre>${esc(ev.raw || ev.response || '—')}</pre></div>
    <div class="section"><h3>Payload</h3><pre>${esc(JSON.stringify(ev.payload || {}, null, 2))}</pre></div>
  `;
  document.getElementById('modal').classList.add('open');
}
function closeModal() {
  document.getElementById('modal').classList.remove('open');
}
document.getElementById('modal-close').onclick = closeModal;
document.getElementById('modal').addEventListener('click', (e) => {
  if (e.target.id === 'modal') closeModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('modal').classList.contains('open')) { closeModal(); return; }
  if (document.getElementById('popup').classList.contains('open')) {
    document.getElementById('popup').classList.remove('open');
    return;
  }
});

// ── Sessions tab ──────────────────────────────────────────────────
async function loadSessions() {
  try {
    const res = await fetch('/progress');
    const data = await res.json();

    const sessions = data.sessions || [];
    const total = data.total || 0;
    const pct = data.overall_pct || 0;
    const totalSets = data.total_sets || 0;
    const doneSets = data.done_sets || 0;

    document.getElementById('ses-total').textContent = total;
    document.getElementById('sets-total').textContent = totalSets.toLocaleString();
    document.getElementById('sets-done').textContent = doneSets.toLocaleString();
    document.getElementById('overall-pct').textContent = pct.toFixed(1) + '%';

    const fill = document.getElementById('progress-fill');
    fill.style.width = pct + '%';
    fill.classList.toggle('running', pct > 0 && pct < 100);

    const tbody = document.getElementById('sessions-body');
    if (sessions.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state">
        <div class="icon"></div>
        <p>No session queue loaded.</p>
        <p>Run <code>run_sessions.py</code> to start a batch.</p>
      </td></tr>`;
      return;
    }

    tbody.innerHTML = sessions.map(s => {
      const statusClass = s.status;
      const statusLabel = s.status.charAt(0).toUpperCase() + s.status.slice(1);
      const sid = s.id || '';
      const shortId = sid.substring(0, 40);
      const miniPct = s.progress || 0;
      const miniClass = (s.status === 'running' || (s.status === 'queued' && miniPct > 0))
        ? 'mini-bar-fill running' : 'mini-bar-fill';
      return `<tr>
        <td><span class="status-dot ${esc(statusClass)}"></span>${esc(statusLabel)}</td>
        <td><span class="session-id" title="${esc(s.path)}">${esc(shortId)}${sid.length > 40 ? '…' : ''}</span></td>
        <td>${esc(s.sets_done)}/${esc(s.sets_total)}</td>
        <td><div class="mini-bar-track"><div class="${miniClass}" style="width:${miniPct}%"></div></div> ${miniPct.toFixed(0)}%</td>
      </tr>`;
    }).join('');
  } catch (e) {
    // fetch failed — dashboard keeps running
  }
}

// ── Stage rows ────────────────────────────────────────────────────
// One row per session_code:stage_code, so re-reading the same JSONL replaces
// a row instead of appending a duplicate. It re-groups the events the page
// already holds; it does not fetch anything of its own.
function renderStageRows() {
  const tbody = document.getElementById('stages-body');
  if (!tbody) return;
  const seen = {};
  events.forEach(e => {
    if (!e || !e.stage) return;
    seen[rowKey(e)] = e;   // last read wins
  });
  const list = Object.values(seen).sort((a, b) => {
    const ka = (a.session_code || '') + ':' + (a.stage_code || '');
    const kb = (b.session_code || '') + ':' + (b.stage_code || '');
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });
  if (list.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-state">
      <div class="icon"></div>
      <p>No stage events recorded.</p>
    </td></tr>`;
    return;
  }
  tbody.innerHTML = list.map(e => {
    const code = (e.session_code && e.stage_code)
      ? `${e.session_code}:${e.stage_code}`
      : e.stage_code || e.stage || '-';
    const st = (e.status || '').toLowerCase();
    const statusLabel = e.status ? String(e.status).charAt(0).toUpperCase() + String(e.status).slice(1) : '-';
    return `<tr>
      <td><span class="session-id">${esc(code)}</span></td>
      <td>${st ? `<span class="status-dot ${esc(st)}"></span>` : ''}${esc(statusLabel)}</td>
      <td>${esc(cell(e.cache_hit_tokens))}</td>
      <td>${esc(cell(e.cache_miss_tokens))}</td>
    </tr>`;
  }).join('');
}

function refreshAll() {
  renderOrbs();
  loadSessions();
  renderStageRows();
  updateHeader();
}

// ── SSE + Polling ─────────────────────────────────────────────────
const es = new EventSource('/events');
es.onmessage = e => {
  try {
    events.push(JSON.parse(e.data));
    renderOrbs();
    renderStageRows();
    updateHeader();
    flashLink();
  } catch (x) {}
  if (currentOrb) openPopup(currentOrb);
};
es.onopen = () => {
  const state = document.getElementById('link-state');
  if (state) state.textContent = 'LINK READY';
};
es.onerror = () => {
  const state = document.getElementById('link-state');
  if (state) state.textContent = 'LINK LOST';
};

// Initial load: take the whole history once.
fetch('/data').then(r => r.json()).then(d => {
  events = (d && d.events) ? d.events : [];
  eventCount = (d && typeof d.total === 'number') ? d.total : events.length;
  renderOrbs();
  renderStageRows();
  updateHeader();
}).catch(() => {});

// Subsequent polls ask only for events after what we already hold. The old
// loop re-downloaded the entire history every 3 seconds.
setInterval(() => {
  fetch('/data?since=' + eventCount).then(r => r.json()).then(d => {
    const fresh = (d && d.events) ? d.events : [];
    if (fresh.length) {
      events = events.concat(fresh);
      eventCount = (typeof d.total === 'number') ? d.total : events.length;
      renderOrbs();
      updateHeader();
      flashLink();
    }
  }).catch(() => {});
  if (document.getElementById('tab-sessions').classList.contains('active')) {
    loadSessions();
    renderStageRows();
  }
}, 3000);

// ── Footer clock ──────────────────────────────────────────────────
function tickClock() {
  const el = document.getElementById('foot-clock');
  if (!el) return;
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  el.textContent = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
setInterval(tickClock, 1000);
tickClock();

// Initial load
renderOrbs();
loadSessions();
renderStageRows();
updateHeader();
</script>
</body>
</html>"""


def json_serialize(obj):
    """Make any object JSON-serializable."""
    import datetime
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)[:500]


def write_event(debug_dir: Path, event: Dict[str, Any]) -> None:
    """Write a single event to JSONL. Called by pipeline when stage completes."""
    global _jsonl_file
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    if _jsonl_file is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _jsonl_file = debug_dir / f"{ts}.jsonl"

    clean = {}
    for k, v in event.items():
        try:
            json.dumps({k: v}, default=json_serialize)
            clean[k] = v
        except (TypeError, ValueError):
            clean[k] = str(v)[:500]

    append_event(clean)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="SOMA skeuomorphic debug dashboard")
    ap.add_argument("--dir", default="/tmp/soma_debug", help="debug/JSONL directory")
    ap.add_argument("--port", type=int, default=9876, help="HTTP port")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    start_dashboard(
        args.dir,
        port=args.port,
        auto_open=not args.no_open,
        soma_dir=str(Path(__file__).resolve().parents[2]),
    )
