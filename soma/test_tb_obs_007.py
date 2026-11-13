"""Unit tests for TB-OBS-007: code and cache columns on the existing dashboard.

TB-OBS-005 put ``session_code``, ``stage_code``, ``cache_hit_tokens`` and
``cache_miss_tokens`` on the debug event, but the dashboard had nowhere to show
them. The stage table is an addition to the sessions tab the page already had:
no new file, no new port, no second HTML page.

Two rules decide whether that table is correct:

  - a row is keyed on ``session_code + ":" + stage_code``, so re-reading the
    same JSONL replaces the row instead of appending a duplicate;
  - a missing key renders as ``-`` and never raises, because a JSONL file
    written before TB-OBS-005 has no codes on its events at all.
"""
from __future__ import annotations

import json

import pytest

import machinetalk.debug_dashboard_skeu as skeu
from machinetalk.debug_dashboard_skeu import MISSING_CELL, load_event_rows, row_key, stage_rows

NEW_EVENT = {
    "session_code": "SES-001",
    "stage_code": "DI-01",
    "stage": "distiller",
    "session_id": "9f2c74a1-uuid",
    "chatset_number": 1,
    "status": "done",
    "cache_hit_tokens": 768,
    "cache_miss_tokens": 132,
}

OLD_EVENT = {
    "stage": "distiller",
    "session_id": "9f2c74a1-uuid",
    "chatset_number": 1,
    "chatset_id": "9f2c74a1-uuid:1",
    "status": "done",
}


# ── the module still loads ──────────────────────────────────────────────────

def test_dashboard_module_imports():
    assert skeu is not None
    assert callable(skeu.start_dashboard)


def test_the_html_is_one_document_and_carries_the_columns():
    html = skeu._DASHBOARD_HTML
    assert html.count("<!DOCTYPE html>") == 1
    assert "<th>Code</th>" in html
    assert "<th>Cache Hit</th>" in html
    assert "<th>Cache Miss</th>" in html
    assert 'id="stages-body"' in html


def test_the_page_keys_rows_by_the_same_rule_as_the_module():
    html = skeu._DASHBOARD_HTML
    assert "function rowKey(e)" in html
    assert "session_code + ':' + e.stage_code" in html


def test_missing_cells_render_as_a_dash():
    assert MISSING_CELL == "-"
    html = skeu._DASHBOARD_HTML
    assert "function cell(v)" in html
    assert "'-'" in html


# ── the row key is stable ──────────────────────────────────────────────────

def test_same_event_gives_the_same_key_every_time():
    first = row_key(NEW_EVENT)
    second = row_key(dict(NEW_EVENT))
    assert first == second
    assert first == "SES-001:DI-01"


def test_key_reads_the_codes_and_not_the_uuid():
    key = row_key(NEW_EVENT)
    assert "SES-001" in key
    assert "9f2c74a1-uuid" not in key


def test_a_different_stage_code_gives_a_different_key():
    later = dict(NEW_EVENT, stage_code="IB-01", stage="introbeaver")
    assert row_key(NEW_EVENT) != row_key(later)


def test_a_different_session_code_gives_a_different_key():
    other = dict(NEW_EVENT, session_code="SES-002")
    assert row_key(NEW_EVENT) != row_key(other)


# ── older events do not throw ──────────────────────────────────────────────

def test_event_without_the_new_keys_still_gets_a_stable_key():
    first = row_key(OLD_EVENT)
    assert first == row_key(dict(OLD_EVENT))
    assert "distiller" in first


def test_event_with_no_keys_at_all_does_not_throw():
    assert isinstance(row_key({}), str)
    assert isinstance(row_key(None), str)


def test_new_and_old_events_for_the_same_stage_stay_distinct():
    assert row_key(NEW_EVENT) != row_key(OLD_EVENT)


# ── re-reading the JSONL replaces instead of appending ─────────────────────

def test_stage_rows_collapses_a_repeated_event():
    rows = stage_rows([NEW_EVENT, dict(NEW_EVENT)])
    assert len(rows) == 1


def test_stage_rows_keeps_the_last_read():
    failed = dict(NEW_EVENT, status="failed", cache_hit_tokens=0)
    rows = stage_rows([NEW_EVENT, failed])
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"


def test_stage_rows_keeps_distinct_stages():
    rows = stage_rows([NEW_EVENT, dict(NEW_EVENT, stage_code="IB-01", stage="introbeaver")])
    assert len(rows) == 2


def test_stage_rows_tolerates_non_dict_entries():
    assert stage_rows([NEW_EVENT, None, "junk"]) == [NEW_EVENT]


def test_load_event_rows_reads_a_file_and_dedupes(tmp_path):
    path = tmp_path / "events.jsonl"
    lines = [json.dumps(NEW_EVENT), "", "not json", json.dumps(NEW_EVENT), json.dumps(OLD_EVENT)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = load_event_rows(path)
    assert len(rows) == 2
    assert {row_key(r) for r in rows} == {row_key(NEW_EVENT), row_key(OLD_EVENT)}


def test_load_event_rows_on_a_missing_file_returns_empty(tmp_path):
    assert load_event_rows(tmp_path / "absent.jsonl") == []


# ── the cache figures are read verbatim ────────────────────────────────────

@pytest.mark.parametrize("hit,miss", [(768, 132), (0, 900), (1024, 0)])
def test_cache_figures_survive_into_the_row(hit, miss):
    rows = stage_rows([dict(NEW_EVENT, cache_hit_tokens=hit, cache_miss_tokens=miss)])
    assert (rows[0]["cache_hit_tokens"], rows[0]["cache_miss_tokens"]) == (hit, miss)
