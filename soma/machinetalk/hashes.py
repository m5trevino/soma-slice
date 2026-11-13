"""Canonical JSON + SHA-256 helpers for machine-talk payloads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_json(obj) -> str:
    """Canonical serialization: sorted keys, compact separators, no ASCII escapes.

    This is the exact form the distiller hashes (and echoes back), so every
    downstream stage verifies against the same bytes.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chatset_hash_for(raw_chat_set) -> str:
    """SHA-256 over the canonical raw_chat_set array (no whitespace variance)."""
    return sha256_hex(canonical_json(raw_chat_set))


# -- introbeaver extraction ------------------------------------------------

# entity kind -> (filename, keys that may wrap the list in the file)
EXTRACTION_FILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "persons": ("soma_introbeaver_persons.json",
                ("persons", "people", "entities", "items", "results")),
    "orgs": ("soma_introbeaver_orgs.json",
             ("orgs", "organisations", "organizations", "entities",
              "items", "results")),
    "documents": ("soma_introbeaver_documents.json",
                  ("documents", "docs", "entities", "items", "results")),
    "events": ("soma_introbeaver_events.json",
               ("events", "entities", "items", "results")),
}


def _entity_list(payload, wrapper_keys: tuple[str, ...]) -> list:
    """The entity list held by one file payload.

    A bare list is the list; a wrapper dict contributes the first known list
    key (other keys, such as a ``count`` echo, are metadata and ignored); a
    lone entity object becomes a one-item list.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in wrapper_keys:
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload] if payload else []
    return []


def load_extraction_lists(directory: str | Path = "/tmp") -> dict[str, list]:
    """Read the four introbeaver entity files into ``{kind: list}``.

    Every kind is always present in the result (missing files yield ``[]``).
    Raises ``FileNotFoundError`` when none of the four files exist — there is
    nothing to hash — and ``ValueError`` when a file exists but is not valid
    JSON.
    """
    base = Path(directory)
    loaded: dict[str, list] = {}
    present: list[str] = []
    missing: list[str] = []
    for kind, (filename, wrapper_keys) in EXTRACTION_FILES.items():
        path = base / filename
        if not path.is_file():
            missing.append(filename)
            loaded[kind] = []
            continue
        present.append(filename)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: unreadable entity file ({exc})") from exc
        loaded[kind] = _entity_list(payload, wrapper_keys)
    if not present:
        raise FileNotFoundError(
            f"no introbeaver entity files in {base}: {', '.join(missing)}")
    return loaded


def extraction_hash_for(entity_lists) -> str:
    """SHA-256 over the canonical JSON of the four entity lists combined.

    ``entity_lists`` maps entity kind -> list (as returned by
    ``load_extraction_lists``). Only the four known kinds are hashed, always
    in the same canonical form, so a re-run over identical extraction yields
    an identical hash and any change to any entity changes it.
    """
    combined = {kind: list(entity_lists.get(kind) or [])
                for kind in EXTRACTION_FILES}
    return sha256_hex(canonical_json(combined))


# -- planbeaver plan -------------------------------------------------------

class PlanNonceError(ValueError):
    """A plan carries no nonce, mixed nonces, or a nonce that was not supplied."""


def plan_nonces(plan) -> list[str]:
    """Distinct non-empty nonces stamped on the plan items, in plan order.

    Every item carries the nonce of the run that produced the plan, so a plan
    describes exactly one run. Duplicates collapse; the order is the order of
    first appearance.
    """
    if not isinstance(plan, list):
        raise ValueError(
            f"plan must be a JSON array of plan items, got "
            f"{type(plan).__name__}")
    seen: list[str] = []
    for item in plan:
        if not isinstance(item, dict):
            raise ValueError(
                f"plan item must be an object, got {type(item).__name__}")
        nonce = item.get("nonce")
        if nonce and nonce not in seen:
            seen.append(nonce)
    return seen


def plan_nonce(plan) -> str:
    """The single nonce stamped on every plan item (``""`` when there is none).

    Raises ``PlanNonceError`` when the items disagree: a plan built from two
    runs cannot be attributed to one nonce, and stamping it with either one
    would be a claim the plan does not support.
    """
    nonces = plan_nonces(plan)
    if len(nonces) > 1:
        raise PlanNonceError(
            "plan items carry conflicting nonces: " + ", ".join(nonces))
    return nonces[0] if nonces else ""


# -- outrobeaver journal ---------------------------------------------------

JOURNAL_CONTRACT = "JOURNAL_EVENTS_V1"


class JournalNonceError(ValueError):
    """A journal carries no nonce, mixed nonces, or a nonce that was not supplied."""


def journal_nonces(journal) -> list[str]:
    """Distinct non-empty nonces stamped on the journal, in journal order.

    A journal carries its run nonce at the top level and (tb-006) on every
    event. Both are read here so a journal whose events disagree with its
    header is rejected instead of being attributed to either one.
    """
    if not isinstance(journal, dict):
        raise ValueError(
            f"journal must be a JSON object, got {type(journal).__name__}")
    seen: list[str] = []
    candidates: list[str] = []
    top = journal.get("nonce")
    if top:
        candidates.append(str(top))
    events = journal.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and event.get("nonce"):
                candidates.append(str(event["nonce"]))
    for nonce in candidates:
        if nonce not in seen:
            seen.append(nonce)
    return seen


def journal_nonce(journal, nonce: str | None = None) -> str:
    """The single nonce the journal carries (``""`` when there is none).

    Args:
        journal: the parsed outrobeaver journal object.
        nonce: the supplied nonce. When given it must match the nonce the
            journal carries, so a stale journal cannot be stamped with a
            fresh run identity.

    Raises:
        ValueError: ``journal`` is not an object.
        JournalNonceError: the journal's nonces are mixed, or disagree with
            ``nonce``.
    """
    stamped = journal_nonces(journal)
    if len(stamped) > 1:
        raise JournalNonceError(
            "journal carries conflicting nonces: " + ", ".join(stamped))
    found = stamped[0] if stamped else ""
    if nonce and found and nonce != found:
        raise JournalNonceError(
            f"supplied nonce {nonce} != journal nonce {found}")
    return nonce or found


def journal_hash_for(journal) -> str:
    """SHA-256 over ``canonical_json(journal)`` — the parsed journal itself.

    The hash covers the parsed journal, never the file's bytes: re-indenting
    the JSON leaves the hash unchanged, while any change to an event, the
    summary, or a provenance field changes it. This is the same canonical
    form ``plan_hash_for`` and ``chatset_hash_for`` use, so the checkpoint
    verifies the journal exactly the way it verifies its inputs.
    """
    if not isinstance(journal, dict):
        raise ValueError(
            f"journal must be a JSON object, got {type(journal).__name__}")
    return sha256_hex(canonical_json(journal))


def plan_hash_for(plan, nonce: str | None = None) -> str:
    """SHA-256 over ``canonical_json(plan)`` — the plan items array itself.

    The hash covers the parsed plan, never the plan file's bytes: re-indenting
    or re-encoding the JSON leaves the hash unchanged, while any change to an
    item id, description, priority or dependency changes it. This is the same
    canonical form ``chatset_hash_for`` and ``extraction_hash_for`` use, so a
    downstream stage verifies the plan exactly the way it verifies its inputs.

    Args:
        plan: the planbeaver items list (as parsed from the plan JSON).
        nonce: the supplied nonce. When given it must match the nonce the plan
            items carry, so a stale plan cannot be stamped with a fresh nonce.

    Raises:
        ValueError: ``plan`` is not a list of objects.
        PlanNonceError: the plan's nonces are mixed, or disagree with ``nonce``.
    """
    stamped = plan_nonce(plan)
    if nonce and stamped and nonce != stamped:
        raise PlanNonceError(
            f"supplied nonce {nonce} != plan nonce {stamped}")
    return sha256_hex(canonical_json(plan))
