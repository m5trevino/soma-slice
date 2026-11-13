#!/usr/bin/env python3
"""Build the Soma Session Picker database.

Copies the session-level tables from the Search Monkey III vault into a new,
lean picker database (skipping the large full-text and per-chatset detail
tables the picker never reads), then adds the machine-talk instructions table
and the ignore list.

The new database is the picker's own store; the Search Monkey vault is left
untouched.

Usage:
    python3 build_picker_db.py
"""

import sqlite3
from pathlib import Path

SOURCE_DB = "/media/storage/graphdb/session_site/vault.db"
OUTPUT_DB = Path(__file__).resolve().parent / "data" / "session-pick.db"
WRITE_UP = Path(__file__).resolve().parent / "machine-talk" / "sp-001-session-picker.md"

# ── Tables to copy (session-level only; excludes FTS + per-chatset detail) ──
COPY_TABLES = [
    "sessions",
    "dev_sessions",
    "session_metadata",
    "projects",
    "session_projects",
    "session_chains",
]

SCHEMA = {
    "sessions": """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            date TEXT,
            provider TEXT,
            model TEXT,
            domain TEXT,
            sub_domain TEXT,
            project_context TEXT,
            topics TEXT,
            entities TEXT,
            relative_dir TEXT,
            anchor_count INTEGER DEFAULT 0,
            decision_count INTEGER DEFAULT 0,
            chars_in INTEGER DEFAULT 0,
            chars_out INTEGER DEFAULT 0
        )
    """,
    "dev_sessions": """
        CREATE TABLE dev_sessions (
            session_id TEXT PRIMARY KEY,
            total_score INTEGER,
            code_blocks INTEGER,
            inline_code INTEGER,
            code_extensions INTEGER,
            imports INTEGER,
            functions INTEGER,
            dev_keywords INTEGER,
            config_files INTEGER,
            heredoc_code_files INTEGER,
            combined_text_length INTEGER,
            chat_set_count INTEGER,
            domain TEXT,
            sub_domain TEXT,
            model TEXT,
            date TEXT,
            scored_at TEXT DEFAULT (datetime('now')),
            session_path TEXT
        )
    """,
    "session_metadata": """
        CREATE TABLE session_metadata (
            session_id TEXT PRIMARY KEY,
            sentiment TEXT,
            idea_maturity TEXT,
            content_type TEXT,
            emotional_tone TEXT,
            chat_set_count INTEGER DEFAULT 0
        )
    """,
    "projects": """
        CREATE TABLE projects (
            project_key   TEXT PRIMARY KEY,
            project_name  TEXT NOT NULL,
            session_count INTEGER DEFAULT 0,
            heredoc_hits  INTEGER DEFAULT 0,
            total_score   INTEGER DEFAULT 0,
            dev_session_count INTEGER DEFAULT 0,
            earliest_date TEXT,
            latest_date   TEXT
        )
    """,
    "session_projects": """
        CREATE TABLE session_projects (
            session_id   TEXT NOT NULL,
            project_key  TEXT NOT NULL,
            project_name TEXT NOT NULL,
            source       TEXT NOT NULL,
            weight       REAL NOT NULL,
            detail       TEXT,
            PRIMARY KEY (session_id, project_key, source)
        )
    """,
    "session_chains": """
        CREATE TABLE session_chains (
            chain_key    TEXT PRIMARY KEY,
            session_ids  TEXT NOT NULL,
            accounts     TEXT NOT NULL,
            member_count INTEGER NOT NULL
        )
    """,
}

# ── Picker-specific tables ─────────────────────────────────────────────────
PICKER_SCHEMA = """
CREATE TABLE picker_instructions (
    instruction_key TEXT PRIMARY KEY,
    tb_number       TEXT,
    body            TEXT NOT NULL,
    handoff         INTEGER DEFAULT 0,
    loaded_from     TEXT,
    version         INTEGER DEFAULT 1
);

CREATE TABLE picker_ignore (
    session_id TEXT PRIMARY KEY,
    reason     TEXT,
    domain     TEXT,
    sub_domain TEXT,
    added_at   TEXT DEFAULT (datetime('now'))
);
"""


def main() -> None:
    OUTPUT_DB.parent.mkdir(parents=True, exist_ok=True)

    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()

    conn = sqlite3.connect(str(OUTPUT_DB))
    try:
        for name, ddl in SCHEMA.items():
            conn.execute(ddl)

        conn.executescript(PICKER_SCHEMA)

        # Copy data from the Search Monkey vault via ATTACH.
        conn.execute(f"ATTACH DATABASE ? AS src", (SOURCE_DB,))
        for name in COPY_TABLES:
            conn.execute(f"INSERT INTO {name} SELECT * FROM src.{name}")
            n = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            print(f"  {name:<20} {n:>8} rows")
        # Commit the copy so the source attachment's read transaction is
        # released; the attachment itself is dropped when the connection closes.
        conn.commit()

        # Insert the machine-talk write-up.
        body = WRITE_UP.read_text(encoding="utf-8")
        conn.execute(
            "INSERT INTO picker_instructions "
            "(instruction_key, tb_number, body, loaded_from, version) "
            "VALUES (?, ?, ?, ?, ?)",
            ("session_picker", "sp-001", body, str(WRITE_UP.name), 1),
        )
        conn.commit()
        print("  picker_instructions     1 row (sp-001)")
        print(f"\nWrote {OUTPUT_DB}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
