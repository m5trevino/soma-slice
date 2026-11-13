"""Unit tests for TB-OBS-010: a transient failure is not given time to clear.

``_call_with_retry`` used to sleep ``1.5 * (attempt + 1)`` seconds between
attempts, so a 429 or a 5xx had a window in which to recover. That wait is
gone. A failure now costs one deck rotation and nothing else: the next card is
tried immediately, and if every card fails the stage fails at once instead of
sitting out a backoff that the run never accounted for.

What must not change:

  - the number of attempts, still ten by default
  - the deck rotation, which happens inside the SDK call
  - the DNS shortcut, which still aborts after two unresolvable attempts
  - the retry line that names the abandoned model, which is the evidence
    that rotation happened
"""
from __future__ import annotations

import time

import pytest

from machinetalk.client import EngineClient

MESSAGES = [{"role": "user", "content": "hello"}]
DNS_TEXT = "Name or service not known"


class FakeTransport:
    """Stands in for the TurnKey SDK call, with a scripted outcome per attempt.

    The transport is the only thing the retry loop can reach, so scripting it
    is enough to drive the loop without a network or a profile on disk.
    """

    def __init__(self, outcomes, model: str = "deepseek-v4") -> None:
        self.outcomes = list(outcomes)
        self.model = model
        self.calls: list[str] = []

    def call(self, stage, messages, pea_session=None, temperature=None):
        self.calls.append(stage)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _client(transport: FakeTransport, models=None) -> EngineClient:
    """An EngineClient whose SDK is the fake transport.

    ``model_for`` is replaced too: it would otherwise ask a rotator that was
    never loaded, and the retry line only needs some name to print.
    """
    holder = models if models is not None else ["model-a", "model-b",
                                                 "model-c"]
    client = object.__new__(EngineClient)
    client.timeout = 180.0

    class _TK:
        pass

    tk = _TK()
    tk.call = transport.call
    client._tk = tk
    counter = {"n": 0}

    def model_for(stage: str) -> str:
        index = min(counter["n"], len(holder) - 1)
        counter["n"] += 1
        return holder[index]

    client.model_for = model_for  # type: ignore[method-assign]
    return client


# ── no wait between attempts ───────────────────────────────────────────────

def test_two_failures_then_success_reaches_the_third_attempt(monkeypatch):
    """AC-001: a transient failure is retried at once, not after a sleep."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    transport = FakeTransport([ConnectionError("503"),
                               TimeoutError("timed out"),
                               "third-attempt-result"])
    client = _client(transport)

    result = client._call_with_retry("distiller", MESSAGES)

    assert result == "third-attempt-result"
    assert transport.calls == ["distiller"] * 3
    assert slept == []


def test_no_sleep_call_remains_in_the_retry_path(monkeypatch):
    """Every card in the deck may be burned without a single wait."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    transport = FakeTransport([ConnectionError("429") for _ in range(9)]
                              + ["tenth-attempt-result"])
    client = _client(transport, models=[f"model-{i}" for i in range(10)])

    assert client._call_with_retry("distiller", MESSAGES) == "tenth-attempt-result"
    assert len(transport.calls) == 10
    assert slept == []


def test_a_failing_stage_fails_without_waiting(monkeypatch):
    """Exhausting the deck is a fast failure now, not a slow one."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    transport = FakeTransport([ConnectionError("503") for _ in range(10)])
    client = _client(transport, models=[f"model-{i}" for i in range(10)])

    with pytest.raises(ConnectionError):
        client._call_with_retry("distiller", MESSAGES)

    assert len(transport.calls) == 10
    assert slept == []


# ── what the slice must not change ─────────────────────────────────────────

def test_attempt_count_is_unchanged(monkeypatch):
    """Ten attempts by default, exactly as before the delay was removed."""
    monkeypatch.setattr(time, "sleep", lambda s: None)

    transport = FakeTransport([ConnectionError("503") for _ in range(10)])
    client = _client(transport, models=[f"model-{i}" for i in range(10)])

    with pytest.raises(ConnectionError):
        client._call_with_retry("distiller", MESSAGES)

    assert len(transport.calls) == 10


def test_the_abandoned_model_is_still_named(monkeypatch, capsys):
    """Rotation is only visible if each failed attempt names its model."""
    monkeypatch.setattr(time, "sleep", lambda s: None)

    transport = FakeTransport([ConnectionError("503"), "ok"])
    client = _client(transport, models=["model-a", "model-b"])

    client._call_with_retry("distiller", MESSAGES)

    err = capsys.readouterr().err
    assert "attempt 1/10" in err
    assert "model=model-a" in err
    assert "ConnectionError" in err


def test_a_dns_failure_still_aborts_after_two_attempts(monkeypatch):
    """AC-002: the unrecoverable shortcut is not the waiting this slice removed."""
    monkeypatch.setattr(time, "sleep", lambda s: None)

    transport = FakeTransport([ConnectionError(DNS_TEXT),
                               ConnectionError(DNS_TEXT),
                               "never-reached"])
    client = _client(transport)

    with pytest.raises(ConnectionError):
        client._call_with_retry("distiller", MESSAGES)

    assert len(transport.calls) == 2


def test_a_single_dns_failure_still_gets_its_second_chance(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    transport = FakeTransport([ConnectionError(DNS_TEXT), "recovered"])
    client = _client(transport)

    assert client._call_with_retry("distiller", MESSAGES) == "recovered"
    assert len(transport.calls) == 2
