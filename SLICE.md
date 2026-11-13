# soma-slice

The part of `soma-10-14` that the running app actually uses, and nothing else.
Everything not listed here stayed behind in the original repo and is meant to move
to its own repo.

## How "in use" was decided
Static import trace (including lazy, in-function imports) from the real entry points:
`run.sh` -> `run_sessions.py` -> `soma/run_engine.py`, plus `soma/curses_launcher.py`,
`run_dashboard.py`, `tools/pea_sync.py`, `tools/pea_viewer.py`. Then the non-Python
files that code reads at runtime: `soma/soma_template.sql`, `soma/machine-talk/*.md`
(read by `machinetalk/fleet.py`), and `soma-session-picker/` (started by `run_sessions.py`).
Tests are kept only when everything they import is itself in the slice.

## Contents
- Launchers: `run.sh`, `run_sessions.py`, `run_dashboard.py`, `soma_board.py`
  (the installed `/usr/local/bin/soma` board, not the older repo copy)
- Engine: `soma/run_engine.py`, `soma/store.py`, `soma/banner.py`, `soma/machinetalk/*`
- `soma/pipeline/compact_cards.py` (the only module from `soma/pipeline/` that the run imports)
- Tools: `tools/pea_sync.py`, `tools/pea_viewer.py`
- Web picker: `soma-session-picker/`
- Docs: `AGENTS.md`, `CLAUDE.md`, `soma/PIPELINE_ACTION_MAP.md`, `soma/SESSION_HANDOFF.md`,
  `soma/TURNKEY_TECHNICAL_MANUAL.md`
- 17 tests whose imports all live in this slice

## Not included (by design)
- Secrets and run data: `.env`, `.venv`, logs, `somas/`, `data/`, `session-picker-json/`
  queues, and `turn-key-profile/` (all gitignored). Create them locally.
- The TurnKey SDK. It is a separate repo (`~/turn-key`); install it into the venv with
  `pip install -e <path-to-turn-key>`.
- Dendrites and pipeline modules under `soma/pipeline/` and `soma/*_dendrite.py` that no
  entry point imports, the `run_tb*.py` scripts, and tests for those.

## Run
```
cp .env.example .env        # fill in ENGINE_URL / ENGINE_KEY
python3 -m venv .venv && .venv/bin/pip install -e <path-to-turn-key>
./run.sh
```
Third-party Python dependencies are not pinned in the repo (the original had no
requirements file). The board needs `textual` and `rich`.
