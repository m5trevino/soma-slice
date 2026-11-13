"""Clean table-based progress output for Stage 1 ingestion.

While a stage runs → one live line, rewritten in place with a spinning
character and elapsed seconds, frozen with a single newline when it ends.
After each chatset finishes → print header → divider → table → divider.
After all chatsets → summary.

Every session is shown by its short code (``SES-120``) and every stage by its
own (``DI-01``), so a line reads ``[SES-120][DI-01] DISTILLER``. A finished
stage also prints the wall-clock time it started, the duration it measured,
and an absolute ``file://`` link to its log, because a relative path cannot be
clicked.

No interleaved logs, no live dashboard.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from . import short_codes


# ── Primitives ──────────────────────────────────────────────────────────

# ANSI styling for the dressed-up stage output. Always emitted so the
# launcher's terminal (which re-prints captured lines) still renders it.
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_CYAN = "\033[36m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"

_DIVIDER = ("■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□"
            "■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□"
            "■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■")

_STAGE_LABELS = {
    "distiller": "DISTILLER",
    "introbeaver": "INTRO-BEAVER",
    "planbeaver": "PLAN-BEAVER",
    "outrobeaver": "OUTRO-BEAVER",
}

# Step aliases: what the user sees
_STEP_LABELS = {
    "distiller":    ("ALPHA", "Receptor",      "DISTILLER"),
    "introbeaver":  ("BRAVO", "Classifier",    "INTRO-BEAVER"),
    "planbeaver":   ("CHARLIE", "Integrator",  "PLAN-BEAVER"),
    "outrobeaver":  ("EDWARD", "Retrograde",   "OUTRO-BEAVER"),
}

_STAGE_ORDER = ["distiller", "introbeaver", "planbeaver", "outrobeaver"]

_DISTILLER_STEPS = [
    "hashing input",
    "extracting heredocs, metadata, constraints",
    "stripping noise: greetings, apologies, fillers, fences",
    "checking for duplicate hashes",
    "last user statement = current goal",
    "classifying signals into graph data",
    "matching intent across turns",
    "logging noise purges + stats",
    "computing overall signal quality",
    "verdict: pass | skip | quarantine",
    "writing canonical payload",
]

_COL_HEADERS = [
    "STAGE",
    "CHARS IN",
    "CHARS OUT",
    "STATUS",
    "SIGNALS",
    "TASKS",
    "OUTCOME",
    "MODEL",
]

# Fixed column widths matching the user's spec exactly
_COL_WIDTHS = [15, 12, 12, 22, 10, 10, 10, 34]


# ── Codes and log links ─────────────────────────────────────────────────
#
# The terminal shows the codes a human can quote: ``SES-120`` for a session
# and ``DI-01`` for a stage. The uuid is never printed, and a code that cannot
# be resolved degrades to a literal ``unknown`` rather than raising — display
# code must not be able to take a run down.

_UNKNOWN_CODE = "unknown"

#: Stage turn shown on screen. The pipeline runs one turn per stage today;
#: the code carries the number so a later multi-turn run needs no new format.
STAGE_TURN = 1


def _remember(session_id: str, code: str) -> str:
    """Record the id -> code pairing so printed text can be scrubbed."""
    if session_id:
        with _SEEN_LOCK:
            _SEEN[session_id] = code
    return code


def _session_code(session_id: str, code: str | None = None) -> str:
    """Short session code, or ``unknown`` when it cannot be resolved.

    ``code`` is the run's already-resolved value. When the caller passes it,
    no store lookup happens here: the display reads the same code the run
    resolved at selection instead of asking a store that may have changed
    since. Without it this module still resolves its own, which keeps direct
    callers (tests, one-off renders) working.

    Resolving a code also records it, so text built elsewhere with the raw id
    can be scrubbed at print time.
    """
    sid = str(session_id or "").strip()
    if code:
        return _remember(sid, code)
    try:
        resolved = short_codes.code_for(sid)
    except Exception:  # noqa: BLE001 - display must not raise
        return _UNKNOWN_CODE
    return _remember(sid, resolved)


def _stage_code(stage: str, turn: int = STAGE_TURN) -> str:
    """Short stage code, or ``unknown`` for a stage outside the map."""
    try:
        return short_codes.stage_code(stage, turn)
    except Exception:  # noqa: BLE001 - display must not raise
        return _UNKNOWN_CODE


def code_tag(stage: str, session_id: str, turn: int = STAGE_TURN,
             session_code: str | None = None) -> str:
    """``[SES-120][DI-01]`` for one stage execution."""
    return (f"[{_session_code(session_id, session_code)}]"
            f"[{_stage_code(stage, turn)}]")


#: Session ids seen on screen, mapped to their codes. The engine formats a
#: few of its own lines with the raw id; keeping the ids here lets every
#: printed string be scrubbed at the boundary, so the uuid cannot leak no
#: matter which caller built the text.
_SEEN: dict[str, str] = {}
_SEEN_LOCK = threading.Lock()


def _scrub(text: str) -> str:
    """Replace every known session id in ``text`` with its short code."""
    if not text or not _SEEN:
        return text
    with _SEEN_LOCK:
        pairs = sorted(_SEEN.items(), key=lambda kv: len(kv[0]), reverse=True)
    for sid, code in pairs:
        if sid in text:
            text = text.replace(sid, code)
    return text


_OUT_DIR: Path | None = None


def set_output_dir(path) -> None:
    """Tell the display where this run writes its logs.

    The engine owns its output directory and the logger writes the session
    sinks under it, so the engine sets it here once and every link printed
    afterwards points at the file the logger actually wrote.
    """
    global _OUT_DIR
    _OUT_DIR = Path(path) if path is not None else None


def _cli_out_dir() -> Path | None:
    """``--out`` from this process's own command line, if it was given.

    The engine's output directory reaches the logger but not this module, and
    the launcher and ``run_engine.py`` both pass it as ``--out``. Reading the
    argv already in hand avoids a filesystem search and cannot pick up a
    directory from an unrelated run.
    """
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--out" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--out="):
            return Path(arg.split("=", 1)[1])
    return None


def output_dir() -> Path:
    """Directory the stage logs live under.

    Resolution order: what the engine registered, then ``SOMA_OUT_DIR``, then
    this command's ``--out``, then the logger's own default of
    ``<cwd>/pipeline_out``. Whichever wins, it is an absolute path, which is
    what makes the link clickable.
    """
    if _OUT_DIR is not None:
        return _OUT_DIR
    override = os.environ.get("SOMA_OUT_DIR")
    if override:
        return Path(override)
    cli = _cli_out_dir()
    return cli if cli is not None else Path.cwd() / "pipeline_out"


def _log_name(stage: str, session_id: str, turn: int = STAGE_TURN) -> str:
    return f"{_session_code(session_id)}_{_stage_code(stage, turn)}.log"


def _log_candidates(stage: str, session_id: str, out_dir=None,
                    turn: int = STAGE_TURN) -> list[Path]:
    """Absolute paths the sink could occupy, most authoritative first.

    The engine owns its output directory and the logger writes there, while
    this module is only told about it through ``set_output_dir`` or
    ``SOMA_OUT_DIR``. Checking a short, fixed list lets a link point at the
    file that exists instead of at a guess, and the list is never widened by
    searching the filesystem.
    """
    name = _log_name(stage, session_id, turn)
    rel = Path("logs") / stage.lower() / name
    roots: list[Path] = []
    if out_dir is not None:
        roots.append(Path(out_dir))
    if _OUT_DIR is not None:
        roots.append(_OUT_DIR)
    if os.environ.get("SOMA_OUT_DIR"):
        roots.append(Path(os.environ["SOMA_OUT_DIR"]))
    cli = _cli_out_dir()
    if cli is not None:
        roots.append(cli)
    roots.append(Path.cwd() / "pipeline_out")
    roots.append(Path.cwd())
    return [(root / rel).resolve() for root in roots]


def _discover_log(stage: str, session_id: str,
                  turn: int = STAGE_TURN) -> Path | None:
    """Find the sink the logger wrote, two directory levels under CWD.

    The engine chooses its output directory and hands it to the logger, not
    to this module; until it does, the sink still exists on disk under that
    directory. Two levels is enough for ``<out>/logs/<stage>/`` and keeps the
    lookup bounded — this never walks a tree.
    """
    name = _log_name(stage, session_id, turn)
    tail = Path("logs") / stage.lower() / name
    for depth in ("*", "*/*"):
        try:
            hits = sorted(Path.cwd().glob(str(Path(depth) / tail)))
        except OSError:
            return None
        for hit in hits:
            if hit.is_file():
                return hit.resolve()
    return None


def log_path(stage: str, session_id: str, out_dir=None,
             turn: int = STAGE_TURN) -> Path:
    """The per-session per-stage log the logger writes for this execution.

    The returned path is always absolute. A candidate that exists wins, then
    a sink found under ``<cwd>/<something>/logs/<stage>/``, then the first
    candidate — so the link points at a real file whenever one is there.
    """
    candidates = _log_candidates(stage, session_id, out_dir, turn)
    for path in candidates:
        if path.exists():
            return path
    found = _discover_log(stage, session_id, turn)
    return found if found is not None else candidates[0]


def log_uri(stage: str, session_id: str, out_dir=None,
            turn: int = STAGE_TURN) -> str:
    """Clickable absolute ``file://`` URI for this stage's log.

    Built with ``Path.resolve().as_uri()`` rather than string concatenation,
    so the path is absolute and correctly escaped. A relative path is not
    clickable in any terminal that linkifies, which is the whole point.
    """
    return log_path(stage, session_id, out_dir, turn).resolve().as_uri()


# ── Helpers ─────────────────────────────────────────────────────────────

def _clean_model(model: str, gateway_map: dict | None = None) -> str:
    """Format model display name using gateway map when available.

    With gateway map: look up gateway for model_id, format as ``model_name-gateway``.
    Without gateway map (or model missing): fall back to uppercased name.

    Examples (with map):
        ``inclusionai/ling-3.0-flash`` (novita) → ``ling-3.0-flash-novita``
        ``nvidia/nemotron-3.5:free`` (openrouter) → ``nemotron-3.5-openrouter``
        ``gpt-4o`` (openai) → ``gpt-4o-openai``

    Examples (without map / fallback):
        ``inclusionai/ling-3.0-flash`` → ``LING-3.0-FLASH (INCLUSIONAI)``
    """
    if not model:
        return ""
    if gateway_map and model in gateway_map:
        gw = gateway_map[model]
        # Strip provider prefix (everything before /)
        name = model.split("/", 1)[1] if "/" in model else model
        # Strip tag at :
        name = name.split(":", 1)[0]
        return f"{name}-{gw}"
    # Fallback — uppercase model name with provider/tag
    parts = model.split("/", 1)
    provider = parts[0].upper() if len(parts) > 1 else None
    model_part = parts[1] if len(parts) > 1 else parts[0]
    mp = model_part.split(":", 1)
    name = mp[0].strip()
    tag = mp[1].strip() if len(mp) > 1 else None
    suffix = tag.upper() if tag else (provider if provider else None)
    if suffix:
        return f"{name.upper()} ({suffix})"
    return name.upper()


def _cell(text: str, width: int, align: str = "left") -> str:
    """Format one cell within its column."""
    text = str(text) if text is not None else ""
    if align == "right":
        return f"{text:>{width}}"
    return f"{text:<{width}}"


def _fmt_number(n) -> str:
    """Format an integer with commas for display."""
    if n is None or n == "":
        return ""
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def _top_line(widths: list[int]) -> str:
    """Top border: ╔══╦══╦═╗"""
    parts = []
    for i, w in enumerate(widths):
        sep = "╦" if i < len(widths) - 1 else "╗"
        parts.append("=" * w + sep)
    return "╔" + "".join(parts)


def _bottom_line(widths: list[int]) -> str:
    """Bottom border: ╚══╩══╩═╝"""
    parts = []
    for i, w in enumerate(widths):
        sep = "╩" if i < len(widths) - 1 else "╝"
        parts.append("=" * w + sep)
    return "╚" + "".join(parts)


def _hsep(widths: list[int]) -> str:
    """Header/body separator: ╠══╬══╬═╣"""
    parts = []
    for i, w in enumerate(widths):
        sep = "╬" if i < len(widths) - 1 else "╣"
        parts.append("=" * w + sep)
    return "╠" + "".join(parts)


def _row(cells: list[str], widths: list[int]) -> str:
    """One data row."""
    parts = []
    for i, (c, w) in enumerate(zip(cells, widths)):
        parts.append(_cell(c, w) + "║")
    return "║" + "".join(parts)


# ── Session Header ──────────────────────────────────────────────────────

def session_header(session_id: str, total_sets: int,
                   session_code: str | None = None) -> str:
    """Session header block.

    The line inside the frame is the session's short code. The uuid identifies
    the session to the store; it means nothing to the operator reading the
    screen and is far too long to scan.

    ``session_code`` is the value the run resolved at selection. Passing it
    means the header cannot disagree with the sink filenames or the debug
    events, which are given the same value.
    """
    code = _session_code(session_id, session_code)
    h_top = ("┏◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚"
             "◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚◚┓")
    h_bot = ("┗◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛"
             "◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛◛┛")
    lines = [
        h_top,
        code,
        h_bot,
        "",
        "                    ▛▝▝▝▝▝▝▝▝▝▝▝▝▝▝▝▝▝▝▝▝ ▜",
        f"                         {total_sets} CHAT SETS",
        "                    ▙ ▖▖▖▖▖▖▖▖▖▖▖▖▖▖▖▖▖▖▖▖▟",
        "",
    ]
    return "\n".join(lines)


# ── Chatset Table ───────────────────────────────────────────────────────

def _chars_out(sd: dict) -> str:
    """Characters the stage produced.

    The store keeps the verbatim response for the distiller, so its output
    size is measurable. Beavers only keep a token count, and tokens are not
    characters, so the cell stays blank rather than showing a number that
    means something else.
    """
    raw = sd.get("raw_response")
    if isinstance(raw, str) and raw:
        return _fmt_number(len(raw))
    out = sd.get("chars_out")
    if isinstance(out, (int, float)) and out:
        return _fmt_number(int(out))
    return ""


def _status_cell(sd: dict | None) -> str:
    """One unambiguous status word, including why a stage did not run."""
    if sd is None:
        return "NOT RUN"
    status = (sd.get("status") or "PENDING").upper()
    if status == "SKIPPED":
        reason = (sd.get("skip_reason") or "").strip()
        return f"SKIP {reason}" if reason else "SKIP"
    if status in ("FAILED", "BLOCKED"):
        errs = sd.get("errors")
        if isinstance(errs, str) and errs.strip():
            return f"{status} {errs.strip()[:14]}"
        return status
    if status == "DONE":
        verdict = str(sd.get("verdict") or "").strip()
        if verdict.lower() in ("true", "1", "yes"):
            # The stage ran and decided there was nothing to keep.
            return "DONE (NO SIGNAL)"
        return "DONE"
    return status


def _stage_counts(sd: dict | None) -> tuple[str, str, str]:
    """Signal / task / outcome counts from the stored payload, if present."""
    if not sd:
        return "", "", ""
    parsed = sd.get("payload")
    if not isinstance(parsed, dict):
        raw = sd.get("raw_response")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                import json as _json
                parsed = _json.loads(raw)
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        return "", "", ""

    body = unwrap_payload(parsed)

    def _n(*keys: str) -> str:
        total = 0
        found = False
        for key in keys:
            val = body.get(key)
            if isinstance(val, list):
                total += len(val)
                found = True
        return str(total) if found else ""

    return (_n("directives", "preferences", "questions", "confirmations"),
            _n("tasks", "plan_items", "files"),
            _n("outcomes", "deliverables", "lessons"))


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def chatset_table(set_num, total: int, stages: dict,
                  gateway_map: dict | None = None,
                  position: int | None = None) -> str:
    """Render the full table for one completed chatset.

    Every cell is either a measured character count or an explicit status.
    Tokens are deliberately absent: they vary by provider and do not describe
    how much text moved through the stage.

    ``stages``: dict keyed by stage name, values are checkpoint dicts
    (status, verdict, model, chars_in, raw_response, errors, ...).
    ``gateway_map``: optional model_id -> gateway lookup for _clean_model.
    """
    rows: list[list[str]] = []

    for sname in _STAGE_ORDER:
        sd = stages.get(sname)
        label = _STAGE_LABELS.get(sname, sname.upper())

        chars_in = ""
        if sd:
            ci = sd.get("chars_in")
            if isinstance(ci, (int, float)) and ci > 0:
                chars_in = _fmt_number(int(ci))

        signals, tasks, outcome = _stage_counts(sd)
        rows.append([
            label,
            chars_in,
            _chars_out(sd) if sd else "",
            _status_cell(sd),
            signals,
            tasks,
            outcome,
            _clean_model((sd or {}).get("model") or "", gateway_map),
        ])

    # Build lines using fixed column widths
    lines: list[str] = []

    # Three different numbers were in play: the set number inside the
    # session (49306), how many chatsets that session has (44), and how many
    # of them this run is doing (34). Reporting one as the other is what made
    # the progress line contradict the header.
    marker_text = f"CHAT SET {set_num}/{total}"
    if position is not None and position != set_num:
        marker_text += f"  ({position} of {total} this run)"
    marker_side = "█" * 7
    lines.append(f"\n{marker_side}   {marker_text}   {marker_side}")
    lines.append("")

    lines.append(_top_line(_COL_WIDTHS))
    lines.append(_row(_COL_HEADERS, _COL_WIDTHS))
    lines.append(_hsep(_COL_WIDTHS))
    for r in rows:
        lines.append(_row(r, _COL_WIDTHS))
    lines.append(_row([""] * len(_COL_HEADERS), _COL_WIDTHS))
    lines.append(_bottom_line(_COL_WIDTHS))
    lines.append("")

    return "\n".join(lines)


# ── Summary ─────────────────────────────────────────────────────────────

def session_summary(done: int, failed: int, skipped: int, total: int) -> str:
    """Final per-session tally.

    ``done`` counts chatsets the distiller processed, ``skipped`` counts
    chatsets it judged signal-free, ``failed`` counts chatsets that errored.
    The three add up to ``total``.
    """
    accounted = done + failed + skipped
    lines = [
        "",
        "  ┌─ SESSION COMPLETE " + "─" * 34,
        f"  │  chatsets        {total}",
        f"  │  distilled       {done}",
        f"  │  skipped         {skipped}   (distiller found no signal)",
        f"  │  failed          {failed}",
        f"  │  accounted       {accounted}/{total}"
        + ("" if accounted == total else "   ← MISMATCH"),
        "  └" + "─" * 52,
        "",
    ]
    return "\n".join(lines)


# ── Live stage line ─────────────────────────────────────────────────────
#
# Exactly one running stage owns the terminal line. The owner rewrites that
# line in place with a carriage return (spinner glyph + elapsed seconds) and
# freezes it with a single newline when the stage returns. Stage 1 runs on
# several worker threads, so extra spinners stay silent rather than fighting
# for the line, and ordinary output is queued until the line is free.

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_TICK = 0.1
_FROZEN_GLYPH = "✓"

_io_lock = threading.RLock()
_active_line: "_LiveLine | None" = None
_deferred: list[tuple[tuple, dict]] = []


class _LiveLine:
    """One spinner line, owned by at most one stage at a time."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._owns = False
        self._width = 0

    def open(self) -> None:
        global _active_line
        with _io_lock:
            self._owns = _active_line is None
            if self._owns:
                _active_line = self
        if not self._owns:
            return
        self._thread = threading.Thread(
            target=self._spin, daemon=True, name="stage-spinner")
        self._thread.start()

    def _spin(self) -> None:
        frames = _SPINNER_FRAMES
        idx = 0
        while True:
            self._draw(frames[idx % len(frames)])
            idx += 1
            if self._stop.wait(_SPINNER_TICK):
                return

    def _elapsed(self) -> int:
        return int(time.monotonic() - self._started)

    def _draw(self, glyph: str) -> None:
        body = f"  {glyph} {self.label}... {self._elapsed()}s"
        padding = " " * max(0, self._width - len(body))
        self._width = len(body)
        with _io_lock:
            sys.stdout.write("\r" + body + padding)
            sys.stdout.flush()

    def close(self) -> None:
        global _active_line
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if not self._owns:
            return
        # Freeze the final elapsed value on the same line, then end the line.
        self._draw(_FROZEN_GLYPH)
        with _io_lock:
            sys.stdout.write("\n")
            sys.stdout.flush()
            if _active_line is self:
                _active_line = None
        _flush_deferred()


class LiveTicker:
    """One terminal line, redrawn in place while a call is in flight.

    Owns exactly one line. Other output must clear() first, or its
    carriage-return redraws will land on top of the printed text.

    Disabled when stdout is not a terminal: a pipe has no carriage return,
    so redrawing there would emit one line per tick.
    """

    def __init__(self, text_fn, interval: float = 0.5) -> None:
        self._text_fn = text_fn
        self._interval = interval
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._width = 0
        self._held = False
        self._enabled = sys.stdout.isatty()

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stage-ticker")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._draw()

    def _draw(self) -> None:
        try:
            text = self._text_fn()
        except Exception:
            return
        with _io_lock:
            if self._held:
                return
            pad = " " * max(0, self._width - len(text))
            self._width = max(self._width, len(text))
            sys.stdout.write("\r" + text + pad)
            sys.stdout.flush()

    def clear(self) -> bool:
        """Erase the line so a normal print can use it."""
        if not self._enabled:
            return False
        with _io_lock:
            if self._held:
                return False
            self._held = True
            sys.stdout.write("\r" + " " * self._width + "\r")
            sys.stdout.flush()
            return True

    def redraw(self) -> None:
        with _io_lock:
            self._held = False
        self._draw()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._enabled and self._width:
            with _io_lock:
                sys.stdout.write("\r" + " " * self._width + "\r")
                sys.stdout.flush()
            self._width = 0


# The ticker currently owning the bottom line, if any.
_ticker: "LiveTicker | None" = None


def _scrub_args(args: tuple) -> tuple:
    """Apply the session-id scrub to string arguments of a print call."""
    return tuple(_scrub(a) if isinstance(a, str) else a for a in args)


def print_live(*args, **kwargs) -> None:
    """Print without destroying the live line underneath."""
    held = _ticker.clear() if _ticker is not None else False
    with _io_lock:
        print(*_scrub_args(args), **kwargs)
        sys.stdout.flush()
    if held and _ticker is not None:
        _ticker.redraw()


def _flush_deferred() -> None:
    """Print output queued while a spinner owned the line."""
    while True:
        with _io_lock:
            if _active_line is not None or not _deferred:
                return
            args, kwargs = _deferred.pop(0)
            print(*_scrub_args(args), **kwargs)


def emit(*args, **kwargs) -> None:
    """Print normally, preserving whatever live line is on screen."""
    with _io_lock:
        if _active_line is not None:
            _deferred.append((args, kwargs))
            return
    print_live(*args, **kwargs)


def spin(label: str, fn, *args, **kwargs):
    """Run ``fn`` behind a one-line spinner showing elapsed seconds."""
    line = _LiveLine(label)
    line.open()
    try:
        return fn(*args, **kwargs)
    finally:
        line.close()


# ── Stage timing ────────────────────────────────────────────────────────
#
# The distiller keeps its ~30 s step display: it is the one call long enough
# that a blank wait looks like a hang.
#
# Every later stage is one line. Before, a stage printed three unrelated
# pieces under two different formats:
#
#   ✓ introbeaver trevino.war.room CS49306 turnkey... 13s      <- spinner, "✓" even on failure
#   -[INTROBEAVER]- [FAILED] missing:introbeaver_output ...     <- result
#   chars in   3,296 / plan   0 file(s) ...                      <- detail block
#
# The spinner froze on ✓ regardless of outcome, so a failed stage looked
# successful, and the model was printed as the literal word "turnkey"
# because EngineClient.model_for() returns a placeholder. Anything long
# enough to be worth watching gets its own live line with a real model.

_STAGE_START: dict[str, float] = {}
_STAGE_WALL_START: dict[str, str] = {}
_STAGE_TIMES: dict[str, list[tuple[float, str, str]]] = {}
_STAGE_ORDER = ["distiller", "introbeaver", "planbeaver", "outrobeaver"]

_DISTILLER_STEPS = [
    "hashing input",
    "extracting heredocs, metadata, constraints",
    "stripping noise: greetings, apologies, fillers, fences",
    "checking for duplicate hashes",
    "last user statement = current goal",
    "classifying signals into graph data",
    "matching intent across turns",
    "logging noise purges + stats",
    "computing overall signal quality",
    "verdict: pass | skip | quarantine",
    "writing canonical payload",
]


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def stage_clock_start(stage: str) -> None:
    """Record when a stage's model call began.

    Two clocks are kept for the same instant. The monotonic one measures the
    duration; the wall clock one is what the operator prints, because
    ``31.4s`` says nothing about when the stage ran.
    """
    _STAGE_START[stage] = time.monotonic()
    _STAGE_WALL_START[stage] = time.strftime("%Y-%m-%d %H:%M:%S")


def stage_wall_start(stage: str) -> str:
    """Wall-clock time this stage's call began, or ``—`` if it never did."""
    return _STAGE_WALL_START.get(stage, "—")


def stage_clock_done(stage: str, status: str) -> float:
    """Record when a stage finished; return elapsed seconds."""
    started = _STAGE_START.pop(stage, None)
    elapsed = time.monotonic() - started if started is not None else 0.0
    _STAGE_TIMES.setdefault(stage, []).append((elapsed, status, _timestamp()))
    return elapsed


def stage_clock_reset() -> None:
    _STAGE_START.clear()
    _STAGE_WALL_START.clear()
    _STAGE_TIMES.clear()


def _stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage.upper())


def _verdict_glyph(status: str) -> tuple[str, str]:
    """Glyph and colour for a finished stage. Never ✓ for a failure."""
    head = status.upper()
    if head.startswith(("DONE", "PASS")):
        return "✓", _C_GREEN
    if head.startswith(("SKIP", "BLOCK")):
        return "⊘", _C_YELLOW
    return "✗", _C_RED


# ── Per-stage telemetry ────────────────────────────────────────────────
#
# One block per stage execution, printed immediately before the call and
# completed after it returns. Every value is measured: the timestamps come
# from the clock, the character counts from the strings, the tokens from the
# provider's usage block. Nothing here is estimated.
#
# The block exists so a run can be read after the fact without guessing what
# happened: which model served the call, whether it was the one announced,
# how much text went in and came out, and what the gate decided.

_TELEMETRY_WIDTH = 74


def _rule(char: str = "─") -> str:
    return "  " + _C_DIM + char * _TELEMETRY_WIDTH + _C_RESET


def _field(label: str, value: str, colour: str = "") -> str:
    colour = colour or _C_RESET
    return f"  {_C_DIM}{label:<12}{_C_RESET}{colour}{value}{_C_RESET}"


def telemetry_header(stage: str, session: str, chatset, chars_in: int,
                     model: str, provider: str,
                     session_code: str | None = None) -> str:
    """The opening block for one stage execution, before the call.

    The heading carries both codes, and the session field shows the code
    rather than the uuid it was resolved from.

    ``session_code`` is the run's resolved value; without it this header
    would ask the store a second time and could print a different code than
    the one the log sink is named after.
    """
    label = _stage_label(stage)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    ses = _session_code(session, session_code)
    lines = [
        "",
        _rule("═"),
        f"  {_C_BOLD}{_C_CYAN}{code_tag(stage, session, session_code=session_code)}"
        f" {label}{_C_RESET}{_C_DIM} · {stage}{_C_RESET}",
        _rule(),
        _field("start", started),
        _field("session", ses),
        _field("stage", _stage_code(stage)),
        _field("chatset", f"CS{chatset}"),
        _field("chars in", f"{chars_in:,}"),
        _field("model", model or "unknown"),
        _field("provider", provider or "unknown"),
        _rule("═"),
    ]
    return "\n".join(lines)


def telemetry_result(stage: str, *, elapsed: float, chars_in: int,
                     chars_out: int, prompt_tokens: int,
                     completion_tokens: int, total_tokens: int,
                     model: str, provider: str, status: str,
                     verdict: str = "", detail: str = "",
                     session: str = "", out_dir=None) -> str:
    """The closing block for one stage execution, after it returns.

    Tokens are reported exactly as the provider returned them. A zero means
    the provider did not report usage, which is shown as ``—`` rather than
    ``0`` so an absent measurement is not mistaken for an empty one.

    ``started`` and ``duration`` are separate labels on purpose: the first is
    a wall-clock time, the second is measured elapsed seconds, and one cannot
    be recovered from the other when several chatsets run in parallel.
    """
    glyph, colour = _verdict_glyph(status)

    def tok(n: int) -> str:
        return f"{n:,}" if n else "—"

    ratio = ""
    if chars_in and chars_out:
        ratio = f"  {_C_DIM}({chars_out / chars_in:.2f}x){_C_RESET}"

    # Reasoning models bill their thinking as completion tokens and then
    # strip it from the message. A reply of 112 characters can carry 1,485
    # completion tokens, which looks like a contradiction until it is
    # labelled; without the note the operator reads it as a counting bug.
    hidden = ""
    if completion_tokens and chars_out:
        per_tok = chars_out / completion_tokens
        if per_tok < 1.5:
            hidden = (f"  {_C_DIM}(reply shorter than token count — "
                      f"thinking tokens are billed, not shown){_C_RESET}")

    lines = [
        _rule(),
        _field("started", stage_wall_start(stage)),
        _field("finished", time.strftime("%Y-%m-%d %H:%M:%S")),
        _field("duration", f"{elapsed:.1f}s"),
        _field("chars out", f"{chars_out:,}{ratio}"),
        _field("tokens", f"prompt {tok(prompt_tokens)}  "
                         f"completion {tok(completion_tokens)}  "
                         f"total {tok(total_tokens)}{hidden}"),
        _field("model", model or "unknown"),
        _field("provider", provider or "unknown"),
        _field("verdict", verdict or "—"),
        _field("status", status, colour),
    ]
    if detail:
        lines.append(_field("detail", detail[:96]))
    if session:
        # The link is the absolute file:// URI, never a relative path: a
        # terminal can only make an absolute URI clickable.
        lines.append(_field("log", log_uri(stage, session, out_dir)))
    lines.append(_rule("═"))
    lines.append("")
    return "\n".join(lines)


class StageTimer:
    """One live line per stage, ending in that stage's real outcome.

    For the distiller this drives the step display as well, so the operator
    sees the run progress inside the one call that lasts minutes. Every other
    stage gets a spinner that names the stage and the model actually serving
    it, then a single frozen line carrying the true result.
    """

    def __init__(self, stage: str, session: str, chatset, chars: int,
                 model: str = "", model_fn=None, out_dir=None,
                 session_code: str | None = None) -> None:
        self.stage = stage
        self.label = _stage_label(stage)
        self.session = session
        # The uuid is kept for the log link and nothing else; every line
        # this timer writes shows the resolved code. A code handed in is the
        # one used: this timer never asks the store when the run has already
        # decided.
        self.ses_code = _session_code(session, session_code)
        self.stg_code = _stage_code(stage)
        self.tag = f"[{self.ses_code}][{self.stg_code}]"
        self.out_dir = out_dir
        self.chatset = chatset
        self.chars = chars
        self.model = model or ""
        # Optional callable returning the model the rotator would serve now.
        # Retries advance the deck, so a name captured at start goes stale.
        self.model_fn = model_fn
        self.attempt = 1
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._started = 0.0
        # Own wall-clock start, so several workers running the same stage in
        # parallel cannot overwrite each other's clock.
        self._wall_started = "—"
        self._is_distiller = stage == "distiller"

    def start(self) -> None:
        self._started = time.monotonic()
        self._wall_started = time.strftime("%Y-%m-%d %H:%M:%S")
        stage_clock_start(self.stage)

        # The ticker owns the bottom line for every stage. Without it a
        # multi-minute call is indistinguishable from a hang: the step fill
        # stops after 30 s and then nothing moves.
        global _ticker
        _ticker = LiveTicker(self._line, interval=0.5)
        _ticker.start()

        if self._is_distiller:
            with _io_lock:
                print(
                    f"  {_C_DIM}{_timestamp()}{_C_RESET} "
                    f"{_C_CYAN}▶ {self.tag} {self.label}{_C_RESET} "
                    f"{_C_DIM}CS{self.chatset} "
                    f"{self.chars:,} chars{_C_RESET}",
                    flush=True,
                )
            self._thread = threading.Thread(
                target=self._distiller_steps, daemon=True,
                name="distiller-steps")
            self._thread.start()
            return

        # Later stages have no step fill, so the ticker is the whole story.
        self._thread = threading.Thread(
            target=self._spin, daemon=True, name=f"spin-{self.stage}")
        self._thread.start()

    def _line(self) -> str:
        """Live bottom line, rendered fresh on every tick.

        The model is re-read each tick rather than captured at start. A retry
        storm rotates the deck, so a frozen name shows one model "failing
        repeatedly" while five cards are actually being burned in turn.
        """
        elapsed = time.monotonic() - self._started
        model = self.model
        if self.model_fn is not None:
            try:
                model = self.model_fn() or model
            except Exception:
                pass
        tail = f" · {model}" if model else ""
        tries = f"  {_C_DIM}(attempt {self.attempt}){_C_RESET}" if self.attempt > 1 else ""
        return (f"  … {self.tag} {self.label}{tail} "
                f"[⏳ {elapsed:.1f}s elapsed]{tries}")

    def _spin(self) -> None:
        """Keep the ticker alive when no step fill is running.

        On a terminal the ticker redraws itself; this loop only needs to
        notice the stop. Without a terminal there is nothing to redraw in
        place, so the elapsed time is printed on a widening interval.
        """
        if not sys.stdout.isatty():
            for mark in (5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
                wait = mark - (time.monotonic() - self._started)
                if self._stop.wait(max(0.0, wait)):
                    return
                print_live(f"  … {self.tag} {self.label}"
                           f"{' · ' + self.model if self.model else ''} "
                           f"[⏳ {mark}s elapsed]")
            return
        self._stop.wait()

    def _distiller_steps(self) -> None:
        """The 11-step fill for the one call that runs for minutes.

        These steps describe what the distiller's prompt asks the model to
        do; they are not independently observable from here, so they pace
        evenly rather than claiming measured durations.
        """
        n = len(_DISTILLER_STEPS)
        width = max(len(s) for s in _DISTILLER_STEPS) + 2
        per = max(1.5, 30.0 / n)

        for i, step in enumerate(_DISTILLER_STEPS, start=1):
            if self._stop.wait(per):
                return
            dots = "." * max(3, width - len(step))
            elapsed = time.monotonic() - self._started
            # print_live clears the ticker, prints, then redraws it below, so
            # the step line lands above the live timer instead of on top of it.
            print_live(
                f"  {_C_DIM}{_timestamp()}{_C_RESET} "
                f"{_C_GREEN}✓{_C_RESET} "
                f"{_C_DIM}{self.tag} [{i:02d}/{n}]{_C_RESET} "
                f"{_C_CYAN}{step}{_C_RESET} "
                f"{_C_DIM}{dots} {elapsed:>4.0f}s{_C_RESET}")

        # Steps are done but the model has not answered. The ticker keeps
        # counting, so only a coarse reminder is added.
        last = 0.0
        while not self._stop.wait(1.0):
            elapsed = time.monotonic() - self._started
            if elapsed - last < 60:
                continue
            last = elapsed
            print_live(
                f"  {_C_DIM}{_timestamp()}   … {self.tag} {self.label} still "
                f"waiting on the model ({elapsed:.0f}s){_C_RESET}")

    def finish(self, status: str, detail: str = "") -> float:
        """Stop the live line and print the stage's real result."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

        # The ticker holds the bottom line; it must go before the result
        # prints or the result lands on top of a half-drawn timer.
        global _ticker
        if _ticker is not None:
            _ticker.stop()
            _ticker = None

        elapsed = stage_clock_done(self.stage, status)
        # The timer's own start, not the shared per-stage entry: with several
        # workers on one stage, the shared entry belongs to whichever started
        # most recently.
        started_at = self._wall_started

        if not self._is_distiller and sys.stdout.isatty():
            with _io_lock:
                sys.stdout.write("\r" + " " * 100 + "\r")
                sys.stdout.flush()

        glyph, colour = _verdict_glyph(status)
        tail = f"  {_C_DIM}{detail}{_C_RESET}" if detail else ""
        with _io_lock:
            print(
                f"  {_C_DIM}{_timestamp()}{_C_RESET} "
                f"{colour}{glyph} {self.tag} {self.label}{_C_RESET} "
                f"{_C_DIM}start{_C_RESET} {started_at}  "
                f"{_C_DIM}duration{_C_RESET} {elapsed:>6.1f}s  "
                f"{colour}{status}{_C_RESET}{tail}",
                flush=True,
            )
            # Absolute file:// on its own line: a wrapped path is hard to
            # click, and a relative one cannot be clicked at all.
            print(
                f"     {_C_DIM}log{_C_RESET} "
                f"{log_uri(self.stage, self.session, self.out_dir)}",
                flush=True,
            )
        return elapsed


def stage_ledger() -> list[str]:
    """Per-stage totals for the end-of-run block."""
    if not _STAGE_TIMES:
        return []
    rows = []
    total = 0.0
    for stage in _STAGE_ORDER:
        entries = _STAGE_TIMES.get(stage)
        if not entries:
            continue
        secs = sum(e[0] for e in entries)
        total += secs
        worst = max(e[0] for e in entries)
        counts: dict[str, int] = {}
        for _, status, _ts in entries:
            key = status.split("(")[0].strip().lower() or status
            counts[key] = counts.get(key, 0) + 1
        mix = "  ".join(f"{k}×{v}" for k, v in sorted(counts.items()))
        rows.append(
            f"  {_stage_label(stage):<14}{len(entries):>4} call   "
            f"{secs:>8.0f}s   {secs / len(entries):>6.0f}s avg   "
            f"{worst:>6.0f}s max   {mix}")

    out = ["", "  " + "─" * 78,
           f"  {'STAGE':<14}{'':>4}       {'TOTAL':>8}   {'MEAN':>10}   "
           f"{'WORST':>10}   OUTCOMES",
           "  " + "─" * 78]
    out.extend(rows)
    out.append("  " + "─" * 78)
    out.append(f"  {'WALL CLOCK':<14}{total:>28.0f}s")
    out.append("  " + "─" * 78)
    return out


def print_stage_ledger() -> None:
    for line in stage_ledger():
        print(line, flush=True)


# ── Distiller result summary ────────────────────────────────────────────

def _fmt_bytes(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def unwrap_payload(parsed):
    """Return a stage's own output object from a stored payload.

    The model returns ``{"_meta": {...}, "<stage>_output": {...}}``. Every
    field of interest lives inside that wrapper, so reading the envelope
    reports zeros. Payloads that are already flat pass through unchanged.

    The wrapper key differs per stage, so every ``*_output`` key is checked
    rather than only the distiller's.
    """
    if not isinstance(parsed, dict):
        return {}
    for key in ("distiller_output", "introbeaver_output",
                "planbeaver_output", "outrobeaver_output"):
        inner = parsed.get(key)
        if isinstance(inner, dict):
            return inner
    return parsed


def _count_of(payload: dict, key: str) -> int:
    v = payload.get(key)
    if isinstance(v, (list, tuple)):
        return len(v)
    return int(v) if isinstance(v, int) else 0


def distiller_summary(soma_path, chars_in, parsed) -> str:
    """One block stating what the distiller actually decided.

    Every number here is either a character count taken from the source or a
    value read out of the model's own output object. Nothing is estimated.
    """
    payload = unwrap_payload(parsed)

    skip = bool(payload.get("skip"))
    skip_reason = str(payload.get("skip_reason") or "").strip()
    intent = str(payload.get("intent_anchor") or payload.get("intent") or "").strip()
    sigq = payload.get("signal_quality")

    counts = {k: _count_of(payload, k) for k in (
        "directives", "constraints", "preferences", "questions",
        "corrections", "confirmations", "artifacts")}

    if skip:
        decision = f"{_C_YELLOW}SKIPPED{_C_RESET}"
        if skip_reason:
            decision += f" {_C_DIM}({skip_reason}){_C_RESET}"
    else:
        decision = f"{_C_GREEN}PROCESSED{_C_RESET}"

    parts = [
        f"  {_C_BOLD}decision{_C_RESET}      {decision}",
    ]
    if isinstance(chars_in, int) and chars_in >= 0:
        parts.append(f"  {_C_BOLD}chars in{_C_RESET}     {chars_in:,}")
    if isinstance(sigq, (int, float)):
        parts.append(f"  {_C_BOLD}signal qual{_C_RESET}  {sigq:.2f}")
    if intent:
        parts.append(f"  {_C_BOLD}intent{_C_RESET}       {intent[:96]}")

    found = "  ".join(f"{k}={v}" for k, v in counts.items() if v)
    if found:
        parts.append(f"  {_C_BOLD}extracted{_C_RESET}    {found}")
    else:
        parts.append(f"  {_C_BOLD}extracted{_C_RESET}    {_C_DIM}nothing "
                     f"(all signal lists empty){_C_RESET}")

    parts.append(f"  {_C_BOLD}soma{_C_RESET}         {soma_path}")
    return "\n".join(parts)


# ── Per-stage result detail ─────────────────────────────────────────────
#
# One block per stage as it completes, so the CLI shows what the stage
# actually produced instead of only START/SKIP/FAIL lines. Every value
# printed is a field from that stage's own output contract.

def _pct(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def introbeaver_detail(chars_in, parsed) -> str:
    """TB-004: entry counts, signal quality, noise, artifacts, verdict."""
    body = unwrap_payload(parsed)
    by_type = body.get("entry_count_by_type") or {}
    noise = body.get("noise_signature") or {}
    entries = body.get("entries") or []
    artifacts = body.get("artifacts") or []
    chron = body.get("chronology_resolved") or []
    discarded = body.get("discarded") or []

    parts = [f"  {_C_BOLD}chars in{_C_RESET}    {chars_in:,}"]
    parts.append(f"  {_C_BOLD}entries{_C_RESET}     {body.get('extracted_count', len(entries))}"
                 f"  " + "  ".join(f"{k}={v}" for k, v in by_type.items() if v))
    parts.append(f"  {_C_BOLD}signal{_C_RESET}      {_pct(body.get('signal_quality'))}"
                 f"   {_C_DIM}user msgs {body.get('user_message_count', '—')}"
                 f"  noise words {noise.get('noise_words', 0)}{_C_RESET}")

    noise_bits = [f"{k} {v}" for k, v in noise.items()
                  if k != "noise_words" and v]
    if noise_bits:
        parts.append(f"  {_C_BOLD}noise{_C_RESET}       {', '.join(noise_bits)}")

    if artifacts:
        kinds = {}
        for a in artifacts:
            kinds[a.get("artifact_type", "?")] = kinds.get(a.get("artifact_type", "?"), 0) + 1
        parts.append(f"  {_C_BOLD}artifacts{_C_RESET}   {len(artifacts)}  ("
                     + ", ".join(f"{k} {v}" for k, v in kinds.items()) + ")")

    if chron:
        superseded = sum(len(c.get("losers") or []) for c in chron)
        parts.append(f"  {_C_BOLD}chronology{_C_RESET}  {len(chron)} intent group(s), "
                     f"{superseded} superseded")

    if discarded:
        reasons = {}
        for d in discarded:
            r = d.get("reason", "?")
            reasons[r] = reasons.get(r, 0) + 1
        parts.append(f"  {_C_BOLD}discarded{_C_RESET}   {len(discarded)}  ("
                     + ", ".join(f"{k} {v}" for k, v in reasons.items()) + ")")

    status = body.get("status") or "—"
    colour = _C_GREEN if status == "PASS" else _C_YELLOW
    line = f"  {_C_BOLD}status{_C_RESET}      {colour}{status}{_C_RESET}"
    if body.get("block_reason"):
        line += f"   {_C_DIM}{body['block_reason']}{_C_RESET}"
    if body.get("skip_reason"):
        line += f"   {_C_DIM}{body['skip_reason']}{_C_RESET}"
    parts.append(line)
    return "\n".join(parts)


def planbeaver_detail(chars_in, parsed) -> str:
    """TB-005: files, functions, line budget, contracts, execution order."""
    body = unwrap_payload(parsed)
    files = body.get("files") or []
    budget = body.get("budget") or {}
    sequence = body.get("execution_sequence") or []
    contracts = body.get("data_contracts") or []

    functions = sum(len(f.get("functions") or []) for f in files)
    lines = body.get("estimated_lines_total")
    if lines is None:
        lines = sum(int(f.get("estimated_lines") or 0) for f in files)

    parts = [f"  {_C_BOLD}chars in{_C_RESET}    {chars_in:,}"]
    parts.append(f"  {_C_BOLD}plan{_C_RESET}        {len(files)} file(s)   "
                 f"{functions} function(s)   ~{lines:,} line(s)")
    if budget:
        parts.append(f"  {_C_BOLD}budget{_C_RESET}      "
                     + "  ".join(f"{k}={v}" for k, v in budget.items()))
    if contracts:
        parts.append(f"  {_C_BOLD}contracts{_C_RESET}   {len(contracts)}")
    if sequence:
        head = " → ".join(str(s) for s in sequence[:6])
        more = "" if len(sequence) <= 6 else f" → … (+{len(sequence) - 6})"
        parts.append(f"  {_C_BOLD}sequence{_C_RESET}    {head}{more}")
    if body.get("cycle"):
        parts.append(f"  {_C_BOLD}cycle{_C_RESET}       {_C_YELLOW}{body['cycle']}{_C_RESET}")
    parts.append(f"  {_C_BOLD}status{_C_RESET}      {body.get('status') or '—'}")
    return "\n".join(parts)


def outrobeaver_detail(chars_in, parsed) -> str:
    """TB-006: journal size, clarity, drift, friction, session health."""
    body = unwrap_payload(parsed)
    journal = body.get("journal_entries") or []
    analysis = body.get("clarity_and_communication_analysis") or {}
    summary = body.get("summary") or {}
    comp = body.get("health_components") or {}
    drift = analysis.get("drift_events") or []
    friction = analysis.get("friction_points") or []

    parts = [f"  {_C_BOLD}chars in{_C_RESET}    {chars_in:,}"]
    parts.append(f"  {_C_BOLD}journal{_C_RESET}     {len(journal)} entr(ies)")

    clarity = analysis.get("session_clarity_score")
    if clarity is not None:
        parts.append(f"  {_C_BOLD}clarity{_C_RESET}     {_pct(clarity)}   "
                     f"{_C_DIM}user reps {analysis.get('user_repetition_count', 0)}"
                     f"  agent reps {analysis.get('agent_repetition_count', 0)}"
                     f"  friction {_pct(analysis.get('friction_rate'))}{_C_RESET}")

    if drift:
        worst = max((d.get("severity", "MINOR") for d in drift),
                    key=lambda s: ["MINOR", "MODERATE", "MAJOR", "CRITICAL"].index(s)
                    if s in ["MINOR", "MODERATE", "MAJOR", "CRITICAL"] else 0)
        parts.append(f"  {_C_BOLD}drift{_C_RESET}       {len(drift)} event(s)  worst {worst}")
    if friction:
        kinds = {}
        for f in friction:
            k = f.get("type", "?")
            kinds[k] = kinds.get(k, 0) + 1
        parts.append(f"  {_C_BOLD}friction{_C_RESET}    "
                     + ", ".join(f"{k} {v}" for k, v in kinds.items()))

    if comp:
        parts.append(f"  {_C_BOLD}health{_C_RESET}      score {_pct(comp.get('score'))}   "
                     f"{_C_DIM}clarity {_pct(comp.get('clarity'))}"
                     f"  drift −{_pct(comp.get('drift_penalty'))}"
                     f"  friction −{_pct(comp.get('friction_penalty'))}{_C_RESET}")

    if summary:
        health = summary.get("session_health", "—")
        colour = (_C_GREEN if health == "GREEN"
                  else _C_YELLOW if health == "YELLOW" else _C_RED
                  if "_C_RED" in globals() else _C_YELLOW)
        parts.append(f"  {_C_BOLD}session{_C_RESET}     {colour}{health}{_C_RESET}")
        if summary.get("built"):
            parts.append(f"  {_C_BOLD}built{_C_RESET}       {str(summary['built'])[:70]}")
        if summary.get("blocked"):
            parts.append(f"  {_C_BOLD}blocked{_C_RESET}     {str(summary['blocked'])[:70]}")

    parts.append(f"  {_C_BOLD}status{_C_RESET}      {body.get('status') or '—'}")
    return "\n".join(parts)


STAGE_DETAIL = {
    "introbeaver": introbeaver_detail,
    "planbeaver": planbeaver_detail,
    "outrobeaver": outrobeaver_detail,
}


def stage_detail(stage: str, chars_in, parsed) -> str:
    """Render the detail block for a stage, or an empty string."""
    fn = STAGE_DETAIL.get(stage)
    if fn is None or not isinstance(parsed, dict):
        return ""
    try:
        return fn(chars_in, parsed)
    except Exception:
        return ""
