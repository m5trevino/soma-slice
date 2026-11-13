"""Unit tests for TB-OBS-001: stable session and stage short codes."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from machinetalk import short_codes
from machinetalk.short_codes import ShortCodeError, code_for, stage_code


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    """Point every test at its own short-code store."""
    path = tmp_path / "short_codes.json"
    monkeypatch.setenv("SOMA_SHORT_CODES_PATH", str(path))
    return path


# ── session codes ────────────────────────────────────────────────────────────

def test_first_session_gets_ses_001(temp_store):
    assert code_for("uuid-a") == "SES-001"
    data = json.loads(temp_store.read_text())
    assert data == {"next": 2, "map": {"uuid-a": "SES-001"}}


def test_codes_are_sequential_in_arrival_order(temp_store):
    assert code_for("uuid-a") == "SES-001"
    assert code_for("uuid-b") == "SES-002"
    assert code_for("uuid-c") == "SES-003"


def test_same_session_always_returns_same_code(temp_store):
    first = code_for("uuid-a")
    code_for("uuid-b")
    assert code_for("uuid-a") == first
    assert json.loads(temp_store.read_text())["next"] == 3


def test_codes_are_not_derived_from_the_uuid(temp_store):
    """Two different uuids with the same derived hash shape still differ.

    The guarantee is order-based, so a uuid that sorts first can still be
    issued a later code; nothing about the uuid's bytes determines the code.
    """
    late = code_for("zzz-last-alphabetically")
    early = code_for("aaa-first-alphabetically")
    assert (late, early) == ("SES-001", "SES-002")


def test_assignment_survives_reload(temp_store):
    code_for("uuid-a")
    # Fresh process-equivalent: drop the module cache of the store.
    short_codes._load(temp_store)
    assert code_for("uuid-a") == "SES-001"
    assert code_for("uuid-b") == "SES-002"


def test_existing_uuid_is_never_reassigned_when_next_fell_behind(temp_store):
    """A store whose counter lagged cannot hand out a code already in the map."""
    temp_store.write_text(json.dumps({
        "next": 1,
        "map": {"uuid-a": "SES-001", "uuid-b": "SES-002"},
    }))
    assert code_for("uuid-a") == "SES-001"
    assert code_for("uuid-c") == "SES-003"
    data = json.loads(temp_store.read_text())
    assert data["map"]["uuid-a"] == "SES-001"
    assert data["map"]["uuid-c"] == "SES-003"
    assert data["next"] == 4


def test_missing_store_is_created(temp_store):
    assert not temp_store.exists()
    code_for("uuid-a")
    assert temp_store.exists()


def test_empty_store_file_is_a_fresh_store(temp_store):
    temp_store.write_text("")
    assert code_for("uuid-a") == "SES-001"


def test_corrupt_store_raises_instead_of_reissuing(temp_store):
    temp_store.write_text("{not json")
    with pytest.raises(ShortCodeError):
        code_for("uuid-a")
    # The broken file is left for an operator, not overwritten.
    assert temp_store.read_text() == "{not json"


def test_non_string_map_entry_raises(temp_store):
    temp_store.write_text(json.dumps({"next": 2, "map": {"uuid-a": 1}}))
    with pytest.raises(ShortCodeError):
        code_for("uuid-a")


def test_empty_session_id_rejected(temp_store):
    with pytest.raises(ShortCodeError):
        code_for("")
    assert not temp_store.exists()


def test_atomic_write_leaves_no_tmp_files(temp_store, tmp_path):
    for i in range(5):
        code_for(f"uuid-{i}")
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == []


def test_file_fsync_is_used(temp_store, monkeypatch):
    """The writer fsyncs the tmp file before replacing the store."""
    calls = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    code_for("uuid-a")
    assert calls, "expected at least one fsync before os.replace"


# ── concurrency ──────────────────────────────────────────────────────────────

def test_threads_mint_unique_codes(temp_store):
    results: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        code = code_for(f"uuid-{index}")
        with lock:
            results.append(code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 24
    assert len(set(results)) == 24
    data = json.loads(temp_store.read_text())
    assert len(set(data["map"].values())) == 24
    assert data["next"] == 25


def test_processes_mint_unique_codes(temp_store):
    """Two processes sharing a store must not mint the same number."""
    project_soma = Path(__file__).resolve().parent
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from machinetalk.short_codes import code_for\n"
        "import sys as _s\n"
        "print(code_for(_s.argv[1]))\n"
    ) % str(project_soma)

    env = dict(os.environ, SOMA_SHORT_CODES_PATH=str(temp_store))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, f"proc-uuid-{i}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(project_soma))
        for i in range(8)
    ]
    codes = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        codes.append(out.strip())

    assert len(set(codes)) == 8
    data = json.loads(temp_store.read_text())
    assert sorted(codes) == sorted(data["map"].values())
    assert set(codes) == {f"SES-{n:03d}" for n in range(1, 9)}


# ── stage codes ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stage,expected", [
    ("distiller", "DI-01"),
    ("introbeaver", "IB-01"),
    ("planbeaver", "PB-01"),
    ("outrobeaver", "OB-01"),
    ("nexus", "NX-01"),
    ("spark", "SP-01"),
    ("falcon", "FL-01"),
    ("eagle", "EG-01"),
    ("drafter", "DR-01"),
    ("scout", "SC-01"),
    ("oc", "OC-01"),
    ("olc", "OLC-01"),
])
def test_stage_prefixes(stage, expected):
    assert stage_code(stage, 1) == expected


def test_stage_turn_is_zero_padded():
    assert stage_code("nexus", 7) == "NX-07"
    assert stage_code("nexus", 12) == "NX-12"
    assert stage_code("nexus", 100) == "NX-100"


def test_stage_code_is_pure():
    """Stage codes carry no state, so repeated calls agree."""
    assert stage_code("distiller", 3) == stage_code("distiller", 3)


def test_stage_name_is_case_insensitive():
    assert stage_code("Nexus", 2) == "NX-02"


def test_unknown_stage_rejected():
    with pytest.raises(ShortCodeError):
        stage_code("nope", 1)


@pytest.mark.parametrize("turn", [0, -1, "x", 1.5, True, None])
def test_invalid_turn_rejected(turn):
    with pytest.raises(ShortCodeError):
        stage_code("distiller", turn)


# ── store location ───────────────────────────────────────────────────────────

def test_default_store_is_data_short_codes(monkeypatch):
    monkeypatch.delenv("SOMA_SHORT_CODES_PATH", raising=False)
    path = short_codes.store_path()
    assert path.name == "short_codes.json"
    assert path.parent.name == "data"
    assert path.parent.parent.name == "soma-10-14"
