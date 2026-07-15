"""Tests for github_signal.scout_sync — fail-open persona memory sync."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_signal import scout_sync as sync_mod  # noqa: E402


@pytest.fixture()
def homie_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(root))
    return root


@pytest.fixture()
def artifact(tmp_path) -> Path:
    path = tmp_path / "2026-W29.md"
    path.write_text("# digest content", encoding="utf-8")
    return path


def test_empty_profile_knob_is_off(homie_root, artifact, monkeypatch):
    monkeypatch.setenv("GITHUB_SIGNAL_SCOUT_PROFILE", "")
    assert sync_mod.sync_to_scout([artifact]) is False


def test_missing_profile_dir_skips_silently(homie_root, artifact, monkeypatch):
    monkeypatch.setenv("GITHUB_SIGNAL_SCOUT_PROFILE", "repo-scout")
    assert sync_mod.sync_to_scout([artifact]) is False


def test_success_copies_and_invokes_persona_index(
    homie_root, artifact, monkeypatch
):
    monkeypatch.setenv("GITHUB_SIGNAL_SCOUT_PROFILE", "repo-scout")
    profile = homie_root / "profiles" / "repo-scout"
    (profile / "memory").mkdir(parents=True)

    invoked = {}

    def fake_run(cmd, **kwargs):
        invoked["cmd"] = cmd
        return None

    monkeypatch.setattr(sync_mod.subprocess, "run", fake_run)
    assert sync_mod.sync_to_scout([artifact]) is True

    copied = profile / "memory" / "research" / "github-signal" / "2026-W29.md"
    assert copied.read_text(encoding="utf-8") == "# digest content"
    assert invoked["cmd"][-2:] == ["-p", "repo-scout"]


def test_index_failure_still_returns_true(homie_root, artifact, monkeypatch):
    monkeypatch.setenv("GITHUB_SIGNAL_SCOUT_PROFILE", "repo-scout")
    profile = homie_root / "profiles" / "repo-scout"
    (profile / "memory").mkdir(parents=True)

    def boom(cmd, **kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(sync_mod.subprocess, "run", boom)
    assert sync_mod.sync_to_scout([artifact]) is True  # copy is the durable part


def test_missing_source_files_return_false(homie_root, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SIGNAL_SCOUT_PROFILE", "repo-scout")
    profile = homie_root / "profiles" / "repo-scout"
    (profile / "memory").mkdir(parents=True)
    assert sync_mod.sync_to_scout([tmp_path / "nope.md"]) is False
