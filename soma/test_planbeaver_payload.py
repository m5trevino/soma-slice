"""PlanBeaver and OutroBeaver must receive raw_chat_set.

Their instructions list raw_chat_set as a required key and answer
INVALID_INPUT without it. The engine used to drop it for every stage after the
distiller, so planbeaver failed on its first check whatever model served it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import machinetalk.engine as engine_mod

RAW = [{"role": "USER", "content": "hello"}]


def _payload(stage: str) -> dict:
    cs = SimpleNamespace(session_id="s", chatset_id="s:1", chatset_hash="h",
                         raw_chat_set=RAW)
    fake = SimpleNamespace(_upstream_output=lambda _cs, _stage: {"k": "v"})
    return engine_mod.Engine._beaver_payload(fake, cs, stage=stage)


@pytest.mark.parametrize("stage", ["distiller", "planbeaver", "outrobeaver"])
def test_stages_that_require_the_transcript_get_it(stage):
    assert _payload(stage)["raw_chat_set"] == RAW


def test_introbeaver_still_gets_only_the_distiller_output():
    p = _payload("introbeaver")
    assert "raw_chat_set" not in p
    assert p["distiller_output"] == {"k": "v"}
    assert p["upstream_stage"] == "distiller"


def test_planbeaver_keeps_its_upstream_output():
    p = _payload("planbeaver")
    assert p["introbeaver_output"] == {"k": "v"}
    assert p["upstream_stage"] == "introbeaver"
