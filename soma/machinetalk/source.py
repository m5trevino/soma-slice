"""Project-agnostic source loader.

A source directory contains one or more session directories. Each session
directory has `session.json` plus one JSON file per physical chatset chunk
(``NNNNNN.json`` or ``NNNNNN.K.json`` for chunked sets).

ChatSet unit:
    one physical JSON file is one ChatSet. Files are never concatenated by
    ``set_number``. A ``set_number`` group (for example ``16160.1`` through
    ``16160.28``) is a series of independent chunks, and each chunk is sent
    to the model on its own. Original group/chunk fields are preserved on
    the ChatSet for provenance. Identical duplicate exports are deduplicated
    by content hash, keeping the first deterministic file.

The canonical `raw_chat_set` (the array that gets hashed and echoed by
every stage) is built per ChatSet as:

    [{"role": "USER",  "content": <full user text>},
     {"role": "AGENT", "content": <full assistant text>}]

(empty roles dropped; order fixed). The distiller receives the same
content as a readable transcript under [RAW_CHAT_TRANSCRIPT].
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_CHUNK_RE = re.compile(r"^(\d+)\.(\d+)\.json$")


@dataclass
class ChatSet:
    session_id: str
    set_number: int
    source_chat_set_id: str
    files: list[str]                        # chunk filenames in order
    raw_chat_set: list[dict]                # canonical hashed array
    user_text: str
    assistant_text: str
    is_chunked: bool
    chars: int
    chatset_hash: str = ""
    models: list[str] = field(default_factory=list)
    provider: str = ""
    total_sets: int = 0
    # A ChatSet is one physical chunk file. ``set_number`` is the unique
    # 1-based unit number inside the session; these fields preserve the
    # original export metadata for logs and provenance.
    source_set_number: int = 0
    source_chunk_id: str = ""
    chunk_index: int = 0
    chunk_total: int = 0
    duplicate_files: list[str] = field(default_factory=list)

    @property
    def chatset_id(self) -> str:
        ident = self.source_chunk_id or str(self.set_number)
        return f"{self.session_id}:{ident}"

    def transcript(self) -> str:
        """Readable transcript for the distiller's [RAW_CHAT_TRANSCRIPT]."""
        parts = []
        for m in self.raw_chat_set:
            parts.append(f"[{m['role']}]\n{m['content']}")
        return "\n\n".join(parts)


@dataclass
class Session:
    session_id: str
    path: Path
    total_sets: int | None = None
    provider: str = ""
    models_used: dict = field(default_factory=dict)
    chatsets: list[ChatSet] = field(default_factory=list)
    title: str = ""
    duplicate_files: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> str:
        # Oldest first. Session ids embed a YYMMDD-HHMM timestamp.
        return self.session_id


def _natural_chunk_index(filename: str) -> int:
    m = _CHUNK_RE.match(filename)
    return int(m.group(2)) if m else 0


def load_chatset(session_id: str, path: Path, set_number: int,
                 files: list[Path]) -> ChatSet:
    """Assemble one ChatSet from one physical chunk file.

    ``files`` may still contain several paths for legacy callers, in which
    case they are concatenated. The session loader passes exactly one file,
    because the LLM unit is the chunk, not the ``set_number`` group.
    """
    files.sort(key=lambda p: _natural_chunk_index(p.name))
    user_parts: list[str] = []
    asst_parts: list[str] = []
    models: list[str] = []
    provider = ""
    src_id = ""
    is_chunked = False
    first_rec: dict = {}
    for p in files:
        rec = json.loads(p.read_text())
        if not first_rec:
            first_rec = rec
        src_id = rec.get("source_chat_set_id", src_id)
        provider = rec.get("provider", provider)
        if rec.get("model") and rec["model"] not in models:
            models.append(rec["model"])
        is_chunked = is_chunked or bool(rec.get("is_chunked"))
        user_parts.append(rec.get("user_text", "") or "")
        asst_parts.append(rec.get("assistant_text", "") or "")
    user_text = "".join(user_parts).strip()
    asst_text = "".join(asst_parts).strip()
    raw: list[dict] = []
    if user_text:
        raw.append({"role": "USER", "content": user_text})
    if asst_text:
        raw.append({"role": "AGENT", "content": asst_text})
    from .hashes import chatset_hash_for

    source_set_number = int(first_rec.get("set_number", set_number) or set_number)
    source_chunk_id = str(
        first_rec.get("chat_set_id") or (files[0].stem if files else set_number))
    chunk_index = int(first_rec.get("chunk_index") or 0)
    chunk_total = int(first_rec.get("total_chunks") or 1)

    return ChatSet(
        session_id=session_id,
        set_number=int(set_number),
        source_chat_set_id=src_id,
        files=[p.name for p in files],
        raw_chat_set=raw,
        user_text=user_text,
        assistant_text=asst_text,
        is_chunked=is_chunked,
        chars=len(user_text) + len(asst_text),
        chatset_hash=chatset_hash_for(raw),
        models=models,
        provider=provider,
        source_set_number=source_set_number,
        source_chunk_id=source_chunk_id,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
    )


def load_session(session_dir: Path) -> Session:
    """Load a session as one ChatSet per physical chunk file.

    Each ``*.json`` record with a ``set_number`` is its own unit. Files are
    never concatenated by ``set_number``. Identical records (the same chunk
    exported twice under different source ids) are deduplicated by content
    hash, keeping the first deterministic file.

    ``ChatSet.set_number`` is a unique 1-based unit number in the session.
    The original export fields are preserved on ``source_set_number``,
    ``source_chunk_id``, ``chunk_index`` and ``chunk_total``.
    """
    session_id = session_dir.name
    sess = Session(session_id=session_id, path=session_dir)
    sj = session_dir / "session.json"
    if sj.exists():
        meta = json.loads(sj.read_text())
        sess.provider = meta.get("provider", "")
        sess.models_used = meta.get("models_used", {})
        # Set title from session.json, fall back to session_id
        sess.title = meta.get("title") or meta.get("name", session_id)

    candidates: list[dict] = []
    for f in session_dir.glob("*.json"):
        if f.name == "session.json" or f.name.startswith("tool-"):
            continue
        try:
            rec = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "set_number" not in rec:
            continue
        user_text = rec.get("user_text") or ""
        asst_text = rec.get("assistant_text") or ""
        digest = hashlib.sha256(
            (user_text + "\x00" + asst_text).encode("utf-8")).hexdigest()
        candidates.append({
            "file": f,
            "group": int(rec["set_number"]),
            "chunk_index": int(rec.get("chunk_index") or 0),
            "source_chunk_id": str(rec.get("chat_set_id") or f.stem),
            "digest": digest,
        })

    # Deterministic order: group, chunk position, physical chunk id, file.
    candidates.sort(key=lambda u: (
        u["group"], u["chunk_index"], u["source_chunk_id"], u["file"].name))

    units: list[dict] = []
    seen: set[str] = set()
    for cand in candidates:
        if cand["digest"] in seen:
            sess.duplicate_files.append(cand["file"].name)
            continue
        seen.add(cand["digest"])
        units.append(cand)

    total_units = len(units)
    sess.total_sets = total_units
    for index, unit in enumerate(units, 1):
        chatset = load_chatset(
            session_id, session_dir, index, [unit["file"]])
        chatset.total_sets = total_units
        sess.chatsets.append(chatset)
    return sess


def load_source(source_dir: Path) -> list[Session]:
    """Load every session under source_dir, oldest first.

    Accepts either a directory OF session dirs, or a single session dir
    (detected by the presence of session.json + chatset .json files).
    """
    source_dir = Path(source_dir).resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source dir not found: {source_dir}")
    if (source_dir / "session.json").exists():
        return [load_session(source_dir)]
    sessions = []
    for child in sorted(source_dir.iterdir()):
        if child.is_dir() and (child / "session.json").exists():
            sessions.append(load_session(child))
    if not sessions:
        raise FileNotFoundError(
            f"no session dirs (with session.json) found under {source_dir}")
    sessions.sort(key=lambda s: s.sort_key)
    return sessions
