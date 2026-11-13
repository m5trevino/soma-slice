"""Stable human-facing short codes for sessions and stage turns.

A session gets a sequential code the first time one is asked for and keeps it
for the life of the store::

    code_for("9f2c...")   ->  "SES-001"

A stage turn is derived from a fixed per-stage prefix and the turn number::

    stage_code("distiller", 1)  ->  "DI-01"

Codes are assigned, never hashed. A hash would change when a session is
renamed and could collide; a counter cannot do either. Nothing here may
reassign an existing session: once a uuid appears in the map, that code is
returned for it forever.

Store (``data/short_codes.json``)::

    {"next": 121, "map": {"<session uuid>": "SES-120", ...}}

The file is re-read inside the allocation lock and rewritten with an atomic
tmp-file + fsync + ``os.replace`` sequence, so a crashed writer leaves the
previous file intact and two writers cannot mint the same number.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SESSION_PREFIX = "SES"
SESSION_DIGITS = 3
TURN_DIGITS = 2

# Stage name -> code prefix. One entry per stage in the pipeline; an unknown
# stage is an error rather than an invented prefix.
STAGE_PREFIXES = {
    "distiller": "DI",
    "introbeaver": "IB",
    "planbeaver": "PB",
    "outrobeaver": "OB",
    "nexus": "NX",
    "spark": "SP",
    "falcon": "FL",
    "eagle": "EG",
    "drafter": "DR",
    "scout": "SC",
    "oc": "OC",
    "olc": "OLC",
}

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = _ROOT / "data" / "short_codes.json"


class ShortCodeError(ValueError):
    """A code cannot be assigned or the store cannot be trusted."""


_thread_lock = threading.RLock()


def store_path() -> Path:
    """Path of the code store.

    ``SOMA_SHORT_CODES_PATH`` overrides it (tests point this at a temporary
    directory); the default is ``data/short_codes.json`` beside the package.
    """
    override = os.environ.get("SOMA_SHORT_CODES_PATH")
    return Path(override) if override else DEFAULT_PATH


# ── store io ──────────────────────────────────────────────────────────────────

def _validated(data: object, path: Path) -> dict:
    """Return the store as ``{"next": int, "map": {uuid: code}}``.

    ``next`` is raised to sit one past the highest assigned code, so a store
    whose counter has fallen behind can never hand out a code that is already
    in the map. Malformed content is an error: silently rebuilding the map
    would reassign codes that have already been shown to a human.
    """
    if not isinstance(data, dict):
        raise ShortCodeError(f"short-code store {path} is not a JSON object")

    mapping = data.get("map", {})
    if not isinstance(mapping, dict):
        raise ShortCodeError(f"short-code store {path} has a non-object 'map'")
    clean_map: dict[str, str] = {}
    highest = 0
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ShortCodeError(
                f"short-code store {path} has a non-string map entry "
                f"{key!r}: {value!r}")
        clean_map[key] = value
        number = _number_from_code(value)
        if number is not None and number > highest:
            highest = number

    next_number = data.get("next", 1)
    if isinstance(next_number, bool) or not isinstance(next_number, int):
        raise ShortCodeError(f"short-code store {path} has a non-integer 'next'")
    if next_number < 1:
        raise ShortCodeError(f"short-code store {path} has 'next' < 1")
    return {"next": max(next_number, highest + 1), "map": clean_map}


def _number_from_code(code: str) -> int | None:
    """Numeric part of ``SES-120``, or None for anything else."""
    prefix = SESSION_PREFIX + "-"
    if not code.startswith(prefix):
        return None
    tail = code[len(prefix):]
    return int(tail) if tail.isdigit() else None


def _load(path: Path) -> dict:
    """Read the store. A missing or empty file is a store not yet written."""
    if not path.exists():
        return {"next": 1, "map": {}}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {"next": 1, "map": {}}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ShortCodeError(
            f"short-code store {path} is not valid JSON: {exc}") from exc
    return _validated(data, path)


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so the rename itself is durable."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write(path: Path, data: dict) -> None:
    """Atomically replace the store: tmp file, fsync, ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _fsync_dir(path.parent)


@contextmanager
def _allocation_lock(path: Path) -> Iterator[None]:
    """Serialise allocation across threads and, where possible, processes.

    The thread lock covers this process. A sidecar ``.lock`` file taken with
    ``flock`` covers other processes sharing the store; on platforms without
    ``fcntl`` the thread lock is all there is, and the re-read inside the
    critical section still prevents a code being reused from this process.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _thread_lock:
        with open(lock_path, "a+", encoding="utf-8") as handle:
            try:
                import fcntl
            except ImportError:  # non-POSIX: thread lock only
                yield
                return
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ── public api ───────────────────────────────────────────────────────────────

def code_for(session_id: str) -> str:
    """Short code for ``session_id``, minting one on first use.

    The mapping is permanent: the same session always gets the same code and
    a number is never handed to a second session.
    """
    sid = str(session_id or "").strip()
    if not sid:
        raise ShortCodeError("session_id is required")

    path = store_path()
    with _allocation_lock(path):
        data = _load(path)
        existing = data["map"].get(sid)
        if existing is not None:
            return existing

        number = int(data["next"])
        used = set(data["map"].values())
        code = _session_code(number)
        while code in used:
            number += 1
            code = _session_code(number)

        data["map"][sid] = code
        data["next"] = number + 1
        _write(path, data)
        return code


def stage_code(stage: str, turn: int) -> str:
    """Code for one stage turn, e.g. ``stage_code("nexus", 7) == "NX-07"``."""
    key = str(stage or "").strip().lower()
    prefix = STAGE_PREFIXES.get(key)
    if prefix is None:
        known = ", ".join(sorted(STAGE_PREFIXES))
        raise ShortCodeError(f"unknown stage {stage!r}; known stages: {known}")

    # An integer, or a string of digits. ``int()`` alone would quietly turn
    # 1.5 into 1 and label a turn that never happened.
    if isinstance(turn, bool):
        raise ShortCodeError(f"turn must be an integer, got {turn!r}")
    if isinstance(turn, int):
        number = turn
    elif isinstance(turn, str) and turn.strip().isdigit():
        number = int(turn.strip())
    else:
        raise ShortCodeError(f"turn must be an integer, got {turn!r}")
    if number < 1:
        raise ShortCodeError(f"turn must be >= 1, got {number}")
    return f"{prefix}-{number:0{TURN_DIGITS}d}"


def _session_code(number: int) -> str:
    return f"{SESSION_PREFIX}-{number:0{SESSION_DIGITS}d}"
