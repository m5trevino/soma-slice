"""Unit tests for TB-OBS-002: short codes in the session log sink.

The per-session per-stage log is named ``<SES-NNN>_<ST-NN>.log`` and every
line inside it is prefixed ``[SES-NNN][ST-NN] ``. The master log and the
per-stage CWD log are untouched by this change.
"""
import re
from pathlib import Path

import pytest

from machinetalk import short_codes
from machinetalk.logger import UNKNOWN_CODE, StageLogger

SID = "9f2c-uuid-for-tb-obs-002"


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    """Point every test at its own short-code store."""
    path = tmp_path / "short_codes.json"
    monkeypatch.setenv("SOMA_SHORT_CODES_PATH", str(path))
    return path


@pytest.fixture
def logger(tmp_path):
    return StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True)


def _sink(logger: StageLogger, stage: str) -> Path:
    """The single file written under logs/<stage>/."""
    directory = logger.out_dir / "logs" / stage
    files = sorted(directory.glob("*.log"))
    assert len(files) == 1, f"expected one sink in {directory}, got {files}"
    return files[0]


# ── filename carries the code ────────────────────────────────────────────────

def test_sink_filename_uses_session_and_stage_codes(logger):
    logger.start("distiller", session=SID, chatset=1)
    sink = _sink(logger, "distiller")
    assert sink.name == "SES-001_DI-01.log"


def test_sink_filename_follows_the_assigned_code(logger):
    """A session that already holds a code keeps it in the filename."""
    assert short_codes.code_for("earlier-session") == "SES-001"
    logger.start("nexus", session=SID)
    assert _sink(logger, "nexus").name == "SES-002_NX-01.log"


def test_sink_filename_uses_the_stage_prefix(logger):
    logger.start("falcon", session=SID)
    logger.start("olc", session=SID)
    assert _sink(logger, "falcon").name == "SES-001_FL-01.log"
    assert _sink(logger, "olc").name == "SES-001_OLC-01.log"


def test_no_uuid_appears_in_the_sink_filename(logger):
    logger.start("distiller", session=SID)
    assert SID not in _sink(logger, "distiller").name


# ── body carries the code ────────────────────────────────────────────────────

def test_every_line_is_prefixed_with_both_codes(logger):
    logger.start("distiller", session=SID, chatset=1)
    logger.work("distiller", session=SID, chatset=1, detail="chunk 1")
    logger.done("distiller", session=SID, chatset=1)

    lines = _sink(logger, "distiller").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        assert line.startswith("[SES-001][DI-01] "), line


def test_body_prefix_survives_every_status(logger):
    for status, call in [
        ("start", logger.start),
        ("work", logger.work),
        ("done", logger.done),
        ("fail", logger.fail),
        ("block", logger.block),
        ("skip", logger.skip),
    ]:
        call("distiller", session=SID)
    logger.redo("distiller", session=SID)
    logger.note("distiller", session=SID, detail="operator note")

    body = _sink(logger, "distiller").read_text(encoding="utf-8")
    assert body.count("[SES-001][DI-01] ") == 8


def test_code_in_body_matches_code_in_filename(logger):
    logger.start("eagle", session=SID)
    sink = _sink(logger, "eagle")
    match = re.fullmatch(r"(SES-\d{3})_([A-Z]+-\d{2})\.log", sink.name)
    assert match, sink.name
    ses, stg = match.group(1), match.group(2)
    assert sink.read_text(encoding="utf-8").startswith(f"[{ses}][{stg}] ")


def test_grep_on_the_code_finds_the_body(logger):
    logger.start("distiller", session=SID)
    body = _sink(logger, "distiller").read_text(encoding="utf-8")
    assert "SES-001" in body and "DI-01" in body


# ── untouched sinks ──────────────────────────────────────────────────────────

def test_master_log_keeps_its_name_and_plain_lines(logger):
    logger.start("distiller", session=SID)
    master = logger._master_log
    assert re.fullmatch(r"\d{8}_\d{6}_pipeline\.log", master.name)
    assert master.read_text(encoding="utf-8").startswith("2")


def test_master_log_lines_are_not_prefixed(logger):
    logger.start("distiller", session=SID)
    first = logger._master_log.read_text(encoding="utf-8").splitlines()[0]
    assert not first.startswith("[SES-")
    assert "[DISTILLER" in first


def test_cwd_stage_log_keeps_its_name_and_plain_lines(logger):
    logger.start("distiller", session=SID)
    stage_log = logger.cwd / "distiller.log"
    assert stage_log.exists()
    first = stage_log.read_text(encoding="utf-8").splitlines()[0]
    assert not first.startswith("[SES-")


def test_cwd_gains_no_code_named_file(logger):
    logger.start("distiller", session=SID)
    coded = [p.name for p in logger.cwd.iterdir()
             if p.suffix == ".log" and p.name.startswith("SES-")]
    assert coded == []


# ── empty session id degrades instead of raising ─────────────────────────────

def test_empty_session_id_falls_back_to_unknown(tmp_path):
    log = StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True)
    log.info("distiller", "no session on this line")  # must not raise
    sink = _sink(log, "distiller")
    assert sink.name == f"{UNKNOWN_CODE}_DI-01.log"
    assert sink.read_text(encoding="utf-8").startswith(f"[{UNKNOWN_CODE}][DI-01] ")


def test_blank_session_id_falls_back_to_unknown(tmp_path):
    log = StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True)
    log.info("distiller", "   ")  # must not raise
    assert _sink(log, "distiller").name == f"{UNKNOWN_CODE}_DI-01.log"


def test_unknown_stage_falls_back_instead_of_raising(tmp_path):
    log = StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True)
    log.start("stage-that-is-not-in-the-map", session=SID)  # must not raise
    sink = _sink(log, "stage-that-is-not-in-the-map")
    assert sink.name == f"SES-001_{UNKNOWN_CODE}.log"
    assert sink.read_text(encoding="utf-8").startswith(
        f"[SES-001][{UNKNOWN_CODE}] ")


def test_corrupt_store_does_not_raise(temp_store, tmp_path):
    temp_store.write_text("{not json")
    log = StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True)
    log.start("distiller", session=SID)  # must not raise
    assert _sink(log, "distiller").name == f"{UNKNOWN_CODE}_DI-01.log"


def test_logging_still_reaches_master_when_code_fails(temp_store, tmp_path):
    temp_store.write_text("{not json")
    log = StageLogger(cwd=tmp_path, out_dir=tmp_path / "out", quiet=True)
    log.start("distiller", session=SID)
    assert "session=" in log._master_log.read_text(encoding="utf-8")


# ── the code is stable across a run ──────────────────────────────────────────

def test_all_stages_of_one_session_share_the_session_code(logger):
    for stage in ("distiller", "introbeaver", "planbeaver", "outrobeaver"):
        logger.start(stage, session=SID)
    names = [_sink(logger, stage).name
             for stage in ("distiller", "introbeaver", "planbeaver", "outrobeaver")]
    assert names == [
        "SES-001_DI-01.log",
        "SES-001_IB-01.log",
        "SES-001_PB-01.log",
        "SES-001_OB-01.log",
    ]


def test_repeat_calls_append_to_the_same_sink(logger):
    logger.start("distiller", session=SID)
    logger.done("distiller", session=SID)
    sink = _sink(logger, "distiller")
    assert len(sink.read_text(encoding="utf-8").splitlines()) == 2
