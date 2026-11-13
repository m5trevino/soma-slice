"""Unit tests for TB-OBS-004: cache and input-size telemetry.

DeepSeek and Qwen report prefix-cache accounting in the usage block. Before
this slice the engine read ``usage`` and kept only prompt/completion/total
tokens, so there was no way to tell from our own logs whether prompting was
hitting the cache. Now every call records:

  - ``cache_hit_tokens``  from ``usage.prompt_cache_hit_tokens``
  - ``cache_miss_tokens`` from ``usage.prompt_cache_miss_tokens``
  - ``chars_in``          counted locally from the outgoing messages

What must not change: one request per call (no retry, no sleep), and no
message text anywhere on the usage record.
"""
from __future__ import annotations

import pytest

from machinetalk.client import (CallRecord, EngineClient, StageResult,
                                TurnKeyClient)


class FakeSDK:
    """Stand-in for the TurnKey SDK. Records calls, returns a canned reply."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def run_stage(self, stage: str, messages: list[dict], **kwargs) -> dict:
        self.calls.append({"stage": stage, "messages": messages, "kwargs": kwargs})
        return self.reply


def _client(reply: dict) -> tuple[TurnKeyClient, FakeSDK]:
    """A TurnKeyClient wired to a fake SDK, bypassing profile loading."""
    client = object.__new__(TurnKeyClient)
    sdk = FakeSDK(reply)
    client._sdk = sdk
    client._pea_kw = False
    return client, sdk


def _reply(usage: dict | None, content: str = "ok") -> dict:
    raw = {"model": "deepseek-v4", "choices": [{"message": {"content": content}}]}
    if usage is not None:
        raw["usage"] = usage
    return raw


MESSAGES = [
    {"role": "system", "content": "abc"},
    {"role": "user", "content": "12345"},
]
MESSAGES_CHARS = len("abc") + len("12345")


# ── defaults ────────────────────────────────────────────────────────────────

def test_stage_result_defaults_to_zero():
    res = StageResult("x")
    assert res.cache_hit_tokens == 0
    assert res.cache_miss_tokens == 0
    assert res.chars_in == 0


def test_call_record_defaults_to_zero():
    rec = CallRecord()
    assert rec.cache_hit_tokens == 0
    assert rec.cache_miss_tokens == 0
    assert rec.chars_in == 0


def test_existing_token_fields_keep_their_meaning():
    res = StageResult("x", prompt_tokens=11, completion_tokens=2, total_tokens=13)
    assert (res.prompt_tokens, res.completion_tokens, res.total_tokens) == (11, 2, 13)


# ── present: the exact provider numbers are kept ────────────────────────────

def test_cache_tokens_are_read_when_present():
    client, _ = _client(_reply({
        "prompt_tokens": 900,
        "completion_tokens": 40,
        "total_tokens": 940,
        "prompt_cache_hit_tokens": 768,
        "prompt_cache_miss_tokens": 132,
    }))
    res = client.call("distiller", MESSAGES)
    assert res.cache_hit_tokens == 768
    assert res.cache_miss_tokens == 132


def test_cache_hit_of_zero_is_not_treated_as_missing():
    client, _ = _client(_reply({
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 900,
    }))
    res = client.call("distiller", MESSAGES)
    assert res.cache_hit_tokens == 0
    assert res.cache_miss_tokens == 900


@pytest.mark.parametrize("hit,miss", [("768", "132"), (768.0, 132.0)])
def test_numeric_strings_and_floats_become_ints(hit, miss):
    client, _ = _client(_reply({
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
    }))
    res = client.call("distiller", MESSAGES)
    assert (res.cache_hit_tokens, res.cache_miss_tokens) == (768, 132)


# ── absent: a provider that reports nothing yields 0, never a raise ─────────

def test_absent_cache_keys_yield_zero():
    client, _ = _client(_reply({
        "prompt_tokens": 900,
        "completion_tokens": 40,
        "total_tokens": 940,
    }))
    res = client.call("distiller", MESSAGES)
    assert res.cache_hit_tokens == 0
    assert res.cache_miss_tokens == 0


def test_no_usage_block_yields_zero():
    client, _ = _client(_reply(None))
    res = client.call("distiller", MESSAGES)
    assert (res.cache_hit_tokens, res.cache_miss_tokens) == (0, 0)


def test_null_usage_values_yield_zero():
    client, _ = _client(_reply({
        "prompt_cache_hit_tokens": None,
        "prompt_cache_miss_tokens": None,
    }))
    res = client.call("distiller", MESSAGES)
    assert (res.cache_hit_tokens, res.cache_miss_tokens) == (0, 0)


def test_unparseable_usage_values_yield_zero():
    client, _ = _client(_reply({
        "prompt_cache_hit_tokens": "n/a",
        "prompt_cache_miss_tokens": {"tokens": 5},
    }))
    res = client.call("distiller", MESSAGES)
    assert (res.cache_hit_tokens, res.cache_miss_tokens) == (0, 0)


def test_usage_that_is_not_a_dict_yields_zero():
    client, _ = _client(_reply(None) | {"usage": "not-a-dict"})
    res = client.call("distiller", MESSAGES)
    assert (res.cache_hit_tokens, res.cache_miss_tokens) == (0, 0)


# ── chars_in is measured locally, before the request ────────────────────────

def test_chars_in_counts_the_outgoing_messages():
    client, _ = _client(_reply(None))
    res = client.call("distiller", MESSAGES)
    assert res.chars_in == MESSAGES_CHARS


def test_chars_in_survives_a_provider_that_reports_no_usage():
    """Only a local count can exist when the provider says nothing."""
    client, _ = _client(_reply(None))
    res = client.call("distiller", MESSAGES)
    assert res.chars_in == MESSAGES_CHARS
    assert res.prompt_tokens == 0


def test_chars_in_is_computed_from_the_messages_actually_sent():
    client, sdk = _client(_reply(None))
    sent = [{"role": "user", "content": "x" * 50}]
    res = client.call("distiller", sent)
    assert sdk.calls[0]["messages"] is sent
    assert res.chars_in == 50


def test_chars_in_ignores_non_string_content():
    client, _ = _client(_reply(None))
    res = client.call("distiller", [
        {"role": "user", "content": "ab"},
        {"role": "user", "content": [{"type": "text", "text": "cdef"}]},
        {"role": "user"},
        "not-a-message",
    ])
    assert res.chars_in == 2


def test_empty_messages_are_zero_chars():
    client, _ = _client(_reply(None))
    assert client.call("distiller", []).chars_in == 0


# ── measurement does not touch the request ──────────────────────────────────

def test_one_request_per_call():
    client, sdk = _client(_reply({"prompt_cache_hit_tokens": 1}))
    client.call("distiller", MESSAGES)
    assert len(sdk.calls) == 1


def test_measurement_adds_no_extra_kwargs_to_the_request():
    client, sdk = _client(_reply(None))
    client.call("distiller", MESSAGES)
    assert sdk.calls[0]["kwargs"] == {}


def test_messages_are_passed_through_unchanged():
    client, sdk = _client(_reply(None))
    client.call("distiller", MESSAGES)
    assert sdk.calls[0]["messages"] == MESSAGES


# ── threading onto the CallRecord built by call_stage ───────────────────────

class _FakeSoma:
    def get_stage_instruction(self, stage: str) -> dict:
        return {"body": "SYSTEM PROMPT"}


def _engine(reply: dict) -> tuple[EngineClient, FakeSDK]:
    """EngineClient whose TurnKey layer is a fake SDK."""
    engine = object.__new__(EngineClient)
    tk, sdk = _client(reply)
    engine._tk = tk
    engine.timeout = 180.0
    return engine, sdk


def test_call_stage_threads_cache_and_chars_onto_the_record():
    engine, _ = _engine(_reply(
        {"prompt_cache_hit_tokens": 640, "prompt_cache_miss_tokens": 60},
        content='{"draft_artifact": "d"}'))
    res = engine.call_stage("drafter", session_id="s1", chatset_id="c1",
                            chunk="payload text", soma=_FakeSoma())
    rec = res.record
    assert rec.cache_hit_tokens == 640
    assert rec.cache_miss_tokens == 60
    assert rec.chars_in == len("SYSTEM PROMPT") + len("payload text")


def test_call_stage_records_zero_when_the_provider_omits_the_keys():
    engine, _ = _engine(_reply(None, content='{"draft_artifact": "d"}'))
    rec = engine.call_stage("drafter", session_id="s1", chatset_id="c1",
                            chunk="payload text", soma=_FakeSoma()).record
    assert (rec.cache_hit_tokens, rec.cache_miss_tokens) == (0, 0)
    assert rec.chars_in == len("SYSTEM PROMPT") + len("payload text")


def test_call_stage_still_counts_one_request():
    engine, sdk = _engine(_reply(None, content='{"draft_artifact": "d"}'))
    engine.call_stage("drafter", session_id="s1", chatset_id="c1",
                      chunk="payload text", soma=_FakeSoma())
    assert len(sdk.calls) == 1


def test_record_fields_stay_typed_and_carry_no_message_text():
    prompt = "UNIQUE-PROMPT-TEXT-7c1f"
    engine, _ = _engine(_reply(
        {"prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 4},
        content='{"draft_artifact": "d"}'))
    rec = engine.call_stage("drafter", session_id="s1", chatset_id="c1",
                            chunk=prompt, soma=_FakeSoma()).record
    for value in (rec.cache_hit_tokens, rec.cache_miss_tokens, rec.chars_in):
        assert isinstance(value, int) and not isinstance(value, bool)
        assert prompt not in str(value)
