#!/usr/bin/env python3
"""Interactive curses pipeline launcher.

Shows all stages with their status. Press keys to run/redo/overwrite
any stage independently.

Stage 1 (per chatset):  a=distiller  b=introbeaver  c=planbeaver  d=outrobeaver
Stages 2-4 (per build): 1=nexus  2=spark  3=falcon  4=eagle  5=drafter  6=scout  7=oc  8=olc  9=codebase

Keys:
  a-d    Run stage 1 stage (a=distiller, b=introbeaver, c=planbeaver, d=outrobeaver)
  1-8    Run stage 2-4 build stage
  9      Write codebase (after olc)
  r      Rerun from selected stage (overwrite)
  o      Overwrite existing stage
  s      Skip stage
  g      Go to specific stage (type letter or number)
  R      Run full pipeline from current position
  Q      Quit
"""
import argparse
import curses
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


# ── Pipeline stage definitions ──────────────────────────────────────────
STAGE1 = (
    ("a", "distiller",   "Receptor"),
    ("b", "introbeaver", "Integrator"),
    ("c", "planbeaver",  "Retrograde"),
    ("d", "outrobeaver", "Output"),
)

STAGE24 = (
    ("1", "nexus",   "Nexus"),
    ("2", "spark",   "Spark"),
    ("3", "falcon",  "Falcon"),
    ("4", "eagle",   "Eagle"),
    ("5", "drafter", "Drafter"),
    ("6", "scout",   "Scout"),
    ("7", "oc",      "OC"),
    ("8", "olc",     "OLC"),
)

CODEBASE = ("9", "codebase", "Codebase")

# All stages in order for "run from here"
ALL_STAGES = [s[1] for s in STAGE1] + [s[1] for s in STAGE24]


def _status_color(status: str) -> int:
    colors = {
        "DONE": curses.COLOR_GREEN,
        "RUNNING": curses.COLOR_CYAN,
        "FAILED": curses.COLOR_RED,
        "BLOCKED": curses.COLOR_MAGENTA,
        "SKIPPED": curses.COLOR_YELLOW,
    }
    return colors.get(status, curses.COLOR_WHITE)


def _load_checkpoint(out_dir: str) -> dict:
    """Load checkpoint data from output directory."""
    ck_path = Path(out_dir) / "checkpoints" / "stage1.json"
    if ck_path.exists():
        try:
            return json.loads(ck_path.read_text())
        except Exception:
            pass
    return {}


def _build_stage_status(out_dir: str) -> tuple[dict, dict]:
    """Build stage status dicts from checkpoints.

    Checkpoint layout:
        <out>/checkpoints/<session_id>.checkpoint.json
        {
          "chatsets": { "3": { "stages": { "distiller": {"status": "DONE"}, ... } } },
          "session_stages": { "nexus": {"status": "DONE"}, ... }
        }

    Returns:
        (stage1_status, stage24_status) dicts mapping stage_name -> status
    """
    stage1_status: dict[str, str] = {}
    stage24_status: dict[str, str] = {}

    ck_dir = Path(out_dir) / "checkpoints"
    if not ck_dir.exists():
        return stage1_status, stage24_status

    stage1_names = ("distiller", "introbeaver", "planbeaver", "outrobeaver")
    stage24_names = ("nexus", "spark", "falcon", "eagle", "drafter", "scout", "oc", "olc")

    for ck_file in ck_dir.glob("*.checkpoint.json"):
        try:
            data = json.loads(ck_file.read_text())
        except Exception:
            continue

        # Stage 1: read from each chatset's stages
        chatsets = data.get("chatsets", {})
        for cs_key, cs_data in chatsets.items():
            stages = cs_data.get("stages", {})
            for sname in stage1_names:
                status = stages.get(sname, {}).get("status")
                if status:
                    stage1_status[sname] = status

        # Stage 2-4: read from session_stages
        session_stages = data.get("session_stages", {})
        for sname in stage24_names:
            status = session_stages.get(sname, {}).get("status")
            if status:
                stage24_status[sname] = status

    return stage1_status, stage24_status


def _get_last_queue(out_dir: str) -> list[str]:
    """Load selected sessions from the last queue file."""
    q_path = Path(out_dir) / "data" / "soma_queue.json"
    if q_path.exists():
        try:
            data = json.loads(q_path.read_text())
            return data.get("selected", [])
        except Exception:
            pass
    return []


class PipelineLauncher:
    def __init__(self, stdscr, out_dir: str, queue_path: str):
        self.stdscr = stdscr
        self.out_dir = out_dir
        self.queue_path = queue_path
        self.session_ids = _get_last_queue(out_dir)
        self.selected_session = self.session_ids[0] if self.session_ids else ""
        self.selected_stage = "a"  # default to distiller
        self.stage1_status, self.stage24_status = _build_stage_status(out_dir)
        self.running = True
        self.output_log = []  # live output lines
        self.running_stage = None
        self._thread = None
        self._lock = threading.Lock()
        self.msg_bar = ""       # message bar text
        self.mode = "select"    # select | running | input
        self.input_buf = ""

    def run(self):
        self._setup_colors()
        while self.running:
            self.stdscr.erase()
            self._draw()
            ch = self.stdscr.getch()
            self._handle_key(ch)

    def _setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # done
        curses.init_pair(2, curses.COLOR_CYAN, -1)    # running
        curses.init_pair(3, curses.COLOR_RED, -1)     # failed
        curses.init_pair(4, curses.COLOR_YELLOW, -1)  # skipped
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)  # header
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)  # selected
        curses.curs_set(0)
        self.stdscr.nodelay(False)  # block on getch

    def _draw(self):
        h, w = self.stdscr.getmaxyx()

        # ── Header ────────────────────────────────────────────────────
        self._draw_box(0, 0, w, 4, " SOMA PIPELINE LAUNCHER ")
        title = f"  Sessions: {len(self.session_ids)}  |  Selected: {self.selected_session[:50] or '(none)'}"
        self.stdscr.addstr(1, 2, title, curses.A_BOLD)

        max_chatsets_str = f"  Max chatsets: {getattr(self, 'max_chatsets', 'all')}"
        if h > 3:
            try:
                self.stdscr.addstr(2, 2, max_chatsets_str, curses.A_DIM)
            except curses.error:
                pass

        # ── Stage 1 ───────────────────────────────────────────────────
        y = 5
        self._draw_box(y, 0, w, 6, " STAGE 1 — Per Chatset ")
        y += 1
        for key, name, label in STAGE1:
            status = self.stage1_status.get(name, "PENDING")
            is_selected = (self.selected_stage == key)
            color = _status_color(status)

            line = f"  [{key.upper()}] {label:<15} {status:<10}"
            attr = curses.color_pair(color)
            if is_selected:
                attr |= curses.A_REVERSE | curses.A_BOLD
            try:
                self.stdscr.addstr(y, 2, line, attr)
            except curses.error:
                pass
            y += 1

        # ── Stage 2-4 ─────────────────────────────────────────────────
        y += 1
        self._draw_box(y, 0, w, 12, " STAGES 2-4 — Per Build ")
        y += 1
        for key, name, label in STAGE24:
            status = self.stage24_status.get(name, "PENDING")
            is_selected = (self.selected_stage == key)
            color = _status_color(status)

            line = f"  [{key}] {label:<15} {status:<10}"
            attr = curses.color_pair(color)
            if is_selected:
                attr |= curses.A_REVERSE | curses.A_BOLD
            try:
                self.stdscr.addstr(y, 2, line, attr)
            except curses.error:
                pass
            y += 1

        # ── Codebase ──────────────────────────────────────────────────
        y += 1
        cb_status = "PENDING"
        is_selected = (self.selected_stage == "9")
        line = f"  [9] Codebase write"
        attr = curses.color_pair(_status_color(cb_status))
        if is_selected:
            attr |= curses.A_REVERSE | curses.A_BOLD
        try:
            self.stdscr.addstr(y, 2, line, attr)
        except curses.error:
            pass
        y += 1

        # ── Output log ────────────────────────────────────────────────
        y += 1
        if y + 6 < h:
            self._draw_box(y, 0, w, min(6, h - y - 2), " OUTPUT ")
            y += 1
            with self._lock:
                lines = self.output_log[-5:] if self.output_log else ["  waiting..."]
            for i, line in enumerate(lines):
                try:
                    self.stdscr.addstr(y + i, 2, line[:w - 4])
                except curses.error:
                    pass

        # ── Bottom bar ────────────────────────────────────────────────
        bar_y = h - 1
        keys = "a-d | 1-8 | 9=codebase  |  r=run from here  |  o=overwrite  |  s=skip  |  g=go  |  R=full run  |  q=quit"
        msg = self.msg_bar if self.msg_bar else keys
        try:
            self.stdscr.attron(curses.color_pair(5) | curses.A_BOLD)
            self.stdscr.addstr(bar_y, 2, f" {msg}"[:w - 2])
            self.stdscr.attroff(curses.color_pair(5) | curses.A_BOLD)
        except curses.error:
            pass

    def _draw_box(self, y, x, w, h, title):
        """Draw a simple box with title."""
        if y + h >= self.stdscr.getmaxyx()[0] or x + w >= self.stdscr.getmaxyx()[1]:
            return
        try:
            # Top border
            self.stdscr.addstr(y, x, "+" + "-" * (w - 2) + "+")
            # Title
            try:
                self.stdscr.addstr(y, x + 2, title.center(w - 4)[:w - 4])
            except curses.error:
                pass
            # Middle lines
            for i in range(1, h):
                if y + i < self.stdscr.getmaxyx()[0]:
                    self.stdscr.addstr(y + i, x, "|" + " " * (w - 2) + "|")
            # Bottom border
            if y + h < self.stdscr.getmaxyx()[0]:
                self.stdscr.addstr(y + h, x, "+" + "-" * (w - 2) + "+")
        except curses.error:
            pass

    def _handle_key(self, ch):
        self.input_buf = ""

        if ch in (ord('q'), ord('Q'), 27):  # q or Escape
            self.running = False
            return

        # Stage keys — a, b, c, d, 1-9
        stage_keys = [k for k, _, _ in STAGE1] + [k for k, _, _ in STAGE24] + ["9"]
        if ch in (ord(k.upper()) for k in stage_keys) or ch in (ord(k) for k in stage_keys):
            # Normalize: uppercase letters
            k = chr(ch).lower()
            if k in ["a", "b", "c", "d"] or k in ["1", "2", "3", "4", "5", "6", "7", "8", "9"]:
                self.selected_stage = k
                key_label, stage_name, label = None, None, None
                for kk, sn, lb in STAGE1:
                    if kk == k:
                        key_label, stage_name, label = kk, sn, lb
                        break
                if not stage_name:
                    for kk, sn, lb in STAGE24:
                        if kk == k:
                            key_label, stage_name, label = kk, sn, lb
                            break
                if not stage_name:
                    key_label, stage_name, label = "9", "codebase", "Codebase"

                status = self.stage1_status.get(stage_name, "PENDING")
                if not status:
                    status = self.stage24_status.get(stage_name, "PENDING")

                if status in ("DONE",):
                    self.msg_bar = f"Already done. Press 'r' to rerun '{label}' or 'o' to overwrite."
                elif status in ("FAILED", "BLOCKED"):
                    self.msg_bar = f"Failed/blocked. Press 'r' to rerun '{label}'."
                else:
                    self.msg_bar = f"Running '{label}' ({stage_name})... check output below."
                    self._run_stage(stage_name)
                return

        # r = rerun from selected stage
        if ch == ord('r'):
            idx = ALL_STAGES.index(self._get_stage_name(self.selected_stage)) if self.selected_stage in ALL_STAGES else 0
            self.msg_bar = f"Running from '{ALL_STAGES[idx]}' onward..."
            self._run_from(ALL_STAGES[idx])
            return

        # R = run full pipeline
        if ch == ord('R'):
            self.msg_bar = "Running full pipeline..."
            self._run_from("distiller")
            return

        # s = skip stage
        if ch == ord('s'):
            stage_name = self._get_stage_name(self.selected_stage)
            if stage_name in ALL_STAGES:
                if stage_name in STAGE1[0][1]:
                    self.stage1_status[stage_name] = "SKIPPED"
                else:
                    self.stage24_status[stage_name] = "SKIPPED"
                self.msg_bar = f"Skipped '{stage_name}'"
            return

        # o = overwrite (force rerun even if done)
        if ch == ord('o'):
            stage_name = self._get_stage_name(self.selected_stage)
            if stage_name in ALL_STAGES:
                self.msg_bar = f"Forcing overwrite of '{stage_name}'..."
                self._run_stage(stage_name, force=True)
            return

        # g = go to specific stage
        if ch == ord('g'):
            self.stdscr.nodelay(False)
            self.stdscr.clear()
            self.stdscr.addstr(0, 2, "Enter stage key (a-d, 1-9): ", curses.A_BOLD)
            self.stdscr.refresh()
            # Read a single char
            gch = self.stdscr.getch()
            if gch in (ord(k) for k in stage_keys) or gch in (ord(k.upper()) for k in stage_keys):
                self.selected_stage = chr(gch).lower()
                self.msg_bar = f"Selected stage '{self.selected_stage}'"
            else:
                self.msg_bar = "Invalid stage key"
            self.stdscr.nodelay(True)
            return

    def _get_stage_name(self, key: str) -> str:
        for k, name, _ in STAGE1:
            if k == key:
                return name
        for k, name, _ in STAGE24:
            if k == key:
                return name
        return "codebase"

    def _run_stage(self, stage_name: str, force: bool = False):
        """Run a single stage via run_engine.py."""
        # Determine if stage 1 or 2-4
        is_stage1 = stage_name in [s[1] for s in STAGE1]

        if not is_stage1 and stage_name != "codebase":
            # Stage 2-4: run from this stage
            self._set_running(stage_name)
            self._exec_stage24(stage_name, force=force)
        elif stage_name == "codebase":
            self._set_running("codebase")
            self._exec_codebase()
        else:
            # Stage 1: run with --stage flag
            self._set_running(stage_name)
            self._exec_stage1(stage_name, force=force)

    def _run_from(self, from_stage: str):
        """Run pipeline from a given stage onward."""
        is_stage1 = from_stage in [s[1] for s in STAGE1]
        if is_stage1:
            self._exec_stage1(from_stage, force=True)
        elif from_stage != "codebase":
            self._exec_stage24(from_stage, force=True)
        else:
            self._exec_codebase()

    def _exec_stage1(self, stage: str, force: bool = False):
        """Run stage 1 with --stage flag."""
        # Check if we have sessions
        sessions = self.session_ids
        if not sessions:
            # Try loading from queue
            sessions = _get_last_queue(self.out_dir)
        if not sessions:
            self._add_log("  ERROR: No sessions found. Queue file empty.")
            self._set_done()
            return

        if force:
            # Delete checkpoint for this stage to force rerun
            ck_path = Path(self.out_dir) / "checkpoints"
            if ck_path.exists():
                for ck in ck_path.glob("*.json"):
                    try:
                        data = json.loads(ck.read_text())
                        if "stages" in data and stage in data["stages"]:
                            data["stages"][stage] = {"status": "PENDING"}
                            ck.write_text(json.dumps(data))
                            self._add_log(f"  Cleared checkpoint for '{stage}'")
                    except Exception:
                        pass

        # Build command
        cmd = [
            sys.executable, str(Path(__file__).parent / "run_engine.py"),
            "--queue", self.queue_path,
            "--stage", stage,
            "--out", str(Path(self.out_dir) / "output"),
            "--debug", str(Path(self.out_dir) / "debug"),
            "--workers", "2",
        ]

        self._run_subprocess(cmd)

    def _exec_stage24(self, from_stage: str, force: bool = False):
        """Run stages 2-4 starting from a specific stage."""
        sessions = self.session_ids
        if not sessions:
            sessions = _get_last_queue(self.out_dir)
        if not sessions:
            self._add_log("  ERROR: No sessions found.")
            self._set_done()
            return

        if force:
            # Delete checkpoint for this stage
            ck_path = Path(self.out_dir) / "checkpoints"
            if ck_path.exists():
                for ck in ck_path.glob("*.json"):
                    try:
                        data = json.loads(ck.read_text())
                        if "stages" in data and from_stage in data["stages"]:
                            data["stages"][from_stage] = {"status": "PENDING"}
                            ck.write_text(json.dumps(data))
                            self._add_log(f"  Cleared checkpoint for '{from_stage}'")
                    except Exception:
                        pass

        cmd = [
            sys.executable, str(Path(__file__).parent / "run_engine.py"),
            "--queue", self.queue_path,
            "--out", str(Path(self.out_dir) / "output"),
            "--debug", str(Path(self.out_dir) / "debug"),
            "--workers", "2",
        ]

        self._run_subprocess(cmd, from_stage=from_stage)

    def _exec_codebase(self):
        """Write codebase from latest artifacts."""
        self._add_log("  Writing codebase from latest artifacts...")
        # This is done as part of olc completion, just note it
        self._add_log("  (Codebase written as part of OLC stage)")
        self._set_done()

    def _run_subprocess(self, cmd: list, from_stage: str | None = None):
        """Run the pipeline stage in a background thread."""
        if self._thread and self._thread.is_alive():
            self.msg_bar = "Already running! Wait for it to finish."
            return

        self._thread = threading.Thread(target=self._subprocess_worker, args=(cmd, from_stage), daemon=True)
        self._thread.start()

    def _subprocess_worker(self, cmd: list, from_stage: str | None):
        try:
            if from_stage:
                cmd.extend(["--from", from_stage])

            self._add_log(f"  {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.rstrip()
                if line:
                    self._add_log(f"  {line}")

            proc.wait()

            if proc.returncode == 0:
                self._set_done()
                self._add_log(f"\n  ✓ Stage complete (exit {proc.returncode})")
            else:
                self._add_log(f"\n  ✗ Stage failed (exit {proc.returncode})")
                self.msg_bar = f"Stage failed with exit code {proc.returncode}"

        except Exception as exc:
            self._add_log(f"  ERROR: {exc}")
            self.msg_bar = f"Error: {exc}"
        finally:
            self._set_done()
            # Refresh status
            self.stage1_status, self.stage24_status = _build_stage_status(self.out_dir)

    def _set_running(self, stage: str):
        self.running_stage = stage
        self.running = True
        self.output_log.clear()
        self._add_log(f"  Running '{stage}'...")

    def _set_done(self):
        self.running_stage = None
        self._thread = None

    def _add_log(self, line: str):
        with self._lock:
            self.output_log.append(line[:120])


def launch(stdscr, out_dir: str, queue_path: str):
    launcher = PipelineLauncher(stdscr, out_dir, queue_path)
    launcher.run()


def main():
    p = argparse.ArgumentParser(description="SOMA Interactive Pipeline Launcher (curses)")
    p.add_argument("--out", default="/tmp/soma_out", help="output directory")
    p.add_argument("--queue", default=None, help="queue JSON file")
    args = p.parse_args()

    out_dir = args.out
    if not args.queue:
        # Default queue path
        soma_pkg = Path(__file__).parent
        args.queue = str(soma_pkg / "data" / "soma_queue.json")

    curses.wrapper(lambda stdscr: launch(stdscr, out_dir, args.queue))


if __name__ == "__main__":
    main()
