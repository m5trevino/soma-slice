"""SOMA banner — the same art in the CLI and the board.

Rendered once here so the launcher and the pipeline cannot drift apart.
Purple-to-green gradient across the glyphs; plain ASCII when colour is off.
"""
from __future__ import annotations

ART = [
    "  ▀▀▀▓▄▄       ▀▀▀▓▄▄             ▄▀   ▄          ▄▀ ",
    " ▄██▄░▀██▓▄      ░░▀██▓▄        ▄▓█▌   ▐▓▄      ▄▓█▌ ",
    "▀ ▀░░░░░▀██▓  ▄▄█▀ ░▒███▓▄    ▄████▓ ▄██▓█▌   ▄████▓ ",
    " ░░▐▒▒▄▄▄    █░░     ░░░░░▌  ░█░░███░░░▐█▓   ░█░▀ █▓▌",
    "  ░  ▀▀░░▒▒ ▐▒▒▌     ░▐▒▒▒▒ ░░░░▀ ░░█▀ ██▓  ░░░░████▌",
    "  ▄▓    ▀▓▓█ ▓▓▓▄   ░▒▓▓▓▓▌▐▒▒▒  ░▒▀ ▄░░░  ▐▒▒▒▀ ▐░░░",
    " ▀▓█▓▄▄ ▓███  ▀███▄▄▄▓███▀ ▐▓▒▌░    ▄▓▓▓▀   ▓▒▌░ ░▒▒▒",
    "   ▀▀  ▀▀▀▀      ▀▀▀▀▀▀▀    ▀▀     ▀▀▀       ▓▄  ▄▓▀ ",
]

TAGLINE = "  provenance · invariants · machine talk"

# Purple at the left edge, green at the right: each column's hue is decided by
# its horizontal position, so the art reads as one gradient rather than stripes.
_START = (168, 85, 247)     # violet
_END = (34, 197, 94)        # emerald
_DIM = (120, 96, 160)


def _rgb_to_ansi(r: int, g: int, b: int, bold: bool = True) -> str:
    head = "\033[1;38;2" if bold else "\033[38;2"
    return f"{head};{r};{g};{b}m"


def _gradient(col: int, width: int) -> str:
    span = max(1, width - 1)
    t = min(1.0, col / span)
    r = int(_START[0] + (_END[0] - _START[0]) * t)
    g = int(_START[1] + (_END[1] - _START[1]) * t)
    b = int(_START[2] + (_END[2] - _START[2]) * t)
    return _rgb_to_ansi(r, g, b)


def gradient_hex(col: int, width: int) -> str:
    """Same gradient as a ``#rrggbb`` string, for Textual style props."""
    span = max(1, width - 1)
    t = min(1.0, col / span)
    r = int(_START[0] + (_END[0] - _START[0]) * t)
    g = int(_START[1] + (_END[1] - _START[1]) * t)
    b = int(_START[2] + (_END[2] - _START[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


RESET = "\033[0m"


def render(color: bool = True) -> str:
    """Return the banner as text, with a horizontal purple→green gradient."""
    if not color:
        return "\n".join(ART + ["", TAGLINE, ""])

    width = max(len(line) for line in ART)
    out: list[str] = []
    for line in ART:
        painted: list[str] = []
        last = None
        for col, ch in enumerate(line):
            if ch == " ":
                painted.append(ch)
                continue
            tone = _gradient(col, width)
            if tone != last:
                painted.append(tone)
                last = tone
            painted.append(ch)
        if last is not None:
            painted.append(RESET)
        out.append("".join(painted))
    out.append("")
    out.append(_rgb_to_ansi(*_DIM, bold=False) + TAGLINE + RESET)
    out.append("")
    return "\n".join(out)


def print_banner(color: bool | None = None) -> None:
    """Print the banner, honouring NO_COLOR and non-tty output."""
    import os
    import sys

    if color is None:
        color = (not os.environ.get("NO_COLOR")) and sys.stdout.isatty()
    print(render(color=color))
