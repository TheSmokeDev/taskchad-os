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


# ─── TALK_PREFER_CODEX_OAUTH (voice-scoped billing directive) ─────────────


def test_the_codex_directive_is_off_unless_the_operator_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TALK_PREFER_CODEX_OAUTH", raising=False)
    assert talk_session.talk_prefer_codex_oauth() is False

    for falsey in ("", "  ", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", falsey)
        assert talk_session.talk_prefer_codex_oauth() is False, falsey


def test_the_codex_directive_reads_the_env_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 1 — flipping the knob takes effect without a reimport."""

    for truthy in ("1", "true", "TRUE", " yes ", "on"):
        monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", truthy)
        assert talk_session.talk_prefer_codex_oauth() is True, truthy


def _fake_auth(source: str, token: str = "tok"):
    return talk_session.openai_platform_auth.OpenAIPlatformAuth(
        token=token, source=source, detail="test"
    )


def _mint_harness(monkeypatch: pytest.MonkeyPatch, resolve) -> list[str]:
    """Wire create_talk_session so only auth behaviour is under test."""

    minted: list[str] = []
    monkeypatch.setattr(talk_session.kill_switches, "requireEnabled", lambda *a, **k: None)
    monkeypatch.setattr(
        talk_session.openai_platform_auth, "resolve_openai_platform_auth", resolve
    )

    def fake_post(token: str, session: dict) -> dict:
        minted.append(token)
        return {"value": "ek-1", "expires_at": 1_800_000_000}

    monkeypatch.setattr(talk_session, "_post_client_secret", fake_post)
    return minted


def test_minting_a_session_threads_the_directive_into_auth(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def resolve(**kwargs):
        seen.update(kwargs)
        return _fake_auth(talk_session.openai_platform_auth.SOURCE_CODEX_OAUTH)

    _mint_harness(monkeypatch, resolve)
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")

    descriptor = talk_session.create_talk_session()

    assert seen["prefer_codex"] is True
    assert descriptor.auth_source == talk_session.openai_platform_auth.SOURCE_CODEX_OAUTH


def test_minting_leaves_the_directive_off_by_default(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def resolve(**kwargs):
        seen.update(kwargs)
        return _fake_auth(talk_session.openai_platform_auth.SOURCE_ENV)

    _mint_harness(monkeypatch, resolve)
    monkeypatch.delenv("TALK_PREFER_CODEX_OAUTH", raising=False)

    talk_session.create_talk_session()

    assert seen["prefer_codex"] is False


def test_an_unusable_subscription_refuses_the_session_instead_of_metering(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end fail-closed: no client secret is ever minted, and the
    operator gets both doors named."""

    def resolve(**kwargs):
        raise talk_session.openai_platform_auth.OpenAIPlatformAuthError(
            talk_session.openai_platform_auth.PREFER_CODEX_UNAVAILABLE_MESSAGE
        )

    minted = _mint_harness(monkeypatch, resolve)
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")

    with pytest.raises(talk_session.TalkAuthError) as caught:
        talk_session.create_talk_session()

    assert minted == [], "nothing was billed to a key"
    assert "codex login" in str(caught.value)
    assert "TALK_PREFER_CODEX_OAUTH" in str(caught.value)


def test_a_rejected_codex_token_names_both_doors_under_the_directive(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local validity is not entitlement — a 401 from Realtime is the other
    way the subscription can be unusable, and it must not retry on a key."""

    attempts: list[str] = []

    def resolve(**kwargs):
        return _fake_auth(
            talk_session.openai_platform_auth.SOURCE_CODEX_OAUTH, "codex-token"
        )

    monkeypatch.setattr(talk_session.kill_switches, "requireEnabled", lambda *a, **k: None)
    monkeypatch.setattr(
        talk_session.openai_platform_auth, "resolve_openai_platform_auth", resolve
    )

    def fake_post(token: str, session: dict) -> dict:
        attempts.append(token)
        raise talk_session.TalkUpstreamError(
            "OpenAI Realtime client secret failed (401): invalid_api_key"
        )

    monkeypatch.setattr(talk_session, "_post_client_secret", fake_post)
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")

    with pytest.raises(talk_session.TalkUpstreamError) as caught:
        talk_session.create_talk_session()

    assert attempts == ["codex-token"], "no silent retry on a metered key"
    assert "codex login" in str(caught.value)
    assert "TALK_PREFER_CODEX_OAUTH" in str(caught.value)


def test_the_401_remediation_is_unchanged_with_the_directive_off(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-off parity for the upstream-rejection message."""

    def resolve(**kwargs):
        return _fake_auth(talk_session.openai_platform_auth.SOURCE_ENV, "sk-env")

    monkeypatch.setattr(talk_session.kill_switches, "requireEnabled", lambda *a, **k: None)
    monkeypatch.setattr(
        talk_session.openai_platform_auth, "resolve_openai_platform_auth", resolve
    )
    monkeypatch.setattr(
        talk_session,
        "_post_client_secret",
        lambda token, session: (_ for _ in ()).throw(
            talk_session.TalkUpstreamError(
                "OpenAI Realtime client secret failed (401): invalid_api_key"
            )
        ),
    )
    monkeypatch.delenv("TALK_PREFER_CODEX_OAUTH", raising=False)

    with pytest.raises(talk_session.TalkUpstreamError) as caught:
        talk_session.create_talk_session()

    assert str(caught.value) == (
        "OpenAI Realtime auth failed (401): the configured OpenAI API key was rejected"
    )


def test_a_non_auth_upstream_failure_is_untouched(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve(**kwargs):
        return _fake_auth(
            talk_session.openai_platform_auth.SOURCE_CODEX_OAUTH, "codex-token"
        )

    monkeypatch.setattr(talk_session.kill_switches, "requireEnabled", lambda *a, **k: None)
    monkeypatch.setattr(
        talk_session.openai_platform_auth, "resolve_openai_platform_auth", resolve
    )
    monkeypatch.setattr(
        talk_session,
        "_post_client_secret",
        lambda token, session: (_ for _ in ()).throw(
            talk_session.TalkUpstreamError(
                "OpenAI Realtime client secret failed (503): upstream unavailable"
            )
        ),
    )
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")

    with pytest.raises(talk_session.TalkUpstreamError, match="503"):
        talk_session.create_talk_session()


def test_status_threads_the_directive(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_status(**kwargs):
        seen.update(kwargs)
        return {"configured": True, "source": "codex-oauth", "detail": "test"}

    monkeypatch.setattr(
        talk_session.openai_platform_auth, "openai_platform_auth_status", fake_status
    )
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "on")

    talk_session.talk_status()

    assert seen["prefer_codex"] is True
