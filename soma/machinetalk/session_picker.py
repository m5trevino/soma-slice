#!/usr/bin/env python3
"""Interactive session picker — curses UI for selecting sessions to process.

Usage:
    python -m soma.machinetalk.session_picker
    python soma/machinetalk/session_picker.py
    python run_sessions.py                    # via launcher
    
Features:
- Arrow keys to navigate sessions
- Space bar to add/remove from selection
- Enter to commit selection and return to caller
- Sort by: name, provider, chat_sets, characters, anchor/domain
- Reverse sort with R key
- Search/filter with / key (substring match)
- Shows metadata: title, sets, chars, provider, domain/anchor
- Reads from /media/storage/dendritic123/chat-logs/sessions/
- Also indexes SearchMonkey III vault.db if available

Returns selected SessionInfo objects as a JSON list of paths to stdout
and a Python list to the caller.
"""
import curses
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict
from typing import Optional


# ── Session root paths ────────────────────────────────────────────────────

SESSIONS_PATH = Path("/media/storage/dendritic123/chat-logs/sessions")
# The populated SearchMonkey vault. The older path under
# searchmonkey-iii/backend/ is a 0-byte file and silently yields nothing.
SEARCHMONKEY_DB = Path("/media/storage/graphdb/session_site/vault.db")


class SessionInfo:
    """Metadata about a single session."""
    __slots__ = [
        "id", "path", "title", "provider", "chat_sets", "characters",
        "anchors", "domain", "sub_domain", "dev_score", "total_sets",
        "code_blocks", "functions", "duplicate_of"
    ]

    def __init__(self, session_id: str, path: Path):
        self.id = session_id
        self.path = str(path)
        self.title = None
        self.provider = None
        self.chat_sets = 0
        self.characters = 0
        self.anchors = []
        self.domain = None
        self.sub_domain = None
        self.dev_score = 0
        self.code_blocks = 0
        self.functions = 0
        self.total_sets = 0
        self.duplicate_of = None
        self._load_metadata()

    def _load_metadata(self):
        """Load metadata from session.json and scan JSONL files."""
        session_dir = Path(self.path)

        # Try session.json first
        session_json = session_dir / "session.json"
        if session_dir.exists() and session_json.exists():
            try:
                with open(session_json) as f:
                    data = json.load(f)
                self.title = data.get("title") or data.get("name")
                self.total_sets = data.get("total_sets", 0)
            except (json.JSONDecodeError, OSError):
                pass

        # Count chat sets and characters from JSON files
        if session_dir.is_dir():
            json_files = sorted(
                [f for f in session_dir.iterdir() if f.suffix == ".json"],
                key=lambda f: f.name
            )
            for json_file in json_files:
                if json_file.name == "session.json":
                    continue
                try:
                    with open(json_file) as f:
                        data = json.load(f)
                    self.chat_sets += 1
                    for key in ("user_text", "assistant_text", "text", "content"):
                        if key in data and isinstance(data[key], str):
                            self.characters += len(data[key])
                except (json.JSONDecodeError, OSError):
                    pass

        # Extract provider from session ID
        if not self.provider:
            if "@" in self.id:
                self.provider = "gmail"
            elif "claude_web" in self.id:
                self.provider = "claude"
            elif "grok_web" in self.id:
                self.provider = "grok"
            elif "cursor" in self.id:
                self.provider = "cursor"
            else:
                self.provider = "unknown"

        # Use total_sets from session.json if we counted fewer
        if self.total_sets and self.total_sets > self.chat_sets:
            self.chat_sets = self.total_sets

    def display_name(self) -> str:
        """Return a display-friendly name for this session."""
        if self.title:
            return self.title
        # Clean up the session ID for display
        name = self.id
        # Remove date prefix if present
        if len(name) > 8 and name[:2].isdigit() and name[2] in "._-":
            name = name[3:]
        # Replace dots with spaces for readability
        name = name.replace(".", " ").replace("_", " ")
        return name[:80]


def _load_searchmonkey_db():
    """Load session metadata from SearchMonkey III vault.db if available."""
    meta = {}
    if not SEARCHMONKEY_DB.exists():
        return meta
    try:
        conn = sqlite3.connect(str(SEARCHMONKEY_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # dev_sessions carries the score; sessions carries the taxonomy.
        # Prefer dev_sessions and fall back to sessions so a vault with only
        # one of the two still contributes what it has.
        try:
            cur.execute(
                "SELECT session_id, total_score, code_blocks, inline_code, "
                "code_extensions, imports, functions, dev_keywords, "
                "config_files, heredoc_code_files, combined_text_length, "
                "chat_set_count, domain, sub_domain FROM dev_sessions")
            for row in cur.fetchall():
                meta[row["session_id"]] = {
                    "domain": row["domain"],
                    "sub_domain": row["sub_domain"],
                    "dev_score": int(row["total_score"] or 0),
                    "code_blocks": int(row["code_blocks"] or 0),
                    "functions": int(row["functions"] or 0),
                    "imports": int(row["imports"] or 0),
                    "dev_keywords": int(row["dev_keywords"] or 0),
                    "config_files": int(row["config_files"] or 0),
                    "combined_text_length": int(row["combined_text_length"] or 0),
                }
        except sqlite3.Error:
            cur.execute("SELECT session_id, domain, sub_domain FROM sessions")
            for row in cur.fetchall():
                meta[row["session_id"]] = {
                    "domain": row["domain"],
                    "sub_domain": row["sub_domain"],
                }
        # sessions fills in taxonomy for ids dev_sessions did not cover.
        try:
            cur.execute("SELECT session_id, domain, sub_domain FROM sessions")
            for row in cur.fetchall():
                entry = meta.setdefault(row["session_id"], {})
                entry.setdefault("domain", row["domain"])
                entry.setdefault("sub_domain", row["sub_domain"])
        except sqlite3.Error:
            pass
        conn.close()
    except Exception:
        pass
    return meta


def scan_sessions() -> list:
    """Scan all sessions from disk and return SessionInfo objects."""
    if not SESSIONS_PATH.exists():
        return []

    # Load SearchMonkey metadata (fast cache)
    sm_meta = _load_searchmonkey_db()

    sessions = []
    for session_dir in sorted(SESSIONS_PATH.iterdir()):
        if session_dir.is_dir():
            try:
                session = SessionInfo(session_dir.name, session_dir)
                # Overlay SearchMonkey metadata
                if session.id in sm_meta:
                    entry = sm_meta[session.id]
                    session.domain = entry.get("domain")
                    session.sub_domain = entry.get("sub_domain")
                    session.dev_score = int(entry.get("dev_score") or 0)
                    session.code_blocks = int(entry.get("code_blocks") or 0)
                    session.functions = int(entry.get("functions") or 0)
                sessions.append(session)
            except Exception:
                continue

    # Collapse duplicate exports. The same conversation is often present
    # under several names ("copy.of", "branch.of", date-prefixed), and at
    # 1,600+ groups the top of the dev list is otherwise the same session
    # repeated. A session that another one supersedes is tagged, not removed:
    # the picker can still select it, but it is not ranked twice.
    by_content: dict[tuple, list] = {}
    for session in sessions:
        if session.characters and session.chat_sets:
            by_content.setdefault((session.characters, session.chat_sets), []).append(session)
    for group in by_content.values():
        if len(group) < 2:
            continue
        canonical = min(group, key=lambda s: (len(s.id), s.id))
        for dup in group:
            if dup is not canonical:
                dup.duplicate_of = canonical.id
    return sessions


# ── Sort functions ─────────────────────────────────────────────────────────

def sort_key_provider(item, reverse=False):
    return (item.provider, item.display_name())

def sort_key_name(item, reverse=False):
    return item.display_name()

def sort_key_sets(item, reverse=True):
    return (item.chat_sets, item.display_name())

def sort_key_chars(item, reverse=True):
    return (item.characters, item.display_name())

def sort_key_anchor(item, reverse=False):
    return (item.domain or "", item.sub_domain or "", item.display_name())

def sort_key_dev(item, reverse=True):
    """Rank by SearchMonkey dev score, with duplicate exports demoted."""
    return (0 if item.duplicate_of else 1,
            item.dev_score, item.code_blocks, item.display_name())

SORT_FUNCTIONS = {
    "dev": sort_key_dev,
    "name": sort_key_name,
    "provider": sort_key_provider,
    "sets": sort_key_sets,
    "chars": sort_key_chars,
    "anchor": sort_key_anchor,
}

SORT_NAMES = {
    "dev": "Dev Score",
    "name": "Name",
    "provider": "Provider",
    "sets": "Chat Sets",
    "chars": "Characters",
    "anchor": "Anchor/Domain",
}


# ── Curses UI ─────────────────────────────────────────────────────────────

def _short_name(name: str, max_len: int = 50) -> str:
    """Truncate a name to fit the display."""
    if not name:
        return "?"
    if len(name) > max_len:
        return name[:max_len - 3] + "..."
    return name


def run_session_picker(sort_by: str = "dev") -> list[str] | None:
    """Main curses UI for session selection.

    Args:
        sort_by: Initial sort key (dev, name, provider, sets, chars, anchor)

    Returns:
        list of selected session paths, or None if user cancels.
    """
    sessions = scan_sessions()

    if not sessions:
        print("No sessions found!")
        return []

    current_sort = sort_by
    sort_reversed = False
    search_filter = ""
    search_re = None

    def sort_sessions():
        nonlocal sessions
        key_fn = SORT_FUNCTIONS[current_sort]
        sessions.sort(key=lambda s: key_fn(s, sort_reversed))

    sort_sessions()

    selected = set()  # set of display indices into current sorted list

    def draw(stdscr, scroll_off, hover_idx):
        stdscr.erase()
        H, W = stdscr.getmaxyx()

        # Title
        stdscr.attron(curses.A_BOLD)
        stdscr.addstr(0, 0, "  SOMA SESSION PICKER".ljust(W - 1))
        stdscr.attroff(curses.A_BOLD)

        # Instructions row
        stdscr.addstr(1, 0, f"  Sort: {SORT_NAMES[current_sort]}{' (REV)' if sort_reversed else ''}  |  "
                           f"Selected: {len(selected)}/{len(sessions)}  |  "
                           f"Filter: {'ON' if search_filter else 'OFF'}".ljust(W - 1)[:W-1])

        # Key hints
        hints = "↑↓Nav  SpaceSel  EnterDone  RRev  D/N/P/S/C/A Sort  /Search  ESC Quit"
        stdscr.addstr(2, 0, hints.ljust(W - 1)[:W-1])

        if search_filter:
            stdscr.addstr(3, 0, f"  Match: \"{search_filter}\"".ljust(W - 1)[:W-1])

        # Separator
        stdscr.addstr(4, 0, ("─" * (W - 1))[:W-1])

        # Session list
        visible = H - 6
        start = scroll_off
        if start < 0:
            start = 0
        if start > len(sessions) - visible:
            start = max(0, len(sessions) - visible)

        for i in range(visible):
            idx = start + i
            if idx >= len(sessions):
                break

            row = 5 + i
            s = sessions[idx]
            marker = "[+]" if idx in selected else "[ ]"
            name = _short_name(s.title or s.id)
            score = f"{s.dev_score:>9,}" if s.dev_score else "        –"
            dup = "  dup" if s.duplicate_of else ""
            meta = (f" {score}  {s.chat_sets:>3}s  {s.characters:>9,}c"
                    f"  {s.provider}{dup}")
            if s.domain:
                meta += f"  ▸ {s.domain}"
            line = f"  {marker} {name}{meta}"

            # Truncate to width
            if len(line) > W - 1:
                line = line[:W - 1]

            try:
                if idx == hover_idx:
                    stdscr.attron(curses.A_REVERSE | curses.A_BOLD)
                stdscr.addstr(row, 0, line)
                stdscr.attroff(curses.A_REVERSE | curses.A_BOLD)
            except curses.error:
                pass

        # Status bar
        if sessions:
            show_idx = min(start, len(sessions) - 1)
            status = f"  {start + 1}-{min(start + visible, len(sessions))}/{len(sessions)}  │  " \
                     f"{_short_name(sessions[show_idx].id)}"
        else:
            status = "  No sessions"
        stdscr.addstr(H - 1, 0, status.ljust(W - 1)[:W-1])

        stdscr.refresh()

    # ── Main loop ───────────────────────────────────────────────────────
    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    stdscr.timeout(-1)  # blocking

    scroll_off = 0
    hover_idx = 0
    search_input = ""
    search_mode = False

    try:
        while True:
            draw(stdscr, scroll_off, hover_idx)
            key = stdscr.getch()

            if key == 27:  # ESC
                return None

            if search_mode:
                if key in (10, 13, 27):  # Enter or ESC ends search
                    if key != 27:
                        search_filter = search_input
                        if search_filter:
                            search_re = re.compile(re.escape(search_filter), re.IGNORECASE)
                    else:
                        search_input = ""
                    search_mode = False
                    scroll_off = 0
                    hover_idx = 0
                    continue
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    search_input = search_input[:-1]
                elif 32 <= key <= 126:
                    search_input += chr(key)
                continue

            # Filtering: only show sessions matching search_re
            if search_re:
                visible_indices = [
                    i for i, s in enumerate(sessions)
                    if search_re.search(s.title or s.id) or
                       search_re.search(s.domain or "") or
                       search_re.search(s.provider)
                ]
            else:
                visible_indices = list(range(len(sessions)))

            if not visible_indices:
                continue

            # Keep hover_idx within visible indices
            if hover_idx not in visible_indices:
                hover_idx = visible_indices[0]

            if key == curses.KEY_UP:
                vi = visible_indices.index(hover_idx)
                if vi > 0:
                    hover_idx = visible_indices[vi - 1]
                    if hover_idx < scroll_off:
                        scroll_off = hover_idx

            elif key == curses.KEY_DOWN:
                vi = visible_indices.index(hover_idx)
                if vi < len(visible_indices) - 1:
                    hover_idx = visible_indices[vi + 1]
                    min_scroll = hover_idx - (stdscr.getmaxyx()[0] - 7)
                    if hover_idx >= scroll_off + (stdscr.getmaxyx()[0] - 7):
                        scroll_off = max(scroll_off, min_scroll)

            elif key == ord(' '):
                if hover_idx in selected:
                    selected.discard(hover_idx)
                else:
                    selected.add(hover_idx)

            elif key == curses.KEY_ENTER or key == 10 or key == 13:
                if selected:
                    return [sessions[i].path for i in sorted(selected)]
                else:
                    return [s.path for s in sessions]

            elif key == ord('r') or key == ord('R'):
                sort_reversed = not sort_reversed
                sort_sessions()
                selected = set()  # reset selection on re-sort
                scroll_off = 0
                hover_idx = 0

            elif key == ord('d') or key == ord('D'):
                current_sort = "dev"
                sort_reversed = True
                sort_sessions()
                selected = set()
                scroll_off = 0
                hover_idx = 0

            elif key == ord('n') or key == ord('N'):
                current_sort = "name"
                sort_reversed = False
                sort_sessions()
                selected = set()
                scroll_off = 0
                hover_idx = 0

            elif key == ord('p') or key == ord('P'):
                current_sort = "provider"
                sort_reversed = False
                sort_sessions()
                selected = set()
                scroll_off = 0
                hover_idx = 0

            elif key == ord('s') or key == ord('S'):
                current_sort = "sets"
                sort_reversed = True
                sort_sessions()
                selected = set()
                scroll_off = 0
                hover_idx = 0

            elif key == ord('c') or key == ord('C'):
                current_sort = "chars"
                sort_reversed = True
                sort_sessions()
                selected = set()
                scroll_off = 0
                hover_idx = 0

            elif key == ord('a') or key == ord('A'):
                current_sort = "anchor"
                sort_reversed = False
                sort_sessions()
                selected = set()
                scroll_off = 0
                hover_idx = 0

            elif key == ord('/'):
                search_mode = True
                search_input = ""

    finally:
        curses.nocbreak()
        stdscr.keypad(False)
        curses.echo()
        curses.endwin()


def save_queue(paths: list[str], output_path: str = "data/queue.json"):
    """Save selected session paths to a JSON queue file."""
    from pathlib import Path as _Path
    _Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    data = {
        "selected": paths,
        "count": len(paths),
        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    return data


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SOMA Session Picker")
    parser.add_argument("--sort", default="name",
                        choices=["dev", "name", "provider", "sets", "chars", "anchor"],
                        help="Initial sort mode")
    parser.add_argument("--queue", default="data/queue.json",
                        help="Output queue file path")
    args = parser.parse_args()

    print(f"Starting session picker (sort={args.sort})...")
    print("Full-screen curses UI. Make sure your terminal is big enough.\n")

    paths = run_session_picker(sort_by=args.sort)
    if paths:
        data = save_queue(paths, args.queue)
        print(f"\n✓ Selected {data['count']} sessions")
        print(f"✓ Queue saved to {args.queue}")
        print("\nSession paths:")
        for p in paths:
            print(f"  {p}")
        print(f"\nRun pipeline: pipeline run --debug /tmp/soma_debug --source {paths[0]}")
    else:
        print("No sessions selected.")
