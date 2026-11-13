"""Checkpoint state: per-session .checkpoint.json, atomic writes.

Layout (one file per session, under the run's output dir):

    <out>/checkpoints/<session_id>.checkpoint.json
    {
      "session_id": "...",
      "chatsets": {
        "3": {
          "chatset_id": "sess:3",
          "chatset_hash": "sha256...",
          "chars": 12345,
          "stages": {
            "distiller":  {"status": "DONE", "verdict": "", "models": ["x"],
                            "tokens": 123, "latency_ms": 100, "ts": "...",
                            "artifact": "stage1/3_distiller.md"},
            "introbeaver": {"status": "PENDING"},
            ...
          }
        }
      },
      "session_stages": {
        "nexus":   {"status": "DONE", ...},
        ...
      },
      "updated_at": "..."
    }

Statuses: PENDING | RUNNING | DONE | FAILED | SKIPPED | BLOCKED
  * DONE    -- gate passed (artifact usable downstream)
  * SKIPPED -- distiller skip; the ChatSet is vaulted, downstream stages
               for it are marked SKIPPED automatically
  * BLOCKED -- hard-block verdict (REWORK/NO-GO/etc.); operator decision
  * FAILED  -- all retry models exhausted

`--from <stage>`: a stage may run if every upstream stage for the same
chatset (stage1) or session (stage2-4) is DONE/SKIPPED-appropriately.
`--redo <stage>`: clears that stage's state (and, for stage1, everything
downstream within the same chatset) before running.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .hashes import (JournalNonceError, PlanNonceError, journal_hash_for,
                     journal_nonce, plan_hash_for, plan_nonce)

PENDING, RUNNING, DONE, FAILED, SKIPPED, BLOCKED = (
    "PENDING", "RUNNING", "DONE", "FAILED", "SKIPPED", "BLOCKED")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class PriorNonceRejected(ValueError):
    """A nonce that already belongs to a prior run was offered again.

    The checkpoint records the nonce a stage ran under. Re-offering a nonce
    that is already bound to a different plan hash means the caller is
    re-using an old run's identity, which would make the stage's provenance a
    lie; the write is refused instead of overwriting the record.
    """


class CheckpointPrecondition(ValueError):
    """The checkpoint cannot carry this stage state.

    Raised when the chatset was never initialized (no distiller state to
    attach to) or when the plan's chatset hash disagrees with the corpus the
    checkpoint recorded — marks planbeaver DONE on the wrong bytes otherwise.
    """

STAGE1 = ["distiller", "introbeaver", "planbeaver", "outrobeaver"]
STAGE234 = ["nexus", "spark", "falcon", "eagle", "drafter", "scout", "oc", "olc"]

_UPSTREAM = {
    "distiller": [],
    "introbeaver": ["distiller"],
    "planbeaver": ["introbeaver"],
    "outrobeaver": ["planbeaver"],
    "nexus": STAGE1,                      # needs all chatsets' stage1 done
    "spark": ["nexus"],
    "falcon": ["spark"],
    "eagle": ["falcon"],
    "drafter": ["eagle"],
    "scout": ["drafter"],
    "oc": ["scout", "drafter"],
    "olc": ["oc"],
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class CheckpointStore:
    def __init__(self, out_dir: Path, layout: str = "group") -> None:
        self.out_dir = Path(out_dir)
        self.dir = self.out_dir / "checkpoints"
        self.layout = layout
        self._cache: dict[str, dict] = {}

    # -- io ----------------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        # Chunk-layout checkpoints are separate from the old set_number-group
        # checkpoints so existing group state cannot skip a chunk. The old
        # file is left untouched.
        suffix = ".chunks.checkpoint.json" if self.layout == "chunk" \
            else ".checkpoint.json"
        return self.dir / f"{session_id}{suffix}"

    def load(self, session_id: str) -> dict:
        if session_id in self._cache:
            return self._cache[session_id]
        p = self._path(session_id)
        if p.exists():
            data = json.loads(p.read_text())
            data.setdefault("layout", self.layout)
        else:
            data = {"session_id": session_id, "layout": self.layout,
                    "chatsets": {}, "session_stages": {},
                    "updated_at": _now()}
        self._cache[session_id] = data
        return data

    def _save(self, session_id: str) -> None:
        data = self._cache[session_id]
        data["updated_at"] = _now()
        _atomic_write(self._path(session_id), data)

    # -- chatset (stage 1) ---------------------------------------------------

    def ensure_chatset(self, session_id: str, set_number: int,
                       chatset_id: str, chatset_hash: str, chars: int) -> dict:
        """Register a chatset, resetting it when its content hash changed.

        The chatset hash identifies the exact raw chat set a stage ran on.
        When the stored hash no longer matches the incoming one, the stored
        stages describe a different corpus: they are cleared and the new
        identity is recorded. Re-running with the same hash is a no-op for
        the stages, while a chatset id or char count that was only filled in
        later is recorded without clearing anything.
        """
        data = self.load(session_id)
        key = str(set_number)
        cs = data["chatsets"].get(key)
        if cs is None:
            cs = {"chatset_id": chatset_id, "chatset_hash": chatset_hash,
                  "chars": chars, "stages": {s: {"status": PENDING}
                                             for s in STAGE1}}
            data["chatsets"][key] = cs
            return cs
        if (chatset_hash
                and cs.get("chatset_hash") not in (None, "", chatset_hash)):
            cs["chatset_hash"] = chatset_hash
            cs["chatset_id"] = chatset_id or cs.get("chatset_id", "")
            cs["chars"] = chars
            cs["stages"] = {s: {"status": PENDING} for s in STAGE1}
        else:
            # Same corpus: fill in identity fields that are still blank or
            # stale without disturbing any stage state.
            if chatset_hash:
                cs["chatset_hash"] = chatset_hash
            if chatset_id:
                cs["chatset_id"] = chatset_id
            if chars:
                cs["chars"] = chars
        return cs

    def cs_stage(self, session_id: str, set_number: int, stage: str) -> dict:
        cs = self.load(session_id)["chatsets"].get(str(set_number))
        if cs is None:
            return {"status": PENDING}
        return cs["stages"].get(stage, {"status": PENDING})

    def cs_mark(self, session_id: str, set_number: int, stage: str, status: str,
                **info) -> None:
        cs = self.ensure_chatset(session_id, set_number, "", "", 0)
        st = cs["stages"].setdefault(stage, {"status": PENDING})
        st["status"] = status
        # Store verbose CallRecord fields only when not PENDING
        for k in ("raw_response", "sys_prompt", "user_msg"):
            if k in info:
                val = info[k]
                if val is not None:
                    # Truncate to prevent checkpoint bloat
                    if k == "sys_prompt":
                        val = val[:5000]
                    else:
                        val = val[:10000]
                st[k] = val
        st.update({k: v for k, v in info.items() if k not in ("raw_response", "sys_prompt", "user_msg") and v is not None})
        st["ts"] = _now()
        self._save(session_id)

    def cs_is_done(self, session_id: str, set_number: int, stage: str) -> bool:
        return self.cs_stage(session_id, set_number, stage)["status"] in (DONE, SKIPPED)

    def record_distiller_hash(self, session_id: str, set_number: int,
                              chatset_id: str, chatset_hash: str,
                              chars: int, **info) -> dict:
        """Register the chatset and mark its distiller stage DONE for it.

        The stage stores the hash it ran on, so downstream stages can verify
        they consume the same bytes.
        """
        self.ensure_chatset(session_id, set_number, chatset_id,
                            chatset_hash, chars)
        info.setdefault("verdict", "PASS")
        self.cs_mark(session_id, set_number, "distiller", DONE,
                     chatset_hash=chatset_hash, chars_in=chars, **info)
        return self.cs_stage(session_id, set_number, "distiller")

    def record_extraction_hash(self, session_id: str, set_number: int,
                               extraction_hash: str, **info) -> dict:
        """Mark the chatset's introbeaver stage DONE for its extraction.

        The stage stores the hash of the entity lists it produced, so
        downstream stages can verify they consume the same extraction bytes
        and a re-run over different entities is visible in the checkpoint.
        """
        info.setdefault("verdict", "PASS")
        self.cs_mark(session_id, set_number, "introbeaver", DONE,
                     extraction_hash=extraction_hash, **info)
        return self.cs_stage(session_id, set_number, "introbeaver")

    def record_plan_hash(self, session_id: str, set_number: int,
                         plan_hash: str, *, nonce: str = "",
                         chatset_hash: str = "", **info) -> dict:
        """Mark the chatset's planbeaver stage DONE for a plan hash.

        The stage stores the canonical hash of the plan items array, so
        downstream stages (nexus, spark, falcon, eagle) verify they consume
        the same plan bytes. The nonce the plan was built under is stored
        beside it: the supplied nonce is echoed, and a nonce already bound to
        a *different* plan hash is rejected as a prior nonce.

        Args:
            session_id: session the chatset belongs to.
            set_number: chatset key (int or str; str() is used).
            plan_hash: 64-char lowercase hex SHA-256 of ``canonical_json(plan)``.
            nonce: the supplied nonce for this run (stored and echoed).
            chatset_hash: when given, must equal the checkpoint's chatset hash.
            **info: extra stage fields (``verdict``, item counts, provenance).

        Raises:
            ValueError: ``plan_hash`` is not a 64-char lowercase hex digest.
            CheckpointPrecondition: the chatset is not initialized, or
                ``chatset_hash`` disagrees with the recorded corpus.
            PriorNonceRejected: ``nonce`` conflicts with the nonce already
                recorded on the stage, or was already bound to another plan.
        """
        if not _HEX64_RE.match(plan_hash or ""):
            raise ValueError(
                f"plan_hash must be 64 lowercase hex chars, got {plan_hash!r}")
        data = self.load(session_id)
        key = str(set_number)
        cs = data["chatsets"].get(key)
        if cs is None:
            # Never create the chatset here: a planbeaver DONE record with no
            # distiller state would claim a corpus that was never ingested.
            raise CheckpointPrecondition(
                f"chatset {key} not initialized for {session_id}: "
                "the distiller stage must record the corpus first")
        stored = cs.get("chatset_hash") or ""
        if chatset_hash and stored and chatset_hash != stored:
            raise CheckpointPrecondition(
                f"plan chatset_hash {chatset_hash} != checkpoint {stored} "
                f"for chatset {key}")

        st = cs["stages"].setdefault("planbeaver", {"status": PENDING})
        prior_nonce = st.get("nonce") or ""
        if nonce:
            if prior_nonce and prior_nonce != nonce:
                raise PriorNonceRejected(
                    f"nonce {nonce} rejected: planbeaver already ran under "
                    f"prior nonce {prior_nonce}")
            recorded = st.get("plan_hash") or ""
            if prior_nonce == nonce and recorded and recorded != plan_hash:
                raise PriorNonceRejected(
                    f"nonce {nonce} rejected: already bound to plan_hash "
                    f"{recorded}, not {plan_hash}")

        st["status"] = DONE
        st["plan_hash"] = plan_hash
        if nonce:
            st["nonce"] = nonce
        if chatset_hash:
            st["chatset_hash"] = chatset_hash
        st.update({k: v for k, v in info.items() if v is not None})
        st["ts"] = _now()
        self._save(session_id)
        return st

    def record_journal_hash(self, session_id: str, set_number: int,
                            journal_hash: str, *, nonce: str = "",
                            chatset_hash: str = "", plan_hash: str = "",
                            **info) -> dict:
        """Mark the chatset's outrobeaver stage DONE for a journal hash.

        The stage stores the canonical hash of the parsed journal, the nonce
        the run was made under and the corpus hash the journal consumed, so a
        downstream stage verifies the same journal bytes the way it verifies
        its other inputs.

        The write is refused when it would make the record claim something
        the earlier stages do not support:

          * the declared upstream (planbeaver) must be DONE for this chatset;
          * ``chatset_hash`` must match the corpus recorded for the chatset;
          * ``plan_hash`` must match the plan planbeaver recorded;
          * ``nonce`` must match the run nonce planbeaver recorded — a
            different nonce is a prior run's identity, not this one.

        Args:
            session_id: session the chatset belongs to.
            set_number: chatset key (int or str; str() is used).
            journal_hash: 64-char lowercase hex SHA-256 of
                ``canonical_json(journal)``.
            nonce: the supplied nonce for this run (stored and echoed).
            chatset_hash: when given, must equal the checkpoint's chatset hash.
            plan_hash: when given, must equal planbeaver's recorded plan hash.
            **info: extra stage fields (``events``, ``journal_path``, ...).

        Raises:
            ValueError: ``journal_hash`` is not a 64-char lowercase hex digest.
            CheckpointPrecondition: the chatset is not initialized, its
                upstream is not DONE, or a recorded hash disagrees.
            PriorNonceRejected: ``nonce`` conflicts with the nonce already
                recorded on the stage, or with the planbeaver run's nonce.
        """
        if not _HEX64_RE.match(journal_hash or ""):
            raise ValueError(
                f"journal_hash must be 64 lowercase hex chars, got "
                f"{journal_hash!r}")
        data = self.load(session_id)
        key = str(set_number)
        cs = data["chatsets"].get(key)
        if cs is None:
            raise CheckpointPrecondition(
                f"chatset {key} not initialized for {session_id}: "
                "the distiller stage must record the corpus first")
        stored = cs.get("chatset_hash") or ""
        if chatset_hash and stored and chatset_hash != stored:
            raise CheckpointPrecondition(
                f"journal chatset_hash {chatset_hash} != checkpoint {stored} "
                f"for chatset {key}")

        ready, why = self.can_run_chatset_stage(session_id, set_number,
                                               "outrobeaver")
        if not ready:
            raise CheckpointPrecondition(
                f"outrobeaver cannot be marked DONE for chatset {key}: {why}")

        plan_state = cs["stages"].get("planbeaver", {})
        recorded_plan = plan_state.get("plan_hash") or ""
        if plan_hash and recorded_plan and plan_hash != recorded_plan:
            raise CheckpointPrecondition(
                f"journal plan_hash {plan_hash} != checkpoint plan_hash "
                f"{recorded_plan} for chatset {key}")
        plan_nonce = plan_state.get("nonce") or ""
        if nonce and plan_nonce and nonce != plan_nonce:
            raise PriorNonceRejected(
                f"nonce {nonce} rejected: chatset {key} plan was built under "
                f"prior nonce {plan_nonce}")

        st = cs["stages"].setdefault("outrobeaver", {"status": PENDING})
        prior_nonce = st.get("nonce") or ""
        if nonce:
            if prior_nonce and prior_nonce != nonce:
                raise PriorNonceRejected(
                    f"nonce {nonce} rejected: outrobeaver already ran under "
                    f"prior nonce {prior_nonce}")
            recorded = st.get("journal_hash") or ""
            if prior_nonce == nonce and recorded and recorded != journal_hash:
                raise PriorNonceRejected(
                    f"nonce {nonce} rejected: already bound to journal_hash "
                    f"{recorded}, not {journal_hash}")

        st["status"] = DONE
        st["journal_hash"] = journal_hash
        if nonce:
            st["nonce"] = nonce
        if chatset_hash:
            st["chatset_hash"] = chatset_hash
        if plan_hash:
            st["plan_hash"] = plan_hash
        st.update({k: v for k, v in info.items() if v is not None})
        st["ts"] = _now()
        self._save(session_id)
        return st

    def cs_downstream_skipped(self, session_id: str, set_number: int, from_stage: str) -> None:
        """After a distiller SKIP: mark the remaining stage1 stages SKIPPED."""
        cs = self.ensure_chatset(session_id, set_number, "", "", 0)
        started = False
        for s in STAGE1:
            if s == from_stage:
                started = True
            if started:
                st = cs["stages"].setdefault(s, {"status": PENDING})
                if st["status"] in (PENDING, RUNNING):
                    st["status"] = SKIPPED
                    st["ts"] = _now()
        self._save(session_id)

    def cs_redo(self, session_id: str, set_number: int, stage: str) -> None:
        """Clear stage + all downstream stage1 stages for this chatset."""
        cs = self.ensure_chatset(session_id, set_number, "", "", 0)
        started = False
        for s in STAGE1:
            if s == stage:
                started = True
            if started:
                cs["stages"][s] = {"status": PENDING}
        self._save(session_id)

    # -- session stages (2-4) --------------------------------------------------

    def ss_stage(self, session_id: str, stage: str) -> dict:
        return self.load(session_id)["session_stages"].get(stage,
                                                           {"status": PENDING})

    def ss_mark(self, session_id: str, stage: str, status: str, **info) -> None:
        data = self.load(session_id)
        st = data["session_stages"].setdefault(stage, {"status": PENDING})
        st["status"] = status
        st.update({k: v for k, v in info.items() if v is not None})
        st["ts"] = _now()
        self._save(session_id)

    def ss_is_done(self, session_id: str, stage: str) -> bool:
        return self.ss_stage(session_id, stage)["status"] == DONE

    def ss_redo(self, session_id: str, stage: str) -> None:
        data = self.load(session_id)
        for s in STAGE234:
            if s == stage:
                data["session_stages"][s] = {"status": PENDING}
                for later in STAGE234[STAGE234.index(s) + 1:]:
                    data["session_stages"][later] = {"status": PENDING}
        self._save(session_id)

    # -- readiness -----------------------------------------------------------

    def can_run_chatset_stage(self, session_id: str, set_number: int,
                              stage: str) -> tuple[bool, str]:
        """Upstream stage1 stages must be DONE (or SKIPPED at the skip point)."""
        cs = self.load(session_id)["chatsets"].get(str(set_number))
        if cs is None:
            return False, "chatset not initialized"
        for up in _UPSTREAM.get(stage, []):
            if up in STAGE1:
                st = cs["stages"].get(up, {}).get("status", PENDING)
                if st not in (DONE, SKIPPED):
                    return False, f"upstream {up} is {st}"
        return True, ""

    def can_run_session_stage(self, session_id: str, stage: str,
                              sessions: list) -> tuple[bool, str]:
        """nexus needs this session's stage1 fully done; others chain."""
        for up in _UPSTREAM.get(stage, []):
            if up in STAGE1:
                for cs in self.load(session_id)["chatsets"].values():
                    if cs["stages"].get(up, {}).get("status") not in (DONE, SKIPPED):
                        return False, f"upstream {up} incomplete for chatset {cs['chatset_id']}"
                continue
            if self.ss_stage(session_id, up)["status"] not in (DONE, SKIPPED):
                return False, f"upstream {up} is {self.ss_stage(session_id, up)['status']}"
        return True, ""

    def delete_soma(self) -> bool:
        """Delete the soma.json file for the output directory.
        Returns True if file was deleted, False if it didn't exist.
        """
        soma_path = self.out_dir / "soma.json"
        if soma_path.exists():
            soma_path.unlink()
            return True
        return False

    def force_clear_chatset(self, session_id: str, set_number: int) -> None:
        """Reset ALL stage1 steps for a chatset to PENDING (force re-run)."""
        cs = self.ensure_chatset(session_id, set_number, "", "", 0)
        for stg in STAGE1:
            cs["stages"][stg] = {"status": PENDING}
        self._save(session_id)

    def force_clear_session(self, session_id: str) -> None:
        """Reset ALL stages for a session to PENDING (full force re-run)."""
        data = self.load(session_id)
        # Stage 1 steps
        for cs_id, cs_data in data.get("chatsets", {}).items():
            for stg in STAGE1:
                cs_data["stages"][stg] = {"status": PENDING}
        # Build/processing stages
        for stg in STAGE234:
            data["session_stages"][stg] = {"status": PENDING}
        self._save(session_id)


# -- planbeaver plan hash -> checkpoint DONE ---------------------------------

def load_plan(path: str | Path) -> list:
    """Parse the planbeaver items array from ``path``.

    Raises ``FileNotFoundError`` when the file is absent and ``ValueError``
    when it is unreadable or is not a JSON array — a planbeaver plan is a list
    of plan items, never a wrapper object.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"plan file not found: {p}")
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{p}: unreadable plan file ({exc})") from exc
    if not isinstance(plan, list):
        raise ValueError(
            f"{p}: plan must be a JSON array of plan items, got "
            f"{type(plan).__name__}")
    return plan


def plan_field(plan, name: str) -> str:
    """The single distinct non-empty ``name`` value across the plan items.

    Raises ``ValueError`` when the items disagree: one plan describes one run
    of one chatset, so a mixed value is not a value this plan has.
    """
    values = sorted({str(item.get(name)) for item in plan
                     if isinstance(item, dict) and item.get(name)})
    if len(values) > 1:
        raise ValueError(f"plan items disagree on {name}: {', '.join(values)}")
    return values[0] if values else ""


def write_plan_hash_file(path: str | Path, plan_hash: str) -> Path:
    """Write the bare 64-char plan hash to ``path`` (atomic, one line)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(plan_hash + "\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return p


def write_digest_file(path: str | Path, digest: str) -> Path:
    """Write a bare hex digest to ``path`` (atomic, one line)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(digest + "\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return p


def write_plan_hash_file(path: str | Path, plan_hash: str) -> Path:
    """Write the bare 64-char plan hash to ``path`` (atomic, one line)."""
    return write_digest_file(path, plan_hash)


# -- outrobeaver journal hash -> checkpoint DONE -----------------------------

def load_journal_object(path: str | Path) -> dict:
    """Parse the outrobeaver journal object from ``path``.

    Raises ``FileNotFoundError`` when the file is absent and ``ValueError``
    when it is unreadable or is not a JSON object — a tb-006 journal is one
    object carrying ``events``, never a bare list.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"journal file not found: {p}")
    try:
        journal = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{p}: unreadable journal ({exc})") from exc
    if not isinstance(journal, dict):
        raise ValueError(
            f"{p}: journal must be a JSON object, got "
            f"{type(journal).__name__}")
    return journal


def journal_field(journal: dict, name: str) -> str:
    """The single distinct non-empty ``name`` value across the journal.

    The header value and every event's value are considered together: a
    journal whose events disagree describes more than one run, so a mixed
    value is rejected rather than picked from.
    """
    values: set[str] = set()
    header = journal.get(name)
    if header:
        values.add(str(header))
    events = journal.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and event.get(name):
                values.add(str(event[name]))
    if len(values) > 1:
        raise ValueError(
            f"journal disagrees on {name}: {', '.join(sorted(values))}")
    return values.pop() if values else ""


def resolve_set_number(store: CheckpointStore, session_id: str,
                       chatset_hash: str, requested: str = "") -> str:
    """The chatset key the journal belongs to.

    An explicit ``requested`` key is returned unchanged. Otherwise the key is
    the single chatset whose recorded ``chatset_hash`` matches the journal's
    — the only corpus the journal can be the outcome of. ``ValueError`` when
    none or several match, so a stray journal cannot be attached to the
    wrong chatset by default.
    """
    if requested:
        return str(requested)
    chatsets = store.load(session_id).get("chatsets") or {}
    if not chatset_hash:
        raise ValueError(
            "the journal carries no chatset_hash; pass --set-number")
    matches = sorted(
        key for key, cs in chatsets.items()
        if cs.get("chatset_hash") == chatset_hash)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"no chatset in {store._path(session_id)} records chatset_hash "
            f"{chatset_hash}")
    raise ValueError(
        f"chatset_hash {chatset_hash} matches several chatsets: "
        + ", ".join(matches))


def journal_main(argv: list[str] | None = None) -> int:
    """Hash an outrobeaver journal and record its checkpoint stage DONE.

    Prints ``NONCE: <supplied nonce>`` and ``JOURNAL_HASH: <sha256>``. Exits 0
    on success, 1 when the journal is missing/unreadable, 2 when the journal
    or its nonce is rejected, 3 when the checkpoint precondition fails.
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m soma.machinetalk.checkpoint journal",
        description="Compute the canonical journal hash and mark outrobeaver "
                    "DONE.")
    parser.add_argument("--journal", default="/tmp/soma_outrobeaver_journal.json",
                        help="outrobeaver journal JSON (object)")
    parser.add_argument("--out-dir", required=True,
                        help="run output dir holding checkpoints/")
    parser.add_argument("--session-id", default="",
                        help="session id (default: read from the journal)")
    parser.add_argument("--set-number", default="",
                        help="chatset key (default: the chatset whose "
                             "recorded hash matches the journal)")
    parser.add_argument("--layout", choices=("group", "chunk"), default="group")
    parser.add_argument("--nonce", default="",
                        help="supplied nonce (default: the journal's nonce)")
    parser.add_argument("--chatset-hash", default="",
                        help="expected chatset hash (default: from the journal)")
    parser.add_argument("--plan-hash", default="",
                        help="expected plan hash (default: from the journal)")
    parser.add_argument("--hash-out", default="/tmp/soma_outrobeaver_hash.txt",
                        help="file receiving the bare journal hash")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and validate without writing anything")
    args = parser.parse_args(argv)

    try:
        journal = load_journal_object(args.journal)
        journal_session = journal_field(journal, "session_id")
        if args.session_id and journal_session and args.session_id != journal_session:
            raise ValueError(
                f"--session-id {args.session_id} != journal session "
                f"{journal_session}")
        session_id = args.session_id or journal_session
        if not session_id:
            raise ValueError("journal carries no session_id; pass --session-id")
        nonce = journal_nonce(journal, args.nonce or None)
        if not nonce:
            raise JournalNonceError(
                "no nonce supplied and the journal carries none")
        journal_hash = journal_hash_for(journal)
        chatset_hash = args.chatset_hash or journal_field(journal, "chatset_hash")
        plan_hash = args.plan_hash or journal_field(journal, "plan_hash")
        events = journal.get("events")
        if not isinstance(events, list) or not events:
            raise ValueError("journal carries no events")
    except FileNotFoundError as exc:
        print(json.dumps({"error": "JOURNAL_INPUT_MISSING",
                          "detail": str(exc)}))
        return 1
    except JournalNonceError as exc:
        print(json.dumps({"error": "NONCE_REJECTED", "detail": str(exc)}))
        return 2
    except ValueError as exc:
        print(json.dumps({"error": "JOURNAL_INPUT_INVALID",
                          "detail": str(exc)}))
        return 2

    store = CheckpointStore(Path(args.out_dir), layout=args.layout)
    try:
        set_number = resolve_set_number(store, session_id, chatset_hash,
                                        args.set_number)
    except ValueError as exc:
        print(json.dumps({"error": "CHECKPOINT_PRECONDITION",
                          "detail": str(exc)}))
        return 3

    print(f"NONCE: {nonce}")
    print(f"JOURNAL_HASH: {journal_hash}")
    print(f"SESSION: {session_id}")
    print(f"CHATSET: {set_number}")
    print(f"EVENTS: {len(events)}")
    if chatset_hash:
        print(f"CHATSET_HASH: {chatset_hash}")
    if plan_hash:
        print(f"PLAN_HASH: {plan_hash}")

    if args.dry_run:
        print("STAGE: outrobeaver DRY-RUN (nothing written)")
        return 0

    try:
        stage = store.record_journal_hash(
            session_id, set_number, journal_hash, nonce=nonce,
            chatset_hash=chatset_hash, plan_hash=plan_hash,
            events=len(events), journal_path=str(Path(args.journal).resolve()))
    except PriorNonceRejected as exc:
        print(json.dumps({"error": "NONCE_REJECTED", "detail": str(exc)}))
        return 2
    except CheckpointPrecondition as exc:
        print(json.dumps({"error": "CHECKPOINT_PRECONDITION",
                          "detail": str(exc)}))
        return 3

    print(f"CHECKPOINT: {store._path(session_id)}")
    print(f"STAGE: outrobeaver {stage['status']}")
    print(f"HASH_FILE: {write_digest_file(args.hash_out, journal_hash)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Hash a planbeaver plan and record its checkpoint stage DONE.

    Prints ``NONCE: <supplied nonce>`` and ``PLAN_HASH: <sha256>``. Exits 0 on
    success, 1 when the plan is missing/unreadable, 2 when the plan or its
    nonce is rejected, 3 when the checkpoint precondition fails.
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m soma.machinetalk.checkpoint",
        description="Compute the canonical plan hash and mark planbeaver DONE.")
    parser.add_argument("--plan", default="/tmp/soma_planbeaver_plan.json",
                        help="planbeaver plan JSON (items array)")
    parser.add_argument("--out-dir", required=True,
                        help="run output dir holding checkpoints/")
    parser.add_argument("--session-id", default="",
                        help="session id (default: read from the plan)")
    parser.add_argument("--set-number", default="1",
                        help="chatset key recorded by the distiller")
    parser.add_argument("--layout", choices=("group", "chunk"), default="group")
    parser.add_argument("--nonce", default="",
                        help="supplied nonce (default: the plan's nonce)")
    parser.add_argument("--chatset-hash", default="",
                        help="expected chatset hash (default: from the plan)")
    parser.add_argument("--hash-out", default="/tmp/soma_planbeaver_hash.txt",
                        help="file receiving the bare plan hash")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and validate without writing anything")
    args = parser.parse_args(argv)

    try:
        plan = load_plan(args.plan)
        plan_session = plan_field(plan, "session_id")
        if args.session_id and plan_session and args.session_id != plan_session:
            raise ValueError(
                f"--session-id {args.session_id} != plan session "
                f"{plan_session}")
        session_id = args.session_id or plan_session
        if not session_id:
            raise ValueError("plan carries no session_id; pass --session-id")
        nonce = args.nonce or plan_nonce(plan)
        if not nonce:
            raise PlanNonceError("no nonce supplied and the plan carries none")
        plan_hash = plan_hash_for(plan, nonce)
        chatset_hash = args.chatset_hash or plan_field(plan, "chatset_hash")
    except FileNotFoundError as exc:
        print(json.dumps({"error": "PLAN_INPUT_MISSING", "detail": str(exc)}))
        return 1
    except PlanNonceError as exc:
        print(json.dumps({"error": "NONCE_REJECTED", "detail": str(exc)}))
        return 2
    except ValueError as exc:
        print(json.dumps({"error": "PLAN_INPUT_INVALID", "detail": str(exc)}))
        return 2

    print(f"NONCE: {nonce}")
    print(f"PLAN_HASH: {plan_hash}")
    print(f"SESSION: {session_id}")
    print(f"CHATSET: {args.set_number}")
    print(f"PLAN_ITEMS: {len(plan)}")
    if chatset_hash:
        print(f"CHATSET_HASH: {chatset_hash}")

    if args.dry_run:
        print("STAGE: planbeaver DRY-RUN (nothing written)")
        return 0

    store = CheckpointStore(Path(args.out_dir), layout=args.layout)
    try:
        stage = store.record_plan_hash(
            session_id, args.set_number, plan_hash, nonce=nonce,
            chatset_hash=chatset_hash, plan_items=len(plan),
            plan_path=str(Path(args.plan).resolve()))
    except PriorNonceRejected as exc:
        print(json.dumps({"error": "NONCE_REJECTED", "detail": str(exc)}))
        return 2
    except CheckpointPrecondition as exc:
        print(json.dumps({"error": "CHECKPOINT_PRECONDITION",
                          "detail": str(exc)}))
        return 3

    print(f"CHECKPOINT: {store._path(session_id)}")
    print(f"STAGE: planbeaver {stage['status']}")
    print(f"HASH_FILE: {write_plan_hash_file(args.hash_out, plan_hash)}")
    return 0


if __name__ == "__main__":
    # ``journal`` selects the outrobeaver journal writer; every other argument
    # set keeps the planbeaver plan CLI unchanged.
    _argv = sys.argv[1:]
    if _argv and _argv[0] == "journal":
        raise SystemExit(journal_main(_argv[1:]))
    raise SystemExit(main())
