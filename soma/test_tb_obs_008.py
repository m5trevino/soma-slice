"""Unit tests for TB-OBS-008: the deck is described, and its absence is fatal.

The PEA corpus shows the same free-tier model failing 45 times on quota while
the run continued. Nothing on the record said which deck was in play, so a run
could not be interpreted afterwards. Loading a profile now prints one line:

    deck: <path>  models=<N>  stages_pinned=<N>

and a profile that is missing, unreadable or not valid JSON raises with the
path in the message. There is no default deck: guessing one would produce
exactly the uninterpretable run this slice exists to prevent.

The profile is opened read-only. ``turn-key-profile/`` is never written.
"""
from __future__ import annotations

import json

import pytest

from machinetalk.client import (ProfileError, TurnKeyClient, describe_deck)

PROFILE = {
    "version": "1.0.0",
    "name": "test-deck",
    "deck": [
        {"uid": "card-1", "modelId": "deepseek-v4-pro:0813-cloud",
         "modelName": "deepseek-v4-pro:0813-cloud", "provider": "ollama"},
        {"uid": "card-2", "modelId": "nvidia/nemotron-3.5-lightning:free",
         "modelName": "nvidia/nemotron-3.5-lightning:free",
         "provider": "openrouter"},
        {"uid": "card-3", "modelId": "inclusionai/ling-3.1-flash",
         "modelName": "inclusionai/ling-3.1-flash", "provider": "novita"},
    ],
    "stages": [
        {"stageName": "distiller", "pinnedModels": [], "useDeck": True},
        {"stageName": "introbeaver", "pinnedModels": [], "useDeck": True},
        {"stageName": "planbeaver",
         "pinnedModels": ["inclusionai/ling-3.1-flash"], "useDeck": True},
    ],
    "pipeline": {"mode": "sequential", "workers": 1, "timeout": 180,
                 "maxRetries": 10},
}


def _write(tmp_path, payload, name="deck.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── a valid profile describes correctly ────────────────────────────────────

def test_a_valid_profile_reports_path_name_models_and_stages(tmp_path):
    path = _write(tmp_path, PROFILE)
    deck = describe_deck(path)

    assert deck["path"] == str(path)
    assert deck["name"] == "test-deck"
    assert deck["models"] == 3
    assert deck["stages"] == ["distiller", "introbeaver", "planbeaver"]


def test_models_counts_the_deck_not_every_card_key(tmp_path):
    path = _write(tmp_path, PROFILE)
    assert describe_deck(path)["models"] == len(PROFILE["deck"])


def test_stages_pinned_counts_only_stages_with_pins(tmp_path):
    path = _write(tmp_path, PROFILE)
    assert describe_deck(path)["stages_pinned"] == 1


def test_a_profile_with_no_pins_reports_zero(tmp_path):
    payload = dict(PROFILE, stages=[
        {"stageName": "distiller", "pinnedModels": [], "useDeck": True},
    ])
    path = _write(tmp_path, payload)
    assert describe_deck(path)["stages_pinned"] == 0


def test_a_string_path_is_accepted(tmp_path):
    path = _write(tmp_path, PROFILE)
    assert describe_deck(str(path))["models"] == 3


def test_the_profile_is_read_and_left_untouched(tmp_path):
    path = _write(tmp_path, PROFILE)
    before = path.read_bytes()
    describe_deck(path)
    assert path.read_bytes() == before


def test_the_real_soma_current_profile_describes(tmp_path):
    from pathlib import Path

    real = (Path(__file__).resolve().parent
            / "turn-key-profiles" / "soma-current.json")
    if not real.exists():
        pytest.skip("soma-current.json is not present")
    deck = describe_deck(real)
    assert deck["models"] > 0
    assert "distiller" in deck["stages"]


# ── a missing path raises ──────────────────────────────────────────────────

def test_a_missing_file_raises_and_names_the_path(tmp_path):
    path = tmp_path / "absent.json"
    with pytest.raises(ProfileError) as exc:
        describe_deck(path)
    assert str(path) in str(exc.value)


def test_an_unset_path_raises():
    with pytest.raises(ProfileError):
        describe_deck("")


def test_a_directory_is_unreadable_rather_than_assumed(tmp_path):
    with pytest.raises(ProfileError) as exc:
        describe_deck(tmp_path)
    assert str(tmp_path) in str(exc.value)


# ── a malformed file raises ────────────────────────────────────────────────

def test_invalid_json_raises_and_names_the_path(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"deck": [', encoding="utf-8")
    with pytest.raises(ProfileError) as exc:
        describe_deck(path)
    assert str(path) in str(exc.value)


def test_a_json_array_root_raises(tmp_path):
    path = _write(tmp_path, [1, 2, 3])
    with pytest.raises(ProfileError):
        describe_deck(path)


def test_a_profile_without_a_deck_raises(tmp_path):
    path = _write(tmp_path, {"version": "1.0.0", "name": "no-deck"})
    with pytest.raises(ProfileError):
        describe_deck(path)


def test_an_empty_file_raises(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ProfileError):
        describe_deck(path)


# ── construction is where the deck becomes visible and fatal ───────────────

def test_construction_prints_one_deck_line(tmp_path, capsys, monkeypatch):
    path = _write(tmp_path, PROFILE)
    monkeypatch.setattr(TurnKeyClient, "_load_sdk",
                        staticmethod(lambda _p: _FakeSDK()))

    TurnKeyClient(str(path))

    out = capsys.readouterr().out
    deck_lines = [ln for ln in out.splitlines() if ln.startswith("deck:")]
    assert len(deck_lines) == 1
    assert str(path) in deck_lines[0]
    assert "models=3" in deck_lines[0]
    assert "stages_pinned=1" in deck_lines[0]


def test_construction_on_a_missing_profile_raises_before_the_sdk(tmp_path, capsys):
    path = tmp_path / "absent.json"
    with pytest.raises(ProfileError) as exc:
        TurnKeyClient(str(path))
    assert str(path) in str(exc.value)
    assert "deck:" not in capsys.readouterr().out


def test_construction_on_a_malformed_profile_raises(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ProfileError):
        TurnKeyClient(str(path))


def test_the_deck_is_kept_on_the_client(tmp_path, monkeypatch):
    path = _write(tmp_path, PROFILE)
    monkeypatch.setattr(TurnKeyClient, "_load_sdk",
                        staticmethod(lambda _p: _FakeSDK()))
    client = TurnKeyClient(str(path))
    assert client._deck["models"] == 3


class _FakeSDK:
    """Minimal stand-in so construction needs no network or real profile."""

    def run_stage(self, stage, messages, **kwargs):
        return {"choices": [{"message": {"content": "ok"}}]}
