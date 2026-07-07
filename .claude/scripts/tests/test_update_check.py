"""Tests for the CLI update-availability check (gh/npm/brew-style banner)."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
if _CHAT_DIR not in sys.path:
    sys.path.insert(0, _CHAT_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import update_check as uc  # noqa: E402
from config import now_local  # noqa: E402


def test_get_current_version_parses_pyproject(tmp_path, monkeypatch):
    fixture = tmp_path / "pyproject.toml"
    fixture.write_text('[project]\nname = "thehomie"\nversion = "2.3.4"\n', encoding="utf-8")
    monkeypatch.setattr(uc, "_PYPROJECT_PATH", fixture)

    assert uc.get_current_version() == "2.3.4"


def test_get_current_version_returns_fallback_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "_PYPROJECT_PATH", tmp_path / "does-not-exist.toml")

    assert uc.get_current_version() == "0.0.0"


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("1.2.0", "1.1.9", True),
        ("v1.2.0", "1.2.0", False),
        ("1.0.0", "1.0.0", False),
        ("2.0.0", "1.99.99", True),
        ("1.0.0", "1.0.1", False),
    ],
)
def test_version_tuple_comparison(a, b, expected):
    assert (uc._version_tuple(a) > uc._version_tuple(b)) is expected


def test_version_tuple_malformed_input_is_safe():
    assert uc._version_tuple("not-a-version") == (0, 0, 0)
    assert uc._version_tuple("") == (0, 0, 0)


def test_check_for_update_skips_network_within_ttl(tmp_path, monkeypatch):
    state_file = tmp_path / "update-check-state.json"
    monkeypatch.setattr(uc, "UPDATE_CHECK_STATE_FILE", state_file)
    monkeypatch.setattr(uc, "UPDATE_CHECK_MIN_INTERVAL_HOURS", 24)
    monkeypatch.setattr(uc, "get_current_version", lambda: "1.0.0")

    from shared import save_state

    save_state(
        {"last_checked": now_local().isoformat(), "latest_version": "1.5.0"},
        state_file,
    )

    calls = []
    monkeypatch.setattr(uc, "get_latest_release_version", lambda timeout=2.0: calls.append(1) or "9.9.9")

    result = uc.check_for_update()

    assert calls == []  # cache was fresh — no network call made
    assert result == ("1.0.0", "1.5.0")  # served from cache, not the mocked "9.9.9"


def test_check_for_update_refreshes_when_cache_is_stale(tmp_path, monkeypatch):
    state_file = tmp_path / "update-check-state.json"
    monkeypatch.setattr(uc, "UPDATE_CHECK_STATE_FILE", state_file)
    monkeypatch.setattr(uc, "UPDATE_CHECK_MIN_INTERVAL_HOURS", 24)
    monkeypatch.setattr(uc, "get_current_version", lambda: "1.0.0")

    from shared import save_state

    stale = now_local() - timedelta(hours=48)
    save_state({"last_checked": stale.isoformat(), "latest_version": "1.1.0"}, state_file)

    calls = []
    monkeypatch.setattr(
        uc, "get_latest_release_version", lambda timeout=2.0: calls.append(1) or "2.0.0"
    )

    result = uc.check_for_update()

    assert calls == [1]  # cache was stale — network call made exactly once
    assert result == ("1.0.0", "2.0.0")


def test_check_for_update_returns_none_on_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "UPDATE_CHECK_STATE_FILE", tmp_path / "update-check-state.json")
    monkeypatch.setattr(uc, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(uc, "get_latest_release_version", lambda timeout=2.0: None)

    assert uc.check_for_update() is None


def test_check_for_update_returns_none_when_already_current(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "UPDATE_CHECK_STATE_FILE", tmp_path / "update-check-state.json")
    monkeypatch.setattr(uc, "get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(uc, "get_latest_release_version", lambda timeout=2.0: "1.0.0")

    assert uc.check_for_update() is None


def test_get_latest_release_version_swallows_network_errors(monkeypatch):
    import urllib.error

    def _raise(*args, **kwargs):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(uc.urllib.request, "urlopen", _raise)

    assert uc.get_latest_release_version(timeout=0.1) is None


class TestCLIUpdateBanner:
    """Click CliRunner tests — confirm the banner goes to stderr, never stdout."""

    def test_banner_prints_to_stderr_when_update_available(self, monkeypatch):
        from click.testing import CliRunner
        import cli

        monkeypatch.setattr(cli, "check_for_update", lambda: ("1.0.0", "1.1.0"))
        runner = CliRunner()
        result = runner.invoke(cli.main, ["status", "--help"])

        assert "Update available: v1.0.0 -> v1.1.0" in result.stderr
        assert "Update available" not in result.stdout

    def test_no_banner_when_already_current(self, monkeypatch):
        from click.testing import CliRunner
        import cli

        monkeypatch.setattr(cli, "check_for_update", lambda: None)
        runner = CliRunner()
        result = runner.invoke(cli.main, ["status", "--help"])

        assert result.stderr == ""
