"""Unit tests for TB-OBS-006: one line saying what a stage actually produced.

``GateResult`` already carried ``ok``, ``errors``, ``verdict``, ``skipped``,
``skip_reason``, ``signal_quality`` and ``provenance``. Reading those to learn
whether a passing stage carried anything meant opening the artifact.

``accomplishment_summary`` answers that in one line:

  - ``"3 directives, 1 constraint"`` from the entries that were accepted
  - ``"skipped: NO_INTENT"``         when the stage stopped by design
  - ``"blocked: hash mismatch"``     when a hard block fired
  - ``"failed: 2 gate errors"``      otherwise

What must not change: the summary is reporting only. ``ok``, ``errors``,
``verdict``, ``skipped`` and ``skip_reason`` are produced exactly as before,
and no summary is ever built from the model's prose.
"""
from __future__ import annotations

import json

import pytest

from machinetalk.client import EngineClient, TurnKeyClient
from machinetalk.gate import GateResult, summarize, validate
from machinetalk.provenance import verify_distiller, verify_introbeaver

HASH = "a" * 64
OTHER_HASH = "b" * 64


# ── helpers ─────────────────────────────────────────────────────────────────

def _distiller_raw(*, skip: bool = False, skip_reason: str | None = None,
                   directives: list[str] | None = None,
                   constraints: list[str] | None = None,
                   echo: str = HASH) -> str:
    body = {
        "session_id": "s1",
        "chatset_id": "s1:1",
        "chatset_hash": echo,
        "skip": skip,
        "skip_reason": skip_reason,
        "exfil_type": "ARCHIVE" if skip else None,
        "signal_quality": "0.710",
        "directives": [{"text": t, "turn": 0} for t in directives or []],
        "constraints": [{"text": t, "turn": 0} for t in constraints or []],
        "preferences": [],
        "questions": [],
        "corrections": [],
        "confirmations": [],
        "artifacts": [],
    }
    return json.dumps({"distiller_output": body}, indent=2)


def _introbeaver_raw(entries: list[dict] | None = None,
                     echo: str = HASH) -> str:
    return json.dumps({"introbeaver_output": {
        "session_id": "s1",
        "input_hash": echo,
        "status": "OK",
        "entries": entries or [],
    }})


def _settled(gate: GateResult) -> tuple[bool, list[str], str, bool, str]:
    """The fields the summary must never influence."""
    return (gate.ok, list(gate.errors), gate.verdict,
            gate.skipped, gate.skip_reason)


# ── the field exists with a safe default ───────────────────────────────────

def test_field_defaults_to_empty_string():
    assert GateResult(ok=True, stage="distiller").accomplishment_summary == ""


def test_field_is_a_string_on_every_gate_validate_returns():
    for stage, raw in (("distiller", _distiller_raw(directives=["do it"])),
                       ("introbeaver", _introbeaver_raw()),
                       ("planbeaver", "{}"),
                       ("scout", "### SCOUT VIOLATION REPORT:\n\n"
                                 "SCOUT REVIEW COMPLETE. VERDICT: PASS"),
                       ("nope", "{}")):
        gate = validate(stage, raw)
        assert isinstance(gate.accomplishment_summary, str)
        assert gate.accomplishment_summary, f"{stage} left the summary empty"


# ── a skip ─────────────────────────────────────────────────────────────────

def test_distiller_skip_names_the_reason_token():
    gate = validate("distiller", _distiller_raw(skip=True,
                                                skip_reason="NO_SIGNAL"),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "skipped: NO_SIGNAL"
    assert gate.skipped is True
    assert gate.ok is True  # a clean skip clears the errors, unchanged


def test_introbeaver_no_intent_skip():
    raw = json.dumps({"introbeaver_output": {"input_hash": HASH,
                                             "status": "NO_INTENT"}})
    gate = validate("introbeaver", raw, expected_hash=HASH)
    assert gate.accomplishment_summary == "skipped: NO_INTENT"
    assert gate.skipped is True
    assert gate.ok is True


def test_a_skip_reports_no_entry_counts():
    """The model may still emit arrays alongside skip=true; they are not real."""
    gate = validate("distiller",
                    _distiller_raw(skip=True, skip_reason="NO_SIGNAL",
                                   directives=["do it", "do that"]),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "skipped: NO_SIGNAL"
    assert "directive" not in gate.accomplishment_summary


# ── a hard block ───────────────────────────────────────────────────────────

def test_hard_block_verdict_is_named():
    raw = json.dumps({"draft_artifact": "d", "status": "BROKEN_DRAFT"})
    gate = validate("drafter", raw)
    assert gate.accomplishment_summary == "blocked: BROKEN_DRAFT"
    assert gate.ok is False
    assert gate.skipped is False


def test_hash_mismatch_is_reported_as_a_block_not_a_retry():
    gate = validate("distiller", _distiller_raw(directives=["do it"],
                                                echo=OTHER_HASH),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "blocked: hash mismatch"
    assert gate.ok is False
    assert any(e.startswith("hash_mismatch") for e in gate.errors)


def test_a_missing_hash_echo_is_also_a_block():
    body = json.loads(_distiller_raw())["distiller_output"]
    del body["chatset_hash"]
    gate = validate("distiller", json.dumps({"distiller_output": body}),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "blocked: hash mismatch"
    assert gate.ok is False


# ── a success with N entries ───────────────────────────────────────────────

def test_success_counts_the_entries():
    gate = validate("distiller",
                    _distiller_raw(directives=["a here", "b here", "c here"],
                                   constraints=["keep it small"]),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "3 directives, 1 constraint"
    assert gate.ok is True
    assert gate.skipped is False


def test_singular_nouns_are_not_pluralised():
    gate = validate("distiller", _distiller_raw(directives=["only one"]),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "1 directive"


def test_empty_lists_are_omitted_rather_than_shown_as_zero():
    raw = _distiller_raw(directives=["a"], constraints=["b"],
                         )
    body = json.loads(raw)["distiller_output"]
    body["preferences"] = []
    gate = validate("distiller", json.dumps({"distiller_output": body}),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "1 directive, 1 constraint"


def test_introbeaver_success_counts_by_instruction_type():
    entries = [{"instruction_type": "DIRECTIVE", "verbatim_text": "a b c"},
               {"instruction_type": "DIRECTIVE", "verbatim_text": "d e f"},
               {"instruction_type": "CONSTRAINT", "verbatim_text": "g h i"},
               {"instruction_type": "PREFERENCE", "verbatim_text": "j k l"}]
    gate = validate("introbeaver", _introbeaver_raw(entries),
                    expected_hash=HASH)
    assert gate.accomplishment_summary == "2 directives, 1 constraint, 1 preference"
    assert gate.ok is True


def test_a_successful_stage_without_entry_lists_still_says_something():
    raw = json.dumps({"planbeaver_output": {"status": "READY"}})
    gate = validate("planbeaver", raw)
    assert gate.ok is True
    assert gate.accomplishment_summary == "accepted: READY"


def test_a_verdict_that_only_means_proceed_is_not_reported_as_a_result():
    """``false`` is how the distiller says "do not skip"; it is not an outcome."""
    gate = validate("distiller", _distiller_raw(directives=["do it"]),
                    expected_hash=HASH)
    assert gate.verdict == "false"
    assert gate.accomplishment_summary == "1 directive"


def test_a_pass_with_no_entries_says_zero_not_nothing():
    gate = validate("distiller", _distiller_raw(), expected_hash=HASH)
    assert gate.ok is True
    assert gate.accomplishment_summary == "accepted: 0 entries"


# ── a failure ──────────────────────────────────────────────────────────────

def test_failure_counts_the_gate_errors():
    gate = validate("nexus", "{}")
    assert gate.ok is False
    assert gate.accomplishment_summary == "failed: 2 gate errors"
    assert gate.accomplishment_summary.startswith("failed: 2 gate errors")


def test_a_single_error_is_singular():
    gate = validate("planbeaver", "{}")
    assert gate.accomplishment_summary == "failed: 1 gate error"


def test_unparseable_output_is_a_failure_not_a_block():
    gate = validate("distiller", "no json here", expected_hash=HASH)
    assert gate.ok is False
    assert gate.accomplishment_summary.startswith("failed: 1 gate error")


# ── ok and the rest are untouched ──────────────────────────────────────────

@pytest.mark.parametrize("stage,raw,expected_ok,expected_skipped", [
    ("distiller", _distiller_raw(skip=True, skip_reason="NO_SIGNAL"), True, True),
    ("distiller", _distiller_raw(directives=["a", "b", "c"]), True, False),
    ("nexus", "{}", False, False),
    ("drafter", json.dumps({"draft_artifact": "d", "status": "BROKEN_DRAFT"}),
     False, False),
])
def test_summary_does_not_change_the_verdict_fields(stage, raw, expected_ok,
                                                    expected_skipped):
    gate = validate(stage, raw, expected_hash=HASH if stage == "distiller"
                    else None)
    assert gate.ok is expected_ok
    assert gate.skipped is expected_skipped
    # Recomputing the summary is idempotent and touches nothing else.
    before = _settled(gate)
    assert summarize(gate) == gate.accomplishment_summary
    assert _settled(gate) == before


def test_block_error_list_still_carries_the_blocked_token():
    gate = validate("drafter", json.dumps({"draft_artifact": "d",
                                           "status": "CONTRACT_VIOLATION"}))
    assert gate.errors == ["blocked:CONTRACT_VIOLATION"]
    assert gate.accomplishment_summary == "blocked: CONTRACT_VIOLATION"


# ── the summary is derived from structure, never from prose ────────────────

def test_prose_around_the_object_is_recorded_but_never_counted():
    raw = ("Here are the results, 12 directives were found:\n"
           + _distiller_raw(directives=["a", "b"], echo=HASH))
    gate = validate("distiller", raw, expected_hash=HASH)
    assert gate.ok is True
    assert gate.accomplishment_summary == "2 directives"
    assert gate.accomplishment_summary != "12 directives"


# ── provenance: only accepted entries are counted ──────────────────────────

CHAT = [{"role": "user", "content": "do the thing. keep it small."}]


def _prov_payload(*texts: str) -> dict:
    return {"distiller_output": {
        "directives": [{"text": t, "turn": 0} for t in texts],
        "constraints": [],
    }}


def test_rejected_entries_are_not_counted_after_a_provenance_check():
    payload = _prov_payload("do the thing", "make it fast", "keep it small")
    prov = verify_distiller(payload, CHAT)
    assert prov.verified == 2 and len(prov.issues) == 1

    gate = GateResult(ok=True, stage="distiller", parsed=payload,
                      verdict="false")
    assert summarize(gate, prov.accepted) == "2 directives"
    # Counting the payload would have claimed three.
    assert summarize(gate) == "3 directives"


def test_provenance_accepts_nothing_when_every_quote_is_fabricated():
    payload = _prov_payload("make it fast", "and cheap")
    prov = verify_distiller(payload, CHAT)
    assert prov.verified == 0

    gate = GateResult(ok=True, stage="distiller", parsed=payload)
    assert summarize(gate, prov.accepted) == "accepted: 0 entries"


def test_introbeaver_counts_only_accepted_entries():
    payload = {"introbeaver_output": {"entries": [
        {"instruction_type": "DIRECTIVE", "verbatim_text": "do the thing"},
        {"instruction_type": "CONSTRAINT", "verbatim_text": "keep it small"},
        {"instruction_type": "DIRECTIVE", "verbatim_text": "ship it today"},
    ]}}
    prov = verify_introbeaver(payload, CHAT)
    assert len(prov.issues) == 1

    gate = GateResult(ok=True, stage="introbeaver", parsed=payload)
    assert summarize(gate, prov.accepted) == "1 directive, 1 constraint"


# ── the client populates it after the provenance check ─────────────────────

class FakeSDK:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    def run_stage(self, stage: str, messages: list[dict], **kwargs) -> dict:
        self.calls.append({"stage": stage, "messages": messages, "kwargs": kwargs})
        return {"model": "deepseek-v4",
                "choices": [{"message": {"content": self.content}}]}


class _FakeSoma:
    def get_stage_instruction(self, stage: str) -> dict:
        return {"body": "SYSTEM PROMPT"}


def _engine(content: str) -> EngineClient:
    engine = object.__new__(EngineClient)
    sdk = FakeSDK(content)
    tk = object.__new__(TurnKeyClient)
    tk._sdk = sdk
    tk._pea_kw = False
    engine._tk = tk
    engine.timeout = 180.0
    return engine


def test_client_summary_counts_verified_distiller_entries():
    content = _distiller_raw(directives=["do the thing", "keep it small"],
                             constraints=["keep it small"], echo=HASH)
    engine = _engine(content)
    res = engine.call_stage("distiller", session_id="s1", chatset_id="s1:1",
                            chunk="do the thing. keep it small.",
                            expected_hash=HASH, soma=_FakeSoma(),
                            raw_chat_set=CHAT)
    gate = res.record.gate
    assert gate.ok is True
    assert gate.accomplishment_summary == "2 directives, 1 constraint"


def test_client_summary_drops_a_fabricated_entry_and_keeps_the_failure():
    content = _distiller_raw(directives=["do the thing", "make it fast"],
                             echo=HASH)
    engine = _engine(content)
    res = engine.call_stage("distiller", session_id="s1", chatset_id="s1:1",
                            chunk="do the thing.", expected_hash=HASH,
                            soma=_FakeSoma(), raw_chat_set=CHAT)
    gate = res.record.gate
    assert gate.ok is False
    assert gate.accomplishment_summary == "failed: 1 gate error"
    assert "2 directives" not in gate.accomplishment_summary


def test_client_summary_is_present_without_a_provenance_check():
    content = json.dumps({"planbeaver_output": {"status": "READY"}})
    engine = _engine(content)
    res = engine.call_stage("planbeaver", session_id="s1", chatset_id="s1:1",
                            chunk="build it", soma=_FakeSoma())
    assert res.record.gate.accomplishment_summary == "accepted: READY"
    assert res.ok is True
