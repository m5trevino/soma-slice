#!/usr/bin/env python3
"""Live HTML debug dashboard for MachineTalk pipeline.

Streams structured data from the pipeline and renders a real-time dashboard
with sort, filter, categorize, search, and statistics.

Added: Sessions/Processing tab — shows session queue, per-session status,
chatset progress, and overall completion percentage.

Usage:
    python3 -c "from debug_dashboard import start_dashboard; start_dashboard('/tmp/dendritic_debug')"
    # Then run pipeline with:
    python3 run_pipeline.py --debug /tmp/dendritic_debug
    
Open the printed URL in your browser. The dashboard auto-refreshes via SSE.
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
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

    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    server.jsonl_path = _jsonl_file
    server.debug_dir = p
    print(f"  Debug dir: {_jsonl_file}")
    print(f"  Dashboard: http://127.0.0.1:{port}")

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
        """Return all events as JSON."""
        all_events = []
        if _jsonl_file:
            debug_dir = _jsonl_file.parent
            for jf in sorted(debug_dir.glob("*.jsonl")):
                try:
                    with open(jf) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    all_events.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                except Exception:
                    pass

        with _events_lock:
            all_events.extend(_events[:200])

        # De-duplicate
        seen = set()
        deduped = []
        for ev in all_events:
            key = json.dumps(ev, sort_keys=True)
            if key not in seen:
                seen.add(key)
                deduped.append(ev)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(deduped, ensure_ascii=False).encode("utf-8"))

    def _queue_handler(self):
        """Return the current session queue."""
        # Try queue.json from data/ dir
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
        """Return processing progress for queued sessions.

        Checks checkpoint data for each session to determine status.
        """
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
            selected = []
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
                # Check in any output dir's checkpoints
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
                                for _ in [1]
                            )
                            set_current = sum(
                                1 for c in cs.values()
                                if any(
                                    s.get("status") == "RUNNING"
                                    for s in c.get("stages", {}).values()
                                )
                                for _ in [1]
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

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SOMA Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0c0d10;
  --panel: #16181d;
  --well: #0e1013;
  --text: #d4d7dc;
  --dim: #8b9098;
  --accent: #ff6b35;
  --green: #3fb950;
  --red: #f85149;
  --yellow: #d29922;
  --border: rgba(255,255,255,0.08);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: Inter, -apple-system, sans-serif;
  font-size: 13px;
  background-image: repeating-linear-gradient(90deg, transparent, transparent 2px, rgba(255,255,255,0.015) 2px, rgba(255,255,255,0.015) 4px);
  min-height: 100vh;
}

/* ── Header ─────────────────────────────────────────────────── */
.header {
  padding: 16px 24px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  border-top: 1px solid rgba(255,255,255,0.12);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-left {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.session-info {
  display: flex;
  gap: 16px;
  align-items: center;
  font-size: 12px;
  color: var(--dim);
}
.session-name {
  font-weight: 600;
  color: var(--accent);
  font-family: "JetBrains Mono", monospace;
  font-size: 13px;
}
.session-meta, .session-chars {
  display: flex;
  gap: 8px;
}
.session-meta b, .session-chars b {
  color: var(--text);
  font-family: "JetBrains Mono", monospace;
}
.stats { display: flex; gap: 16px; font-size: 12px; color: var(--dim); }
.stats b { color: var(--text); font-family: "JetBrains Mono", monospace; }

/* ── Tabs ───────────────────────────────────────────────────── */
.tab-bar {
  display: flex;
  gap: 0;
  background: var(--well);
  border-bottom: 1px solid var(--border);
}
.tab {
  padding: 10px 24px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--dim);
  border-bottom: 2px solid transparent;
  transition: color 150ms, border-color 150ms;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* ── Orbs tab ───────────────────────────────────────────────── */
.toolbar {
  padding: 12px 24px;
  background: var(--well);
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 12px;
  align-items: center;
}
.toolbar button, .toolbar input {
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-top: 1px solid rgba(255,255,255,0.1);
  border-bottom: 1px solid rgba(0,0,0,0.4);
  padding: 6px 14px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
}
.toolbar button:active { transform: translateY(1px); }
.page-info { font-family: "JetBrains Mono", monospace; color: var(--dim); }
.orb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(90px, 1fr));
  gap: 16px;
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}
.orb {
  width: 80px; height: 80px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: "JetBrains Mono", monospace;
  font-size: 11px; font-weight: 500;
  cursor: pointer; position: relative;
  background: radial-gradient(circle at 35% 30%, rgba(255,255,255,0.4), transparent 50%),
              radial-gradient(circle at 50% 60%, #4fc3f7, #0288d1);
  box-shadow: 0 4px 12px rgba(0,0,0,0.4), inset 0 -4px 8px rgba(0,0,0,0.2), inset 0 2px 4px rgba(255,255,255,0.3);
  border: 2px solid rgba(255,255,255,0.2);
  transition: transform 160ms cubic-bezier(.2,.9,.3,1.2);
}
.orb:hover { filter: brightness(1.04); }
.orb:active { transform: translateY(1px); }
.orb.done { background: radial-gradient(circle at 35% 30%, rgba(255,255,255,0.4), transparent 50%), radial-gradient(circle at 50% 60%, #66bb6a, #2e7d32); }
.orb.failed { background: radial-gradient(circle at 35% 30%, rgba(255,255,255,0.3), transparent 50%), radial-gradient(circle at 50% 60%, #ef5350, #c62828); }
.orb.running { background: radial-gradient(circle at 35% 30%, rgba(255,255,255,0.4), transparent 50%), radial-gradient(circle at 50% 60%, #ffca28, #f9a825); animation: pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.75; } }

/* ── Popup (orb click) ──────────────────────────────────────── */
.popup {
  display: none; position: fixed; inset: 0;
  background: var(--bg); z-index: 50; flex-direction: column;
}
.popup.open { display: flex; }
.popup-head {
  padding: 12px 20px; background: var(--panel);
  border-bottom: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: center;
}
.popup-nav { display: flex; gap: 8px; align-items: center; }
.popup-nav button, .popup-nav input {
  background: var(--well); color: var(--text);
  border: 1px solid var(--border); padding: 4px 10px;
  border-radius: 4px; font-family: "JetBrains Mono", monospace;
}
.popup-nav input { width: 60px; text-align: center; }
.popup-close { background: none; border: none; color: var(--dim); font-size: 24px; cursor: pointer; }
.popup-body { flex: 1; overflow: auto; padding: 20px; }
.stage-strip { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
.stage-card {
  background: var(--panel); border-radius: 8px; padding: 12px;
  min-width: 160px; border-top: 1px solid rgba(255,255,255,0.12);
  border-bottom: 1px solid rgba(0,0,0,0.4);
  box-shadow: 0 2px 6px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.08);
  cursor: pointer; position: relative;
}
.stage-card:active { transform: translateY(1px); box-shadow: inset 0 2px 4px rgba(0,0,0,0.4); }
.stage-card.done { box-shadow: 0 0 8px rgba(63,185,80,0.4), inset 0 1px 0 rgba(255,255,255,0.1); }
.stage-card.failed { box-shadow: 0 0 8px rgba(248,81,73,0.5), inset 0 1px 0 rgba(255,255,255,0.1); }
.stage-card.running { box-shadow: 0 0 6px rgba(210,153,34,0.4); animation: pulse 1.5s ease-in-out infinite; }
.stage-card h3 { font-size: 11px; letter-spacing: 1px; text-transform: uppercase; color: var(--dim); margin-bottom: 6px; }
.stage-card .meta { font-family: "JetBrains Mono", monospace; font-size: 11px; color: var(--text); }
.stage-card .btn { margin-top: 8px; padding: 4px 8px; background: var(--well); border: 1px solid var(--border); border-radius: 4px; font-size: 10px; color: var(--text); cursor: pointer; }
.retry { position: absolute; top: 4px; right: 4px; background: var(--accent); color: #fff; font-size: 9px; padding: 1px 4px; border-radius: 8px; font-family: "JetBrains Mono", monospace; cursor: pointer; }
.token-tooltip {
  position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%);
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  padding: 8px 10px; font-family: "JetBrains Mono", monospace; font-size: 10px;
  color: var(--text); white-space: nowrap; pointer-events: none; z-index: 200;
  box-shadow: 0 4px 12px rgba(0,0,0,0.5); display: none;
}
.stage-card:hover .token-tooltip { display: block; }
.token-tooltip .warn { color: var(--yellow); }
.token-tooltip .ok { color: var(--green); }
.retry-cascade {
  display: none; position: absolute; bottom: calc(100% + 6px); right: 0;
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  padding: 8px; font-family: "JetBrains Mono", monospace; font-size: 10px;
  z-index: 200; box-shadow: 0 4px 12px rgba(0,0,0,0.5); min-width: 200px;
}
.retry-cascade.open { display: block; }
.retry-cascade .attempt { padding: 2px 0; }
.retry-cascade .attempt .fail { color: var(--red); }
.retry-cascade .attempt .pass { color: var(--green); }
.output-toggle {
  display: none; margin-top: 6px; background: var(--well); border: 1px solid var(--border);
  border-radius: 4px; padding: 6px 8px; font-family: "JetBrains Mono", monospace;
  font-size: 10px; color: var(--text); max-height: 200px; overflow: auto;
  white-space: pre-wrap; word-break: break-all;
}
.output-toggle.show { display: block; }

/* ── Sessions tab ───────────────────────────────────────────── */
.sessions-panel { padding: 24px; }

.overall-bar {
  margin-bottom: 24px;
  background: var(--panel);
  border-radius: 8px;
  padding: 20px;
  border-top: 1px solid rgba(255,255,255,0.12);
  border-bottom: 1px solid rgba(0,0,0,0.4);
  box-shadow: 0 2px 6px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.08);
}
.overall-stats {
  display: flex; gap: 32px; margin-bottom: 16px;
}
.overall-stat { text-align: center; }
.overall-stat .label { font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--dim); }
.overall-stat .value { font-family: "JetBrains Mono", monospace; font-size: 24px; font-weight: 600; }
.overall-stat .value.green { color: var(--green); }
.overall-stat .value.yellow { color: var(--yellow); }
.overall-stat .value.red { color: var(--red); }
.progress-track {
  height: 8px; background: var(--well); border-radius: 4px;
  border-top: 1px solid rgba(255,255,255,0.1);
  border-bottom: 1px solid rgba(0,0,0,0.4);
  overflow: hidden;
}
.progress-fill {
  height: 100%; background: linear-gradient(90deg, var(--green), #4caf50);
  border-radius: 4px;
  transition: width 500ms ease;
}
.progress-fill.running { background: linear-gradient(90deg, var(--yellow), #ffa726); animation: pulse 1.5s ease-in-out infinite; }

.sessions-table {
  width: 100%;
  border-collapse: collapse;
}
.sessions-table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--dim);
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}
.sessions-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.sessions-table tr:hover { background: rgba(255,255,255,0.03); }

.status-dot {
  display: inline-block;
  width: 8px; height: 8px;
  border-radius: 50%;
  margin-right: 8px;
  vertical-align: middle;
}
.status-dot.queued { background: var(--dim); }
.status-dot.running { background: var(--yellow); animation: pulse 1.5s ease-in-out infinite; }
.status-dot.done { background: var(--green); }
.status-dot.failed { background: var(--red); }

.mini-bar-track {
  width: 80px; height: 4px;
  background: var(--well); border-radius: 2px;
  display: inline-block; vertical-align: middle;
  border-top: 1px solid rgba(255,255,255,0.1);
  border-bottom: 1px solid rgba(0,0,0,0.4);
  overflow: hidden;
}
.mini-bar-fill {
  height: 100%; background: var(--green); border-radius: 2px;
}
.mini-bar-fill.running { background: var(--yellow); }

.session-id {
  font-family: "JetBrains Mono", monospace;
  font-size: 11px;
  color: var(--dim);
}

.empty-state {
  text-align: center;
  padding: 60px 20px;
  color: var(--dim);
}
.empty-state .icon { font-size: 48px; margin-bottom: 16px; opacity: 0.3; }
.empty-state p { font-size: 14px; }

.spinner {
  display: inline-block;
  width: 12px; height: 12px;
  border: 2px solid var(--dim);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin-right: 8px;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Modal ──────────────────────────────────────────────────── */
.modal {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.75); z-index: 100;
  align-items: center; justify-content: center;
}
.modal.open { display: flex; }
.modal-panel {
  background: var(--panel); border-radius: 8px;
  width: 85%; max-width: 900px; max-height: 80vh;
  overflow: auto;
  box-shadow: 0 8px 32px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.1);
}
.modal-head { padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; }
.modal-close { background: none; border: none; color: var(--dim); font-size: 20px; cursor: pointer; }
.modal-body { padding: 20px; }
.modal-body pre { background: var(--well); padding: 12px; border-radius: 4px; font-family: "JetBrains Mono", monospace; font-size: 11px; white-space: pre-wrap; word-break: break-all; max-height: 250px; overflow: auto; }
.section { margin-bottom: 16px; }
.section h3 { font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--dim); margin-bottom: 6px; }

/* ── Refresh button ─────────────────────────────────────────── */
.refresh-btn {
  position: fixed;
  bottom: 20px; right: 20px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-top: 1px solid rgba(255,255,255,0.1);
  border-bottom: 1px solid rgba(0,0,0,0.4);
  color: var(--dim);
  padding: 8px 16px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  z-index: 100;
}
.refresh-btn:hover { color: var(--text); }
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <h1>SOMA DASHBOARD</h1>
    <div class="session-info" id="session-info">
      <span class="session-name" id="session-name">No session</span>
      <span class="session-meta" id="session-meta">
        <span>Chatsets <b id="h-total-cs">0</b></span>
        <span>Current <b id="h-current-cs">0</b></span>
      </span>
      <span class="session-chars" id="session-chars">
        <span>Total <b id="h-total-chars">0</b></span>
        <span>Current <b id="h-current-chars">0</b></span>
      </span>
    </div>
  </div>
  <div class="stats">
    <span>Events <b id="s-events">0</b></span>
    <span>Done <b id="s-done" style="color:var(--green)">0</b></span>
    <span>Failed <b id="s-fail" style="color:var(--red)">0</b></span>
    <span>Running <b id="s-run" style="color:var(--yellow)">0</b></span>
  </div>
</div>

<!-- Tab bar -->
<div class="tab-bar">
  <div class="tab active" data-tab="orbs">Orbs</div>
  <div class="tab" data-tab="sessions">Sessions</div>
</div>

<!-- ═══════════════ ORBS TAB ═══════════════ -->
<div class="tab-content active" id="tab-orbs">
<div class="toolbar">
  <button id="prev">←</button>
  <span class="page-info" id="page-info">1 / 1</span>
  <button id="next">→</button>
  <button id="failed-only" class="failed-only-btn">Failed Only</button>
  <input type="text" id="search" placeholder="Filter…">
  <button id="search-clear">×</button>
</div>
<div class="orb-grid" id="orbs"></div>
</div>

<!-- ═══════════════ SESSIONS TAB ═══════════════ -->
<div class="tab-content" id="tab-sessions">
<div class="sessions-panel">
  <div class="overall-bar">
    <div class="overall-stats">
      <div class="overall-stat">
        <div class="label">Sessions</div>
        <div class="value" id="ses-total">—</div>
      </div>
      <div class="overall-stat">
        <div class="label">Chat Sets</div>
        <div class="value" id="sets-total">—</div>
      </div>
      <div class="overall-stat">
        <div class="label">Done</div>
        <div class="value green" id="sets-done">—</div>
      </div>
      <div class="overall-stat">
        <div class="label">Completion</div>
        <div class="value green" id="overall-pct">—</div>
      </div>
    </div>
    <div class="progress-track">
      <div class="progress-fill" id="progress-fill" style="width:0%"></div>
    </div>
  </div>
  <table class="sessions-table">
    <thead>
      <tr>
        <th>Status</th>
        <th>Session</th>
        <th>Chat Sets</th>
        <th>Progress</th>
      </tr>
    </thead>
    <tbody id="sessions-body">
      <tr><td colspan="4" class="empty-state">
        <div class="icon">📋</div>
        <p>No session queue loaded.</p>
        <p style="font-size:12px; margin-top:8px;">Run <code style="color:var(--accent)">run_sessions.py</code> to start a batch.</p>
      </td></tr>
    </tbody>
  </table>
</div>
</div>

<div class="popup" id="popup">
  <div class="popup-head">
    <div class="popup-nav">
      <button id="pop-prev">←</button>
      <input type="text" id="pop-num" value="1">
      <button id="pop-next">→</button>
      <span id="pop-title">Chatset</span>
    </div>
    <button class="popup-close" id="pop-close">×</button>
  </div>
  <div class="popup-body" id="popup-body"></div>
</div>

<div class="modal" id="modal">
  <div class="modal-panel">
    <div class="modal-head">
      <h2 id="modal-title">Stage</h2>
      <button class="modal-close" id="modal-close">×</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<button class="refresh-btn" id="refresh-btn">↻ Refresh</button>

<script>
// ── State ────────────────────────────────────────────────────────
const STAGES = [
  ['Ingestion', ['distiller','introbeaver','planbeaver','outrobeaver']],
  ['Blueprint', ['nexus','spark','falcon']],
  ['Build', ['eagle','drafter','scout']],
  ['Audit', ['oc']],
  ['Harden', ['olc']]
];
const PER_PAGE = 50;
let events = [];
let rows = {};
let page = 0;
let currentOrb = null;
let failedOnly = false;

// ── Header updates ─────────────────────────────────────────────
function updateHeader() {
  // Auto-detect current session from events
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
    document.getElementById('session-name').textContent = 'No session';
    document.getElementById('h-total-cs').textContent = '0';
    document.getElementById('h-current-cs').textContent = '0';
    document.getElementById('h-total-chars').textContent = '0';
    document.getElementById('h-current-chars').textContent = '0';
    return;
  }

  // Prefer the running session; otherwise pick the last one
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

  const nameEl = document.getElementById('session-name');
  nameEl.textContent = info.title || activeSid;
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

// ── Orbs logic ───────────────────────────────────────────────────
function group() {
  rows = {};
  events.forEach(e => {
    const key = e.chatset_id || (e.session_id + ':' + e.chatset_number);
    if (!rows[key]) rows[key] = { id: key, set: e.chatset_number, stages: {} };
    rows[key].stages[e.stage] = e;
  });
}

function orbStatus(r) {
  const sts = Object.values(r.stages).map(s => (s.status||'').toLowerCase());
  if (sts.some(s => s === 'failed')) return 'failed';
  if (sts.some(s => s === 'running')) return 'running';
  if (sts.length && sts.every(s => s === 'done')) return 'done';
  return '';
}

function renderOrbs() {
  group();
  const list = Object.values(rows).sort((a,b) => (a.set||0) - (b.set||0));
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = list.filter(r => {
    const matchSearch = !q || String(r.set).includes(q) || r.id.toLowerCase().includes(q);
    const matchFailed = !failedOnly || orbStatus(r) === 'failed';
    return matchSearch && matchFailed;
  });
  const pages = Math.max(1, Math.ceil(filtered.length / PER_PAGE));
  page = Math.min(page, pages - 1);
  const slice = filtered.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  document.getElementById('orbs').innerHTML = slice.map(r => {
    const st = orbStatus(r);
    return `<div class="orb ${st}" data-id="${r.id}">${r.set || '?'}</div>`;
  }).join('');
  document.getElementById('page-info').textContent = `${page + 1} / ${pages}`;
  document.getElementById('s-events').textContent = events.length;
  document.getElementById('s-done').textContent = list.filter(r => orbStatus(r) === 'done').length;
  document.getElementById('s-fail').textContent = list.filter(r => orbStatus(r) === 'failed').length;
  document.getElementById('s-run').textContent = list.filter(r => orbStatus(r) === 'running').length;
}

document.getElementById('failed-only').onclick = function() {
  failedOnly = !failedOnly;
  this.classList.toggle('pressed', failedOnly);
  page = 0;
  renderOrbs();
};
document.getElementById('search').oninput = function() {
  renderOrbs();
  document.getElementById('search-clear').style.display = this.value ? 'inline' : 'none';
};
document.getElementById('search-clear').onclick = function() {
  document.getElementById('search').value = '';
  document.getElementById('search-clear').style.display = 'none';
  renderOrbs();
};
document.getElementById('prev').onclick = () => { page = Math.max(0, page - 1); renderOrbs(); };
document.getElementById('next').onclick = () => { page++; renderOrbs(); };
document.getElementById('refresh-btn').onclick = () => { refreshAll(); };

// ── Popup ────────────────────────────────────────────────────────
function openPopup(id) {
  currentOrb = id;
  const r = rows[id];
  if (!r) return;
  document.getElementById('pop-title').textContent = `Chatset ${r.set || id}`;
  document.getElementById('pop-num').value = r.set || '';
  let html = '';
  STAGES.forEach(([group, stages]) => {
    html += '<div class="stage-strip">';
    stages.forEach(stage => {
      const ev = r.stages[stage] || {};
      const st = (ev.status||'').toLowerCase();
      const cls = st === 'done' ? 'done' : st === 'failed' ? 'failed' : st === 'running' ? 'running' : '';
      const attCount = typeof ev.attempts === 'number' ? ev.attempts : (Array.isArray(ev.attempts) ? ev.attempts.length : 0);
      const retry = (attCount > 1) ? `<span class="retry" data-stage="${stage}">${attCount}</span>` : '';
      const outputId = 'out-' + stage + '-' + r.set;
      let effHtml = '';
      if (ev.prompt_tokens && ev.prompt_tokens > 0 && ev.chars_in) {
        const promptEff = (ev.chars_in / ev.prompt_tokens).toFixed(1);
        const warn = promptEff < 2;
        const effCls = warn ? 'warn' : 'ok';
        const compEff = ev.completion_tokens && ev.raw_response ? (ev.raw_response.length / ev.completion_tokens).toFixed(1) : '—';
        const warnNote = warn ? ' ⚠' : '';
        effHtml = `<div class="token-tooltip"><span class="${effCls}">${promptEff} ch/token${warnNote}</span> · ${compEff} out/token</div>`;
      }
      let cascadeHtml = '';
      if (attCount > 1) {
        const attList = typeof ev.attempts === 'number' ? [] : (Array.isArray(ev.attempts) ? ev.attempts : []);
        cascadeHtml = `<div class="retry-cascade" id="cascade-${r.set}-${stage}">`;
        attList.forEach((a, i) => {
          const isError = a.status === 'fail' || a.verdict === 'fail';
          cascadeHtml += `<div class="attempt ${isError ? 'fail' : 'pass'}">${i+1}: ${a.model || '?'} ${isError ? '✗' : '✓'}</div>`;
        });
        cascadeHtml += '</div>';
      }
      html += `<div class="stage-card ${cls}" data-stage="${stage}">
        ${retry}${effHtml}${cascadeHtml}
        <h3>${stage}</h3>
        <div class="meta">set ${r.set || ''}</div>
        <div class="meta">${ev.chars_in || '—'} chars</div>
        <div class="meta">${ev.model || '—'}</div>
        <button class="btn" data-action="output" data-output="${outputId}">Output</button>
        <div class="output-toggle" id="${outputId}">${ev.raw_response ? JSON.stringify(ev.raw_response, null, 2).substring(0, 2000) : '—'}</div>
      </div>`;
    });
    html += '</div>';
  });
  document.getElementById('popup-body').innerHTML = html;
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
    const outId = outBtn.dataset.output;
    const outEl = document.getElementById(outId);
    if (outEl) outEl.classList.toggle('show');
    return;
  }
  const retryBadge = e.target.closest('.retry');
  if (retryBadge) {
    const stage = card.dataset.stage;
    const setId = rows[currentOrb]?.set ?? '';
    const cascade = document.getElementById(`cascade-${setId}-${stage}`);
    if (cascade) document.querySelectorAll('.retry-cascade.open').forEach(c => { if (c !== cascade) c.classList.remove('open'); });
    cascade?.classList.toggle('open');
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
document.getElementById('pop-num').onchange = function() {
  const n = parseInt(this.value);
  const list = Object.values(rows).sort((a,b) => (a.set||0) - (b.set||0));
  const found = list.find(r => r.set === n);
  if (found) openPopup(found.id);
};

function stepOrb(dir) {
  const list = Object.values(rows).sort((a,b) => (a.set||0) - (b.set||0));
  const idx = list.findIndex(r => r.id === currentOrb);
  const next = list[idx + dir];
  if (next) openPopup(next.id);
}

// ── Modal ────────────────────────────────────────────────────────
function openModal(stage) {
  const r = rows[currentOrb];
  if (!r) return;
  const ev = r.stages[stage] || {};
  document.getElementById('modal-title').textContent = `${stage} · ${ev.model || ''}`;
  document.getElementById('modal-body').innerHTML = `
    <div class="section"><h3>Gate Error</h3><pre>${ev.errors || 'none'}</pre></div>
    <div class="section"><h3>Raw Response</h3><pre>${ev.raw || ev.response || '—'}</pre></div>
    <div class="section"><h3>Payload</h3><pre>${JSON.stringify(ev.payload || {}, null, 2)}</pre></div>
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

// ── Sessions tab ─────────────────────────────────────────────────
let sessionPollTimer = null;

async function loadSessions() {
  try {
    const res = await fetch('/progress');
    const data = await res.json();
    
    const sessions = data.sessions || [];
    const total = data.total || 0;
    const pct = data.overall_pct || 0;
    const totalSets = data.total_sets || 0;
    const doneSets = data.done_sets || 0;

    // Update overall stats
    document.getElementById('ses-total').textContent = total;
    document.getElementById('sets-total').textContent = totalSets.toLocaleString();
    document.getElementById('sets-done').textContent = doneSets.toLocaleString();
    document.getElementById('overall-pct').textContent = pct.toFixed(1) + '%';

    // Update progress bar
    const fill = document.getElementById('progress-fill');
    fill.style.width = pct + '%';
    if (pct > 0 && pct < 100) {
      fill.classList.add('running');
    } else {
      fill.classList.remove('running');
    }

    // Build table
    const tbody = document.getElementById('sessions-body');
    if (sessions.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state">
        <div class="icon">📋</div>
        <p>No session queue loaded.</p>
        <p style="font-size:12px; margin-top:8px;">Run <code style="color:var(--accent)">run_sessions.py</code> to start a batch.</p>
      </td></tr>`;
      return;
    }

    tbody.innerHTML = sessions.map(s => {
      const statusClass = s.status; // queued, running, done, failed
      const statusLabel = s.status.charAt(0).toUpperCase() + s.status.slice(1);
      const setId = (s.id || '').substring(0, 40);
      const miniPct = s.progress || 0;
      const miniClass = (s.status === 'running' || (s.status === 'queued' && miniPct > 0)) ? 'mini-bar-fill running' : 'mini-bar-fill';
      const setsStr = `${s.sets_done}/${s.sets_total}`;
      return `<tr>
        <td><span class="status-dot ${statusClass}"></span>${statusLabel}</td>
        <td><span class="session-id" title="${s.path}">${setId}${s.id.length > 40 ? '…' : ''}</span></td>
        <td>${setsStr}</td>
        <td><div class="mini-bar-track"><div class="${miniClass}" style="width:${miniPct}%"></div></div> ${miniPct.toFixed(0)}%</td>
      </tr>`;
    }).join('');
  } catch (e) {
    // Fetch failed, that's ok
  }
}

function refreshAll() {
  renderOrbs();
  loadSessions();
  updateHeader();
}

// ── SSE + Polling ────────────────────────────────────────────────
const es = new EventSource('/events');
es.onmessage = e => {
  try { events.push(JSON.parse(e.data)); renderOrbs(); } catch(x) {}
  if (currentOrb) openPopup(currentOrb);
};
fetch('/data').then(r => r.json()).then(d => { events = d; renderOrbs(); }).catch(()=>{});

// Poll for progress updates
setInterval(() => {
  fetch('/data').then(r=>r.json()).then(d => {
    if (d.length !== events.length) { events = d; renderOrbs(); }
  }).catch(()=>{});
  // Also poll sessions tab if visible
  if (document.getElementById('tab-sessions').classList.contains('active')) {
    loadSessions();
  }
}, 3000);

// Initial load
renderOrbs();
loadSessions();
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
