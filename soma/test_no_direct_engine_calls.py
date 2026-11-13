"""The TurnKey SDK is the only transport to the engine.

Nothing under soma/ or the launchers may POST to /v1/chat/completions or open
a non-local URL itself. A direct call would skip the SDK's auth, deck rotator
and provenance. Local dashboard and picker checks (127.0.0.1 / localhost) are
fine.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = [ROOT / "run_sessions.py", ROOT / "run_dashboard.py", ROOT / "soma_board.py",
        *sorted((ROOT / "soma").rglob("*.py"))]
SELF = Path(__file__).resolve()

ENGINE_PATH = re.compile(r"/v1/chat/completions")
HTTP_CLIENT = re.compile(r"\b(requests\.(get|post|put|request)|httpx\.|http\.client\.HTTPS?Connection)")
URLOPEN = re.compile(r"urlopen\(([^)]*)\)")
LOCAL = ("127.0.0.1", "localhost", "{PORT}", "PORT", "url", "{port}")


def _files():
    return [p for p in SCAN if p.exists() and p.resolve() != SELF
            and not p.name.startswith("test_")]


def test_nothing_posts_to_the_engine_directly():
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in _files()
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if ENGINE_PATH.search(line) and not line.lstrip().startswith("#")]
    assert not hits, f"direct /v1/chat/completions reference: {hits}"


def test_no_raw_http_client_in_the_pipeline():
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in _files()
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if HTTP_CLIENT.search(line) and not line.lstrip().startswith("#")]
    assert not hits, f"raw HTTP client use: {hits}"


def test_urlopen_only_reaches_local_services():
    bad = []
    for p in _files():
        for i, line in enumerate(p.read_text().splitlines(), 1):
            m = URLOPEN.search(line)
            if m and not any(tok in line for tok in LOCAL):
                bad.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
    assert not bad, f"urlopen to a non-local target: {bad}"
