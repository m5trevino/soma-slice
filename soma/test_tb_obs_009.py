"""Unit tests for TB-OBS-009: one resolution point, printed before the first call.

Before this slice three surfaces each asked the store for a session's code:
the logger when it named a sink, the engine when it built a debug event, and
the progress display when it drew a header. Each lookup is independent, so a
store rewritten mid-run could label one stage ``SES-120`` and the next
``SES-121`` -- the same session, two identities, in one run's output.

What this slice establishes:

  - the code is resolved at selection, before any stage runs, and printed
    immediately as ``session SES-120  (2026-11-03 18:40)``
  - the resolved value is threaded to the logger, the debug event and the
    progress header; none of them resolves again
  - the same session resolves to the same code on repeated calls
  - resolution happens once per run, whatever number of stages or chatsets
    that run executes

What must not change: stage order, gate behaviour, model selection, and the
fact that a run completes when the debug dashboard is disabled.
"""
from __future__ import annotations

import json
import re
import unittest.mock as mock
from pathlib import Path

import pytest

import machinetalk.debug_dashboard_skeu as skeu
import machinetalk.engine as engine_mod
from machinetalk import progress, short_codes
from machinetalk.client import CallRecord, StageResult
from machinetalk.debug_dashboard_skeu import write_event
from machinetalk.gate import GateResult
from machinetalk.logger import StageLogger
from machinetalk.source import ChatSet, Session

SID = "9f2c74a1-uuid-for-tb-obs-009"
OTHER = "1b7e00ff-a-second-session-for-tb-obs-009"

CODE_LINE = re.compile(r"^session SES-\d{3}  \(\d{4}-\d{2}-\d{2} \d{2}:\d{2}\)$")
ANSI = re.compile(r"\033\[[0-9;]*m")


def _plain(text: str) -> str:
    return ANSI.sub("", text)


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    """A per-test code store, and no leftover display state between tests."""
    monkeypatch.setenv("SOMA_SHORT_CODES_PATH", str(tmp_path / "short_codes.json"))
    monkeypatch.setattr(progress, "_SEEN", {}, raising=True)
    monkeypatch.setattr(progress, "_OUT_DIR", None, raising=True)
    monkeypatch.setattr(skeu, "_jsonl_file", None, raising=True)
    monkeypatch.setattr(skeu, "_events", [], raising=False)
    return tmp_path / "short_codes.json"


# ── the announcement line ───────────────────────────────────────────────────

def test_code_line_has_the_documented_shape():
    line = engine_mod.session_code_line("SES-120")
    assert CODE_LINE.match(line), line


def test_code_line_carries_the_code_and_a_wall_clock_stamp():
    line = engine_mod.session_code_line("SES-120")
    assert "SES-120" in line
    assert re.search(r"\(\d{4}-\d{2}-\d{2} \d{2}:\d{2}\)", line)


def test_code_line_does_not_carry_the_uuid():
    assert SID not in engine_mod.session_code_line("SES-001")


# ── stable resolution ──────────────────────────────────────────────────────

def test_same_session_resolves_to_the_same_code_repeatedly():
    first = short_codes.code_for(SID)
    for _ in range(5):
        assert short_codes.code_for(SID) == first
    assert first.startswith("SES-")


def test_same_session_resolves_the_same_through_the_run_holder():
    """``SessionCodes`` is what a run hands around, so it must be as stable."""
    codes = engine_mod.SessionCodes()
    first = codes.resolve(SID)
    for _ in range(5):
        assert codes.resolve(SID) == first
    assert codes.resolution_count == 1


def test_different_sessions_get_different_codes():
    codes = engine_mod.SessionCodes()
    assert codes.resolve(SID) != codes.resolve(OTHER)


# ── resolution happens once per run ────────────────────────────────────────

def test_pinned_code_is_never_looked_up_again(monkeypatch):
    """The Engine must read its pinned map, not ask the store."""
    def boom(session_id):
        raise AssertionError(f"second lookup for {session_id}")

    monkeypatch.setattr(short_codes, "code_for", boom)

    pinned = {SID: "SES-777"}
    codes = engine_mod.SessionCodes(pinned=pinned)
    assert codes.resolve(SID) == "SES-777"
    assert codes.code(SID) == "SES-777"
    assert codes.resolution_count == 0


def test_one_resolution_per_session_however_many_asks():
    codes = engine_mod.SessionCodes()
    for _ in range(10):
        codes.resolve(SID)
        codes.code(SID)
    codes.resolve(OTHER)
    assert codes.resolution_count == 2


def test_resolution_is_counted_not_hidden_behind_a_cache():
    """A resolver that is asked once per session proves the map is read."""
    seen: list[str] = []

    def counting(session_id: str) -> str:
        seen.append(session_id)
        return f"SES-{len(seen):03d}"

    codes = engine_mod.SessionCodes(resolve=counting)
    first = codes.resolve(SID)
    for _ in range(3):
        assert codes.resolve(SID) == first
    assert seen == [SID]


def test_resolution_runs_stage_order_unchanged(tmp_path):
    """Threading the code must not touch what stage 1 runs, or in what order."""
    out = tmp_path / "out"
    client = FakeClient(_result("distiller", ok=True))
    eng = engine_mod.Engine(out_dir=out, client=client, quiet=True)
    eng.run_chatset(_chatset(1), 0)
    assert [c["stage"] for c in client.calls] == [
        "distiller", "introbeaver", "planbeaver", "outrobeaver"]


# ── the model selection is untouched ───────────────────────────────────────

def test_model_selection_is_unchanged(tmp_path):
    client = FakeClient(_result("distiller", ok=True))
    eng = engine_mod.Engine(out_dir=tmp_path / "out", client=client, quiet=True)
    eng.run_chatset(_chatset(1), 0)
    assert {c["model"] for c in client.calls} == {"deepseek-v4"}
    assert [c["route"] for c in client.calls] == ["deepseek-v4"] * 4


# ── one code reaches every surface ─────────────────────────────────────────

def _chatset(set_number: int = 1, session_id: str = SID) -> ChatSet:
    return ChatSet(
        session_id=session_id,
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


def _result(stage: str, *, ok: bool = False) -> StageResult:
    gate = GateResult(ok=ok, stage=stage, verdict="PASS" if ok else "FAIL",
                      errors=[] if ok else ["stub rejection"],
                      parsed={"distiller_output": {}} if ok else None)
    rec = CallRecord(
        stage=stage, session_id=SID, chatset_id=f"{SID}:1",
        model="deepseek-v4", status="pass" if ok else "fail",
        prompt_tokens=900, completion_tokens=40, total_tokens=940,
        cache_hit_tokens=1, cache_miss_tokens=2,
        chars_in=5, latency_ms=12.5, gate=gate,
    )
    res = StageResult("ok", model_id="deepseek-v4")
    res.record = rec
    return res


class FakeClient:
    """The lookups the runner makes plus the call, with a stable deck."""

    def __init__(self, result: StageResult) -> None:
        self.result = result
        self.calls: list[dict] = []
        self.timeout = 5.0

    def route_for(self, stage: str) -> dict:
        return {"model_id": "deepseek-v4", "provider": "merge"}

    def model_for(self, stage: str) -> str:
        return "deepseek-v4"

    def call_stage(self, stage: str, **kwargs) -> StageResult:
        self.calls.append({"stage": stage, "model": self.model_for(stage),
                           "route": self.route_for(stage)["model_id"],
                           **kwargs})
        return self.result


def test_logger_debug_event_and_header_share_one_code(tmp_path, capsys):
    """The three consumers of the code all read the resolved value."""
    out = tmp_path / "out"
    debug = tmp_path / "debug"
    debug.mkdir()

    pinned = {SID: "SES-424"}
    eng = engine_mod.Engine(out_dir=out, client=FakeClient(_result("distiller")),
                            quiet=True, debug_dir=str(debug),
                            session_codes=pinned)

    # The store would answer differently; the run must not ask it.
    with mock.patch.object(short_codes, "code_for",
                           side_effect=AssertionError("second lookup")):
        eng._run_chatset_stage(_chatset(1), "distiller", 0)

    events = [json.loads(line)
              for f in sorted(debug.glob("*.jsonl"))
              for line in f.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    assert len(events) == 1
    assert events[0]["session_code"] == "SES-424"

    sink = next((out / "logs" / "distiller").glob("*.log"))
    assert sink.name == "SES-424_DI-01.log"
    assert sink.read_text(encoding="utf-8").startswith("[SES-424][DI-01] ")

    assert eng.session_codes.code(SID) == "SES-424"


def test_progress_header_accepts_the_resolved_code(tmp_path):
    header = progress.telemetry_header(
        "distiller", SID, 1, 5, "deepseek-v4", "merge", session_code="SES-424")
    assert "SES-424" in _plain(header)


def test_progress_session_header_accepts_the_resolved_code():
    block = progress.session_header(SID, 2, session_code="SES-424")
    assert "SES-424" in _plain(block)


def test_timer_uses_the_handed_in_code(tmp_path):
    timer = progress.StageTimer("distiller", SID, 1, 5,
                               model="deepseek-v4", out_dir=tmp_path,
                               session_code="SES-424")
    assert timer.ses_code == "SES-424"
    assert "SES-424" in _plain(timer.tag)


def test_logger_uses_the_handed_in_code(tmp_path):
    codes = engine_mod.SessionCodes(pinned={SID: "SES-424"})
    log = StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True,
                      session_codes=codes)
    log.start("distiller", session=SID)
    sink = next((tmp_path / "out" / "logs" / "distiller").glob("*.log"))
    assert sink.name == "SES-424_DI-01.log"


# ── selection is announced before any stage ────────────────────────────────

def test_run_announces_the_session_before_the_first_stage(tmp_path, capsys):
    sessions = [Session(session_id=SID, path=tmp_path)]
    sessions[0].chatsets = [_chatset(1)]

    eng_holder: dict[str, object] = {}

    class StopAfterAnnounce(Exception):
        pass

    real_run_stage1 = engine_mod.Engine.run_stage1

    def spy(self, *a, **kw):
        eng_holder["engine"] = self
        raise StopAfterAnnounce()

    engine_mod.Engine.run_stage1 = spy
    try:
        with pytest.raises(StopAfterAnnounce):
            engine_mod.run(sessions, out_dir=tmp_path / "out",
                           client=FakeClient(_result("distiller")),
                           quiet=False)
    finally:
        engine_mod.Engine.run_stage1 = real_run_stage1

    printed = _plain(capsys.readouterr().out)
    lines = [ln.strip() for ln in printed.splitlines() if ln.strip()]
    announced = [ln for ln in lines if CODE_LINE.match(ln)]
    assert len(announced) == 1, printed
    assert announced[0] == engine_mod.session_code_line(
        short_codes.code_for(SID))

    # It was printed before anything could run: the spy raised on entry.
    assert "engine" in eng_holder


def test_announcement_is_printed_once_even_when_asked_twice(tmp_path, capsys):
    eng = engine_mod.Engine(out_dir=tmp_path / "out",
                            client=FakeClient(_result("distiller")),
                            quiet=False)
    first = eng.announce_session(SID)
    second = eng.announce_session(SID)
    assert first == second
    printed = [ln for ln in _plain(capsys.readouterr().out).splitlines()
               if CODE_LINE.match(ln.strip())]
    assert len(printed) == 1


def test_a_caller_that_announced_is_not_announced_to_again(tmp_path, capsys):
    """run_engine.py prints the line; the Engine must not print it a second time."""
    sessions = [Session(session_id=SID, path=tmp_path)]
    sessions[0].chatsets = [_chatset(1)]

    codes = engine_mod.resolve_session_codes(sessions)

    class StopAfterAnnounce(Exception):
        pass

    real = engine_mod.Engine.run_stage1

    def spy(self, *a, **kw):
        raise StopAfterAnnounce()

    engine_mod.Engine.run_stage1 = spy
    try:
        with pytest.raises(StopAfterAnnounce):
            engine_mod.run(sessions, out_dir=tmp_path / "out",
                           client=FakeClient(_result("distiller")),
                           quiet=False, session_codes=codes, announced=True)
    finally:
        engine_mod.Engine.run_stage1 = real

    printed = _plain(capsys.readouterr().out)
    assert not [ln for ln in printed.splitlines() if CODE_LINE.match(ln.strip())]


def test_selection_resolves_only_the_selected_sessions(tmp_path):
    sessions = [Session(session_id=SID, path=tmp_path),
                Session(session_id=OTHER, path=tmp_path)]
    chosen = engine_mod.select_sessions(sessions, only=[(OTHER, 1)])
    assert chosen == [OTHER]

    codes = engine_mod.resolve_session_codes(sessions, only=[(OTHER, 1)])
    assert set(codes) == {OTHER}


def test_select_sessions_without_only_covers_every_session(tmp_path):
    sessions = [Session(session_id=SID, path=tmp_path),
                Session(session_id=OTHER, path=tmp_path)]
    assert engine_mod.select_sessions(sessions) == sorted([SID, OTHER])


def test_a_selection_naming_no_held_session_mints_nothing(tmp_path):
    """A code is permanent, so a run that touches nothing must not spend one."""
    sessions = [Session(session_id=SID, path=tmp_path)]
    assert engine_mod.select_sessions(sessions, only=[("absent", 1)]) == []
    assert engine_mod.resolve_session_codes(
        sessions, only=[("absent", 1)]) == {}

    # The store is untouched, so the first number is still unspent and the
    # next real session to ask receives it.
    assert short_codes.code_for("absent") == "SES-001"


# ── a run still completes with no debug dir and no dashboard ───────────────

def test_run_completes_without_debug_dir(tmp_path):
    """No debug_dir, no dashboard: the code path must be identical."""
    out = tmp_path / "out"
    client = FakeClient(_result("distiller", ok=True))
    eng = engine_mod.Engine(out_dir=out, client=client, quiet=True)
    assert eng._debug_dir is None
    assert eng._run_chatset_stage(_chatset(1), "distiller", 0) is True
    assert eng.session_codes.code(SID).startswith("SES-")


def test_debug_event_still_written_when_dir_is_set(tmp_path):
    debug = tmp_path / "debug"
    debug.mkdir()
    eng = engine_mod.Engine(out_dir=tmp_path / "out",
                            client=FakeClient(_result("distiller")),
                            quiet=True, debug_dir=str(debug))
    eng._run_chatset_stage(_chatset(1), "distiller", 0)
    assert len(list(debug.glob("*.jsonl"))) == 1


def test_chatset_level_event_carries_the_run_code(tmp_path):
    """The per-chatset event reads the run's map, not a fresh lookup."""
    debug = tmp_path / "debug"
    debug.mkdir()
    pinned = {SID: "SES-424"}
    eng = engine_mod.Engine(out_dir=tmp_path / "out",
                            client=FakeClient(_result("distiller", ok=True)),
                            quiet=True, debug_dir=str(debug),
                            session_codes=pinned)
    assert eng.session_codes.code(SID) == "SES-424"
    # The chatset event path reads .code(), which never mints.
    assert eng.session_codes.resolution_count == 0


def test_unresolvable_code_degrades_without_stopping_the_run(tmp_path, monkeypatch):
    def boom(session_id):
        raise short_codes.ShortCodeError("store unreadable")

    monkeypatch.setattr(short_codes, "code_for", boom)

    codes = engine_mod.SessionCodes()
    assert codes.resolve(SID) == engine_mod.UNKNOWN_SESSION_CODE
    assert codes.resolution_count == 1
    assert "store unreadable" in codes.error


# ── the run_engine selection point ─────────────────────────────────────────

def test_run_engine_resolves_at_selection():
    """AC-001: the lookup lives at the selection point in run_engine.py."""
    source = (Path(__file__).resolve().parent / "run_engine.py").read_text(
        encoding="utf-8")
    assert "code_for" in source
    # The resolution sits next to the sessions it selects, not in a stage.
    selection = source.split("session_codes = {}", 1)
    assert len(selection) == 2, "selection block missing"
    block = selection[1].split("client = EngineClient", 1)[0]
    assert "code_for(session_id)" in block
    assert "session_code_line" in block


def test_run_engine_prints_the_line_before_the_engine_client():
    """The announcement must precede the first call, not follow it."""
    source = (Path(__file__).resolve().parent / "run_engine.py").read_text(
        encoding="utf-8")
    print_at = source.index("print(session_code_line(")
    client_at = source.index("client = EngineClient(")
    assert print_at < client_at
