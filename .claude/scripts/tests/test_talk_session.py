"""Voice prompt provider boundary, operator opt-in, and fail-open behavior."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import config
import talk_session


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture memory dir with every identity file the voice prompt reads."""
    (tmp_path / "SOUL.md").write_text("BE-DIRECT-RULE", encoding="utf-8")
    (tmp_path / "USER.md").write_text("OPERATOR-IS-SMOKE", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("ARCHON-IS-THE-HANDS", encoding="utf-8")
    (tmp_path / "WORKING.md").write_text("OPEN-THREAD-WAVE-3", encoding="utf-8")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "SOUL_FILE", tmp_path / "SOUL.md")
    monkeypatch.delenv("TALK_IDENTITY_INCLUDE", raising=False)
    return tmp_path


def test_the_voice_prompt_defaults_to_soul_only(vault: Path) -> None:
    instructions = talk_session.build_talk_instructions()

    assert "BE-DIRECT-RULE" in instructions
    assert "OPERATOR-IS-SMOKE" not in instructions
    assert "ARCHON-IS-THE-HANDS" not in instructions
    assert "OPEN-THREAD-WAVE-3" not in instructions


def test_opted_in_identity_keeps_soul_first(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "SOUL,USER,MEMORY,WORKING")
    instructions = talk_session.build_talk_instructions()

    assert instructions.index("BE-DIRECT-RULE") < instructions.index("OPERATOR-IS-SMOKE")
    assert instructions.index("OPERATOR-IS-SMOKE") < instructions.index("ARCHON-IS-THE-HANDS")


def test_each_file_is_capped_because_the_prompt_is_paid_for(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200-line MEMORY.md must not blow out a Realtime session prompt."""
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "SOUL,MEMORY")
    (vault / "MEMORY.md").write_text("M" * 50_000, encoding="utf-8")

    instructions = talk_session.build_talk_instructions()

    assert "M" * 100 in instructions  # it IS carried
    assert instructions.count("M") <= talk_session._IDENTITY_CAPS["MEMORY"] + 500


def test_the_include_set_is_an_operator_knob(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "SOUL,USER")

    instructions = talk_session.build_talk_instructions()

    assert "BE-DIRECT-RULE" in instructions
    assert "OPERATOR-IS-SMOKE" in instructions
    assert "ARCHON-IS-THE-HANDS" not in instructions


def test_a_typo_in_the_knob_does_not_silence_the_surface(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "SOUL,NOSUCHFILE")

    instructions = talk_session.build_talk_instructions()

    assert "BE-DIRECT-RULE" in instructions
    assert instructions.strip()


def test_it_fails_open_to_soul_only_when_the_shim_is_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice without memory is a degradation the operator can work around.
    Voice that will not start is an outage — so the shim failing must not be
    able to take the session down."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if "identity_payload" in name:
            raise ImportError("simulated shim failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    instructions = talk_session.build_talk_instructions()

    assert "BE-DIRECT-RULE" in instructions, "SOUL must survive the fallback"
    assert "OPERATOR-IS-SMOKE" not in instructions, "the payload genuinely failed"
    assert instructions.startswith(talk_session._VOICE_PREAMBLE)


def test_an_empty_vault_still_yields_a_usable_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "SOUL_FILE", tmp_path / "SOUL.md")

    instructions = talk_session.build_talk_instructions()

    assert instructions == talk_session._VOICE_PREAMBLE
