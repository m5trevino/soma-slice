"""Unit tests for TB-OBS-005: codes and cache figures on the debug event.

``debug_dashboard_skeu.write_event()`` already persisted one JSONL line per
completed stage, but the event dict carried only the session uuid, the stage
name and the provider's token counts. That made a dashboard row impossible to
correlate with the terminal (which shows ``SES-001`` / ``DI-01``) or with a PEA
file (which is named by the same codes).

Each event now also carries:

  - ``session_code``      from ``short_codes.code_for(sid)``
  - ``stage_code``        from ``short_codes.stage_code(stage, n)``
  - ``cache_hit_tokens``  from the call record
  - ``cache_miss_tokens`` from the call record

What must not change: the event is still written inside the existing
try/except, so a failing write cannot fail the stage, and ``write_event``
still serialises every key it is handed.
"""
from __future__ import annotations

import json

import pytest

import machinetalk.debug_dashboard_skeu as skeu
from machinetalk import engine as engine_mod
from machinetalk import short_codes
from machinetalk.client import CallRecord, StageResult
from machinetalk.debug_dashboard_skeu import write_event
from machinetalk.gate import GateResult
from machinetalk.source import ChatSet

SID = "9f2c74a1-uuid-for-tb-obs-005"
NEW_KEYS = ("session_code", "stage_code", "cache_hit_tokens", "cache_miss_tokens")


# ── the stage runner's collaborators, faked ─────────────────────────────────

class FakeClinical:
    """The lookups the runner makes before the call, plus the call itself."""

    def __init__(self, result: StageResult) -> None:
        self.result = result
        self.calls: list[dict] = []
        self.timeout = 5.0

    def route_for(self, stage: str) -> dict:
        return {"model_id": "deepseek-v4", "provider": "merge"}

    def model_for(self, stage: str) -> str:
        return "deepseek-v4"

    def call_stage(self, stage: str, **kwargs) -> StageResult:
        self.calls.append({"stage": stage, **kwargs})
        return self.result


def _chatset(set_number: int = 1) -> ChatSet:
    return ChatSet(
        session_id=SID,
        set_number=set_number,
        source_chat_set_id="src-1",
        files=["000001.json"],
        raw_chat_set=[{"role": "user", "content": "hello"}],
        user_text="hello",
        assistant_text="hi",
        is_chunked=False,
        chars=5,
        chatset_hash="h" * 64,
        provider="test",
    )


def _result(stage: str, *, ok: bool = False, cache_hit: int = 768,
            cache_miss: int = 132) -> StageResult:
    """A StageResult whose ``record`` is what ``client.call_stage`` attaches."""
    gate = GateResult(ok=ok, stage=stage, verdict="PASS" if ok else "FAIL",
                      errors=[] if ok else ["stub rejection"],
                      parsed={"distiller_output": {"nodes": []}} if ok else None)
    rec = CallRecord(
        stage=stage, session_id=SID, chatset_id=f"{SID}:1",
        model="deepseek-v4", status="pass" if ok else "fail",
        prompt_tokens=900, completion_tokens=40, total_tokens=940,
        cache_hit_tokens=cache_hit, cache_miss_tokens=cache_miss,
        chars_in=5, latency_ms=12.5, gate=gate,
    )
    res = StageResult("ok", model_id="deepseek-v4")
    res.record = rec
    return res


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    """A per-test short-code store and a per-test JSONL sink.

    ``_jsonl_file`` is module state in the dashboard, so without resetting it
    the second test would append to the first test's file.
    """
    monkeypatch.setenv("SOMA_SHORT_CODES_PATH", str(tmp_path / "short_codes.json"))
    monkeypatch.setattr(skeu, "_jsonl_file", None, raising=True)
    monkeypatch.setattr(skeu, "_events", [], raising=False)
    return tmp_path


def _run_stage(tmp_path, stage: str = "distiller", set_number: int = 1,
               *, ok: bool = False, cache_hit: int = 768,
               cache_miss: int = 132):
    """Drive ``Engine._run_chatset_stage`` and return the persisted event.

    A failing gate keeps the run on the short path (no Soma graph writes),
    while the event dict is built by the same code either way.
    """
    out = tmp_path / "out"
    debug = tmp_path / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    client = FakeClinical(_result(stage, ok=ok, cache_hit=cache_hit,
                                  cache_miss=cache_miss))
    eng = engine_mod.Engine(out_dir=out, client=client, quiet=True,
                            debug_dir=str(debug))
    eng._run_chatset_stage(_chatset(set_number), stage, 0)

    files = sorted(debug.glob("*.jsonl"))
    assert len(files) == 1, f"expected one JSONL sink, found {files}"
    lines = [json.loads(line) for line in
             files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one event, found {len(lines)}"
    return lines[0], eng, client


# ── wiring: the import belongs at module level ──────────────────────────────

def test_engine_imports_short_codes_at_module_level():
    assert engine_mod.short_codes is short_codes


def test_every_runnable_stage_has_a_stage_code():
    """``stage_code`` raises on an unknown stage; no engine stage may hit that."""
    for stage in engine_mod.GROUP_OF:
        code = short_codes.stage_code(stage, 1)
        assert code.endswith("-01")


# ── all four keys are on the event ──────────────────────────────────────────

def test_event_carries_all_four_new_keys(tmp_path):
    ev, _, _ = _run_stage(tmp_path)
    for key in NEW_KEYS:
        assert key in ev, f"{key} missing from the debug event"


@pytest.mark.parametrize("stage,set_number,expected", [
    ("distiller", 1, "DI-01"),
    ("introbeaver", 2, "IB-02"),
    ("nexus", 3, "NX-03"),
    ("drafter", 12, "DR-12"),
])
def test_codes_match_the_helpers(tmp_path, stage, set_number, expected):
    ev, _, _ = _run_stage(tmp_path, stage, set_number)
    assert ev["session_code"] == short_codes.code_for(SID)
    assert ev["stage_code"] == short_codes.stage_code(stage, set_number)
    assert ev["stage_code"] == expected
    assert ev["session_id"] == SID


def test_session_code_is_the_resolved_code_not_the_uuid(tmp_path):
    ev, _, _ = _run_stage(tmp_path)
    assert ev["session_code"].startswith("SES-")
    assert SID not in ev["session_code"]


def test_cache_figures_are_the_record_values(tmp_path):
    ev, _, _ = _run_stage(tmp_path, cache_hit=640, cache_miss=60)
    assert ev["cache_hit_tokens"] == 640
    assert ev["cache_miss_tokens"] == 60


def test_zero_cache_hit_is_kept_as_zero(tmp_path):
    ev, _, _ = _run_stage(tmp_path, cache_hit=0, cache_miss=900)
    assert ev["cache_hit_tokens"] == 0
    assert ev["cache_miss_tokens"] == 900


def test_new_keys_are_typed_as_documented(tmp_path):
    ev, _, _ = _run_stage(tmp_path)
    assert isinstance(ev["session_code"], str)
    assert isinstance(ev["stage_code"], str)
    for key in ("cache_hit_tokens", "cache_miss_tokens"):
        assert isinstance(ev[key], int) and not isinstance(ev[key], bool)


def test_done_path_also_carries_the_new_keys(tmp_path):
    """The event is written before the gate branches, so DONE must carry them."""
    ev, _, _ = _run_stage(tmp_path, "distiller", 1, ok=True,
                          cache_hit=1024, cache_miss=256)
    assert ev["status"] == "done"
    assert ev["session_code"] == short_codes.code_for(SID)
    assert ev["stage_code"] == "DI-01"
    assert (ev["cache_hit_tokens"], ev["cache_miss_tokens"]) == (1024, 256)


# ── the existing event shape is intact ─────────────────────────────────────

def test_existing_keys_survive(tmp_path):
    ev, _, _ = _run_stage(tmp_path)
    for key in ("stage", "status", "session_id", "chatset_number", "chatset_id",
                "chars_in", "model", "tokens", "prompt_tokens",
                "completion_tokens", "latency_ms", "attempts", "errors",
                "verdict", "payload", "timestamp"):
        assert key in ev, f"regression: {key} dropped from the debug event"
    assert ev["tokens"] == 940
    assert ev["stage"] == "distiller"


# ── write_event serialises whatever it is given ────────────────────────────

def test_write_event_keeps_the_new_keys_and_unknown_ones(tmp_path):
    """The ``clean`` filter must not drop keys it does not know about."""
    event = {"stage": "distiller", "session_code": "SES-001",
             "stage_code": "DI-01", "cache_hit_tokens": 768,
             "cache_miss_tokens": 132, "an_invented_key": "kept"}
    write_event(tmp_path / "d", event)

    written = [json.loads(line)
               for f in (tmp_path / "d").glob("*.jsonl")
               for line in f.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    assert len(written) == 1
    for key in NEW_KEYS + ("an_invented_key",):
        assert written[0][key] == event[key]


def test_written_event_round_trips_the_codes(tmp_path):
    ev, _, _ = _run_stage(tmp_path, "planbeaver", 4)
    assert ev["session_code"] == short_codes.code_for(SID)
    assert ev["stage_code"] == short_codes.stage_code("planbeaver", 4) == "PB-04"


# ── the try/except around write_event is preserved ─────────────────────────

def test_write_failure_does_not_break_the_stage(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(skeu, "write_event", boom)

    debug = tmp_path / "debug"
    debug.mkdir()
    eng = engine_mod.Engine(out_dir=tmp_path / "out",
                            client=FakeClinical(_result("distiller")),
                            quiet=True, debug_dir=str(debug))

    # The stage reports its own failure; the debug write must not raise.
    assert eng._run_chatset_stage(_chatset(1), "distiller", 0) is False
    assert list(debug.glob("*.jsonl")) == []


def test_code_allocation_failure_degrades_and_the_event_still_lands(
        tmp_path, monkeypatch):
    """A code that cannot be resolved is ``unknown``, not a dropped event.

    TB-OBS-009 moved resolution ahead of the stage and made its failure
    non-fatal, the same way the logger and the display already treat it. The
    stage must still run and the event must still be written, because losing
    the record of a stage is worse than labelling it ``unknown``.
    """
    def boom(session_id):
        raise short_codes.ShortCodeError("store unreadable")

    monkeypatch.setattr(short_codes, "code_for", boom)

    debug = tmp_path / "debug"
    debug.mkdir()
    eng = engine_mod.Engine(out_dir=tmp_path / "out",
                            client=FakeClinical(_result("distiller")),
                            quiet=True, debug_dir=str(debug))

    assert eng._run_chatset_stage(_chatset(1), "distiller", 0) is False

    files = sorted(debug.glob("*.jsonl"))
    assert len(files) == 1
    ev = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert ev["session_code"] == engine_mod.UNKNOWN_SESSION_CODE
    assert ev["stage_code"] == "DI-01"
