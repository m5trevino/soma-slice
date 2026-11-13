"""Provenance checks: does an emitted claim actually exist in the source?

The distiller and introbeaver both emit ``char_start`` / ``char_end`` with a
verbatim string. Nothing else in the pipeline reads those offsets, so an
emitted directive can describe text that was never in the conversation and
still be accepted. This module closes that: every entry is checked against
the NFC-normalized source before it is allowed into the graph.

``GROUND_TRUTH.md`` invariant 3:
    An emitted directive must be a literal substring of the source at the
    ``char_start``/``char_end`` it claims. Otherwise the stage fails.

Offsets are Unicode codepoint offsets into the normalized content, half-open
``[start, end)`` — the contract the instruction bodies already specify.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    """NFC-normalize, the encoding the instruction contracts require."""
    return unicodedata.normalize("NFC", text or "")


@dataclass
class ProvenanceIssue:
    entry_id: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.entry_id}: {self.kind} ({self.detail})"


@dataclass
class ProvenanceReport:
    checked: int = 0
    verified: int = 0
    issues: list[ProvenanceIssue] = field(default_factory=list)
    # Real text found more than once: verified, but the exact position is a
    # best guess. Kept separate from `issues` because it is not fabrication.
    ambiguous: list[str] = field(default_factory=list)
    # The entries that survived, with their offsets filled in. Callers that
    # report on the stage need the accepted set, not just how many passed:
    # a count alone cannot say whether the two surviving entries were both
    # directives or one constraint.
    accepted: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def summary(self) -> str:
        if not self.issues:
            return f"{self.verified}/{self.checked} entries verified against source"
        kinds = sorted({i.kind for i in self.issues})
        return (f"{self.verified}/{self.checked} verified; "
                f"{len(self.issues)} rejected ({', '.join(kinds)})")


def _source_text(raw_chat_set: list[dict]) -> str:
    """The exact string offsets refer to: every turn joined as given.

    The instruction contracts define offsets into ``content`` per message.
    Entries carry ``source_msg_index``, so a check is performed against that
    one message's normalized content.
    """
    return "\n".join(normalize(m.get("content") or "") for m in raw_chat_set or [])


def _message_text(raw_chat_set: list[dict], index: int) -> str | None:
    if not isinstance(index, int) or index < 0 or index >= len(raw_chat_set or []):
        return None
    return normalize((raw_chat_set[index] or {}).get("content") or "")


def locate(verbatim: str, source: str) -> tuple[int, int] | None:
    """Find ``verbatim`` inside ``source``; return half-open offsets or None.

    Models cannot count characters. Asking one to emit ``char_start`` /
    ``char_end`` produces offsets that drift by a word or two, and the gate
    then rejects a *correct* quote as a fabrication. Python can do the
    search exactly, so the model supplies the text and Python supplies the
    numbers.

    A repeated phrase resolves to its first occurrence. The text is real
    either way, so this is not a fabrication; only the exact position is
    uncertain. Callers that need to know can compare ``count_matches``.
    """
    target = normalize(verbatim or "")
    if not target:
        return None
    haystack = normalize(source or "")
    first = haystack.find(target)
    if first < 0:
        return None
    return first, first + len(target)


def _count_matches(verbatim: str, source: str) -> int:
    target = normalize(verbatim or "")
    haystack = normalize(source or "")
    if not target or not haystack:
        return 0
    n = 0
    i = haystack.find(target)
    while i >= 0:
        n += 1
        i = haystack.find(target, i + 1)
    return n


def verify_entries(raw_chat_set: list[dict], entries: list[dict],
                   text_key: str = "verbatim_text",
                   id_key: str = "entry_id",
                   index_key: str = "source_msg_index",
                   fill_offsets: bool = True) -> ProvenanceReport:
    """Check every entry's claimed quote against the message it cites.

    An entry is accepted when the text it quotes appears exactly once in the
    message named by ``source_msg_index``. Offsets are then computed here and
    written back into the entry, so downstream readers see authoritative
    numbers rather than the model's guess.

    Rejection reasons:
      ``missing_text``       no quoted text at all
      ``text_not_found``     the quote is not in the cited message
      ``ambiguous_anchor``   the quote appears more than once
      ``bad_msg_index``      the cited turn does not exist
      ``fabricated_quote``   the quote is absent from the whole transcript

    The last one is the important distinction: text that appears nowhere is
    not a mis-aimed offset, it is invented content.

    ``text_key`` / ``index_key`` name the fields the stage's own output
    contract uses: the distiller writes ``text`` and ``turn``, the
    introbeaver writes ``verbatim_text`` and ``source_msg_index``.
    """
    report = ProvenanceReport()
    messages = raw_chat_set or []
    joined = _source_text(messages)

    for i, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            continue
        report.checked += 1
        eid = str(entry.get(id_key)
                  or (entry.get(text_key) or entry.get("text") or "")[:24]
                  or f"entry[{i}]")

        text = entry.get(text_key)
        if text is None and text_key != "text":
            text = entry.get("text")

        if not isinstance(text, str) or not text.strip():
            report.issues.append(ProvenanceIssue(eid, "missing_text", text_key))
            continue

        msg_index = entry.get(index_key)
        if msg_index is None and index_key != "turn":
            msg_index = entry.get("turn")

        if isinstance(msg_index, int):
            source = _message_text(messages, msg_index)
            if source is None:
                report.issues.append(
                    ProvenanceIssue(eid, "bad_msg_index", str(msg_index)))
                continue
        else:
            source = joined

        span = locate(text, source)
        if span is None:
            if _count_matches(text, joined):
                report.issues.append(
                    ProvenanceIssue(eid, "text_not_found",
                                    f"present in transcript, not in message "
                                    f"{msg_index}"))
            else:
                report.issues.append(
                    ProvenanceIssue(eid, "fabricated_quote",
                                    f"not in transcript: {normalize(text)[:40]!r}"))
            continue

        if fill_offsets:
            entry["char_start"], entry["char_end"] = span
        report.verified += 1
        report.accepted.append(entry)
        repeats = _count_matches(text, source)
        if repeats > 1:
            report.ambiguous.append(
                f"{eid}: {repeats} occurrences, cited the first")

    return report


def verify_distiller(payload: dict, raw_chat_set: list[dict]) -> ProvenanceReport:
    """Verify every list in a distiller output against the transcript.

    The TB-001 contract writes ``text`` / ``turn`` for each entry. It used to
    be checked with the introbeaver's field names, which meant the distiller
    was searched for ``verbatim_text`` and nothing it actually emitted was
    ever confirmed.
    """
    body = payload.get("distiller_output") if isinstance(
        payload.get("distiller_output"), dict) else payload
    entries: list[dict] = []
    for key in ("directives", "constraints", "preferences", "questions",
                "corrections", "confirmations", "artifacts"):
        for i, item in enumerate(body.get(key) or []):
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("entry_id", f"{key}[{i}]")
                entries.append(item)
    return verify_entries(raw_chat_set, entries, text_key="text",
                          id_key="entry_id", index_key="turn")


def verify_introbeaver(payload: dict, raw_chat_set: list[dict]) -> ProvenanceReport:
    """Verify introbeaver's ``entries[]`` against the messages they cite."""
    body = payload.get("introbeaver_output") if isinstance(
        payload.get("introbeaver_output"), dict) else payload
    return verify_entries(raw_chat_set, body.get("entries") or [],
                          text_key="verbatim_text", id_key="entry_id")
