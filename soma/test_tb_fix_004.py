"""Unit tests for TB-FIX-004: run_tb006 empty-session exit."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def test_run_tb006_missing_session_dir():
    """Missing session dir prints 'NO RECORDS missing_dir' and exits 2."""
    missing_path = Path("/tmp/this_directory_does_not_exist_12345")

    result = subprocess.run(
        [sys.executable, "run_tb006.py", str(missing_path)],
        capture_output=True,
        text=True,
        cwd="/home/flintx/soma"
    )

    assert result.returncode == 2
    assert "NO RECORDS missing_dir" in result.stdout
    assert "IndexError" not in result.stderr


def test_run_tb006_empty_dir():
    """Empty dir prints 'NO RECORDS no_records' and exits 2."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)

        result = subprocess.run(
            [sys.executable, "run_tb006.py", str(path)],
            capture_output=True,
            text=True,
            cwd="/home/flintx/soma"
        )

        assert result.returncode == 2
        assert "NO RECORDS no_records" in result.stdout
        assert "IndexError" not in result.stderr


def test_run_tb006_valid_session_does_not_exit_early():
    """A directory with one valid record does not take the empty exit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)

        valid_record = {
            "chat_set_id": "cs-001",
            "session_id": "session-123",
            "set_number": 1,
            "user_text": "Hello world",
            "assistant_text": "Hi there"
        }

        (path / "chat1.json").write_text(json.dumps(valid_record))

        result = subprocess.run(
            [sys.executable, "run_tb006.py", str(path)],
            capture_output=True,
            text=True,
            cwd="/home/flintx/soma",
            timeout=30
        )

        # Should not exit with code 2 (empty session)
        # It may fail later due to missing dependencies, but not due to empty session
        assert result.returncode != 2 or "NO RECORDS" not in result.stdout
        assert "IndexError" not in result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])