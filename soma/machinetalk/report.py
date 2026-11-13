"""Reports: per-chatset per-stage .md artifacts, run manifest, live cards.

Layout under the run's output dir:

    <out>/
      stage1/<session>/<set>_distiller.md      (one per chatset per stage)
      stage1/<session>/<set>_introbeaver.md
      stage2/<session>_nexus.md ... stage4/<session>_olc.md
      skip_vault/<session>/<set>.md            (distiller SKIPs: evidence kept)
      checkpoints/<session>.checkpoint.json
      run_manifest.md                           (full proof of the whole run)

run_manifest.md records, for every LLM call: stage, chatset, model, attempt,
input hash, gate verdict, tokens, latency. That is what a later re-run of a
different model needs to reproduce a run.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .gate import GateResult
from .client import CallRecord


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _json_block(obj) -> str:
    try:
        return "```json\n" + json.dumps(obj, indent=2, ensure_ascii=False) + "\n```"
    except (TypeError, ValueError):
        return "```\n" + repr(obj) + "\n```"


# ---------------------------------------------------------------------------
# Per-stage artifact markdown
# ---------------------------------------------------------------------------

def stage_artifact_md(*, session_id: str, set_number: int | None, stage: str,
                      rec: CallRecord, gate: GateResult,
                      extra: dict | None = None) -> str:
    """Detailed .md for one stage result. This is the 'saved evidence' file."""
    L: list[str] = []
    title = f"Stage: {stage}"
    if set_number is not None:
        title += f" | ChatSet {set_number}"
    L.append(f"# {title}")
    L.append("")
    L.append(f"- **session**: `{session_id}`")
    L.append(f"- **chatset**: `{rec.chatset_id}`")
    L.append(f"- **timestamp**: {_now()}")
    L.append(f"- **status**: **{rec.status.upper()}**"
             + (f"  (verdict: `{gate.verdict}`)" if gate.verdict else ""))
    L.append(f"- **model (final)**: `{rec.model}`")
    L.append(f"- **tokens**: {rec.prompt_tokens} in / {rec.completion_tokens} out"
             f" / {rec.total_tokens} total")
    L.append(f"- **latency**: {rec.latency_ms:.0f} ms")
    if gate.skip_reason:
        L.append(f"- **skip_reason**: `{gate.skip_reason}`")
    if gate.exfil_type:
        L.append(f"- **exfil_type**: `{gate.exfil_type}`")
    if gate.signal_quality:
        L.append(f"- **signal_quality**: {gate.signal_quality}")
    L.append("")
    L.append("## Attempts")
    L.append("")
    L.append("| # | model | result | gate errors | ms |")
    L.append("|---|-------|--------|-------------|----|")
    for a in rec.attempts:
        res = a.get("gate_errors")
        res = f"{' ; '.join(res)}" if res else a.get("error", "PASS" if a is rec.attempts[-1] else "")
        L.append(f"| {a['attempt']} | `{a['model']}` | {a.get('status', res)} | {res} | {a.get('latency_ms', '')} |")
    L.append("")
    if gate.errors:
        L.append("## Gate Errors (final attempt)")
        L.append("")
        for e in gate.errors:
            L.append(f"- {e}")
        L.append("")
    if gate.parsed is not None:
        L.append("## Parsed Output (gate-verified)")
        L.append("")
        L.append(_json_block(gate.parsed))
        L.append("")
    if extra:
        for k, v in extra.items():
            L.append(f"## {k}")
            L.append("")
            if isinstance(v, (dict, list)):
                L.append(_json_block(v))
            else:
                L.append(str(v))
            L.append("")
    L.append("## Raw Response")
    L.append("")
    L.append("```")
    L.append(rec.raw_response[:20000] or "(empty)")
    L.append("```")
    L.append("")
    return "\n".join(L)


def skip_vault_md(*, session_id: str, set_number: int, gate: GateResult,
                  rec: CallRecord, transcript_head: str = "") -> str:
    """Evidence kept for a distiller-SKIP ChatSet (no downstream tokens)."""
    L = [f"# Skip Vault | {session_id} ChatSet {set_number}", ""]
    L.append(f"- **timestamp**: {_now()}")
    L.append(f"- **model**: `{rec.model}`")
    L.append(f"- **verdict**: SKIP")
    L.append(f"- **skip_reason**: `{gate.skip_reason}`")
    L.append(f"- **exfil_type**: `{gate.exfil_type}`")
    L.append(f"- **signal_quality**: {gate.signal_quality}")
    if gate.parsed:
        L.append("")
        L.append("## Distiller Output")
        L.append("")
        L.append(_json_block(gate.parsed))
    if transcript_head:
        L.append("")
        L.append("## Transcript (first 2000 chars, for audit)")
        L.append("")
        L.append("```")
        L.append(transcript_head[:2000])
        L.append("```")
    L.append("")
    return "\n".join(L)


def artifact_path(out_dir: Path, session_id: str, stage: str,
                  set_number: int | None, group: str) -> Path:
    if set_number is not None:
        return out_dir / "stage1" / session_id / f"{set_number}_{stage}.md"
    return out_dir / group / f"{session_id}_{stage}.md"


def write_artifact(out_dir: Path, session_id: str, stage: str,
                   set_number: int | None, rec: CallRecord, gate: GateResult,
                   group: str = "", extra: dict | None = None) -> Path:
    group = group or ("stage1" if set_number is not None else "stage2")
    p = artifact_path(out_dir, session_id, stage, set_number, group)
    _atomic_write(p, stage_artifact_md(session_id=session_id,
                                       set_number=set_number, stage=stage,
                                       rec=rec, gate=gate, extra=extra))
    return p


def write_skip_vault(out_dir: Path, session_id: str, set_number: int,
                     gate: GateResult, rec: CallRecord,
                     transcript_head: str = "") -> Path:
    p = out_dir / "skip_vault" / session_id / f"{set_number}.md"
    _atomic_write(p, skip_vault_md(session_id=session_id, set_number=set_number,
                                   gate=gate, rec=rec,
                                   transcript_head=transcript_head))
    return p


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------

class Manifest:
    """Accumulates every call; renders run_manifest.md on finish."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = Path(out_dir)
        self.rows: list[dict] = []

    def add(self, rec: CallRecord, gate: GateResult) -> None:
        self.rows.append({
            "ts": _now(),
            "session": rec.session_id,
            "chatset": rec.chatset_id,
            "stage": rec.stage,
            "model": rec.model,
            "attempts": len(rec.attempts),
            "models": " -> ".join(a["model"] for a in rec.attempts),
            "verdict": gate.verdict or rec.status,
            "status": rec.status,
            "tokens": rec.total_tokens,
            "latency_ms": round(rec.latency_ms, 1),
            "errors": "; ".join(gate.errors)[:200],
        })

    def render(self, header: dict | None = None) -> Path:
        L = ["# Run Manifest", ""]
        if header:
            for k, v in header.items():
                L.append(f"- **{k}**: {v}")
            L.append("")
        L.append(f"Calls: {len(self.rows)}")
        L.append("")
        L.append("| time | session | chatset | stage | model (final) | attempts | models tried | verdict | tokens | ms |")
        L.append("|------|---------|---------|-------|---------------|----------|--------------|---------|--------|----|")
        for r in self.rows:
            L.append(f"| {r['ts']} | `{r['session']}` | `{r['chatset']}` | {r['stage']} "
                     f"| `{r['model']}` | {r['attempts']} | {r['models']} "
                     f"| {r['verdict']} | {r['tokens']} | {r['latency_ms']} |")
        L.append("")
        total_tok = sum(r["tokens"] for r in self.rows)
        L.append(f"**Total tokens**: {total_tok}")
        L.append("")
        p = self.out_dir / "run_manifest.md"
        _atomic_write(p, "\n".join(L))
        return p


# ---------------------------------------------------------------------------
# Live console cards
# ---------------------------------------------------------------------------

# When run_engine.py puts soma/ on sys.path, 'pipeline' is top-level.
# When imported as package (systems_check), use relative import.
try:
    from pipeline.compact_cards import (
        print_compact_card,
        print_compact_card_done,
        print_compact_card_start,
    )
except ImportError:
    from ..pipeline.compact_cards import (
        print_compact_card,
        print_compact_card_done,
        print_compact_card_start,
    )


def _bar(width: int, frac: float) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(width * frac)
    return "#" * n + "-" * (width - n)


def card_stage1_start(worker: int, session: str, set_number: int,
                      model: str, chars: int, h: str, total: int) -> str:
    return ""  # header handled by engine; no per-chatset start output


def card_session_stage(session: str, stage: str, status: str,
                       rec: CallRecord, gate: GateResult) -> str:
    mark = "✓" if gate.ok else ("⊘" if rec.status == "blocked" else "✗")
    v = gate.verdict or rec.status
    return (
        f"  {mark} [{session}] {stage}  {v}  "
        f"{rec.total_tokens} tok  {rec.latency_ms / 1000:.1f}s  "
        f"model={rec.model}"
        + (f"  ({gate.reasons[:60]})" if gate.errors else "")
    )


def status_table(ck, sessions: list) -> str:
    """--status output: one line per session, per stage group."""
    from .checkpoint import STAGE1, STAGE234
    L = ["", "PENDING | RUNNING | DONE | SKIPPED | BLOCKED | FAILED", ""]
    for s in sessions:
        sid = s.session_id
        data = ck.load(sid)
        cs_rows = data.get("chatsets", {})
        for stage in STAGE1:
            counts: dict[str, int] = {}
            for cs in cs_rows.values():
                st = cs["stages"].get(stage, {}).get("status", "PENDING")
                counts[st] = counts.get(st, 0) + 1
            line = " ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
            L.append(f"{sid}  {stage:<12} {line or '-'}")
        for stage in STAGE234:
            st = data.get("session_stages", {}).get(stage, {}).get("status", "PENDING")
            L.append(f"{sid}  {stage:<12} {st}")
        L.append("")
    return "\n".join(L)
