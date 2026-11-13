"""Stage logger: writes per-stage logs to CWD + output dir with clear labels.

Every stage start/work/complete/failure is logged to:
  - stdout (CLI notification)
  - CWD/<timestamp>_pipeline.log (master chronological log)
  - CWD/<stage>.log (per-stage append log)
  - <out>/logs/<stage>/<SES-NNN>_<ST-NN>.log (per-session per-stage detail)

The session sink is named by short code -- ``SES-001_DI-01.log`` -- and each
line in it carries the same codes as a ``[SES-001][DI-01] `` prefix, so an
operator can grep a run by either code without decoding a uuid. The master
log and the per-stage CWD log keep their long-standing names and plain lines.

This ensures operators can watch live progress AND review historical
logs from any run, without hunting through subdirectories.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import short_codes

LOG_LOCK = threading.Lock()

#: Label used when a session or stage code cannot be resolved. Logging must
#: never take the run down, so an unresolvable code degrades to this.
UNKNOWN_CODE = "unknown"

#: The session sink records one file per stage, not per turn.
STAGE_TURN = 1


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _label(status: str) -> str:
    labels = {
        "START": "[▶ START]",
        "WORK":  "[⟳ WORK] ",
        "DONE":  "[✓ DONE] ",
        "FAIL":  "[✗ FAIL] ",
        "BLOCK": "[⊘ BLOCK]",
        "SKIP":  "[≋ SKIP] ",
        "REDO":  "[↺ REDO] ",
        "INFO":  "[ℹ INFO] ",
        "SUM":   "[Σ TOTAL]",
        "NOTE":  "[· NOTE] ",
    }
    return labels.get(status, f"[{status:7}]")


def _format_line(stage: str, status: str, msg: str) -> str:
    return f"{_ts()}  {_label(status)}  [{stage.upper():12}]  {msg}"


def _write_to(path: Path, line: str) -> None:
    with LOG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


def _session_code(session_id: str, codes=None) -> str:
    """Short code for a session, or ``unknown`` when it cannot be resolved.

    ``codes`` is the run's resolver (``engine.SessionCodes``). When the run
    resolved the code at selection, the logger reads that same value instead
    of asking the store again, so the sink filename and the screen cannot
    disagree. Without a resolver this still resolves its own, which keeps
    direct ``StageLogger`` users working.

    ``code_for`` mints on first use and raises on an empty id or a corrupt
    store. A log line is never worth failing a run over, so every failure
    here degrades to a literal ``unknown`` that is still greppable.
    """
    if codes is not None:
        try:
            return codes.resolve(session_id)
        except Exception:  # noqa: BLE001 - logging must not raise
            return UNKNOWN_CODE
    try:
        return short_codes.code_for(session_id)
    except Exception:  # noqa: BLE001 - logging must not raise
        return UNKNOWN_CODE


def _stage_code(stage: str) -> str:
    """Short code for a stage's single turn, or ``unknown`` for odd stages."""
    try:
        return short_codes.stage_code(stage, STAGE_TURN)
    except Exception:  # noqa: BLE001 - logging must not raise
        return UNKNOWN_CODE


class StageLogger:
    """Thread-safe logger: console + cwd master log + per-stage logs."""

    def __init__(self, *, cwd: Path | None = None, out_dir: Path | None = None,
                 quiet: bool = False, session_codes=None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.out_dir = Path(out_dir or self.cwd / "pipeline_out").resolve()
        self.quiet = quiet
        # The run's resolved session codes, when the engine has them. Held
        # here so every line and every sink filename in the run reads one
        # value rather than resolving again.
        self.session_codes = session_codes
        self._master_log = self.cwd / f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_pipeline.log"

    # -- public API --------------------------------------------------------

    def start(self, stage: str, *, session: str, chatset: int | None = None,
              model: str = "", detail: str = "") -> None:
        msg = f"session={session}"
        if chatset is not None:
            msg += f" chatset={chatset}"
        if model:
            msg += f" model={model}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "START", msg)

    def work(self, stage: str, *, session: str, chatset: int | None = None,
             detail: str = "") -> None:
        msg = f"session={session}"
        if chatset is not None:
            msg += f" chatset={chatset}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "WORK", msg)

    def done(self, stage: str, *, session: str, chatset: int | None = None,
             detail: str = "") -> None:
        msg = f"session={session}"
        if chatset is not None:
            msg += f" chatset={chatset}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "DONE", msg)

    def fail(self, stage: str, *, session: str, chatset: int | None = None,
             detail: str = "") -> None:
        msg = f"session={session}"
        if chatset is not None:
            msg += f" chatset={chatset}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "FAIL", msg)

    def block(self, stage: str, *, session: str, chatset: int | None = None,
              detail: str = "") -> None:
        msg = f"session={session}"
        if chatset is not None:
            msg += f" chatset={chatset}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "BLOCK", msg)

    def skip(self, stage: str, *, session: str, chatset: int | None = None,
             detail: str = "") -> None:
        msg = f"session={session}"
        if chatset is not None:
            msg += f" chatset={chatset}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "SKIP", msg)

    def redo(self, stage: str, *, session: str, detail: str = "") -> None:
        msg = f"session={session}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "REDO", msg)

    def info(self, stage: str, msg: str) -> None:
        self._emit(stage, "INFO", msg)

    def banner(self, text: str) -> None:
        line = f"\n{'=' * 60}\n  {text}\n{'=' * 60}"
        if not self.quiet:
            print(line)
        _write_to(self._master_log, line)

    def summary(self, *, session: str, done: int, failed: int,
                skipped: int, total: int) -> None:
        """Final per-session tally, written to console and the master log.

        Stage 1 is otherwise only visible as individual START/DONE lines, so
        the run total is recorded here rather than left implicit.
        """
        self._emit(session, "SUM",
                   f"session={session} | done={done} failed={failed} "
                   f"skipped={skipped} total={total}")

    def note(self, stage: str, *, session: str, detail: str = "") -> None:
        """Free-form operator note (tables, paths, non-stage output)."""
        msg = f"session={session}"
        if detail:
            msg += f" | {detail}"
        self._emit(stage, "NOTE", msg)

    # -- internal ----------------------------------------------------------

    def _emit(self, stage: str, status: str, msg: str, session_id: str = "") -> None:
        line = _format_line(stage, status, msg)
        # Suppress console for Stage 1 — clean tables only
        stage1_stages = {"distiller", "introbeaver", "planbeaver", "outrobeaver"}
        should_print = not self.quiet and stage not in stage1_stages
        if should_print:
            print(line)
        _write_to(self._master_log, line)
        # Per-stage log in CWD
        stage_log = self.cwd / f"{stage.lower()}.log"
        _write_to(stage_log, line)
        # Per-session per-stage log in out_dir, named by short code
        sid = session_id or (msg.split("session=")[1].split(" ")[0] if "session=" in msg else "")
        ses = _session_code(sid, self.session_codes)
        stg = _stage_code(stage)
        session_log = self.out_dir / "logs" / stage.lower() / f"{ses}_{stg}.log"
        _write_to(session_log, f"[{ses}][{stg}] {line}")
