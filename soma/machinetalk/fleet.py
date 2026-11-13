"""Machine-talk fleet: prompt loading, stage definitions, payload assembly.

Stage graph (matches the final cli-manifest.json + beaver v1.1.1 pipeline):

  Stage 1 -- INGESTION (per ChatSet, ChatSets run in parallel)
    distiller   (tb-001)  [RAW_CHAT_TRANSCRIPT]{chunk}        -> signal or skip
    introbeaver (tb-004)  input: distiller output             -> extraction
    planbeaver  (tb-005)  input: introbeaver + raw set        -> build plan
    outrobeaver (tb-006)  input: planbeaver + extraction      -> session journal

  Stage 2 -- MAPPING (per session, sessions sequential oldest -> newest)
    nexus   (tb-007)  payload: raw_chat_set JSON               -> blueprints
    spark   (tb-008)  payload: merged nexus blueprints         -> consolidated spec
    falcon  (tb-009)  payload: spark + blueprint               -> execution plan

  Stage 3 -- CREATION
    eagle   (tb-010)  [FALCON_PAYLOAD]{chunk}                  -> architecture
    drafter (tb-011)  input: eagle + plan                      -> first-draft code
    scout   (tb-012)  {chunk} + [DNA_CONTRACTS]{contracts}     -> violation report

  Stage 4 -- FINALIZING
    oc    (tb-013)  input: draft + scout                       -> verdict
    olc   (tb-014)  input: approved draft                      -> hardened code

Every system prompt = tb-003-system-base + (tb-002-pipeline-protocol on
handoff stages) + the stage's tb-NNN prompt. The user message carries a
machine-talk JSON payload; responses must be raw JSON echoing the payload's
hash. Python validates -- never the LLM.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .hashes import (PlanNonceError, canonical_json, chatset_hash_for,
                     extraction_hash_for, load_extraction_lists, plan_hash_for,
                     plan_nonce)

FLEET_DIR = Path(__file__).resolve().parent.parent / "machine-talk"


@dataclass
class FleetPrompt:
    stage: str          # logical name, e.g. "distiller"
    number: str         # "tb-001"
    filename: str
    body: str
    handoff: bool = False   # append pipeline-protocol to system prompt
    slot: str = ""          # "{chunk}" injection, or "" for payload-fed
    payload_key: str = ""   # marker used when appending payload, e.g. "[FALCON_PAYLOAD]"


FLEET: list[FleetPrompt] = [
    FleetPrompt("distiller",   "tb-001", "tb-001-distiller.v2.md",   "", True,  "{chunk}",          "[RAW_CHAT_TRANSCRIPT]"),
    FleetPrompt("introbeaver", "tb-004", "tb-004-introbeaver.v2.md", "", True,  "",                "[EXTRACTION_PAYLOAD]"),
    FleetPrompt("planbeaver",  "tb-005", "tb-005-planbeaver.v2.md",  "", True,  "",                "[EXTRACTION_PAYLOAD]"),
    FleetPrompt("outrobeaver", "tb-006", "tb-006-outrobeaver.v2.md", "", True,  "",                "[EXTRACTION_PAYLOAD]"),
    FleetPrompt("nexus",       "tb-007", "tb-007-nexus.v2.md",       "", True,  "",                "[NEXUS_PAYLOAD]"),
    FleetPrompt("spark",       "tb-008", "tb-008-spark.v2.md",       "", True,  "",                "[NEXUS_PAYLOAD]"),
    FleetPrompt("falcon",      "tb-009", "tb-009-falcon.v2.md",      "", True,  "",                "[FALCON_PAYLOAD]"),
    FleetPrompt("eagle",       "tb-010", "tb-010-eagle.v2.md",       "", True,  "{chunk}",         "[FALCON_PAYLOAD]"),
    FleetPrompt("drafter",     "tb-011", "tb-011-drafter.v2.md",     "", True,  "",                "[DRAFTING_PAYLOAD]"),
    FleetPrompt("scout",       "tb-012", "tb-012-scout.v2.md",       "", True,  "{chunk}",         "[DNA_CONTRACTS]"),
    FleetPrompt("oc",          "tb-013", "tb-013-oc.v2.md",          "", True,  "",                "[REVIEW_PAYLOAD]"),
    FleetPrompt("olc",         "tb-014", "tb-014-olc.v2.md",         "", True,  "",                "[APPROVED_PAYLOAD]"),
]

_BY_STAGE = {p.stage: p for p in FLEET}

STAGE_ORDER = ["distiller", "introbeaver", "planbeaver", "outrobeaver",
               "nexus", "spark", "falcon",
               "eagle", "drafter", "scout",
               "oc", "olc"]

STAGE_GROUPS = {
    "stage1": ["distiller", "introbeaver", "planbeaver", "outrobeaver"],
    "stage2": ["nexus", "spark", "falcon"],
    "stage3": ["eagle", "drafter", "scout"],
    "stage4": ["oc", "olc"],
}

_STAGE_OF = {s: g for g, sts in STAGE_GROUPS.items() for s in sts}


def stage_of(stage: str) -> str:
    return _STAGE_OF.get(stage, stage)


def get_prompt(stage: str) -> FleetPrompt:
    if stage not in _BY_STAGE:
        raise KeyError(f"unknown stage: {stage!r} (known: {', '.join(_BY_STAGE)})")
    return _BY_STAGE[stage]


def load_system_prompt(stage: str, fleet_dir: Path | None = None,
                       strip_injection: bool = False) -> str:
    """system-base + (pipeline-protocol on handoffs) + stage prompt.

    strip_injection: for {chunk}/{contracts} slot prompts, drop the trailing
    marker+slot lines from the prompt body (they are re-emitted as the user
    message by assemble() with the real content).
    """
    fleet_dir = fleet_dir or FLEET_DIR
    p = get_prompt(stage)
    base = (fleet_dir / "tb-003-system-base.v2.md").read_text().rstrip()
    parts = [base]
    if p.handoff:
        proto = (fleet_dir / "tb-002-pipeline-protocol.v2.md").read_text().rstrip()
        parts.append(proto)
    # Load the actual stage prompt body from disk if not already cached
    body = p.body
    if not body:
        body = (fleet_dir / p.filename).read_text().rstrip()
    if strip_injection and p.slot == "{chunk}":
        body, _ = strip_injection_tail(body, stage)
    parts.append(body.rstrip())
    return "\n\n".join(parts)


_INJECTION_TAILS = {
    "distiller": "[RAW_CHAT_TRANSCRIPT]",
    "eagle":     "[FALCON_PAYLOAD]",
}


def strip_injection_tail(body: str, stage: str) -> tuple[str, list[str]]:
    """Split a {chunk}/{contracts} prompt into (instructions, marker_lines).

    Returns the prompt with its trailing marker/slot lines removed, plus the
    marker lines in order so assemble() can re-emit them with real content.
    """
    if stage == "scout":
        m = body.rfind("[CODE_ARTIFACT]")
        if m == -1:
            return body.rstrip(), ["[CODE_ARTIFACT]", "[DNA_CONTRACTS]"]
        return body[:m].rstrip(), ["[CODE_ARTIFACT]", "[DNA_CONTRACTS]"]
    marker = _INJECTION_TAILS[stage]
    i = body.rfind(marker)
    if i == -1:
        return body.rstrip(), [marker]
    return body[:i].rstrip(), [marker]


def assemble(stage: str, system_prompt: str, *,
             chunk: str = "", contracts: str = "",
             payload: dict | str | None = None) -> tuple[str, str]:
    """Build (system, user) for one stage call.

    - Slot prompts (distiller/eagle/scout): caller passes the system prompt
      from load_system_prompt(stage, strip_injection=True); the user message
      re-emits the markers with the real content.
    - Payload prompts (beavers/nexus/spark/falcon/drafter/oc/olc): the user
      message is the canonical JSON payload itself; the chatset_hash inside
      is what the model must echo back.
    """
    p = get_prompt(stage)
    if p.slot == "{chunk}":
        _, markers = strip_injection_tail(p.body, stage)
        if stage == "scout":
            user = f"{markers[0]}\n{chunk}\n{markers[1]}\n{contracts}"
        elif stage == "eagle":
            content = payload_envelope(payload) if isinstance(payload, dict) else (payload or chunk)
            user = f"{markers[0]}\n{content}"
        else:  # distiller: meta header (hash is given, never recomputed)
            meta = payload_envelope(payload) if isinstance(payload, dict) else ""
            header = f"[CHAT_SET_META]\n{meta}\n" if meta else ""
            user = f"{header}{markers[0]}\n{chunk}"
        return system_prompt, user
    if payload is not None:
        return system_prompt, (payload_envelope(payload) if isinstance(payload, dict) else payload)
    return system_prompt, chunk


def payload_envelope(payload: dict) -> str:
    """Canonical JSON for a payload block (the hash inside must be echoed)."""
    return canonical_json(payload)


def make_payload(*, session_id: str, chatset_id: str, raw_chat_set: list[dict],
                 **extra) -> dict:
    """Standard machine-talk payload: identity + timestamp + hash + data."""
    payload = {
        "session_id": session_id,
        "chatset_id": chatset_id,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chatset_hash": chatset_hash_for(raw_chat_set),
        "raw_chat_set": raw_chat_set,
    }
    payload.update(extra)
    return payload


EXTRACTION_KINDS = ("persons", "orgs", "documents", "events")


def _read_json(path: str | Path, label: str):
    """Parse one JSON file; missing/unreadable/invalid files raise."""
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"{label} not found: {p}") from None
    except OSError as exc:
        raise ValueError(f"{label} unreadable: {p} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {p} ({exc})") from exc


def _session_id_from_introbeaver(introbeaver) -> str | None:
    if not isinstance(introbeaver, dict):
        return None
    candidates = [introbeaver.get("session_id")]
    output = introbeaver.get("introbeaver_output")
    if isinstance(output, dict):
        candidates.append(output.get("session_id"))
    for cand in candidates:
        if isinstance(cand, str) and cand:
            return cand
    return None


def _extraction_from_introbeaver(introbeaver) -> tuple[object | None, str]:
    """Pull extraction data out of an introbeaver payload, if it carries any."""
    if not isinstance(introbeaver, dict):
        return None, "missing"
    ext = introbeaver.get("extraction")
    if isinstance(ext, (dict, list)) and ext:
        return ext, "introbeaver_payload"
    output = introbeaver.get("introbeaver_output")
    if isinstance(output, dict):
        entries = output.get("entries")
        if isinstance(entries, list) and entries:
            return {"entries": entries}, "introbeaver_output"
    return None, "missing"


def _extraction_from_files(entities_dir: str | Path) -> tuple[dict, str]:
    """Load the four entity files; an empty source reads as missing."""
    try:
        lists = load_extraction_lists(entities_dir)
    except FileNotFoundError:
        return {}, "missing"
    if not any(lists.get(kind) for kind in EXTRACTION_KINDS):
        return {}, "missing"
    return lists, "entity_files"


def _extraction_counts(extraction) -> dict[str, int]:
    """Item counts per entity kind (zero for kinds absent from the payload)."""
    if not isinstance(extraction, dict):
        return {"items": len(extraction) if isinstance(extraction, list) else 0}
    return {kind: len(extraction[kind]) if isinstance(extraction.get(kind), list) else 0
            for kind in EXTRACTION_KINDS}


def build_planbeaver_payload(
        introbeaver_path: str | Path | None = None,
        distiller_path: str | Path = "/tmp/soma_distiller_chatset.json",
        out_path: str | Path | None = None,
        *,
        session_id: str | None = None,
        entities_dir: str | Path = "/tmp",
        allow_empty_extraction: bool = False) -> dict:
    """Build the combined planbeaver payload (tb-005 input) and optionally write it.

    Sources:
        distiller_path   -> raw_chat_set (source of truth); chatset_hash is
                            recomputed here, never copied from a model echo.
        introbeaver_path -> extraction, when the payload carries it
                            (``extraction`` or ``introbeaver_output.entries``).
        entities_dir     -> extraction fallback: the four entity files read by
                            ``hashes.load_extraction_lists``.

    Args:
        introbeaver_path: Introbeaver payload JSON. When given it must exist;
            when omitted only the entity files can supply the extraction.
        distiller_path: Distiller chatset JSON array (the raw chat set).
        out_path: If given, write the payload JSON to this path.
        session_id: Explicit session id; otherwise taken from the introbeaver
            payload, else derived from the chatset hash.
        entities_dir: Directory holding soma_introbeaver_{kind}.json files.
        allow_empty_extraction: Permit a payload whose extraction has no items.
            Off by default so a missing upstream stage fails loudly.

    Returns:
        Payload dict with keys session_id, chatset_hash, extraction,
        raw_chat_set, plus a provenance block describing every source.

    Raises:
        FileNotFoundError: distiller file (or a named introbeaver file) is
            missing, or no extraction source exists and empty output is
            not allowed.
        ValueError: a source file is unreadable/invalid JSON, the raw chat set
            is not a list, or an introbeaver-supplied chatset_hash disagrees
            with the recomputed one.
    """
    raw_chat_set = _read_json(distiller_path, "distiller chatset")
    if not isinstance(raw_chat_set, list):
        raise ValueError(
            f"distiller chatset must be a JSON array, got {type(raw_chat_set).__name__}: "
            f"{distiller_path}")
    chatset_hash = chatset_hash_for(raw_chat_set)

    introbeaver = None
    if introbeaver_path is not None:
        introbeaver = _read_json(introbeaver_path, "introbeaver payload")
        claimed = introbeaver.get("chatset_hash") if isinstance(introbeaver, dict) else None
        if claimed and claimed != chatset_hash:
            raise ValueError(
                f"introbeaver chatset_hash {claimed} != recomputed {chatset_hash} "
                f"for {distiller_path}")

    if session_id:
        resolved_session_id, session_id_source = session_id, "explicit"
    else:
        resolved_session_id = _session_id_from_introbeaver(introbeaver)
        session_id_source = "introbeaver_payload" if resolved_session_id else "derived"
        if not resolved_session_id:
            resolved_session_id = f"sess-{chatset_hash[:12]}"

    extraction, extraction_source = _extraction_from_introbeaver(introbeaver)
    if extraction is None:
        extraction, extraction_source = _extraction_from_files(entities_dir)
    if extraction_source == "missing":
        if not allow_empty_extraction:
            raise FileNotFoundError(
                "EXTRACTION_UNAVAILABLE: no extraction in the introbeaver payload "
                f"({introbeaver_path or 'not supplied'}) and no entity files in "
                f"{entities_dir} ({', '.join('soma_introbeaver_' + k + '.json' for k in EXTRACTION_KINDS)})"
                " -- pass allow_empty_extraction=True for a placeholder extraction")
        extraction = {kind: [] for kind in EXTRACTION_KINDS}

    provenance = {
        "extraction_source": extraction_source,
        "extraction_counts": _extraction_counts(extraction),
        "session_id_source": session_id_source,
        "introbeaver_payload": str(introbeaver_path) if introbeaver_path else None,
        "distiller_chatset": str(distiller_path),
        "raw_chat_set_entries": len(raw_chat_set),
    }
    if isinstance(extraction, dict) and all(
            isinstance(extraction.get(kind), list) for kind in EXTRACTION_KINDS):
        provenance["extraction_hash"] = extraction_hash_for(extraction)

    payload = {
        "session_id": resolved_session_id,
        "chatset_hash": chatset_hash,
        "extraction": extraction,
        "raw_chat_set": raw_chat_set,
        "provenance": provenance,
    }

    if out_path is not None:
        Path(out_path).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

    return payload


def _plan_field(plan, name: str) -> str:
    """The single distinct non-empty ``name`` value across the plan items.

    Mirrors ``checkpoint.plan_field``; fleet deliberately stays independent of
    the checkpoint module. One plan describes one run of one chatset, so items
    that disagree mean the plan belongs to no single run and the value is
    refused rather than guessed.
    """
    if not isinstance(plan, list):
        raise ValueError(
            f"plan must be a JSON array of plan items, got {type(plan).__name__}")
    values = sorted({str(item.get(name)) for item in plan
                     if isinstance(item, dict) and item.get(name)})
    if len(values) > 1:
        raise ValueError(f"plan items disagree on {name}: {', '.join(values)}")
    return values[0] if values else ""


# Fields the tb-006 input contract names that no allowed source supplies.
OUTROBEAVER_CONTRACT_GAPS = ("final_artifact_state", "user_feedback")


def build_outrobeaver_payload(
        plan_path: str | Path = "/tmp/soma_planbeaver_plan.json",
        distiller_path: str | Path = "/tmp/soma_distiller_chatset.json",
        out_path: str | Path | None = None,
        *,
        introbeaver_path: str | Path | None = "/tmp/soma_introbeaver_payload.json",
        entities_dir: str | Path = "/tmp",
        session_id: str | None = None,
        nonce: str = "",
        allow_empty_extraction: bool = False) -> dict:
    """Build the combined outrobeaver payload (tb-006 input) and write it.

    Sources:
        plan_path        -> plan (a planbeaver items array) and its stamped nonce.
        distiller_path   -> raw_chat_set (source of truth); chatset_hash is
                            recomputed here, never copied from a model echo.
        introbeaver_path -> extraction when the payload carries it
                            (``extraction`` or ``introbeaver_output.entries``),
                            plus chatset_id; its chatset_hash must agree.
        entities_dir     -> extraction fallback: the four entity files read by
                            ``hashes.load_extraction_lists``.

    Args:
        plan_path: Planbeaver plan JSON (a list of plan items).
        distiller_path: Distiller chatset JSON array (the raw chat set).
        out_path: If given, write the payload JSON to this path.
        introbeaver_path: Introbeaver payload JSON; when given it must exist.
        entities_dir: Directory holding soma_introbeaver_{kind}.json files.
        session_id: Explicit session id; otherwise taken from the plan, then
            the introbeaver payload, else derived from the chatset hash.
        nonce: Supplied nonce. Defaults to the nonce the plan items carry; a
            supplied nonce that disagrees with the plan is rejected, so a stale
            plan cannot be stamped with a fresh run's nonce.
        allow_empty_extraction: Permit a payload whose extraction has no items.
            Off by default so a missing upstream stage fails loudly.

    Returns:
        Payload dict with keys session_id, nonce, chatset_hash, plan_hash, plan,
        extraction, raw_chat_set, plus a provenance block naming every source.

    Raises:
        FileNotFoundError: the plan or distiller file (or a named introbeaver
            file) is missing, or no extraction source exists and an empty
            extraction is not allowed.
        ValueError: a source file is unreadable/invalid JSON, the plan is not a
            list of objects or its items disagree on session_id/chatset_hash,
            the raw chat set is not a list, or a chatset_hash in the plan or the
            introbeaver payload disagrees with the recomputed one.
        PlanNonceError: no nonce is supplied and the plan carries none, or the
            supplied nonce disagrees with the plan's nonce.
    """
    plan = _read_json(plan_path, "planbeaver plan")
    _plan_field(plan, "nonce")          # refuses a plan of mixed provenance

    raw_chat_set = _read_json(distiller_path, "distiller chatset")
    if not isinstance(raw_chat_set, list):
        raise ValueError(
            f"distiller chatset must be a JSON array, got {type(raw_chat_set).__name__}: "
            f"{distiller_path}")
    chatset_hash = chatset_hash_for(raw_chat_set)

    plan_chatset_hash = _plan_field(plan, "chatset_hash")
    if plan_chatset_hash and plan_chatset_hash != chatset_hash:
        raise ValueError(
            f"plan chatset_hash {plan_chatset_hash} != recomputed {chatset_hash} "
            f"for {distiller_path}")

    plan_nonce_value = plan_nonce(plan)
    resolved_nonce = nonce or plan_nonce_value
    if not resolved_nonce:
        raise PlanNonceError("no nonce supplied and the plan carries none")
    plan_hash = plan_hash_for(plan, resolved_nonce)
    nonce_source = "supplied" if nonce else "plan"

    introbeaver = None
    if introbeaver_path is not None:
        introbeaver = _read_json(introbeaver_path, "introbeaver payload")
        claimed = introbeaver.get("chatset_hash") if isinstance(introbeaver, dict) else None
        if claimed and claimed != chatset_hash:
            raise ValueError(
                f"introbeaver chatset_hash {claimed} != recomputed {chatset_hash} "
                f"for {distiller_path}")

    plan_session = _plan_field(plan, "session_id")
    if session_id and plan_session and session_id != plan_session:
        raise ValueError(
            f"supplied session_id {session_id} != plan session {plan_session}")
    resolved_session_id = session_id or plan_session
    session_id_source = ("explicit" if session_id else "plan") if resolved_session_id else ""
    if not resolved_session_id:
        resolved_session_id = _session_id_from_introbeaver(introbeaver)
        session_id_source = "introbeaver_payload" if resolved_session_id else "derived"
        if not resolved_session_id:
            resolved_session_id = f"sess-{chatset_hash[:12]}"

    chatset_id = ""
    if isinstance(introbeaver, dict) and isinstance(introbeaver.get("chatset_id"), str):
        chatset_id = introbeaver["chatset_id"]

    extraction, extraction_source = _extraction_from_introbeaver(introbeaver)
    if extraction is None:
        extraction, extraction_source = _extraction_from_files(entities_dir)
    if extraction_source == "missing":
        if not allow_empty_extraction:
            raise FileNotFoundError(
                "EXTRACTION_UNAVAILABLE: no extraction in the introbeaver payload "
                f"({introbeaver_path or 'not supplied'}) and no entity files in "
                f"{entities_dir} ({', '.join('soma_introbeaver_' + k + '.json' for k in EXTRACTION_KINDS)})"
                " -- pass allow_empty_extraction=True for a placeholder extraction")
        extraction = {kind: [] for kind in EXTRACTION_KINDS}

    provenance = {
        "extraction_source": extraction_source,
        "extraction_counts": _extraction_counts(extraction),
        "session_id_source": session_id_source,
        "nonce_source": nonce_source,
        "plan_path": str(plan_path),
        "plan_items": len(plan),
        "plan_hash": plan_hash,
        "introbeaver_payload": str(introbeaver_path) if introbeaver_path else None,
        "distiller_chatset": str(distiller_path),
        "raw_chat_set_entries": len(raw_chat_set),
        "contract_gaps": list(OUTROBEAVER_CONTRACT_GAPS),
    }
    if isinstance(extraction, dict) and all(
            isinstance(extraction.get(kind), list) for kind in EXTRACTION_KINDS):
        provenance["extraction_hash"] = extraction_hash_for(extraction)

    payload = {
        "session_id": resolved_session_id,
        "nonce": resolved_nonce,
        "chatset_hash": chatset_hash,
        "plan_hash": plan_hash,
        "plan": plan,
        "extraction": extraction,
        "raw_chat_set": raw_chat_set,
        "provenance": provenance,
    }
    if chatset_id:
        payload["chatset_id"] = chatset_id

    if out_path is not None:
        Path(out_path).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

    return payload
