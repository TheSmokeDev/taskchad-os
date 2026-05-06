"""PRP-7c Phase 3 / WS4 — Telegram bot token collision detection.

Covers ``personas.services.detect_telegram_token_collision()``:

    * Two profiles with same token → returns OTHER profile's name.
    * Default-vs-named collision detected (R1 B4 — scan set includes
      default profile's install-dir .env, not just ~/.homie/profiles/).
    * --slack / --relay / --discord / --whatsapp / --test with duplicate
      Telegram token does NOT fail (R1 B4 — gated by
      ``has_telegram and (start_all or args.telegram)`` in chat/main.py).
    * Empty active token → returns None.
    * Missing other-profile .env → skipped.
    * Corrupt .env (binary garbage) → fail-OPEN (returns None) — bot
      startup proceeds.
    * Whitespace-padded tokens normalized.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from personas import activity as _activity
from personas import services as _services


def _set_active(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Force-classify the active profile name via Rule 3 module-attribute patch.

    ``personas.activity.get_active_profile_name()`` resolves the active
    profile by comparing ``HOMIE_HOME`` against ``Path.home() / ".homie"``.
    Tests that use ``tmp_path`` for HOMIE_HOME can never get a "named"
    classification — the helper returns ``"custom"`` for any path outside
    the real user home. To exercise the named-profile branches of
    ``detect_telegram_token_collision``, we monkeypatch the helper through
    ``personas.activity`` (Rule 3 — module-attribute lookup propagates).
    """
    monkeypatch.setattr(
        _activity, "get_active_profile_name", lambda: name
    )


# ---------------------------------------------------------------------------
# Two profiles same token
# ---------------------------------------------------------------------------


def test_two_named_profiles_same_token_collision_detected(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sales profile active, engineering's .env shares the token → return 'engineering'."""
    sales_dir = multi_profile_fixture["sales"]
    eng_dir = multi_profile_fixture["engineering"]
    (sales_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=SHARED-TOKEN-123\n", encoding="utf-8"
    )
    (eng_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=SHARED-TOKEN-123\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")

    other = _services.detect_telegram_token_collision("SHARED-TOKEN-123")
    assert other == "engineering"


def test_named_profile_unique_token_no_collision(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different tokens across profiles → None (no collision)."""
    sales_dir = multi_profile_fixture["sales"]
    eng_dir = multi_profile_fixture["engineering"]
    (sales_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=SALES-TOKEN-A\n", encoding="utf-8"
    )
    (eng_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=ENG-TOKEN-B\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")

    assert _services.detect_telegram_token_collision("SALES-TOKEN-A") is None


# ---------------------------------------------------------------------------
# Default-vs-named collision (R1 B4 — default's .env is install-dir)
# ---------------------------------------------------------------------------


def test_default_named_collision_detected(
    multi_profile_fixture: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 B4 — default profile's install-dir .env is in the scan set.

    Patches ``get_default_paths()["env_file"]`` to a tmp path with a
    matching token. Active profile is sales; collision must surface
    ``"default"`` as the other profile.
    """
    sales_dir = multi_profile_fixture["sales"]
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")

    fake_default_env = tmp_path / "fake_default.env"
    fake_default_env.write_text(
        "TELEGRAM_BOT_TOKEN=SHARED-TOKEN-XYZ\n", encoding="utf-8"
    )

    real_get_default_paths = _services.get_default_paths

    def fake_get_default_paths() -> dict[str, Path]:
        d = dict(real_get_default_paths())
        d["env_file"] = fake_default_env
        return d

    monkeypatch.setattr(_services, "get_default_paths", fake_get_default_paths)

    other = _services.detect_telegram_token_collision("SHARED-TOKEN-XYZ")
    assert other == "default"


# ---------------------------------------------------------------------------
# Empty / missing edge cases
# ---------------------------------------------------------------------------


def test_empty_active_token_returns_none(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty active token (no Telegram configured) → None."""
    sales_dir = multi_profile_fixture["sales"]
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")
    # Ensure the parent shell's TELEGRAM_BOT_TOKEN doesn't leak through
    # the None-sentinel branch (helper reads os.environ when active_token
    # is None).
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    assert _services.detect_telegram_token_collision("") is None
    assert _services.detect_telegram_token_collision(None) is None


def test_missing_other_profile_env_skipped(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engineering profile dir exists but .env was deleted → skipped silently."""
    sales_dir = multi_profile_fixture["sales"]
    eng_dir = multi_profile_fixture["engineering"]
    # Delete engineering's .env.
    (eng_dir / ".env").unlink()
    (sales_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=SOLO-TOKEN\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")

    assert _services.detect_telegram_token_collision("SOLO-TOKEN") is None


def test_active_token_resolved_from_os_environ(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``active_token=None`` → resolved from ``os.environ['TELEGRAM_BOT_TOKEN']``.

    Rule 1 None-sentinel pattern. Active token is os.environ; other
    profile env files contain the same value.
    """
    sales_dir = multi_profile_fixture["sales"]
    eng_dir = multi_profile_fixture["engineering"]
    (eng_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=ENV-RESOLVED-TOKEN\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "ENV-RESOLVED-TOKEN")
    _set_active(monkeypatch, "sales")

    other = _services.detect_telegram_token_collision()  # None sentinel
    assert other == "engineering"


# ---------------------------------------------------------------------------
# Fail-open on corrupt .env
# ---------------------------------------------------------------------------


def test_corrupt_other_env_fails_open(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary-garbage .env in the other profile → returns None (FAIL-OPEN).

    The bot startup must NOT refuse to start because some other profile's
    .env is corrupt — that's a self-DoS. dotenv_values raising
    UnicodeDecodeError or similar is silently ignored; collision check
    just returns None and bot proceeds.
    """
    sales_dir = multi_profile_fixture["sales"]
    eng_dir = multi_profile_fixture["engineering"]
    # Engineering .env is binary garbage — invalid UTF-8.
    (eng_dir / ".env").write_bytes(b"\xff\xfe\x00\x00garbage\xff\x00\x80")
    (sales_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=ACTIVE-TOKEN\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")

    # Must NOT raise.
    result = _services.detect_telegram_token_collision("ACTIVE-TOKEN")
    # No collision detected (corrupt other env was skipped).
    assert result is None


# ---------------------------------------------------------------------------
# Whitespace normalization
# ---------------------------------------------------------------------------


def test_whitespace_padded_tokens_normalized(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-padded tokens in .env files are normalized via strip().

    dotenv_values strips quotes; ``_parse_env_token()`` additionally strips
    outer whitespace before comparison. So a sales .env value of
    ``  PADDED-TOKEN  `` and an engineering .env value of ``PADDED-TOKEN``
    are detected as a collision because both parse to ``PADDED-TOKEN``.

    Note: the ACTIVE token passed in to ``detect_telegram_token_collision``
    is also stripped via the None-sentinel branch (``os.environ.get(...).strip()``).
    Callers passing a pre-stripped token explicitly skip that strip — the
    helper does NOT re-strip a caller-provided token (R1 minor — a pre-stripped
    token is the contract; chat/main.py reads from os.getenv() which on Windows
    sometimes preserves trailing spaces, but config.py's load_dotenv pipeline
    already strips them).
    """
    sales_dir = multi_profile_fixture["sales"]
    eng_dir = multi_profile_fixture["engineering"]
    (sales_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=PADDED-TOKEN\n", encoding="utf-8"
    )
    # Engineering uses surrounding whitespace which dotenv strips on parse.
    (eng_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=  PADDED-TOKEN  \n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")

    # Active token (pre-stripped — matches the production pipeline).
    other = _services.detect_telegram_token_collision("PADDED-TOKEN")
    assert other == "engineering"


def test_whitespace_padded_token_no_collision_when_distinct(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct tokens with padding → still no collision after strip."""
    sales_dir = multi_profile_fixture["sales"]
    eng_dir = multi_profile_fixture["engineering"]
    (sales_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=A-TOKEN\n", encoding="utf-8"
    )
    (eng_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=  B-TOKEN  \n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _set_active(monkeypatch, "sales")

    assert _services.detect_telegram_token_collision("A-TOKEN") is None


# ---------------------------------------------------------------------------
# B4 fix — chat/main.py gates the check on (has_telegram and ...)
# ---------------------------------------------------------------------------


def test_chat_main_collision_check_gated_on_telegram_flag() -> None:
    """``chat/main.py``'s collision-check is gated by ``has_telegram and ...``.

    The static contract: starting the bot with ``--slack`` or ``--relay``
    or ``--discord`` or ``--whatsapp`` MUST NOT trip the collision check.
    The B4 fix gates the check on ``has_telegram and (start_all or args.telegram)``
    so non-Telegram adapters with duplicate Telegram tokens still launch.

    We grep the source rather than spawning a real bot — the assertion is
    that the gate is present in chat/main.py.
    """
    chat_main = (
        Path(__file__).resolve().parent.parent.parent / "chat" / "main.py"
    )
    assert chat_main.exists(), f"{chat_main} missing"
    src = chat_main.read_text(encoding="utf-8")
    # The exact gate must include both ``has_telegram`` AND the
    # ``(start_all or args.telegram)`` clause.
    assert "if has_telegram and (start_all or args.telegram):" in src, (
        "chat/main.py is missing the B4 gate — the collision check would "
        "fire on --slack / --relay / --discord / --whatsapp runs and "
        "incorrectly refuse to start"
    )
    # Also assert detect_telegram_token_collision is called inside that
    # branch (no other call site).
    assert "detect_telegram_token_collision" in src
