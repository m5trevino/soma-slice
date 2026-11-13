#!/usr/bin/env python3
"""SOMA board — a seven-tile control panel for the Soma launchers.

Each tile starts one service in its own terminal window. The window owns the
process: closing the window stops the service. The board keeps no state, so it
can be closed at any moment without affecting anything it launched. The lamps
are live port checks, so they stay honest even for services started by hand.

The PEA sync strip below the tiles is deliberately not a tile: it is a
background loop with no window and no service manager, so it gets a status
line instead. It reads the pid file the loop's starter writes, falls back to
scanning /proc, and starts the loop detached when asked.

Layout: 2 columns × 4 rows:
  [Droid Session Picker]  [Goose Session Picker]
  [Turn Key Builder    ]  [Debug Dashboard      ]
  [Engine TUI          ]  [Run Soma             ]
  [PEA Manifests       ]  [Soma Logs (site)     ]

The SOMA banner sits boxed and docked at the top (it never scrolls away) and
shrinks to a one-line plate on short terminals. Every card has its own colour;
the three premier cards (Run Soma, Turn Key Builder, Debug Dashboard) pulse.
Cards show live stats under their title once something is running.
A strip of small link chips under the tiles opens the engine's Soma docs.
A small pulsing ENGINE UPDATE card (right, above the chips) runs ./freshner-update
on the Hetzner server over ssh. It asks for a second press, because the update
restarts the engine.

Keys:
    arrows / tab   move between tiles and the sync strip
    space / enter  launch the focused tile, or start the sync when focused
    1 2 3 4 5 6 7  launch a specific tile (1=Droid, 2=Goose, 3=Turnkey,
                    4=Dashboard, 5=Engine TUI, 6=Run Soma, 7=PEA Manifests, 8=Soma Logs)
    8              open the Soma log site
    u              engine update (press twice to confirm)
    p              start the PEA sync
    q              quit the board (does not stop anything)

Usage:
    soma                 open the board
    soma --check         print service status and exit
    soma --dry           open the board but never spawn windows
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal
from textual.widgets import Static

TURNKEY_DIR = Path("/home/flintx/turn-key")
SOMA_DIR = Path("/home/flintx/soma-10-14")
TUI_DIR = Path("/home/flintx/peacock-engine-tui-core")
TUI_DIR = Path("/home/flintx/peacock-engine-tui-core")
TUI_DIR = Path("/home/flintx/peacock-engine-tui-core")
TUI_DIR = Path("/home/flintx/peacock-engine-tui-core")
# PEA log browser (tools/pea_viewer.py). Read-only: pea_sync.py owns syncing.
PEA_LOG_PORT = 5199
# The manifest viewer picks the next free port if this one is busy, so the
# board probes the preferred one and falls back to whatever the process says.
PEA_MANIFEST_PORT = 9893

# One-button engine refresh on the Hetzner box. "engine" is the ssh alias in
# ~/.ssh/config (root@100.87.51.23, key ~/.ssh/hetzner). The script updates the
# model list and restarts the engine, so the card asks for a second press.
ENGINE_SSH_HOST = "engine"
ENGINE_UPDATE_DIR = "/root/hetzner/ai-engine"
ENGINE_UPDATE_CMD = (
    f"ssh -t {ENGINE_SSH_HOST} "
    f"'cd {ENGINE_UPDATE_DIR} && ./freshner-update'"
)
UPDATE_CONFIRM_SECONDS = 6.0

# The engine serves the Soma log site and its docs under one prefix.
SOMA_SITE = "https://engine.save-aichats.com/v1/soma/"
SOMA_DOCS: list[tuple[str, str]] = [
    ("Agent Guide", "https://engine.save-aichats.com/v1/soma/agent-guide"),
    ("SDK", "https://engine.save-aichats.com/v1/soma/docs/sdk"),
    ("Stage Guide", "https://engine.save-aichats.com/v1/soma/docs/session-stage-guide"),
    ("Pipeline Guide", "https://engine.save-aichats.com/v1/soma/docs/pipeline-guide"),
    ("Engine SDK", "https://engine.save-aichats.com/v1/soma/docs/engine-sdk"),
]


# PEA sync now runs the repo's own puller, which fetches every session into
# ~/pea-sessions/<session>/ and renames each file so the order is chronological
# and the name carries the stage and outcome. The old rsync loop only mirrored
# raw files and left them unlabelled.
PEA_TOOL = SOMA_DIR / "tools" / "pea_sync.py"
PEA_DEST = Path.home() / "pea-sessions"
SYNC_SCRIPT = PEA_TOOL
SYNC_PIDFILE = Path.home() / ".pea-sync.pid"
SYNC_MIRROR = PEA_DEST

BONE = "#e9e4d7"
DIM = "#8d9aa4"
FAINT = "#5f6a77"
AMBER = "#f2a93b"
AMBER_HI = "#ffd28c"
GREEN = "#4ade80"

# ── palette ──────────────────────────────────────────────────────────────
# 80s terminal hacker: phosphor green and neon violet on near-black glass.

PHOSPHOR = "#39ff14"
PANEL_BG = "#0a0f14"
LCD_BG = "#03060a"
FRAME = "#8b5cf6"

ACCENTS = {
    "droid-picker": "#22d3ee",
    "goose-picker": "#a3e635",
    "turnkey": "#ffd60a",
    "dashboard": "#ff4fd8",
    "tui": "#5b8cff",
    "soma": PHOSPHOR,
    "pealogs": "#b36bff",
    "somalogs": "#ff7a59",
}
PREMIER = frozenset({"soma", "turnkey", "dashboard"})


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def blend(a: str, b: str, t: float) -> str:
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    mix = [ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t]
    return "#" + "".join(f"{max(0, min(255, int(v))):02x}" for v in mix)


def lighten(color: str, t: float) -> str:
    return blend(color, "#ffffff", t)


def darken(color: str, t: float) -> str:
    return blend(color, "#000000", t)


# ── probes ───────────────────────────────────────────────────────────────

def port_open(port: Optional[int], timeout: float = 0.15) -> bool:
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def dashboard_port() -> int:
    try:
        return int((SOMA_DIR / ".dashboard_port").read_text().strip())
    except (OSError, ValueError):
        return 9877


# ── PEA sync probe ───────────────────────────────────────────────────────
#
# pea-sync.sh is a bare `while true` rsync loop. It writes no pid file of its
# own, so we honour the one its starter leaves behind, verify the pid is still
# the script (guarding against pid reuse), and fall back to a /proc scan for
# copies started without one. Negative scans are cached so a running loop that
# predates the pid file does not cost a /proc walk every second.

_SYNC_SCAN_TTL = 5.0
_sync_scan_cache = {"at": 0.0, "pid": None}


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _is_sync_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return "pea-sync" in _proc_cmdline(pid)


def _scan_for_sync() -> Optional[int]:
    now = time.monotonic()
    if now - _sync_scan_cache["at"] < _SYNC_SCAN_TTL:
        cached = _sync_scan_cache["pid"]
        # Re-validate: a cached pid that has since died must not read as
        # running just because the TTL has not elapsed.
        if cached is None or _is_sync_pid(cached):
            return cached

    found: Optional[int] = None
    try:
        names = os.listdir("/proc")
    except OSError:
        names = []
    me = os.getpid()
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        if "pea-sync" in _proc_cmdline(pid):
            found = pid
            break

    _sync_scan_cache["at"] = now
    _sync_scan_cache["pid"] = found
    return found


def sync_pid() -> Optional[int]:
    try:
        pid = int(SYNC_PIDFILE.read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid and _is_sync_pid(pid):
        return pid
    return _scan_for_sync()


def _proc_age(pid: int) -> Optional[float]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        after_comm = stat.rsplit(")", 1)[1].split()
        start_ticks = int(after_comm[19])  # field 22 of /proc/<pid>/stat
    except (IndexError, ValueError):
        return None
    hz = os.sysconf("SC_CLK_TCK") or 100
    return max(0.0, uptime - start_ticks / hz)


def format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    if total < 60:
        return "<1m"
    minutes = total // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


# ── tile definitions ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Spec:
    key: str
    title: str
    target: str
    short: str
    hint: str
    cwd: Path
    command: str
    probe: Optional[Callable[[], bool]] = None
    url: Optional[Callable[[], Optional[str]]] = None
    # A fixed web address: the tile opens it in the browser and starts nothing.
    link: Optional[str] = None


SPECS: list[Spec] = [
    Spec(
        key="droid-picker",
        title="Droid Session Picker",
        target="terminal · droid exec",
        short="droid",
        hint="close the window to stop",
        cwd=SOMA_DIR,
        command='/usr/local/bin/droid',
        probe=None,
        url=None,
    ),
    Spec(
        key="goose-picker",
        title="Goose Session Picker",
        target="terminal · goose run",
        short="goose",
        hint="close the window to stop",
        cwd=SOMA_DIR,
        command='$HOME/.local/bin/goose',
        probe=None,
        url=None,
    ),
    Spec(
        key="turnkey",
        title="Turn Key Builder",
        target="browser · port 5173",
        short="5173",
        hint="close the window to stop",
        cwd=TURNKEY_DIR,
        command="./launcher.sh fg",
        probe=lambda: port_open(5173),
        url=lambda: "http://127.0.0.1:5173",
    ),
    Spec(
        key="dashboard",
        title="Debug Dashboard",
        target="browser · port 9877+",
        short="9877+",
        hint="ctrl+c or close to stop",
        cwd=SOMA_DIR,
        command="python3 run_dashboard.py",
        probe=lambda: port_open(dashboard_port()),
        url=lambda: f"http://127.0.0.1:{dashboard_port()}",
    ),
    Spec(
        key="tui",
        title="Engine TUI",
        target="terminal · opentui",
        short="tui",
        hint="q to quit",
        cwd=TUI_DIR,
        command="./start.sh",
        probe=None,
        url=None,
    ),
    Spec(
        key="soma",
        title="Run Soma",
        target="terminal · ./run.sh",
        short="./run.sh",
        hint="exits when the run finishes",
        cwd=SOMA_DIR,
        command="./run.sh",   
        probe=None,
        url=None,
    ),
    Spec(
        key="pealogs",
        title="PEA Manifests",
        target="browser · port 9893",
        short="9893",
        hint="read or download one, or all",
        cwd=SOMA_DIR,
        # The board opens the tab once the port answers; without --no-open the
        # viewer opens a second one itself.
        command="python3 tools/pea_viewer.py --no-open",
        probe=lambda: port_open(PEA_MANIFEST_PORT),
        url=lambda: f"http://127.0.0.1:{PEA_MANIFEST_PORT}",
    ),
    Spec(
        key="somalogs",
        title="Soma Logs",
        target="browser · engine site",
        short="site",
        hint="opens the Soma log site on the engine",
        cwd=SOMA_DIR,
        command="",
        probe=None,
        url=None,
        link=SOMA_SITE,
    ),
]

BY_KEY = {spec.key: spec for spec in SPECS}


# ── window launching ─────────────────────────────────────────────────────

def terminal_argv(title: str, inner: str) -> Optional[list[str]]:
    """Build an argv for the first terminal emulator we can find."""
    argv = ["bash", "-lc", inner]
    if shutil.which("alacritty"):
        return ["alacritty", "-t", title, "-e", *argv]
    if shutil.which("kitty"):
        return ["kitty", "-T", title, *argv]
    if shutil.which("xfce4-terminal"):
        return ["xfce4-terminal", "--title", title, "-x", *argv]
    if shutil.which("x-terminal-emulator"):
        return ["x-terminal-emulator", "-T", title, "-e", *argv]
    if shutil.which("xterm"):
        return ["xterm", "-T", title, "-e", *argv]
    return None


def window_inner(spec: Spec) -> str:
    """Command body run inside the new window.

    Holds the window open on exit so a crash is readable instead of a window
    that flashes and disappears.
    """
    body = f"cd {shlex.quote(str(spec.cwd))} && {spec.command}"
    return (
        f"{body}; rc=$?; echo; "
        f"printf '[exit %s] Press Enter to close this window. ' \"$rc\"; read -r _"
    )


# ── banner ───────────────────────────────────────────────────────────────
# The art is imported from soma/banner.py so it is the exact CLI banner. Each
# shade glyph gets its own tone: the dense blocks glow phosphor green, the
# light shading and edges are neon violet. A diagonal highlight sweeps across.

# One hue, so the letters stay readable: brightness follows the art's own
# shading (solid blocks brightest, light shade dimmest) and the outline glyphs
# stay bright to hold the shape of each letter.
_GLYPH_TONES = {
    "█": "#39ff14", "▓": "#2fe03a", "▒": "#22a83a", "░": "#177a2c",
    "▀": "#39ff14", "▄": "#39ff14", "▌": "#39ff14", "▐": "#39ff14",
}


def load_banner() -> tuple[list[str], str]:
    if str(SOMA_DIR) not in sys.path:
        sys.path.insert(0, str(SOMA_DIR))
    try:
        from soma.banner import ART, TAGLINE
    except ImportError:
        return ["▓▒░  S O M A  ░▒▓"], "machine room launcher"
    rows = list(ART)
    # Row 6 of the source art has the O and M touching (no gap at col 27) while
    # every other row has one. Open the gap and drop a space before the A so the
    # row keeps its width. Guarded: a changed banner.py is left alone.
    if len(rows) > 5 and rows[5][26:28] == "\u258c\u2590" and rows[5][41:43] == "  ":
        rows[5] = rows[5][:27] + " " + rows[5][27:41] + rows[5][42:]
    width = max(len(line) for line in rows)
    return [line.ljust(width) for line in rows], TAGLINE.strip()


def _art_line(line: str, row: int, sweep: int) -> Text:
    out = Text(no_wrap=True)
    run, run_style = "", ""
    for col, ch in enumerate(line):
        if ch == " ":
            style = ""
        else:
            tone = _GLYPH_TONES.get(ch, PHOSPHOR)
            dist = abs(col + row * 2 - sweep)
            if dist <= 3:
                tone = lighten(tone, 0.45 - 0.12 * dist)
            style = f"bold {tone}"
        if style != run_style and run:
            out.append(run, style=run_style)
            run = ""
        run_style = style
        run += ch
    if run:
        out.append(run, style=run_style)
    return out


def banner_text(art: list[str], tagline: str, sweep: int = -99,
                compact: bool = False) -> Text:
    """The boxed banner. The box is sized from the art, so it always fits."""
    width = max(len(line) for line in art)
    inner = max(width, 56) + 4
    out = Text(no_wrap=True)
    frame = f"bold {FRAME}"

    out.append("╔" + "═" * inner + "╗\n", style=frame)

    def body(content: Text) -> None:
        pad = inner - len(content.plain)
        left = pad // 2
        out.append("║", style=frame)
        out.append(" " * left)
        out.append_text(content)
        out.append(" " * (pad - left))
        out.append("║\n", style=frame)

    if compact:
        plate = Text(no_wrap=True)
        plate.append("▓▒░ ", style=f"bold {FRAME}")
        plate.append("S O M A", style=f"bold {PHOSPHOR}")
        plate.append(" ░▒▓", style=f"bold {FRAME}")
        plate.append("  ·  ", style=FAINT)
        plate.append(tagline, style=DIM)
        body(plate)
    else:
        body(Text(""))
        for row, line in enumerate(art):
            body(_art_line(line, row, sweep))
        body(Text(""))

    label = f" {tagline} "[: inner - 4]
    side = inner - len(label)
    left = side // 2
    out.append("╚" + "═" * left, style=frame)
    out.append(label, style=f"bold {darken(PHOSPHOR, 0.25)}")
    out.append("═" * (side - left) + "╝", style=frame)
    return out


class Banner(Static):
    """Boxed, colourised SOMA art. Docked by the parent, so it never moves."""

    def __init__(self) -> None:
        super().__init__(id="banner")
        self.art, self.tagline = load_banner()
        self.compact = False
        self.sweep = -99
        self.span = max(len(line) for line in self.art) + len(self.art) * 2 + 24
        self.styles.width = max(max(len(line) for line in self.art), 56) + 6

    @property
    def full_height(self) -> int:
        return len(self.art) + 4

    @property
    def height_now(self) -> int:
        return 3 if self.compact else self.full_height

    def on_mount(self) -> None:
        self.styles.height = self.height_now
        self.redraw()

    def redraw(self) -> None:
        self.update(banner_text(self.art, self.tagline, self.sweep, self.compact))

    def set_compact(self, compact: bool) -> None:
        if compact == self.compact:
            return
        self.compact = compact
        self.styles.height = self.height_now
        self.redraw()

    def tick(self) -> None:
        if self.compact:
            return
        self.sweep = (self.sweep + 1) % self.span - 12
        self.redraw()


# ── widgets ──────────────────────────────────────────────────────────────

STATE_LED = {
    "running": ("●", PHOSPHOR, "RUNNING"),
    "launched": ("◐", AMBER, "LAUNCHED"),
    "done": ("●", "#5eead4", "DONE"),
    "idle": ("○", FAINT, "IDLE"),
    "off": ("○", FAINT, "OFF"),
    "link": ("◇", None, "LINK"),
}


class Tile(Static):
    """A skeuomorphic card: titled bezel, status lamp, LCD readout, lit button.

    The bezel (border + title) always names the card. The lamp, the readout and
    the meter change with live state, so the card never loses its label.
    """

    can_focus = True

    def __init__(self, spec: Spec) -> None:
        super().__init__(id=f"tile-{spec.key}")
        self.spec = spec
        self.accent = ACCENTS.get(spec.key, AMBER)
        self.premier = spec.key in PREMIER
        self.state = "link" if spec.link else ("off" if spec.probe else "idle")
        self.stat = ""
        self.meter: Optional[float] = None
        self.glow = False

    @property
    def running(self) -> bool:
        return self.state == "running"

    def on_mount(self) -> None:
        self.border_title = f" {self.spec.title.upper()} "
        self.border_subtitle = f" {self.spec.short} "
        self._paint()
        self._draw()

    def set_info(self, state: str, stat: str, meter: Optional[float]) -> None:
        if (state, stat, meter) == (self.state, self.stat, self.meter):
            return
        changed = state != self.state
        self.state, self.stat, self.meter = state, stat, meter
        if changed:
            self._paint()
        self._draw()

    def pulse(self, glow: bool) -> None:
        if self.premier and glow != self.glow:
            self.glow = glow
            self._paint()

    def _paint(self) -> None:
        if self.has_focus:
            edge = lighten(self.accent, 0.55)
        elif self.premier and self.glow:
            edge = lighten(self.accent, 0.4)
        elif self.state in ("running", "launched"):
            edge = self.accent
        else:
            edge = darken(self.accent, 0.4)
        self.styles.border = ("double" if self.premier else "heavy", edge)
        tint = 0.14 if self.has_focus else (0.08 if self.state == "running" else 0.0)
        self.styles.background = blend(PANEL_BG, self.accent, tint)
        try:
            self.styles.border_title_color = lighten(self.accent, 0.3)
            self.styles.border_title_style = "bold"
            self.styles.border_subtitle_color = darken(self.accent, 0.1)
        except Exception:
            pass

    def on_focus(self, event: events.Focus) -> None:
        self._paint()

    def on_blur(self, event: events.Blur) -> None:
        self._paint()

    def _label(self) -> str:
        if self.spec.link:
            return "OPEN SITE"
        if self.running and self.spec.url:
            return "OPEN TAB"
        return "LAUNCH"

    def _draw(self) -> None:
        try:
            width = self.content_size.width or 30
            height = self.content_size.height or 5
        except Exception:
            width, height = 30, 5
        width = max(width, 12)

        led, led_color, word = STATE_LED[self.state]
        led_color = led_color or self.accent

        lamp = Text(no_wrap=True)
        lamp.append(f"{led} {word}", style=f"bold {led_color}")
        target = self.spec.target
        if width >= len(lamp.plain) + len(target) + 3:
            lamp.append(" " * (width - len(lamp.plain) - len(target)))
            lamp.append(target, style=FAINT)

        readout = (self.stat or self.spec.hint)[: width - 2]
        lcd = Text(no_wrap=True)
        lit = lighten(self.accent, 0.25) if self.stat else FAINT
        lcd.append(f" {readout.ljust(width - 2)} ", style=f"{lit} on {LCD_BG}")

        if self.meter is not None:
            cells = max(4, width - 7)
            filled = round(max(0.0, min(1.0, self.meter)) * cells)
            meter = Text(no_wrap=True)
            meter.append("▰" * filled, style=self.accent)
            meter.append("▱" * (cells - filled), style=darken(self.accent, 0.6))
            meter.append(f" {self.meter * 100:>3.0f}%", style=DIM)
        else:
            meter = Text(self.spec.hint[:width], style=FAINT, no_wrap=True)

        label = f" ▶ {self._label()} "
        lead = max(0, (width - len(label)) // 2)
        button = Text(no_wrap=True)
        button.append(" " * lead)
        button.append(label, style=f"bold {LCD_BG} on {self.accent}")

        if height >= 4:
            rows = [lamp, lcd, meter, button]
        elif height == 3:
            rows = [lamp, lcd, button]
        elif height == 2:
            rows = [lcd, button]
        else:
            rows = [lcd]

        text = Text(no_wrap=True)
        for i, row in enumerate(rows):
            if i:
                text.append("\n")
            text.append_text(row)
        self.update(text)

    def on_resize(self, event: events.Resize) -> None:
        self._draw()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        app = self.app
        if isinstance(app, SomaBoard):
            app.launch(self)


class LinkChip(Static):
    """A small labelled link. Opens its address in the browser; owns nothing."""

    can_focus = True

    def __init__(self, label: str, url: str, index: int) -> None:
        super().__init__(id=f"chip-{index}")
        self.label = label
        self.url = url

    def on_mount(self) -> None:
        self.update(Text(self.label, style=f"bold {AMBER_HI}"))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        app = self.app
        if isinstance(app, SomaBoard):
            app.open_link(self)


class UpdateCard(Static):
    """Small standalone card: run ./freshner-update on the engine server.

    It pulses so it reads as the one special control on the board. It is not
    a tile: it has no probe and no window of its own until it is confirmed.
    """

    can_focus = True

    def __init__(self) -> None:
        super().__init__(id="update-card")
        self.armed = False
        self.bright = False

    def on_mount(self) -> None:
        self._draw()
        self.set_interval(0.8, self._pulse)

    def _pulse(self) -> None:
        self.bright = not self.bright
        self.set_class(self.bright, "pulse")

    def set_armed(self, armed: bool) -> None:
        if armed == self.armed:
            return
        self.armed = armed
        self.set_class(armed, "armed")
        self._draw()

    def _draw(self) -> None:
        text = Text(justify="center")
        text.append("ENGINE UPDATE", style=f"bold {AMBER_HI}")
        text.append("\n")
        if self.armed:
            text.append("press again to run", style=f"bold {AMBER}")
        else:
            text.append("freshner-update · hetzner", style=DIM)
        self.update(text)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        app = self.app
        if isinstance(app, SomaBoard):
            app.engine_update()


class SyncStrip(Static):
    """Status and switch for the PEA delivery loop.

    Not a tile: the loop has no window of its own, so there is nothing to own
    its lifetime and nothing to close. It runs detached once started.
    """

    can_focus = True

    def __init__(self) -> None:
        super().__init__(id="sync")
        self.running = False
        self.pid: Optional[int] = None
        self.age = ""

    def on_mount(self) -> None:
        self._draw()

    def set_state(self, running: bool, pid: Optional[int], age: str) -> None:
        if (running, pid, age) == (self.running, self.pid, self.age):
            return
        self.running, self.pid, self.age = running, pid, age
        self.set_class(running, "running")
        self._draw()

    def _draw(self) -> None:
        width = self.size.width or 100
        text = Text()
        text.append(" PEA SYNC", style=f"bold {BONE}")
        text.append("   ", style=FAINT)

        if self.running:
            text.append("● ", style=GREEN)
            text.append("running", style=GREEN)
            if self.pid:
                text.append(f"   pid {self.pid}", style=DIM)
            if self.age:
                text.append(f"   up {self.age}", style=DIM)
        else:
            text.append("○ ", style=FAINT)
            text.append("off", style=FAINT)
            text.append("   press p to start", style=AMBER)

        if width >= 76:
            text.append("   ", style=FAINT)
            text.append("~/peacock/pea_sessions", style=FAINT)

        self.update(text)

    def on_resize(self, event: events.Resize) -> None:
        self._draw()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        app = self.app
        if isinstance(app, SomaBoard):
            app.start_sync()


# ── app ──────────────────────────────────────────────────────────────────

class SomaBoard(App):
    # Tile grid geometry is derived from SPECS in compose().
    # It used to be hardcoded to 2x2, which silently clipped the fifth tile:
    # the "soma" engine tile (run_sessions.py) was composed but off-screen,
    # so the board showed four tiles and the main app could not be launched.
    _COLS = 2

    CSS = """
    Screen {
        background: #080c10;
        color: #d7dee5;
    }

    #top {
        dock: top;
        height: 12;
        align: center top;
        background: #060a0e;
    }

    #grid {
        layout: grid;
        grid-gutter: 1 2;
        padding: 0 2;
        height: 1fr;
    }

    #updaterow {
        height: 4;
        padding: 0 2;
    }

    #updatespacer {
        width: 1fr;
    }

    UpdateCard {
        width: 30;
        height: 4;
        border: double #b8791f;
        background: #1b242d;
        content-align: center middle;
        text-align: center;
    }

    UpdateCard.pulse {
        border: double #ffd28c;
    }

    UpdateCard:focus {
        background: #2a3642;
        border: double #ffd28c;
    }

    UpdateCard.armed {
        border: double #4ade80;
    }

    #links {
        height: 3;
        padding: 0 2;
        background: #0c1116;
        border-top: solid #2a3642;
    }

    LinkChip {
        width: 1fr;
        height: 1;
        margin: 0 1 0 0;
        padding: 0 1;
        content-align: center middle;
        background: #141b22;
    }

    LinkChip:focus {
        background: #1b242d;
        text-style: underline;
    }

    #sync {
        height: 2;
        padding: 0 2;
        content-align: left middle;
        background: #0c1116;
        border-top: solid #2a3642;
    }

    #sync:focus {
        background: #1b242d;
    }

    #bar {
        height: 2;
        padding: 0 2;
        content-align: left middle;
        background: #141b22;
    }

    Tile {
        background: #0a0f14;
        border: heavy #2a3642;
        padding: 0 1;
        content-align: left middle;
        height: 100%;
        width: 100%;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit", priority=True),
        Binding("space", "launch_focused", "launch", priority=True),
        Binding("enter", "launch_focused", "launch", priority=True),
        Binding("right", "focus_tile(1)", "next", priority=True),
        Binding("down", "focus_tile(1)", "next", priority=True),
        Binding("left", "focus_tile(-1)", "prev", priority=True),
        Binding("up", "focus_tile(-1)", "prev", priority=True),
        Binding("1", "launch_key('droid-picker')", "", priority=True),
        Binding("2", "launch_key('goose-picker')", "", priority=True),
        Binding("3", "launch_key('turnkey')", "", priority=True),
        Binding("4", "launch_key('dashboard')", "", priority=True),
        Binding("5", "launch_key('tui')", "", priority=True),
        Binding("6", "launch_key('soma')", "", priority=True),
        Binding("7", "launch_key('pealogs')", "", priority=True),
        Binding("8", "launch_key('somalogs')", "", priority=True),
        Binding("u", "engine_update", "engine update", priority=True),
        Binding("p", "start_sync", "pea sync", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.notice = ""
        self.notice_at = 0.0
        self.states: dict[str, bool] = {}
        self.procs: dict[str, tuple] = {}
        self.opened: dict[str, float] = {}
        self.up_since: dict[str, float] = {}
        self.progress: Optional[dict] = None
        self.pea: tuple[int, Optional[float]] = (0, None)
        self._glow = False
        self._stop = False

    def compose(self) -> ComposeResult:
        with Container(id="top"):
            yield Banner()
        with Grid(id="grid"):
            for spec in SPECS:
                yield Tile(spec)
        with Horizontal(id="updaterow"):
            yield Static("", id="updatespacer")
            yield UpdateCard()
        with Horizontal(id="links"):
            for index, (label, url) in enumerate(SOMA_DOCS):
                yield LinkChip(label, url, index)
        yield SyncStrip()
        yield Static(self._bar_text(), id="bar")

    def on_mount(self) -> None:
        self._relayout()
        tiles = list(self.query(Tile))
        if tiles:
            self.set_focus(tiles[0])
        threading.Thread(target=self._stats_loop, daemon=True).start()
        self.refresh_states()
        self.set_interval(1.0, self.refresh_states)
        self.set_interval(0.08, self._tick_banner)
        self.set_interval(0.7, self._tick_glow)

    def on_unmount(self) -> None:
        self._stop = True

    def on_resize(self, event: events.Resize) -> None:
        self._relayout()

    def _relayout(self) -> None:
        """Columns follow the width; the banner shrinks on short terminals.

        A fixed 2x2 grid once clipped tiles off-screen, so the grid is always
        sized from the tile count.
        """
        width, height = self.size
        cols = 4 if width >= 150 else 2
        rows = max(1, -(-len(SPECS) // cols))
        try:
            grid = self.query_one("#grid", Grid)
            grid.styles.grid_size_columns = cols
            grid.styles.grid_size_rows = rows
            banner = self.query_one(Banner)
            banner.set_compact(height < 40)
            self.query_one("#top").styles.height = banner.height_now
        except Exception:
            pass

    def _tick_banner(self) -> None:
        try:
            self.query_one(Banner).tick()
        except Exception:
            pass

    def _tick_glow(self) -> None:
        self._glow = not self._glow
        for tile in self.query(Tile):
            tile.pulse(self._glow)

    # ── live stats ──

    def _stats_loop(self) -> None:
        """Polls off the UI thread: the dashboard's /progress and the PEA mirror."""
        n = 0
        while not self._stop:
            try:
                port = dashboard_port()
                if port_open(port, 0.2):
                    url = f"http://127.0.0.1:{port}/progress"
                    with urllib.request.urlopen(url, timeout=1.0) as resp:
                        self.progress = json.loads(resp.read())
                else:
                    self.progress = None
            except Exception:
                self.progress = None
            if n % 3 == 0:
                try:
                    dirs = [d for d in PEA_DEST.iterdir() if d.is_dir()]
                    newest = max((d.stat().st_mtime for d in dirs), default=None)
                    self.pea = (len(dirs), newest)
                except OSError:
                    pass
            n += 1
            time.sleep(2.0)

    def tile_info(self, spec: Spec) -> tuple[str, str, Optional[float]]:
        """State, readout and meter for one card, from real signals only."""
        now = time.monotonic()
        key = spec.key
        number = SPECS.index(spec) + 1
        proc = self.procs.get(key)
        alive = bool(proc) and proc[0].poll() is None
        since = format_age(now - proc[1]) if proc else ""

        running = self.is_running(spec)
        if running:
            self.up_since.setdefault(key, now)
        else:
            self.up_since.pop(key, None)
        up = format_age(now - self.up_since[key]) if running else ""

        if spec.link:
            opened = self.opened.get(key)
            if opened:
                return "link", f"opened {format_age(now - opened)} ago", None
            return "link", "engine.save-aichats.com", None

        if key == "soma":
            prog = self.progress
            if isinstance(prog, dict):
                sessions = prog.get("sessions") or []
                live = any((s or {}).get("status") == "running" for s in sessions)
                failed = sum(1 for s in sessions if (s or {}).get("status") == "failed")
                total = prog.get("total_sets", 0) or 0
                done = prog.get("done_sets", 0) or 0
                pct = float(prog.get("overall_pct", 0) or 0)
                stat = f"sets {done}/{total} · {pct:.0f}%"
                if failed:
                    stat += f" · {failed} failed"
                state = "running" if live else ("done" if total and done >= total else "idle")
                return state, stat, pct / 100.0
            if alive:
                return "launched", f"picker open · {since}", None
            return "idle", f"press {number} · ./run.sh", None

        if spec.probe is not None:
            ports = {"turnkey": 5173, "dashboard": dashboard_port(),
                     "pealogs": PEA_MANIFEST_PORT}
            count = self.pea[0]
            if running:
                stat = f"live :{ports.get(key, '')} · up {up}"
                if key == "pealogs":
                    stat += f" · {count} sess"
                return "running", stat, None
            if key == "pealogs":
                return "off", f"{count} sessions on disk", None
            return "off", f"not running · press {number}", None

        if alive:
            return "launched", f"window open · {since}", None
        if proc:
            return "idle", f"closed · ran {since}", None
        return "idle", f"press {number}", None

    # ── rendering ──

    def _bar_text(self) -> Text:
        text = Text()
        text.append(" ←→↑↓ move   space launch   click a tile   q quit", style=DIM)
        text.append("\n")

        fresh_notice = self.notice and (time.monotonic() - self.notice_at) < 5.0
        if fresh_notice:
            text.append(f" {self.notice}", style=AMBER)
            return text

        focused = self.focused
        if isinstance(focused, Tile):
            text.append(f" {focused.spec.hint}", style=FAINT)
        elif isinstance(focused, UpdateCard):
            text.append(f" ssh {ENGINE_SSH_HOST}: {ENGINE_UPDATE_DIR}/freshner-update"
                        " (restarts the engine)", style=FAINT)
        elif isinstance(focused, LinkChip):
            text.append(f" {focused.url}", style=FAINT)
        elif isinstance(focused, SyncStrip):
            if focused.running:
                text.append(" PEA delivery loop is running detached", style=FAINT)
            else:
                text.append(" start the PEA delivery loop", style=FAINT)
        else:
            text.append(" stop a service by closing its window", style=FAINT)
        return text

    def _refresh_bar(self) -> None:
        try:
            self.query_one("#bar", Static).update(self._bar_text())
        except Exception:
            pass

    def notice_line(self, message: str) -> None:
        self.notice = message
        self.notice_at = time.monotonic()
        self._refresh_bar()

    # ── state ──

    def is_running(self, spec: Spec) -> bool:
        if spec.probe is None:
            return False
        try:
            return bool(spec.probe())
        except Exception:
            return False

    def refresh_states(self) -> None:
        for spec in SPECS:
            state, stat, meter = self.tile_info(spec)
            self.states[spec.key] = state == "running"
            try:
                tile = self.query_one(f"#tile-{spec.key}", Tile)
            except Exception:
                continue
            tile.set_info(state, stat, meter)

        pid = sync_pid()
        try:
            strip = self.query_one("#sync", SyncStrip)
        except Exception:
            strip = None
        if strip is not None:
            age = format_age(_proc_age(pid)) if pid else ""
            strip.set_state(bool(pid), pid, age)

        self._refresh_bar()

    # ── actions ──

    def _focus_order(self) -> list:
        items: list = list(self.query(Tile))
        items.extend(self.query(UpdateCard))
        items.extend(self.query(LinkChip))
        try:
            items.append(self.query_one("#sync", SyncStrip))
        except Exception:
            pass
        return items

    def action_focus_tile(self, delta: int) -> None:
        items = self._focus_order()
        if not items:
            return
        focused = self.focused
        try:
            index = items.index(focused)  # type: ignore[arg-type]
        except ValueError:
            index = 0
        self.set_focus(items[(index + delta) % len(items)])

    def action_launch_focused(self) -> None:
        focused = self.focused
        if isinstance(focused, Tile):
            self.launch(focused)
        elif isinstance(focused, UpdateCard):
            self.engine_update()
        elif isinstance(focused, LinkChip):
            self.open_link(focused)
        elif isinstance(focused, SyncStrip):
            self.start_sync()

    def action_start_sync(self) -> None:
        self.start_sync()

    def start_sync(self) -> None:
        if os.environ.get("SOMA_TUI_DRY"):
            self.notice_line("dry run: would start the PEA sync")
            return

        pid = sync_pid()
        if pid:
            self.notice_line(f"PEA sync already running · pid {pid}")
            return

        if not SYNC_SCRIPT.exists():
            self.notice_line(f"missing {SYNC_SCRIPT}")
            return

        try:
            proc = subprocess.Popen(
                [sys.executable, str(SYNC_SCRIPT), "--watch",
                 "--dest", str(PEA_DEST)],
                cwd=str(SOMA_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self.notice_line(f"start failed: {exc}")
            return

        # The loop does not write its own pid file; the documented stop command
        # reads this one, so keep it truthful.
        try:
            SYNC_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
            SYNC_PIDFILE.write_text(f"{proc.pid}\n")
        except OSError:
            pass

        self.notice_line(f"PEA sync started · pid {proc.pid}")
        _sync_scan_cache["pid"] = proc.pid
        _sync_scan_cache["at"] = time.monotonic()
        self.refresh_states()

    def action_launch_key(self, key: str) -> None:
        spec = BY_KEY.get(key)
        if spec is None:
            return
        try:
            tile = self.query_one(f"#tile-{spec.key}", Tile)
        except Exception:
            return
        self.launch(tile)

    def action_engine_update(self) -> None:
        self.engine_update()

    def engine_update(self) -> None:
        """First press arms, a second press inside the window runs the update."""
        try:
            card = self.query_one("#update-card", UpdateCard)
        except Exception:
            return

        if os.environ.get("SOMA_TUI_DRY"):
            self.notice_line(f"dry run: would run {ENGINE_UPDATE_CMD}")
            return

        now = time.monotonic()
        if not getattr(self, "_update_armed_at", 0.0) or \
                now - self._update_armed_at > UPDATE_CONFIRM_SECONDS:
            self._update_armed_at = now
            card.set_armed(True)
            self.set_timer(UPDATE_CONFIRM_SECONDS, lambda: card.set_armed(False))
            self.notice_line("engine update armed: press again to run (restarts the engine)")
            return

        self._update_armed_at = 0.0
        card.set_armed(False)

        spec = Spec(
            key="engine-update",
            title="Engine Update",
            target="ssh · freshner-update",
            short="update",
            hint="do not close until it finishes",
            cwd=SOMA_DIR,
            command=ENGINE_UPDATE_CMD,
        )
        argv = terminal_argv("SOMA · Engine Update", window_inner(spec))
        if argv is None:
            self.notice_line("no terminal emulator found")
            return
        try:
            subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.notice_line(f"update launch failed: {exc}")
            return
        self.notice_line("engine update started: leave that window open until it finishes")

    def open_link(self, chip: LinkChip) -> None:
        if os.environ.get("SOMA_TUI_DRY"):
            self.notice_line(f"dry run: would open {chip.url}")
            return
        webbrowser.open(chip.url)
        self.notice_line(f"opened {chip.label}")

    def launch(self, tile: Tile) -> None:
        spec = tile.spec

        if os.environ.get("SOMA_TUI_DRY"):
            self.notice_line(f"dry run: would open {spec.title}")
            return

        # A link tile has no process behind it: just open the address.
        if spec.link:
            webbrowser.open(spec.link)
            self.opened[spec.key] = time.monotonic()
            self.notice_line(f"opened {spec.link}")
            return

        # Already up: bring the page back instead of starting a second copy.
        if self.is_running(spec):
            url = spec.url() if spec.url else None
            if url:
                webbrowser.open(url)
                self.notice_line(f"already running · opened {url}")
            else:
                self.notice_line(f"{spec.title} is already running")
            return

        argv = terminal_argv(f"SOMA · {spec.title}", window_inner(spec))
        if argv is None:
            self.notice_line("no terminal emulator found")
            return

        try:
            proc = subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.notice_line(f"launch failed: {exc}")
            return
        self.procs[spec.key] = (proc, time.monotonic())

        self.notice_line(f"launched {spec.title}")

        if spec.url is not None and spec.probe is not None:
            threading.Thread(
                target=self._open_when_ready,
                args=(spec,),
                daemon=True,
            ).start()

    def _open_when_ready(self, spec: Spec) -> None:
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            try:
                if spec.probe is not None and spec.probe():
                    url = spec.url() if spec.url else None
                    if url:
                        webbrowser.open(url)
                        self._notify_threadsafe(f"opened {url}")
                    return
            except Exception:
                pass
            time.sleep(0.5)
        self._notify_threadsafe(f"{spec.title}: did not come up within 40s")

    def _notify_threadsafe(self, message: str) -> None:
        try:
            self.call_from_thread(self.notice_line, message)
        except Exception:
            pass


# ── entry point ──────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="soma",
        description="SOMA machine room launcher",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the detected state of each service and exit",
    )
    parser.add_argument(
        "--dry",
        action="store_true",
        help="open the board but never spawn windows",
    )
    args = parser.parse_args()

    if args.dry:
        os.environ["SOMA_TUI_DRY"] = "1"

    if args.check:
        for spec in SPECS:
            if spec.probe is None:
                state = "n/a"
            else:
                state = "running" if spec.probe() else "off"
            print(f"{spec.key:<10} {state:<8} {spec.target}")

        pid = sync_pid()
        if pid:
            age = format_age(_proc_age(pid))
            detail = f"pid {pid}" + (f" up {age}" if age else "")
            print(f"{'sync':<10} {'running':<8} {detail}")
        else:
            print(f"{'sync':<10} {'off':<8} {SYNC_MIRROR}")
        return

    SomaBoard().run()


if __name__ == "__main__":
    main()
