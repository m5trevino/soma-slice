"""Test --soma-only: first-session, first-chat-set, distiller-only, Soma-write.

Tests the new `_soma_only_run` path in run_engine.py.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest

# Ensure local soma path is importable
SOMA_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _add_soma_path():
    if str(SOMA_DIR) not in os.sys.path:
        os.sys.path.insert(0, str(SOMA_DIR))


# ---------------------------------------------------------------------------
# Mock LLM response for distiller (no real LLM call)
# ---------------------------------------------------------------------------

RecMock = mock.MagicMock()
RecMock.gate = mock.MagicMock(
    verdict="PASS",
    skipped=False,
    parsed=[{"signal_type": "IntentNode", "confidence": 0.95}],
    errors=[],
    reasons=[],
)
RecMock.model = "mock-model"
RecMock.latency_ms = 42.0

ResMock = mock.MagicMock(
    ok=True, record=RecMock, skipped=False, verdict="PASS", payload=RecMock.gate.parsed
)


@pytest.fixture(autouse=True)
def _mock_engine_client():
    """Patch EngineClient entirely to avoid real LLM calls."""
    import machinetalk.client as client_mod

    class FakeEngineClient:
        def __init__(self, *a, **kw):
            pass

        def call_stage(self, stage, **kw):
            return ResMock

    orig = client_mod.EngineClient
    client_mod.EngineClient = FakeEngineClient
    yield
    client_mod.EngineClient = orig


@pytest.fixture()
def test_session_dir(tmp_path: Path) -> Path:
    """Create a minimal test session with exactly 2 chat sets."""
    sd = tmp_path / "test-session-001"
    sd.mkdir()
    (sd / "session.json").write_text(
        json.dumps(
            {
                "session_id": "test-session-001",
                "total_sets": 2,
                "provider": "test",
                "models_used": {"mock-model": 2},
            }
        )
    )
    (sd / "000001.json").write_text(
        json.dumps(
            {
                "chat_set_id": "cs-001",
                "session_id": "test-session-001",
                "set_number": 1,
                "user_text": "Hello, create a test file.",
                "assistant_text": "Sure! I will create a test file.",
                "model": "mock-model",
                "provider": "test",
            }
        )
    )
    (sd / "000002.json").write_text(
        json.dumps(
            {
                "chat_set_id": "cs-002",
                "session_id": "test-session-001",
                "set_number": 2,
                "user_text": "Now add a function.",
                "assistant_text": "Done!",
                "model": "mock-model",
                "provider": "test",
            }
        )
    )
    return sd


@pytest.fixture()
def test_queue(tmp_path: Path, test_session_dir: Path) -> Path:
    """Create a queue JSON pointing to the test session."""
    base = tmp_path / "sessions"
    base.mkdir()
    dst = base / test_session_dir.name
    dst.mkdir()
    for f in test_session_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    qp = tmp_path / "queue.json"
    qp.write_text(json.dumps({"selected": [str(dst)], "count": 1}))
    return qp


@pytest.fixture()
def test_profile(tmp_path: Path) -> Path:
    """Copy the real profile for EngineClient."""
    here = Path(__file__).resolve().parent
    src = here / "turn-key-profiles" / "soma-current.json"
    if not src.exists():
        # The profile dirs are gitignored. Use the newest real profile.
        found = sorted((here.parent / "turn-key-profile").glob("*.json"),
                       key=lambda f: f.stat().st_mtime)
        if not found:
            pytest.skip("no TurnKey profile available (turn-key-profile/ is empty)")
        src = found[-1]
    dst = tmp_path / "profile.json"
    dst.write_text(src.read_text())
    return dst


@pytest.fixture()
def clean_out(tmp_path: Path) -> Path:
    """Empty output directory."""
    out = tmp_path / "soma_out"
    out.mkdir()
    return out


class TestSomaOnlyFirstChatSet:
    """Verify that --soma-only processes exactly ONE chat set and writes to Soma."""

    def test_soma_only_source_flag(
        self,
        test_session_dir: Path,
        test_profile: Path,
        clean_out: Path,
        test_queue: Path,
    ):
        """Using --soma-only --source should process only first chat set."""
        from run_engine import _soma_only_run

        args = argparse.Namespace(
            source=str(test_session_dir),
            queue=None,
            profile=str(test_profile),
            out=str(clean_out),
            debug=None,
        )
        rc = _soma_only_run(args)
        assert rc == 0

        soma_path = clean_out / "soma.json"
        assert soma_path.exists()

        with open(soma_path) as f:
            data = json.load(f)

        nodes = data.get("nodes", {})
        node_types = {}
        for nid, ndata in nodes.items():
            t = ndata.get("type", "unknown")
            node_types.setdefault(t, []).append(nid)

        # Must have SessionAnchor
        assert "SessionAnchor" in node_types

        # Must have at least one ChatSet (only the first one)
        assert "ChatSet" in node_types

        # Must have ReceptorLog (distiller result)
        assert "ReceptorLog" in node_types

        # Must NOT have downstream stage node types
        downstream_types = {
            "IntentNode",
            "TaskNode",
            "OutcomeNode",
            "StrategicBlueprintNode",
            "RequirementsNode",
            "ArchitectureNode",
            "FileNode",
            "InvariantQueryNode",
            "OptimizedSlot",
            "ImplementationNode",
            "VerifiedNode",
        }
        for dt in downstream_types:
            assert dt not in node_types, (
                f"Unexpected downstream node type {dt} — "
                "soma-only must not call classifier/integrator/retrograde/stages 2-4"
            )

        # At least 3 nodes: SessionAnchor + ChatSet + ReceptorLog
        assert len(nodes) >= 3

        # Edges exist
        assert len(data.get("edges", [])) >= 2

        # Only set 1 should be present
        chatset_nodes = node_types.get("ChatSet", [])
        for cnid in chatset_nodes:
            sn = nodes[cnid]["properties"]["set_number"]
            assert sn == 1

    def test_soma_only_queue_flag(
        self,
        test_session_dir: Path,
        test_profile: Path,
        clean_out: Path,
        test_queue: Path,
    ):
        """Using --soma-only --queue should also process only first chat set."""
        from run_engine import _soma_only_run

        args = argparse.Namespace(
            source=None,
            queue=str(test_queue),
            profile=str(test_profile),
            out=str(clean_out),
            debug=None,
        )
        rc = _soma_only_run(args)
        assert rc == 0

        soma_path = clean_out / "soma.json"
        assert soma_path.exists()

        with open(soma_path) as f:
            data = json.load(f)

        types_present = {ndata["type"] for ndata in data["nodes"].values()}
        assert "SessionAnchor" in types_present
        assert "ChatSet" in types_present
        assert "ReceptorLog" in types_present

    def test_no_classifier_integrator_retrograde(
        self,
        test_session_dir: Path,
        test_profile: Path,
        clean_out: Path,
    ):
        """Hard gate: no classifier/integrator/retrograde stage calls."""
        from run_engine import _soma_only_run

        args = argparse.Namespace(
            source=str(test_session_dir),
            queue=None,
            profile=str(test_profile),
            out=str(clean_out),
            debug=None,
        )
        rc = _soma_only_run(args)
        assert rc == 0

        with open(clean_out / "soma.json") as f:
            data = json.load(f)

        types = {ndata["type"] for ndata in data["nodes"].values()}
        assert "IntentNode" not in types, "classifier must not be called"
        assert "TaskNode" not in types, "integrator must not be called"
        assert "OutcomeNode" not in types, "retrograde must not be called"

    def test_exit_code_zero(self, test_session_dir: Path, test_profile: Path, clean_out: Path):
        """Process exits with code 0 after the first chat set."""
        from run_engine import _soma_only_run

        args = argparse.Namespace(
            source=str(test_session_dir),
            queue=None,
            profile=str(test_profile),
            out=str(clean_out),
            debug=None,
        )
        rc = _soma_only_run(args)
        assert rc == 0

    def test_soma_path_printed(self, test_session_dir: Path, test_profile: Path, clean_out: Path, capsys):
        """Console shows the Soma path."""
        from run_engine import _soma_only_run

        args = argparse.Namespace(
            source=str(test_session_dir),
            queue=None,
            profile=str(test_profile),
            out=str(clean_out),
            debug=None,
        )
        _soma_only_run(args)
        captured = capsys.readouterr()
        assert "Soma:" in captured.out
        assert str(clean_out / "soma.json") in captured.out

    def test_node_count_printed(self, test_session_dir: Path, test_profile: Path, clean_out: Path, capsys):
        """Console shows node count."""
        from run_engine import _soma_only_run

        args = argparse.Namespace(
            source=str(test_session_dir),
            queue=None,
            profile=str(test_profile),
            out=str(clean_out),
            debug=None,
        )
        _soma_only_run(args)
        captured = capsys.readouterr()
        assert "Nodes:" in captured.out
