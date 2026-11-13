"""Compact stage cards for the Dendritic CLI pipeline.

Produces the ▐░░ operator-approved card format:

    ▐── [distiller] session-id CS1 (1/5) ────────────────┐
    │ model=compound-mini  chars=4200  hash=a3f…         │
    │ attempt=1/4  status=PASS  tokens=1240  time=3.2s   │
    │ verdict=DONE                                        │
    └─────────────────────────────────────────────────────┘

    ▐── nexus ───────────────────────────────────────────┐
    │ model=deepseek-v4  status=PASS  tokens=8420 time=12s│
    │ verdict=DONE  build=260611-1609                     │
    └─────────────────────────────────────────────────────┘

The module intentionally contains no stage-specific logic. Callers
assemble the fields (model, tokens, status, verdict, etc.) and pass
them to the card printer. No Soma/LLM access.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TextIO

# ── Card geometry ──────────────────────────────────────────────────────────
# Border uses ▐ for start, └ for end, ─ for horizontal, │ for vertical.
# Content area width = 55 chars between side borders.

_VERT_START = "▐"
_VERT_END = "└"
_HORIZ = "─"
_SIDE = "│"
_TOP_RIGHT = "┐"
_BOTTOM_RIGHT = "┘"
_CONTENT_WIDTH = 55  # characters inside the side borders


# ── Data helpers ───────────────────────────────────────────────────────────

@dataclass
class CardFields:
    """Flattened data for a single compact card."""
    title: str = ""
    subtitle: str = ""
    details: list[str] = field(default_factory=list)
    verdict: str = ""
    status: str = ""
    extra: str = ""

    def lines(self) -> list[str]:
        """Return the body lines for this card (no borders)."""
        parts: list[str] = []
        if self.subtitle:
            parts.append(self.subtitle)
        parts.extend(self.details)
        if self.verdict:
            parts.append(f"verdict={self.verdict}")
        if self.extra:
            parts.append(self.extra)
        return parts


# ── Card printer ───────────────────────────────────────────────────────────

def _line(text: str) -> str:
    """Center and pad a single line to CONTENT_WIDTH."""
    if not text:
        return " " * _CONTENT_WIDTH
    text = text[:_CONTENT_WIDTH]
    return text.center(_CONTENT_WIDTH)


def _compact_card(fields: CardFields, out: TextIO | None = None) -> str:
    """Build a compact card string (no final newline)."""
    if out is None:
        out = sys.stdout
    lines: list[str] = []
    # Title line: ▐── TITLE ───────────────────────────────┐
    title_line = f" {fields.title} " if fields.title else ""
    border_len = _CONTENT_WIDTH - len(title_line)
    if border_len > 0:
        h = _HORIZ * border_len
    else:
        h = ""
    lines.append(f"{_VERT_START}{_HORIZ * 2}{title_line}{h}{_TOP_RIGHT}")
    # Body lines
    for body in fields.lines():
        lines.append(f"{_SIDE}{_line(body)}")
    # Verdict line (optional extra emphasis)
    if fields.status:
        lines.append(f"{_SIDE}{_line(f'status={fields.status.upper()}')}"
                     if fields.status not in (l for l in fields.lines()) else "")
    # Bottom: └─────────────────────────────────────────────┘
    h = _HORIZ * _CONTENT_WIDTH
    lines.append(f"{_VERT_END}{h}{_BOTTOM_RIGHT}")
    return "\n".join(lines)


def print_compact_card(
    title: str,
    *,
    subtitle: str = "",
    model: str = "",
    chars: int = 0,
    tokens: int = 0,
    time_ms: float = 0.0,
    attempt: str = "",
    status: str = "",
    verdict: str = "",
    details: list[str] | None = None,
    extra: str = "",
    out: TextIO | None = None,
) -> str:
    """Print a compact ▐░░ card and return its string representation.

    Args:
        title: Main title — e.g. ``distiller`` or ``nexus``.
        subtitle: Sub-info — e.g. ``session-id CS1 (1/5)``.
        model: Model name used.
        chars: Input character count.
        tokens: Output token count.
        time_ms: Latency in milliseconds.
        attempt: Current attempt — e.g. ``"1/4"``.
        status: Current status — e.g. ``"PASS"`` or ``"RUNNING"``.
        verdict: Gate verdict — e.g. ``"DONE"``.
        details: Extra detail lines (rendered as-is, no centering).
        extra: One-off extra field rendered after verdict.
        out: Output stream. Defaults to ``sys.stdout``.
    """
    if out is None:
        out = sys.stdout
    parts: list[str] = []
    if subtitle:
        parts.append(subtitle)
    fields = CardFields(
        title=title,
        subtitle=subtitle,
        details=details or [],
        verdict=verdict,
        status=status,
        extra=extra,
    )
    # Add computed detail lines
    detail_parts: list[str] = []
    if model:
        detail_parts.append(f"model={model}")
    if chars:
        detail_parts.append(f"chars={chars}")
    if tokens:
        detail_parts.append(f"tokens={tokens}")
    if time_ms > 0:
        detail_parts.append(f"time={time_ms / 1000:.1f}s")
    if attempt:
        detail_parts.append(f"attempt={attempt}")
    if detail_parts:
        # Join computed details into the subtitle area or details
        if not subtitle:
            fields.subtitle = "  ".join(detail_parts)
        else:
            fields.details = detail_parts + fields.details
    card = _compact_card(fields, out)
    print(card)
    return card


def print_compact_card_start(
    stage: str = "",
    *,
    title: str = "",
    subtitle: str = "",
    model: str = "",
    chars: int = 0,
    total: int = 0,
    out: TextIO | None = None,
) -> str:
    """Print a compact START card (stage is beginning)."""
    if out is None:
        out = sys.stdout
    fields = CardFields(
        title=title or stage,
        subtitle=subtitle,
        details=[],
        status="RUNNING",
    )
    detail_parts: list[str] = []
    if model:
        detail_parts.append(f"model={model}")
    if chars:
        detail_parts.append(f"chars={chars}")
    if total:
        detail_parts.append(f"{total} total")
    if detail_parts:
        fields.details = detail_parts
    card = _compact_card(fields, out)
    print(card)
    return card


def print_compact_card_done(
    stage: str = "",
    *,
    title: str = "",
    subtitle: str = "",
    model: str = "",
    tokens: int = 0,
    time_ms: float = 0.0,
    attempt: int = 1,
    max_attempts: int = 1,
    verdict: str = "",
    skipped: bool = False,
    skip_reason: str = "",
    status: str = "DONE",
    errors: str = "",
    out: TextIO | None = None,
) -> str:
    """Print a compact DONE/FAIL card (stage is complete)."""
    if out is None:
        out = sys.stdout
    fields = CardFields(
        title=title or stage,
        subtitle=subtitle,
        verdict=verdict,
        status=status,
    )
    detail_parts: list[str] = []
    if model:
        detail_parts.append(f"model={model}")
    if tokens:
        detail_parts.append(f"tokens={tokens}")
    if time_ms > 0:
        detail_parts.append(f"time={time_ms / 1000:.1f}s")
    if max_attempts > 1:
        detail_parts.append(f"attempt={attempt}/{max_attempts}")
    if skipped:
        detail_parts.append(f"SKIP")
        if skip_reason:
            fields.extra = f"skip={skip_reason}"
    elif errors:
        fields.extra = errors[:80]
    if detail_parts:
        fields.details = detail_parts
    card = _compact_card(fields, out)
    print(card)
    return card
