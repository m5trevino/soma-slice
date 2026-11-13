#!/usr/bin/env python3
"""Session batch launcher for SOMA pipeline.

Ties together the session picker (web UI), pipeline execution, and
dashboard progress tracking in one workflow.

Usage:
    python run_sessions.py                  # Interactive picker → run pipeline
    python run_sessions.py --queue FILE     # Use existing queue or export file
    python run_sessions.py --progress       # Just show progress, don't run
    python run_sessions.py --dashboard      # Start dashboard only
    python run_sessions.py --all            # Process ALL sessions on disk

Pipeline execution:
    The engine is invoked via soma/run_engine.py with --queue support.
    Requires a TurnKey profile (auto-detected from turn-key-profiles/).

Dashboard:
    Runs on port 9877. Two tabs:
      - ORBS: per-chatset stage cards (existing view)
      - SESSIONS: batch progress queue (new view)

    Watch progress at: http://127.0.0.1:9877

Session Picker Integration (NEW):
    1. ./soma-session-picker/start.sh     # Starts picker (port 5174)
    2. Pick + export favorites in browser
    3. python run_sessions.py --queue data/soma_queue.json
    The queue file contains session IDs that resolve to on-disk paths.
    Full export JSON (with metadata, anchors, topics) is also supported.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
import urllib.request
import webbrowser
from pathlib import Path


SOMA_DIR = Path(__file__).parent
DATA_DIR = SOMA_DIR / "data"
DEBUG_DIR = "/tmp/soma_debug"
QUEUE_FILE = str(DATA_DIR / "soma_queue.json")
QUEUE_FILE_ALT = str(DATA_DIR / "queue.json")
LAUNCH_CONFIG = SOMA_DIR / "data" / "soma_launch.json"
BASE_PATH_FILE = DATA_DIR / ".base_path"
PORT = 9877
SOMA_PKG = SOMA_DIR / "soma"
SOMA_RUNNER = SOMA_PKG / "run_engine.py"

# The banner lives beside the engine so the CLI and the board render the same
# art from one source.
if str(SOMA_PKG) not in sys.path:
    sys.path.insert(0, str(SOMA_PKG))
try:
    from banner import print_banner
except ImportError:  # a missing banner must never stop a run
    def print_banner(color: bool | None = None) -> None:
        return None

def ensure_debug_dir():
    """Create the debug directory."""
    Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)


def find_profile() -> str | None:
    """Auto-detect TurnKey profile from turn-key-profiles/."""
    for candidate in [
        SOMA_DIR / "soma" / "turn-key-profiles" / "soma-current.json",
        SOMA_DIR / "soma" / "turn-key-profiles" / "*profile*.json",
    ]:
        if candidate.exists():
            return str(candidate)
        patterns = list(SOMA_DIR.glob(str(candidate)))
        if patterns:
            return str(patterns[0])
    for candidate in [
        SOMA_DIR / "soma-current.json",
        SOMA_DIR / "*profile*.json",
    ]:
        if candidate.exists():
            return str(candidate)
        patterns = list(SOMA_DIR.glob(str(candidate)))
        if patterns:
            return str(patterns[0])
    return None


def load_queue(path: str) -> tuple[list[str], list[dict]] | None:
    """Load session queue from JSON file.

    Supports three formats:
      1. Queue format:       {"selected": ["id1", "id2"], "count": 2, ...}
      2. Full export JSON:   [{...session objects with metadata...}]
      3. Plain string array: ["id1", "id2"]

    Returns (session_ids, full_objects) where:
      - session_ids: list of session identifiers to resolve
      - full_objects: list of full session dicts (for metadata, anchors, etc.)
        — empty if the file was in queue format
    """
    qpath = Path(path)
    if not qpath.exists():
        print(f"  Queue file not found: {path}")
        return None
    try:
        data = json.loads(qpath.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  Failed to read queue: {e}")
        return None

    if not data:
        print(f"  Queue is empty: {path}")
        return None

    # Format 1: Full export JSON — array of session objects
    if isinstance(data, list) and len(data) > 0:
        first = data[0]
        if isinstance(first, dict) and "session_id" in first:
            ids = [s.get("session_id", "") for s in data if isinstance(s, dict)]
            return (ids, data)

    # Format 2: Queue format — {"selected": [...]}
    if isinstance(data, dict):
        ids = data.get("selected", [])
        if isinstance(ids, list) and len(ids) > 0:
            return (ids, [])

    # Format 3: Plain array of strings
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], str):
        return (data, [])

    print(f"  Unknown queue format in: {path}")
    return None


def resolve_sessions_from_ids(session_ids: list[str]) -> list[str]:
    """Resolve session IDs (from the picker) to on-disk session paths.

    The session picker writes session_ids like:
      "26-01-12-peacock.project.timeline.revealed"

    These resolve to /media/storage/dendritic123/chat-logs/sessions/<dir_name>
    where dir_name = session_id.split("/")[-1].
    """
    vault = Path("/media/storage/dendritic123/chat-logs/sessions")
    resolved = []
    for sid in session_ids:
        if not sid:
            continue
        # Exact path first: a consolidated session (lineage/consolidated/<key>)
        # must not be shadowed by a same-named source session under the vault.
        exact = Path(sid)
        if exact.is_dir():
            resolved.append(str(exact))
            continue
        dir_name = sid.split("/")[-1]
        candidate = vault / dir_name
        if candidate.exists():
            resolved.append(str(candidate))
        elif (vault / sid).exists():
            resolved.append(str(vault / sid))
        else:
            d = Path(sid)
            if d.exists():
                resolved.append(str(d))
            else:
                print(f"    \u26a0 Session not found on disk: {sid}")
    return resolved


def load_session_info(path: str) -> dict:
    """Load session metadata from session.json."""
    sj = Path(path) / "session.json"
    if sj.exists():
        try:
            return json.loads(sj.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def interactive_pick():
    """Run the curses session picker and save queue."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOMA_DIR / "soma") + ":" + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "soma.machinetalk.session_picker"],
        cwd=str(SOMA_DIR),
        env=env,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  Session picker error:\n{result.stderr}")
        return None, []

    paths = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("  /"):
            paths.append(line[2:])

    if not paths:
        print("  No sessions selected.")
        return None, []

    queue_data = {
        "selected": paths,
        "count": len(paths),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    qpath = Path(QUEUE_FILE)
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(json.dumps(queue_data, indent=2))
    print(f"\n  Saved {len(paths)} sessions to {QUEUE_FILE}")
    return paths, []


def run_pipeline(paths: list[str], full_objects: list[dict] | None = None,
                 max_chatsets: int | None = None,
                 profile: str | None = None,
                 workers: int | None = None,
                 step_mode: bool = False,
                 stage: str | None = None,
                 granular: bool = False,
                 force: bool = False,
                 only: list[tuple[str, int]] | None = None):
    """Run the SOMA pipeline on the selected sessions.

    Uses soma/run_engine.py with --queue for native queue consumption.
    This is the canonical runner — it supports debug dashboard, checkpointing,
    and the full MachineTalk pipeline stages.

    Parameters
    ----------
    profile : str | None
        Explicit profile path. Falls back to find_profile() if None.
    workers : int | None
        Number of parallel workers. Defaults to 2 if not specified.
    """
    # Step mode pauses after every build stage. It is opt-in: running from a
    # terminal no longer forces it, because an unattended run that stops on a
    # prompt stalls with no one there to press Enter.
    ensure_debug_dir()

    if profile is None:
        profile = find_profile()
    if not profile:
        print("  ERROR: No TurnKey profile found!")
        print("  Place a profile JSON in soma/turn-key-profiles/")
        print("  Or use: python run_sessions.py --profile /path/to/profile.json")
        return

    print_banner()

    print(f"\n  Profile: {profile}")
    print(f"  Sessions: {len(paths)}")
    print(f"  Debug dir: {DEBUG_DIR}")
    print(f"  Dashboard: http://127.0.0.1:{PORT}")
    if max_chatsets is not None:
        print(f"  Max chatsets: {max_chatsets}")

    # Show metadata summary if we have full export objects
    if full_objects:
        def _labels(value) -> list[str]:
            """Export metadata is either a string, a list, or a list of
            {name, confidence} objects depending on the extractor version."""
            if not value:
                return []
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                label = value.get("name") or value.get("label") or ""
                return [str(label)] if label else []
            if isinstance(value, (list, tuple)):
                out: list[str] = []
                for item in value:
                    out.extend(_labels(item))
                return out
            return [str(value)]

        domains = set()
        sentiments = set()
        for obj in full_objects:
            md = obj.get("metadata", {})
            if isinstance(md, dict):
                domains.update(_labels(md.get("domain")))
                sentiments.update(_labels(md.get("sentiment")))
        if domains:
            print(f"  Domains:   {', '.join(sorted(domains))}")
        if sentiments:
            print(f"  Sentiments: {', '.join(sorted(sentiments))}")
    print()

    # Start dashboard
    if granular:
        print("\n  [Step: start dashboard. Enter to run, Ctrl+C to skip]")
        try: input()
        except EOFError: pass
    dashboard_proc = start_dashboard()

    # Write queue file for dashboard compatibility
    queue_data = {
        "selected": paths,
        "count": len(paths),
        "running": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile": profile,
    }
    qpath = Path(QUEUE_FILE)
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(json.dumps(queue_data, indent=2))

    # Also write to legacy queue location for dashboard compatibility
    legacy_qpath = Path(QUEUE_FILE_ALT)
    legacy_qpath.parent.mkdir(parents=True, exist_ok=True)
    legacy_qpath.write_text(json.dumps(queue_data, indent=2))

    # Write full export JSON if we have metadata from the picker
    if full_objects:
        export_path = Path(str(QUEUE_FILE).replace("soma_queue.json", "soma_export.json"))
        export_path.write_text(json.dumps(full_objects, indent=2))

    # Run the SOMA engine via the canonical runner with --queue
    cmd = [
        sys.executable, str(SOMA_RUNNER),
        "--queue", str(qpath),
        "--profile", profile,
        "--debug", DEBUG_DIR,
        "--out", str(SOMA_DIR / "soma" / "output"),
        "--port", str(PORT),
        "--workers", str(workers if workers is not None else 2),
    ]
    if max_chatsets is not None and max_chatsets > 0:
        cmd.extend(["--max-chatsets", str(max_chatsets)])
    if step_mode:
        cmd.append("--step-mode")
    if stage:
        cmd.extend(["--stage", stage])
    if granular:
        cmd.append("--granular")
    if force:
        cmd.append("--force")
    for spec in (only or []):
        # Accept either a "session:set" string from the CLI or an already
        # split (session, set) tuple from the interactive picker.
        if isinstance(spec, str):
            sid, _, setnum = spec.rpartition(":")
            if not sid or not setnum.isdigit():
                print(f"  Error: --only expects SESSION:SET, got {spec!r}")
                return
            cmd.extend(["--only", f"{sid}:{setnum}"])
        else:
            sid, setnum = spec
            cmd.extend(["--only", f"{sid}:{setnum}"])

    exit_code = None
    try:
        # Inherit stdio so stage output (and granular prompts) stream live
        # instead of being buffered until the subprocess exits.
        result = subprocess.run(
            cmd,
            cwd=str(SOMA_DIR),
            env=os.environ,
            timeout=36000,
        )
        exit_code = result.returncode

        if result.returncode == 0:
            print(f"\n  \u2713 Pipeline complete")
        else:
            print(f"\n  \u2717 Pipeline failed (exit {result.returncode})")
    except subprocess.TimeoutExpired:
        print("\n  \u2717 Pipeline timed out (10h limit)")
    except KeyboardInterrupt:
        print("\n  Pipeline interrupted.")
        queue_data["running"] = False
        queue_data["interrupted"] = True
        queue_data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        qpath.write_text(json.dumps(queue_data, indent=2))
        if dashboard_proc:
            dashboard_proc.terminate()
        return

    queue_data["running"] = False
    queue_data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    queue_data["exit_code"] = exit_code
    qpath.write_text(json.dumps(queue_data, indent=2))

    print(f"\n  Dashboard: http://127.0.0.1:{PORT}")
    print(f"  Press Ctrl+C to stop the dashboard.")


def start_dashboard():
    """Start the debug dashboard server as a background process."""
    ensure_debug_dir()

    cmd = [
        sys.executable, str(SOMA_DIR / "run_dashboard.py"),
        "--no-open",
        "--port", str(PORT),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(SOMA_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    return proc


def show_progress():
    """Show current pipeline progress from queue and checkpoint data."""
    result = load_queue(QUEUE_FILE)
    if result is None:
        result = load_queue(QUEUE_FILE_ALT)
    if result is None:
        print("  No queue file found. Run with --queue or use the picker.")
        return

    session_ids, full_objects = result
    paths = resolve_sessions_from_ids(session_ids)

    print(f"\n  Session Queue: {len(paths)} sessions\n")

    # Check progress from dashboard /progress endpoint
    try:
        import urllib.request
        resp = urllib.request.urlopen(f'http://127.0.0.1:{PORT}/progress', timeout=3)
        data = json.loads(resp.read())
        sessions = data.get("sessions", [])
        pct = data.get("overall_pct", 0)

        total = data.get("total", len(sessions))
        total_sets = data.get("total_sets", 0)
        done_sets = data.get("done_sets", 0)

        print(f"  Sessions: {total}  |  Chat Sets: {total_sets}  |  Done: {done_sets}  |  {pct:.1f}%")
        print()

        for s in sessions:
            status = s.get("status", "?")
            sid = (s.get("id") or "?")[:50]
            sets = f"{s.get('sets_done', 0)}/{s.get('sets_total', 0)}"
            bar = ""
            p = s.get("progress", 0)
            bar_len = 20
            filled = int(bar_len * p / 100)
            bar = "\u2588" * filled + "\u25ca" * (bar_len - filled)
            status_icon = {"done": "\u2713", "running": "\u2192", "queued": "\u25cb", "failed": "\u2717"}.get(status, "?")
            print(f"  {status_icon} [{bar}] {pct:>5.0f}%  {sid}")
        return
    except Exception:
        pass

    # Fallback: check checkpoints on disk
    done_count = 0
    running_count = 0
    pending_count = 0

    for path in paths:
        sid = Path(path).name
        found = False
        for sd in SOMA_PKG.glob("*/"):
            cp = sd / "checkpoints" / f"{sid}.chunks.checkpoint.json"
            if not cp.exists():
                cp = sd / "checkpoints" / f"{sid}.checkpoint.json"
            if cp.exists():
                found = True
                try:
                    cp_data = json.loads(cp.read_text())
                    cs = cp_data.get("chatsets", {})
                    done_sets = sum(
                        1 for c in cs.values()
                        if all(
                            st.get("status") in ("DONE", "SKIPPED")
                            for st in c.get("stages", {}).values()
                        )
                        for _ in [1]
                    )
                    total_sets = len(cs)
                    if done_sets == total_sets and total_sets > 0:
                        done_count += 1
                    elif total_sets > 0:
                        running_count += 1
                    else:
                        pending_count += 1
                except Exception:
                    pending_count += 1
                break

        if not found:
            pending_count += 1

    total = done_count + running_count + pending_count
    pct = (done_count / total * 100) if total > 0 else 0

    print(f"  Done:   {done_count}")
    print(f"  Running: {running_count}")
    print(f"  Pending: {pending_count}")
    print(f"  Overall: {pct:.1f}% complete\n")

    print(f"  Sessions:")
    for i, path in enumerate(paths[:20]):
        sid = Path(path).name
        meta = load_session_info(path)
        title = meta.get("title", sid)
        sets = meta.get("total_sets", "?")
        print(f"    [{i+1}] {title} ({sets} sets)")
    if len(paths) > 20:
        print(f"    ... and {len(paths) - 20} more")


def start_dashboard_only():
    """Start the debug dashboard server only."""
    ensure_debug_dir()
    proc = start_dashboard()
    print(f"\n  Dashboard running at http://127.0.0.1:{PORT}")
    print("  Press Ctrl+C to stop.\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


def start_picker() -> subprocess.Popen | None:
    """Launch the session picker server, wait until reachable, open browser.

    Runs soma-session-picker/start.sh in the background, polls
    http://localhost:5174 until it returns 200 (or 30 s), prints the URL,
    opens the browser, and returns the process handle.
    """
    picker_script = Path(__file__).parent / "soma-session-picker" / "start.sh"
    if not picker_script.exists():
        print(f"  ERROR: Picker script not found at {picker_script}")
        return None

    env = os.environ.copy()
    env["SESSIONS_PATH"] = "/media/storage/dendritic123/chat-logs/sessions"
    env["QUEUE_OUTPUT_PATH"] = str(Path(__file__).parent / QUEUE_FILE)

    print(f"\n  Launching session picker from {picker_script}")
    proc = subprocess.Popen(
        ["bash", str(picker_script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    url = "http://localhost:5174"
    deadline = time.monotonic() + 30
    print(f"  Waiting for picker at {url}...")
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.getcode() == 200:
                print(f"  Picker is ready at {url}")
                webbrowser.open(url)
                print(f"  Process PID: {proc.pid}")
                return proc
        except Exception:
            pass
        time.sleep(0.5)

    print(f"  ERROR: Picker did not become ready within 30 s")
    proc.terminate()
    return None


def prompt_for_json_path() -> str:
    """Prompt the user for a JSON file path after the picker is ready.

    Validates that the file exists and contains valid JSON.
    Re-prompts on invalid input, missing file, or empty input.
    """
    while True:
        path = input("  Enter the path to the exported JSON file: ").strip()
        if not path:
            print("  Empty path. Please enter a valid JSON file path.")
            continue
        qpath = Path(path)
        if not qpath.exists():
            print(f"  File not found: {path}")
            continue
        try:
            json.loads(qpath.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Invalid JSON ({e}). Please enter a valid file.")
            continue
        return path


# ── Base Path Management ─────────────────────────────────────────────

def _load_base_path() -> str | None:
    """Load the project base path from data/.base_path.

    Returns None if the file does not exist or is empty.
    """
    if not BASE_PATH_FILE.exists():
        return None
    try:
        path = BASE_PATH_FILE.read_text().strip()
        if path:
            return path
    except OSError:
        pass
    return None


def _save_base_path(path: str) -> None:
    """Persist the project base path to data/.base_path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BASE_PATH_FILE.write_text(path + "\n")


def _prompt_and_save_base_path() -> str:
    """On first run, prompt the user for the project base path.

    Default suggestion is the directory containing run_sessions.py.
    Accepts the default (Enter) or a custom path.
    """
    default = str(SOMA_DIR)
    print()
    print(f"  Project base path not configured.")
    print(f"  Default: {default}")
    while True:
        raw = input(f"  Base path [{default}]: ").strip()
        if not raw:
            path = default
            break
        candidate = Path(raw)
        if not candidate.exists():
            print(f"  Path does not exist: {raw}")
            continue
        path = str(candidate.resolve())
        break
    _save_base_path(path)
    print(f"  → Base path saved: {path}")
    return path


def _get_base_dir() -> Path:
    """Resolve the project base directory.

    Reads from data/.base_path if available; otherwise falls back
    to SOMA_DIR (directory of run_sessions.py).
    """
    bp = _load_base_path()
    if bp:
        return Path(bp).resolve()
    return SOMA_DIR


# ── Launcher Helpers ─────────────────────────────────────────────────

def _get_last_state() -> dict:
    """Load last-used values from data/.last_* files."""
    state = {}
    for key in ("profile", "queue", "workers"):
        p = DATA_DIR / f".last_{key}"
        if p.exists():
            state[key] = p.read_text().strip()
    return state


def _save_state(state: dict) -> None:
    """Persist last-used values to data/.last_* files."""
    for key in ("profile", "queue", "workers"):
        if key in state:
            p = DATA_DIR / f".last_{key}"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(state[key] + "\n")


def _discover_profiles() -> list[dict]:
    """Discover TurnKey profile JSON files from <base>/turn-key-profile/.

    Returns list of {"path": str, "name": str} dicts sorted by name.
    """
    profiles = []
    seen = set()
    profile_dir = _get_base_dir() / "turn-key-profile"
    if profile_dir.exists():
        for p in sorted(profile_dir.glob("*.json")):
            if p.name not in seen and p.suffix == ".json":
                seen.add(p.name)
                profiles.append({"path": str(p), "name": p.name})
    # Also check legacy location
    legacy_dir = SOMA_DIR / "soma" / "turn-key-profiles"
    if legacy_dir.exists():
        for p in sorted(legacy_dir.glob("*profile*.json")):
            if p.name not in seen and p.suffix == ".json":
                seen.add(p.name)
                profiles.append({"path": str(p), "name": p.name})
    return profiles


def _launch_profile_builder() -> None:
    """Launch the profile builder server and block until it exits.

    Starts the Solid launcher, opens the browser, and waits for
    the server process to terminate.
    """
    builder_script = Path("/home/flintx/turn-key/profile_builder/solid/launch.sh")
    if not builder_script.exists():
        print(f"  ERROR: Profile builder not found at {builder_script}")
        return

    print(f"\n  Launching profile builder from {builder_script}")
    proc = subprocess.Popen(
        ["bash", str(builder_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    url = "http://127.0.0.1:5173"
    deadline = time.monotonic() + 30
    print(f"  Waiting for builder at {url}...")
    ready = False
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.getcode() == 200:
                print(f"  Builder is ready at {url}")
                webbrowser.open(url)
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ready:
        print(f"  Builder did not become ready within 30 s")
        proc.terminate()
        proc.wait()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n  Profile builder interrupted.")
        proc.terminate()


def _select_profile(state: dict) -> str:
    """Step 1: Interactive profile selection with last-used highlighting."""
    profiles = _discover_profiles()
    if not profiles:
        print("\n  No TurnKey profiles found.")
        answer = input("  Open the profile builder? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            _launch_profile_builder()
            profiles = _discover_profiles()
            if not profiles:
                print("\n  No profiles found after builder closed. Exiting.")
                sys.exit(1)
        else:
            print("\n  No profiles. Exiting.")
            sys.exit(1)

    last = state.get("profile")
    last_idx = next(
        (i for i, p in enumerate(profiles) if p["path"] == last),
        None,
    )

    print("\n  ═══════════════════════════════════════════════════════")
    print("  Step 1/4 — TurnKey Profile")
    print("  ═══════════════════════════════════════════════════════")
    print()
    print("  Profiles:")
    for i, p in enumerate(profiles, 1):
        marker = " (last used)" if last_idx is not None and i == (last_idx + 1) else ""
        print(f"    [{i}] {p['name']}{marker}")
    print(f"    [n] New path…")
    print()

    while True:
        raw = input(f"  Select profile [{last_idx + 1 if last_idx is not None else 'n'}]: ").strip()
        if not raw:
            if last_idx is not None:
                path = profiles[last_idx]["path"]
                print(f"  → {profiles[last_idx]['name']}")
                return path
            print("  Please enter a selection.")
            continue
        if raw.lower() == "n":
            path = input("  Profile path: ").strip()
            if not path:
                print("  Empty path. Please enter a valid path or a number.")
                continue
            return path
        try:
            num = int(raw)
            if 1 <= num <= len(profiles):
                path = profiles[num - 1]["path"]
                print(f"  → {profiles[num - 1]['name']}")
                return path
        except ValueError:
            pass
        print(f"  Invalid. Enter 1-{len(profiles)}, 'n' for a new path.")


def _select_queue(state: dict) -> tuple[str, list[dict] | None]:
    """Step 2: Interactive queue/session selection.

    Reads *.json from session-picker-json/.
    Returns (queue_file_path, full_objects | None).
    """
    last_queue = state.get("queue")
    queue_dir = _get_base_dir() / "session-picker-json"
    queue_files = sorted(queue_dir.glob("*.json")) if queue_dir.exists() else []

    print("\n  ═══════════════════════════════════════════════════════")
    print("  Step 2/4 — Sessions / Queue")
    print("  ═══════════════════════════════════════════════════════")
    print()

    if queue_files:
        print("  Available queues:")
        for i, qf in enumerate(queue_files, 1):
            marker = " (last used)" if last_queue and qf.name == Path(last_queue).name else ""
            print(f"    [{i}] {qf.name}{marker}")
    else:
        print("  No session JSONs found in session-picker-json/.")

    print(f"    [p] Open web session picker…")
    print(f"    [s] Skip (will use defaults)")
    print()

    # ── Picker loop: re-scan after picker closes ───────────────────────
    while True:
        if not queue_files:
            answer = input("  Open the web session picker? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                picker_env = os.environ.copy()
                picker_env["SESSIONS_PATH"] = "/media/storage/dendritic123/chat-logs/sessions"
                picker_env["QUEUE_OUTPUT_PATH"] = str(SOMA_DIR / QUEUE_FILE)
                picker_script = SOMA_DIR / "soma-session-picker" / "start.sh"
                subprocess.run(["bash", str(picker_script)], cwd=str(picker_script.parent), env=picker_env)
                queue_files = sorted(queue_dir.glob("*.json")) if queue_dir.exists() else []
                if not queue_files:
                    print("  No session JSONs after picker. Returning.")
                    return "skip", None
                print("  Session JSONs found after picker.")
            else:
                return "skip", None

        raw = input(f"  Select queue [{last_queue if last_queue else 'p'}]: ").strip()

        # Reuse last queue
        if not raw and last_queue and Path(last_queue).exists():
            name = Path(last_queue).name
            print(f"  → {name}")
            result = load_queue(last_queue)
            full_objs = result[1] if result else []
            return last_queue, full_objs if full_objs else None

        # Numbered selection
        if not raw and queue_files:
            print("  Please enter a number, 'p', or 's'.")
            continue
        try:
            num = int(raw)
            if 1 <= num <= len(queue_files):
                qf = queue_files[num - 1]
                name = qf.name
                print(f"  → {name}")
                result = load_queue(str(qf))
                full_objs = result[1] if result else []
                return str(qf), full_objs if full_objs else None
        except ValueError:
            pass

        if raw.lower() == "p":
            return "web_picker", None
        if raw.lower() == "s":
            return "skip", None

        print(f"  Invalid. Enter 1-{len(queue_files)}, 'p' for picker, 's' to skip.")


def _select_workers(state: dict) -> int:
    """Step 3: Worker count selection."""
    last_workers = state.get("workers")
    default = int(last_workers) if last_workers and last_workers.isdigit() else 2

    print("\n  ═══════════════════════════════════════════════════════")
    print("  Step 3/4 — Workers")
    print("  ═══════════════════════════════════════════════════════")
    print()
    print(f"  How many parallel workers? (default: {default})")

    while True:
        raw = input(f"  Workers [{default}]: ").strip()
        if not raw:
            print(f"  → {default} workers")
            return default
        try:
            n = int(raw)
            if n < 1:
                print("  Please enter a number >= 1.")
                continue
            print(f"  → {n} workers")
            return n
        except ValueError:
            print("  Invalid. Enter a number >= 1.")


def run_launcher(granular: bool = False, force: bool = False) -> None:
    """4-step interactive launcher.

    Step 1 — Pick TurnKey profile
    Step 2 — Pick sessions / queue
    Step 3 — Pick worker count
    Step 4 — Summary + 3-2-1 countdown → run_pipeline()
    """
    state = _get_last_state()

    if granular:
        print("\n  [Step: profile selection]")
        print("  Enter to continue, Ctrl+C to skip")
        try: input()
        except EOFError: pass

    # Step 1: Profile
    profile = _select_profile(state)
    state["profile"] = profile

    if granular:
        print("\n  [Step: queue/session selection]")
        print("  Enter to continue, Ctrl+C to skip")
        try: input()
        except EOFError: pass

    # Step 2: Sessions / Queue
    queue_file, full_objects = _select_queue(state)
    state["queue"] = queue_file

    # Collect sessions early (needed for summary)
    session_ids = []
    paths = []
    if queue_file == "web_picker":
        run_web_picker_and_pipeline(QUEUE_FILE, granular=granular, force=force)
        return
    if queue_file != "skip":
        result = load_queue(queue_file)
        if result is None:
            print("  ERROR: Cannot load queue.")
            return
        session_ids, _ = result
        paths = resolve_sessions_from_ids(session_ids)
    else:
        paths = []

    # Step 3: Workers
    if granular:
        print("\n  [Step: workers]")
        print("  Enter to continue, Ctrl+C to skip")
        try: input()
        except EOFError: pass
    workers = _select_workers(state)
    state["workers"] = str(workers)

    # Session selection
    if granular:
        print("\n  [Step: session selection]")
        print("  Enter to continue, Ctrl+C to skip")
        try: input()
        except EOFError: pass
    pick = _prompt_session(paths)
    only = None
    max_chatsets = None
    selected_stage = None
    if pick:
        sid, first = pick
        total = next((t for s, f, t, _ in _load_session_entries(paths) if s == sid), 0)
        count = _prompt_chatset_count(total)
        if count == 1:
            only = [(sid, first)]
            selected_stage = "distiller"
        elif count is not None:
            only = [(sid, first + i) for i in range(count)]
    else:
        max_chatsets = _prompt_max_chatsets()

    # Save state
    _save_state(state)

    # Compute totals from the actual chunk-level source loader. Picker
    # metadata counts set_number groups, but the engine now processes one
    # physical chunk file per ChatSet.
    total_chatsets = 0
    total_characters = 0
    try:
        sys.path.insert(0, str(SOMA_PKG))
        from machinetalk.source import load_session
        for path in paths:
            try:
                sess = load_session(Path(path))
            except Exception:
                continue
            total_chatsets += len(sess.chatsets)
            total_characters += sum(cs.chars for cs in sess.chatsets)
    except Exception:
        pass

    # Fallback if no sessions could be loaded (for example a skip queue).
    if total_chatsets == 0:
        for obj in full_objects or []:
            if isinstance(obj, dict):
                total_chatsets += int(obj.get("total_sets") or 0)
                total_characters += int(obj.get("total_session_chars") or 0)

    # Step 4: Summary + countdown
    if granular:
        print("\n  [Step: summary + countdown]")
        print("  Enter to run, Ctrl+C to skip")
        try:
            answer = input().strip().lower()
            if answer == "n" or answer == "no":
                print("  Aborted.")
                return
        except EOFError:
            pass

    print("\n  ═══════════════════════════════════════════════════════")
    print("  Summary")
    print("  ═══════════════════════════════════════════════════════")
    print()
    print(f"  Profile:  {os.path.basename(profile)}")
    print(f"  Sessions: {len(paths)}")
    print(f"  Workers:  {workers}")
    print(f"  Dashboard: http://127.0.0.1:{PORT}")
    print(f"  Total chatsets: {total_chatsets}")
    print(f"  Total characters: {total_characters}")
    if max_chatsets is not None:
        print(f"  Max chatsets: {max_chatsets}")
    if only:
        sid, n = only[0]
        print(f"  Session:  {sid}")
        if len(only) == 1:
            print(f"  Chatset:  set {n}")
            print(f"  Stage:    {selected_stage or 'all'}")
        else:
            print(f"  Chatsets: {len(only)} starting at set {n}")
    else:
        print("  Chatsets: all")
    print()

    answer = input("  Launch? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        print("  Aborted.")
        return

    # 3-2-1 countdown
    print("\n  Launching...")
    for i in (3, 2, 1):
        print(f"  {i}…")
        time.sleep(0.5)

    print(f"  \u2713 Starting pipeline...\n")

    if not paths:
        print("  No sessions to run. Provide a queue file or use the session picker.")
        return

    run_pipeline(paths, full_objects if full_objects else [],
                 max_chatsets=max_chatsets, profile=profile,
                 workers=workers, granular=granular, force=force,
                 only=only, stage=selected_stage)


# ── CLI Session Picker ───────────────────────────────────────────────

def _parse_selection_input(user_input: str, session_count: int) -> list[int] | None:
    """Parse user selection into a list of 1-indexed session numbers.

    Accepted formats:
        "all"       → all sessions
        "1,3,5"     → sessions 1, 3, 5
        "1-10"      → sessions 1 through 10
        "1"         → session 1 (single number)

    Returns None if the input is empty, malformed, or selects out-of-range numbers.
    """
    raw = user_input.strip()
    if not raw:
        return None

    if raw.lower() == "all":
        return list(range(1, session_count + 1))

    if "-" in raw and "," not in raw:
        # Range, e.g. "1-10"
        parts = raw.split("-")
        if len(parts) != 2:
            return None
        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip())
        except ValueError:
            return None
        if start > end:
            return None
        nums = list(range(start, end + 1))
    else:
        # Comma-separated or single number, e.g. "1,3,5" or "1"
        parts = [p.strip() for p in raw.split(",")]
        try:
            nums = [int(p) for p in parts if p]
        except ValueError:
            return None

    # Validate all numbers are within [1, session_count]
    if not nums or any(n < 1 or n > session_count for n in nums):
        return None
    return nums


def load_and_pick_sessions(queue_path: str) -> list[str] | None:
    """Load sessions from a JSON queue, let the user pick which to run.

    Loads the JSON from *queue_path*, prints the session list with numbers,
    prompts for a selection, parses the input, and prints the final selection.

    Parameters
    ----------
    queue_path : str
        Path to a queue JSON (format: {"selected": ["/path/...", ...], ...})
        or full export JSON array.

    Returns
    -------
    list[str] | None
        Selected session IDs (from the JSON), or None on invalid input.
    """
    print("\n  ═══════════════════════════════════════════════════════")
    print(f"  CLI Session Picker — {queue_path}")
    print("  ═══════════════════════════════════════════════════════\n")

    result = load_queue(queue_path)
    if result is None:
        return None

    session_ids, full_objects = result
    all_paths = resolve_sessions_from_ids(session_ids)

    if not all_paths:
        print("  No sessions found on disk.")
        return None

    session_count = len(all_paths)
    print(f"  Total sessions loaded: {session_count}\n")
    print("  Sessions:")

    # Build display entries with metadata
    names = []
    sets_info = []
    for p in all_paths:
        meta = load_session_info(p)
        title = meta.get("title") or meta.get("name") or ""
        if not title:
            title = Path(p).name
        names.append(title)
        idx = all_paths.index(p)
        sets_info.append(full_objects[idx].get("total_sets", "?") if full_objects else "?")

    for i, (name, total_sets) in enumerate(zip(names, sets_info), start=1):
        display_name = name[:60] + "..." if len(name) > 60 else name
        sets_str = f" ({total_sets} sets)" if total_sets else ""
        print(f"    [{i:>3}] {display_name}{sets_str}")

    print()

    # Interactive selection loop
    while True:
        user_input = input("  Enter numbers (1,3,5), a range (1-10), or 'all': ").strip()
        nums = _parse_selection_input(user_input, session_count)
        if nums is None:
            print("  Invalid input. Enter numbers (1,3,5), a range (1-10), or 'all'.\n")
            continue
        break

    selected_ids = [session_ids[i - 1] for i in nums]

    print(f"\n  ✓ {len(selected_ids)} session(s) selected:")
    for sid in selected_ids:
        print(f"    - {sid}")
    print()

    return selected_ids


def run_web_picker_and_pipeline(queue_path: str, granular: bool = False,
                                force: bool = False):
    """Launch the web session picker, let user pick sessions, then run pipeline."""
    import os

    picker_script = Path(__file__).parent / "soma-session-picker" / "start.sh"
    if not picker_script.exists():
        print(f"  ERROR: Web picker not found at {picker_script}")
        return

    if granular:
        print("\n  [Step: web session picker. Enter to run, Ctrl+C to skip]")
        print("  Close the browser or press Ctrl+C when done picking.")
        try: input()
        except EOFError: pass

    print(f"\n  ═══════════════════════════════════════════════════════")
    print(f"  Starting Web Session Picker...")
    print(f"  ═══════════════════════════════════════════════════════")
    print(f"\n  Frontend: http://localhost:5174")
    print(f"  Backend:  http://localhost:8001")
    print(f"\n  1. Pick your favorite sessions in the browser")
    print(f"  2. Click 'Export N Favorites'")
    print(f"  3. Close the browser or press Ctrl+C when done")
    print(f"\n  Selections will be saved to: {queue_path}\n")

    # Launch the web picker (blocking — waits for Ctrl+C or completion)
    picker_env = os.environ.copy()
    picker_env["SESSIONS_PATH"] = "/media/storage/dendritic123/chat-logs/sessions"
    picker_env["QUEUE_OUTPUT_PATH"] = queue_path

    try:
        subprocess.run(
            [str(picker_script)],
            cwd=str(picker_script.parent),
            env=picker_env,
        )
    except KeyboardInterrupt:
        print("\n  Picker interrupted.")
        return

    # After picker exits, check if a queue file was written
    if not Path(queue_path).exists():
        print(f"\n  No queue file written at {queue_path}")
        print("  Make sure you clicked 'Export N Favorites' in the picker.")
        return

    # Load the queue
    result = load_queue(queue_path)
    if result is None:
        return
    session_ids, full_objects = result
    paths = resolve_sessions_from_ids(session_ids)

    if not paths:
        print(f"\n  No sessions resolved from {queue_path}")
        return

    print(f"\n  \u2713 {len(paths)} sessions selected from web picker")
    print(f"  Queue: {queue_path}")

    # Run pipeline
    pick = _prompt_session(paths)
    only = [pick] if pick else None
    max_chatsets = None if only else _prompt_max_chatsets()
    stage_sel = "distiller" if only else None
    run_pipeline(paths, full_objects if full_objects else [], max_chatsets=max_chatsets,
                 granular=granular, force=force, only=only, stage=stage_sel)


def poll_for_launch_config(timeout=None) -> dict:
    """Block until soma_launch.json exists, then read and return it.

    Polls every 0.5 seconds. If timeout is None, polls indefinitely.
    Returns the parsed JSON dict.
    Exits with error on malformed JSON.
    """
    print(f"\n  Waiting for launch config: {LAUNCH_CONFIG}")
    deadline = None
    if timeout is not None:
        deadline = time.monotonic() + timeout

    while True:
        if deadline and time.monotonic() > deadline:
            print(f"  ERROR: Timed out waiting for {LAUNCH_CONFIG}")
            sys.exit(1)
        if LAUNCH_CONFIG.exists():
            break
        time.sleep(0.5)

    try:
        data = json.loads(LAUNCH_CONFIG.read_text())
    except json.JSONDecodeError as e:
        print(f"  ERROR: Malformed launch config at {LAUNCH_CONFIG}: {e}")
        sys.exit(1)
    except OSError as e:
        print(f"  ERROR: Cannot read launch config at {LAUNCH_CONFIG}: {e}")
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"  ERROR: Launch config must be a JSON object")
        sys.exit(1)
    return data


def select_sessions_from_config(config: dict) -> list[str]:
    """Load a queue file and return the mode-filtered subset of session paths.

    Config keys:
      - json_path: path to a queue/export JSON (relative to project root)
      - mode:      "all", "one", or "few"
      - count:     number of sessions for "few" mode (max 3)
    """
    queue_path = config.get("json_path")
    if not queue_path:
        print("  ERROR: Launch config missing 'json_path'")
        sys.exit(1)

    mode = config.get("mode", "all")
    if mode not in ("all", "one", "few"):
        print(f"  ERROR: Unknown launch mode '{mode}' (must be all/one/few)")
        sys.exit(1)

    count = config.get("count", 1)
    if not isinstance(count, int) or count < 1:
        count = 1

    # Load the queue
    result = load_queue(queue_path)
    if result is None:
        print(f"  ERROR: Cannot load queue from {queue_path}")
        sys.exit(1)

    session_ids, full_objects = result
    all_paths = resolve_sessions_from_ids(session_ids)

    if not all_paths:
        print("  ERROR: No sessions resolved from queue")
        sys.exit(1)

    # Filter by mode
    if mode == "all":
        paths = all_paths
    elif mode == "one":
        paths = [all_paths[0]]
    elif mode == "few":
        n = min(count, 3, len(all_paths))
        paths = all_paths[:n]
    else:
        paths = all_paths

    return paths, full_objects if full_objects else []


def _load_session_entries(paths: list[str]) -> list[tuple[str, int, int, int]]:
    """Return [(session_id, first_set, total_sets, total_chars)] per session."""
    entries: list[tuple[str, int, int, int]] = []
    try:
        sys.path.insert(0, str(SOMA_PKG))
        from machinetalk.source import load_session
    except Exception:
        return entries
    for path in paths:
        try:
            sess = load_session(Path(path))
        except Exception:
            continue
        if not sess.chatsets:
            continue
        entries.append((
            sess.session_id,
            sess.chatsets[0].set_number,
            len(sess.chatsets),
            sum(cs.chars for cs in sess.chatsets),
        ))
    return entries


def _prompt_session(paths: list[str]) -> tuple[str, int] | None:
    """List chat sessions and pick one. Returns (session_id, first set)."""
    entries = _load_session_entries(paths)
    if not entries:
        return None

    print(f"\n  Chat sessions ({len(entries)}):")
    for i, (sid, first, total, chars) in enumerate(entries, start=1):
        label = sid if len(sid) <= 34 else sid[:31] + "..."
        print(f"    [{i:>3}] {label:<36} {total:>4} chatsets  "
              f"{chars:>10,} chars   first set {first}")
    print()

    while True:
        raw = input("  Select session [1]: ").strip()
        if not raw:
            sid, first, _, _ = entries[0]
            return sid, first
        try:
            n = int(raw)
        except ValueError:
            print("  Enter a session number.")
            continue
        if n < 1 or n > len(entries):
            print(f"  Enter 1-{len(entries)}.")
            continue
        sid, first, _, _ = entries[n - 1]
        return sid, first


def _prompt_chatset_count(total: int) -> int | None:
    """How many chatsets from the selected session. None means all."""
    if total:
        print(f"\n  This session has {total} chatsets.")
    while True:
        raw = input("  How many to process? [1] / [a]ll / number: ").strip().lower()
        if not raw:
            return 1
        if raw in ("a", "all"):
            return None
        try:
            n = int(raw)
            if n >= 1:
                return min(n, total) if total else n
        except ValueError:
            pass
        print("  Enter a number or 'a'.")


def _prompt_max_chatsets() -> int | None:
    """Prompt for how many chatset chunks to process.

    The engine unit is one physical chunk. The default is 1 so a plain run
    never fans out across the whole queue.
    """
    while True:
        raw = input("  How many chatset chunks to process? [1]: ").strip()
        if not raw:
            return 1
        try:
            n = int(raw)
            if n < 1:
                print("  Please enter a positive number.")
                continue
            return n
        except ValueError:
            print("  Invalid input. Enter a number.")


def main():
    parser = argparse.ArgumentParser(
        description="SOMA Session Batch Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_sessions.py                    # Launch web picker → run pipeline
  python run_sessions.py --queue q.json     # Use existing JSON, skip picker
  python run_sessions.py --progress         # Show queue progress
  python run_sessions.py --dashboard        # Start dashboard only
  python run_sessions.py --all              # Process ALL sessions on disk
  python run_sessions.py --cli-picker       # CLI prompt to pick from JSON
  python run_sessions.py --profile PROF     # Specify profile explicitly
        """,
    )
    parser.add_argument("--queue", "-q", default=QUEUE_FILE,
                        help="Queue or export JSON file (default: data/soma_queue.json)")
    parser.add_argument("--run-only", action="store_true",
                        help="Skip picker, use --queue directly")
    parser.add_argument("--progress", action="store_true",
                        help="Show progress only, don't run")
    parser.add_argument("--dashboard", action="store_true",
                        help="Start dashboard only")
    parser.add_argument("--all", action="store_true",
                        help="Process all sessions on disk")
    parser.add_argument("--cli-picker", action="store_true",
                        help="Interactively pick sessions from queue JSON via CLI")
    parser.add_argument("--port", type=int, default=PORT,
                        help="Dashboard port (default: 9877)")
    parser.add_argument("--profile", type=str, default=None,
                        help="TurnKey profile path")
    parser.add_argument("--step-mode", action="store_true",
                        help="Pause after each stage, waiting for Enter to continue")
    parser.add_argument("--stage", default=None,
                        help="Run only one stage: distiller, introbeaver, planbeaver, outrobeaver")
    parser.add_argument("--granular", action="store_true",
                        help="Pause before every single step: profile, queue, sessions, chatsets, workers, "
                             "dashboard, each beaver, each build stage, codebase, manifest")
    parser.add_argument("--force", action="store_true",
                        help="Re-run stages even when the checkpoint says DONE")
    parser.add_argument("--only", action="append", default=None,
                        metavar="SESSION:SET",
                        help="Run a single chatset, e.g. --only trevino.war.room:6 "
                             "(repeatable). Pairs with --stage to inspect one "
                             "stage of one chatset.")
    parser.add_argument("--max-chatsets", type=int, default=None,
                        help="Stop after this many chatsets")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel chatset workers (default 2)")

    args = parser.parse_args()

    if args.dashboard:
        start_dashboard_only()
        return

    if args.progress:
        show_progress()
        return

    # --only: inspect one chatset directly, skipping every interactive prompt.
    # This is the path used to verify a single stage's telemetry before
    # running the rest of the pipeline.
    if args.only:
        result = load_queue(args.queue)
        if result is None:
            return
        session_ids, full_objects = result
        paths = resolve_sessions_from_ids(session_ids)
        if not paths:
            print("  No sessions resolved on disk.")
            return
        run_pipeline(
            paths, full_objects if full_objects else [],
            max_chatsets=args.max_chatsets,
            workers=args.workers,
            granular=args.granular,
            force=args.force,
            profile=args.profile,
            only=args.only,
            stage=args.stage,
        )
        return

    # --run-only: skip picker, use queue directly
    if args.run_only:
        result = load_queue(args.queue)
        if result is None:
            return
        session_ids, full_objects = result
        paths = resolve_sessions_from_ids(session_ids)
        if not paths:
            print("  No sessions resolved on disk.")
            return
        max_chatsets = _prompt_max_chatsets()
        run_pipeline(paths, full_objects if full_objects else [], max_chatsets=max_chatsets,
                     granular=args.granular, force=args.force)
        return

    # --all: scan all sessions, skip picker
    if args.all:
        from soma.machinetalk.session_picker import scan_sessions
        sessions = scan_sessions()
        paths = [s.path for s in sessions]
        if not paths:
            print("  No sessions found!")
            return
        queue_data = {
            "selected": paths,
            "count": len(paths),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        qpath = Path(QUEUE_FILE)
        qpath.parent.mkdir(parents=True, exist_ok=True)
        qpath.write_text(json.dumps(queue_data, indent=2))
        print(f"  Queued {len(paths)} sessions for processing")
        max_chatsets = _prompt_max_chatsets()
        run_pipeline(paths, [], max_chatsets=max_chatsets, granular=args.granular,
                     force=args.force)
        return

    # --cli-picker: interactive CLI selection from queue JSON
    if args.cli_picker:
        selected_ids = load_and_pick_sessions(args.queue)
        if selected_ids is None:
            return
        paths = resolve_sessions_from_ids(selected_ids)
        if not paths:
            print("  No sessions resolved on disk.")
            return
        # We need full_objects for metadata — reload with load_queue
        result = load_queue(args.queue)
        _, full_objects = result if result else (None, [])
        max_chatsets = _prompt_max_chatsets()
        run_pipeline(paths, full_objects if full_objects else [], max_chatsets=max_chatsets,
                     granular=args.granular, force=args.force)
        return

    # DEFAULT: run the 4-step interactive launcher
    run_launcher(granular=args.granular, force=args.force)


if __name__ == "__main__":
    main()
