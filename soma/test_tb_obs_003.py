"""Unit tests for TB-OBS-003: codes, clickable log links, real timing.

What the operator sees:
  - the session header shows ``SES-001``, never the uuid
  - a stage line reads ``[SES-001][DI-01] DISTILLER``
  - a finished stage prints an absolute ``file://`` link to its log
  - the start wall-clock time and the measured duration are separate labels

What must survive: the in-place spinner and the per-chatset table.
"""
import re
import time
from pathlib import Path

import pytest

from machinetalk import progress
from machinetalk.logger import StageLogger

SID = "9f2c74a1-uuid-for-tb-obs-003"
OTHER = "1b7e00ff-a-second-session"

WALL_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
DURATION = re.compile(r"duration\s+\d+\.\d+s")
ANSI = re.compile(r"\033\[[0-9;]*m")


def _plain(text: str) -> str:
    """Drop ANSI styling so assertions read the text, not the colour codes."""
    return ANSI.sub("", text)


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    """Point codes at a per-test store and logs at a per-test out dir."""
    path = tmp_path / "short_codes.json"
    monkeypatch.setenv("SOMA_SHORT_CODES_PATH", str(path))
    monkeypatch.setenv("SOMA_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(progress, "_SEEN", {}, raising=True)
    monkeypatch.setattr(progress, "_OUT_DIR", None, raising=True)
    return path


@pytest.fixture
def out(tmp_path):
    out_dir = tmp_path / "out"
    (out_dir / "logs" / "distiller").mkdir(parents=True)
    return out_dir


def _timer(stage: str = "introbeaver", session: str = SID) -> progress.StageTimer:
    return progress.StageTimer(stage, session, 1, 1234)


# ── the header carries a code, not the uuid ─────────────────────────────────

def test_header_contains_a_ses_code():
    header = progress.session_header(SID, 4)
    assert "SES-001" in header


def test_header_shows_the_code_on_its_own_line():
    blocked = progress.session_header(SID, 4).splitlines()
    assert blocked[1] == "SES-001"


def test_header_never_shows_the_uuid():
    assert SID not in progress.session_header(SID, 4)


def test_header_code_is_stable_across_calls():
    first = progress.session_header(SID, 2)
    second = progress.session_header(SID, 9)
    assert "SES-001" in first and "SES-001" in second


def test_a_second_session_gets_its_own_code():
    assert "SES-001" in progress.session_header(SID, 1)
    assert "SES-002" in progress.session_header(OTHER, 1)


def test_header_keeps_the_chatset_count():
    assert "4 CHAT SETS" in progress.session_header(SID, 4)


# ── stage lines carry both codes ────────────────────────────────────────────

@pytest.mark.parametrize("stage,code", [
    ("distiller", "DI-01"),
    ("introbeaver", "IB-01"),
    ("planbeaver", "PB-01"),
    ("outrobeaver", "OB-01"),
])
def test_stage_tag_reads_session_then_stage_code(stage, code):
    assert progress.code_tag(stage, SID) == f"[SES-001][{code}]"


def test_telemetry_header_names_both_codes():
    text = progress.telemetry_header("distiller", SID, 1, 900, "m", "p")
    assert "[SES-001][DI-01] DISTILLER" in text


def test_telemetry_header_shows_no_uuid():
    text = progress.telemetry_header("distiller", SID, 1, 900, "m", "p")
    assert SID not in text


def test_finished_stage_line_names_both_codes(capsys):
    timer = _timer()
    timer.start()
    timer.finish("DONE")
    printed = capsys.readouterr().out
    assert "[SES-001][IB-01] INTRO-BEAVER" in printed


def test_live_line_names_both_codes():
    timer = _timer()
    timer._started = time.monotonic()
    assert "[SES-001][IB-01]" in timer._line()


def test_live_line_never_names_the_uuid():
    timer = _timer()
    timer._started = time.monotonic()
    assert SID not in timer._line()


# ── the log link is absolute and clickable ──────────────────────────────────

def test_log_uri_is_a_file_uri(out):
    uri = progress.log_uri("distiller", SID, out_dir=out)
    assert uri.startswith("file:///")


def test_log_uri_is_absolute(out):
    uri = progress.log_uri("distiller", SID, out_dir=out)
    assert Path(uri[len("file://"):]).is_absolute()


def test_log_uri_ends_in_the_coded_filename(out):
    uri = progress.log_uri("distiller", SID, out_dir=out)
    assert uri.endswith("SES-001_DI-01.log")


def test_log_uri_contains_both_codes(out):
    uri = progress.log_uri("planbeaver", SID, out_dir=out)
    assert "SES-001_PB-01.log" in uri


def test_log_uri_has_no_uuid(out):
    assert SID not in progress.log_uri("distiller", SID, out_dir=out)


def test_log_uri_points_at_the_file_the_logger_wrote(tmp_path):
    """The link names the sink the logger actually wrote, not a guess."""
    log = StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True)
    log.start("distiller", session=SID, chatset=1)
    sink = tmp_path / "out" / "logs" / "distiller" / "SES-001_DI-01.log"
    assert sink.is_file()
    assert progress.log_uri("distiller", SID, out_dir=tmp_path / "out") == \
        sink.resolve().as_uri()


def test_log_uri_finds_the_sink_under_cwd_without_a_configured_out_dir(
        tmp_path, monkeypatch):
    sink = tmp_path / "runout" / "logs" / "distiller" / "SES-001_DI-01.log"
    sink.parent.mkdir(parents=True)
    sink.write_text("line\n", encoding="utf-8")
    monkeypatch.delenv("SOMA_OUT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert progress.log_uri("distiller", SID) == sink.resolve().as_uri()


def test_registered_out_dir_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("SOMA_OUT_DIR", raising=False)
    out_dir = tmp_path / "engine_choice"
    progress.set_output_dir(out_dir)
    assert progress.log_uri("distiller", SID).startswith(
        out_dir.resolve().as_uri())


def test_engine_registers_the_dir_its_logger_writes(tmp_path, monkeypatch):
    """The link must follow the engine's --out, not a guessed default.

    The engine hands its output directory to the logger, so the display has
    to be told the same one; otherwise a run left at the default ``--out``
    printed a link to ``<cwd>/pipeline_out`` where nothing was written.
    """
    from machinetalk.engine import Engine

    monkeypatch.delenv("SOMA_OUT_DIR", raising=False)
    monkeypatch.setattr(progress.sys, "argv", ["run_engine.py"])
    out_dir = tmp_path / "engine_out"
    Engine(out_dir=out_dir, client=None, quiet=True)
    assert progress.output_dir() == out_dir.resolve()
    assert progress.log_uri("distiller", SID).startswith(
        out_dir.resolve().as_uri())


def test_cli_out_flag_names_the_log_directory(tmp_path, monkeypatch):
    """The run's --out is where the logger writes, so the link follows it."""
    monkeypatch.delenv("SOMA_OUT_DIR", raising=False)
    out_dir = tmp_path / "cli_out"
    monkeypatch.setattr(progress.sys, "argv",
                        ["run_engine.py", "--out", str(out_dir), "--debug", "d"])
    assert progress.log_uri("distiller", SID).startswith(out_dir.resolve().as_uri())


def test_cli_out_with_equals_form_is_read(tmp_path, monkeypatch):
    monkeypatch.delenv("SOMA_OUT_DIR", raising=False)
    out_dir = tmp_path / "eq_out"
    monkeypatch.setattr(progress.sys, "argv",
                        ["run_engine.py", f"--out={out_dir}"])
    assert progress._cli_out_dir() == out_dir


def test_no_cli_out_falls_back_to_pipeline_out(monkeypatch):
    monkeypatch.delenv("SOMA_OUT_DIR", raising=False)
    monkeypatch.setattr(progress.sys, "argv", ["run_engine.py"])
    assert progress.output_dir() == Path.cwd() / "pipeline_out"


def test_cli_out_trailing_without_value_is_ignored(monkeypatch):
    monkeypatch.setattr(progress.sys, "argv", ["run_engine.py", "--out"])
    assert progress._cli_out_dir() is None


def test_finished_stage_prints_a_file_uri(capsys, out):
    timer = _timer()
    timer.start()
    timer.finish("DONE")
    printed = capsys.readouterr().out
    assert "file:///" in printed
    assert "SES-001_IB-01.log" in printed


def test_finished_stage_prints_no_relative_path(capsys, out):
    timer = _timer()
    timer.start()
    timer.finish("DONE")
    printed = capsys.readouterr().out
    for line in printed.splitlines():
        if ".log" in line:
            assert "file:///" in line, line


def test_telemetry_result_prints_a_file_uri(out):
    text = progress.telemetry_result(
        "distiller", elapsed=12.5, chars_in=100, chars_out=50,
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        model="m", provider="p", status="DONE", session=SID, out_dir=out)
    assert "file:///" in text
    assert "SES-001_DI-01.log" in text


def test_telemetry_result_without_a_session_prints_no_bare_log_label(out):
    text = progress.telemetry_result(
        "distiller", elapsed=12.5, chars_in=100, chars_out=50,
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        model="m", provider="p", status="DONE")
    assert "file:///" not in text


# ── two timings, two labels ─────────────────────────────────────────────────

def test_finished_stage_prints_a_wall_clock_start(capsys):
    timer = _timer()
    timer.start()
    timer.finish("DONE")
    printed = _plain(capsys.readouterr().out)
    assert "start" in printed
    assert WALL_CLOCK.search(printed), printed


def test_finished_stage_prints_a_measured_duration(capsys):
    timer = _timer()
    timer.start()
    time.sleep(0.05)
    timer.finish("DONE")
    printed = _plain(capsys.readouterr().out)
    assert DURATION.search(printed), printed
    assert float(DURATION.search(printed).group().split()[-1].rstrip("s")) >= 0.05


def test_start_is_not_the_duration(capsys):
    """One label must not be filled with the other's value."""
    timer = _timer()
    timer.start()
    timer.finish("DONE")
    printed = _plain(capsys.readouterr().out)
    line = next(ln for ln in printed.splitlines() if "duration" in ln)
    start_at = WALL_CLOCK.search(line)
    shown_duration = DURATION.search(line)
    assert start_at and shown_duration, line
    assert start_at.group() != shown_duration.group().split()[-1]


def test_telemetry_result_carries_both_timings(out):
    progress.stage_clock_start("distiller")
    text = progress.telemetry_result(
        "distiller", elapsed=12.5, chars_in=100, chars_out=50,
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        model="m", provider="p", status="DONE", session=SID, out_dir=out)
    plain = _plain(text)
    assert "started" in plain and WALL_CLOCK.search(plain), plain
    assert "duration    12.5s" in plain


def test_telemetry_header_labels_the_start_time():
    text = _plain(progress.telemetry_header("distiller", SID, 1, 900, "m", "p"))
    assert "start " in text and WALL_CLOCK.search(text), text


def test_stage_clock_records_a_wall_start():
    progress.stage_clock_reset()
    progress.stage_clock_start("distiller")
    assert WALL_CLOCK.fullmatch(progress.stage_wall_start("distiller"))
    progress.stage_clock_reset()


def test_stage_clock_forgets_the_wall_start_on_reset():
    progress.stage_clock_reset()
    assert progress.stage_wall_start("distiller") == "—"


# ── what must survive ───────────────────────────────────────────────────────

def test_chatset_table_still_renders():
    text = progress.chatset_table(1, 1, {
        "distiller": {"status": "DONE", "chars_in": 10, "raw_response": "x"},
    })
    assert "CHAT SET 1/1" in text
    assert "DISTILLER" in text
    assert "╔" in text and "╚" in text


def test_session_summary_still_renders():
    text = progress.session_summary(done=2, failed=0, skipped=1, total=3)
    assert "SESSION COMPLETE" in text
    assert "accounted       3/3" in text


def test_live_ticker_and_spinner_are_preserved():
    assert hasattr(progress, "LiveTicker")
    assert hasattr(progress, "spin")
    assert len(progress._SPINNER_FRAMES) > 1
    assert progress._FROZEN_GLYPH == "✓"


def test_stage_timer_still_owns_a_ticker():
    timer = _timer()
    timer.start()
    try:
        assert progress._ticker is not None
    finally:
        timer.finish("DONE")
    assert progress._ticker is None


def test_ansi_styling_is_still_emitted(capsys):
    timer = _timer()
    timer.start()
    timer.finish("DONE")
    assert "\033[" in capsys.readouterr().out


# ── the uuid cannot reach the screen ────────────────────────────────────────

def test_emit_scrubs_a_uuid_it_was_never_told_about_directly(capsys):
    progress.session_header(SID, 1)  # resolve the code, as a run does
    progress.emit(f"  Session: {SID} | 7 total chatset(s)")
    printed = capsys.readouterr().out
    assert SID not in printed
    assert "SES-001" in printed


def test_print_live_scrubs_a_uuid(capsys):
    progress.session_header(SID, 1)
    progress.print_live(f"  … {SID} resumed")
    assert SID not in capsys.readouterr().out


def test_scrub_leaves_text_without_a_session_id_alone(capsys):
    progress.session_header(SID, 1)
    progress.emit("  Total session: 6,900 character(s) across 4 chatset(s)")
    printed = capsys.readouterr().out
    assert "6,900 character(s) across 4 chatset(s)" in printed


def test_unresolvable_code_degrades_instead_of_raising(tmp_path, monkeypatch):
    bad = tmp_path / "short_codes.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SOMA_SHORT_CODES_PATH", str(bad))
    assert progress._session_code(SID) == progress._UNKNOWN_CODE
    assert "SES-" not in progress.session_header(SID, 1)


def test_unknown_stage_degrades_instead_of_raising():
    assert progress._stage_code("stage-not-in-the-map") == progress._UNKNOWN_CODE
