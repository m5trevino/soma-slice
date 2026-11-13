#!/usr/bin/env python3
"""Soma dashboard launcher — runs the dashboard server and opens browser.

Usage:
    python run_dashboard.py              # Start dashboard only
    python run_dashboard.py --run        # Start dashboard + run pipeline
    python run_dashboard.py --dry        # Start dashboard + run pipeline dry-mode

The dashboard listens on port 9877 and reads debug events from
/tmp/soma_debug/. When you run the pipeline with --debug, events
flow live into the dashboard.

The Sessions tab shows real-time progress from /tmp/soma_debug/ checkpoints
and data/queue.json.
"""
import sys, os, threading, signal, socket
from pathlib import Path

SOMA_DIR = Path(__file__).parent
DEBUG_DIR = "/tmp/soma_debug"
PORT = 9877
PORT_FILE = SOMA_DIR / ".dashboard_port"


def find_port(base=PORT):
    for p in range(base, base + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return base


def write_port_file(port):
    PORT_FILE.write_text(str(port))


def read_port_file():
    if PORT_FILE.exists():
        try:
            return int(PORT_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    return PORT


def kill_port(port):
    """Try to kill any process on the port."""
    import subprocess
    try:
        result = subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, timeout=3
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass
    return False


def open_browser(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def show_menu(port):
    """Print interactive menu and handle keypresses."""
    print()
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║                                                          ║")
    print("  ║   SOMA Dashboard Launcher                                ║")
    print("  ║                                                          ║")
    print(f"  ║   Dashboard: http://127.0.0.1:{port:<4}               ║")
    print("  ║                                                          ║")
    print("  ║   Debug dir: /tmp/soma_debug                             ║")
    print("  ║                                                          ║")
    print("  ║   Tabs: ORBS (per-chatset) + SESSIONS (batch progress)  ║")
    print("  ║                                                          ║")
    print("  ║   Press [r] to run pipeline  [e] to exit                ║")
    print("  ╚══════════════════════════════════════════════════════════╝")
    print()
    print("  Dashboard server is running...")
    print()
    print("  ── Quick Reference ──")
    print("  Batch picker:       python run_sessions.py")
    print("  Pipeline with debug: pipeline run --debug /tmp/soma_debug")
    print("  Pipeline dry-run:     pipeline run --debug /tmp/soma_debug --dry")
    print("  Pipeline batch:       pipeline batch run --debug /tmp/soma_debug")
    print()


def main():
    # Parse flags
    run_pipeline = "--run" in sys.argv
    dry_mode = "--dry" in sys.argv
    no_open = "--no-open" in sys.argv

    # Dashboard skin: skeuomorphic by default.
    # --skin classic  (or SOMA_DASHBOARD_SKIN=classic) restores the original UI.
    skin = ""
    if "--skin" in sys.argv:
        i = sys.argv.index("--skin")
        skin = sys.argv[i + 1].lower() if i + 1 < len(sys.argv) else ""
    skin = skin or os.environ.get("SOMA_DASHBOARD_SKIN", "skeu").lower()

    # Kill any existing dashboard on the port
    kill_port(PORT)

    # Find available port and write port file
    port = find_port()
    write_port_file(port)

    # Ensure debug dir
    Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)

    # Start dashboard server
    os.chdir(str(SOMA_DIR))
    sys.path.insert(0, str(SOMA_DIR))

    if skin in ("classic", "legacy", "original", "dark"):
        from soma.machinetalk.debug_dashboard import start_dashboard
    else:
        from soma.machinetalk.debug_dashboard_skeu import start_dashboard

    # Open browser
    if not no_open:
        threading.Timer(1, lambda: open_browser(f"http://127.0.0.1:{port}")).start()

    # Show menu in console
    show_menu(port)

    # Run pipeline if requested
    if run_pipeline or dry_mode:
        print("  ── Launching pipeline ──")
        cmd = [
            "pipeline", "run",
            "--debug", DEBUG_DIR,
        ]
        if dry_mode:
            cmd.append("--dry")

        print(f"  Command: {' '.join(cmd)}\n")
        try:
            subprocess.run(cmd)
        except FileNotFoundError:
            print("  ⚠  'pipeline' command not found — dashboard keeps running.\n")

    # Start dashboard (blocks until Ctrl+C)
    try:
        print("  Press Ctrl+C to stop.")
        start_dashboard(DEBUG_DIR, port=port, auto_open=False, soma_dir=str(SOMA_DIR))
    except KeyboardInterrupt:
        print("\n  Stopping dashboard...")
        PORT_FILE.unlink(missing_ok=True)
        print("  Done.")


if __name__ == "__main__":
    import subprocess
    signal.signal(signal.SIGTERM, lambda s, f: None)
    main()
