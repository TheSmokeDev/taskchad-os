from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _path in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cli import main  # noqa: E402
from repository_config import (  # noqa: E402
    build_repository_config_briefing,
    load_repository_config,
    parse_repository_config,
)


def _pin_custom_profile(monkeypatch, profile_root: Path) -> None:
    monkeypatch.setenv("HOMIE_HOME", str(profile_root))
    monkeypatch.setenv("HOMIE_NAME", "custom")


def test_missing_config_is_valid_disabled(monkeypatch, tmp_path: Path) -> None:
    _pin_custom_profile(monkeypatch, tmp_path / "profile")

    report = load_repository_config()

    assert report.valid is True
    assert report.enabled is False
    assert report.config_exists is False
    assert report.items == ()


def test_disabled_config_does_not_validate_items(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "repositories:\n  enabled: false\n  items: not-a-list\n",
        encoding="utf-8",
    )
    _pin_custom_profile(monkeypatch, profile)

    report = load_repository_config()

    assert report.valid is True
    assert report.enabled is False
    assert report.items == ()


def test_enabled_config_validates_and_briefs(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    repo = tmp_path / "repo-a"
    profile.mkdir()
    repo.mkdir()
    (profile / "config.yaml").write_text(
        f"""
repositories:
  enabled: true
  items:
    - slug: repo-a
      github_repo: owner/repo-a
      default_branch: main
      local_path: "{repo.as_posix()}"
      archon_enabled: true
      dispatch_mode: manual
""",
        encoding="utf-8",
    )
    _pin_custom_profile(monkeypatch, profile)

    report = load_repository_config()
    briefing = build_repository_config_briefing()

    assert report.valid is True
    assert report.enabled is True
    assert report.slugs == ("repo-a",)
    assert "### Configured Repositories" in briefing
    assert "repo-a: owner/repo-a" in briefing
    assert repo.as_posix() not in briefing


def test_malformed_config_is_invalid(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "repositories:\n  enabled: [\n",
        encoding="utf-8",
    )
    _pin_custom_profile(monkeypatch, profile)

    report = load_repository_config()

    assert report.valid is False
    assert report.errors
    assert "yaml:" in report.errors[0]


def test_enabled_config_reports_duplicate_slug_and_missing_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing"
    data = {
        "repositories": {
            "enabled": True,
            "items": [
                {
                    "slug": "repo-a",
                    "github_repo": "owner/repo-a",
                    "default_branch": "main",
                    "local_path": str(tmp_path),
                    "archon_enabled": False,
                    "dispatch_mode": "manual",
                },
                {
                    "slug": "repo-a",
                    "github_repo": "owner/repo-b",
                    "default_branch": "main",
                    "local_path": str(missing_path),
                    "archon_enabled": False,
                    "dispatch_mode": "automatic",
                },
            ],
        }
    }

    report = parse_repository_config(
        data,
        profile="default",
        config_path=tmp_path / "config.yaml",
        config_exists=True,
    )

    assert report.valid is False
    assert any("duplicate slug" in error for error in report.errors)
    assert any("path does not exist" in error for error in report.errors)
    assert any("dispatch_mode" in error and "manual" in error for error in report.errors)


def test_cli_repositories_status_json(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    repo = tmp_path / "repo-a"
    profile.mkdir()
    repo.mkdir()
    (profile / "config.yaml").write_text(
        f"""
repositories:
  enabled: true
  items:
    - slug: repo-a
      github_repo: owner/repo-a
      default_branch: main
      local_path: "{repo.as_posix()}"
      archon_enabled: false
      dispatch_mode: archon-preferred
""",
        encoding="utf-8",
    )
    _pin_custom_profile(monkeypatch, profile)

    result = CliRunner().invoke(main, ["repositories", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["enabled"] is True
    assert payload["valid"] is True
    assert payload["items"][0]["slug"] == "repo-a"


def test_cli_repositories_validate_exits_nonzero_on_invalid(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        """
repositories:
  enabled: true
  items:
    - slug: repo-a
      github_repo: owner/repo-a
      default_branch: main
      local_path: missing
      archon_enabled: false
      dispatch_mode: manual
""",
        encoding="utf-8",
    )
    _pin_custom_profile(monkeypatch, profile)

    result = CliRunner().invoke(main, ["repositories", "validate"])

    assert result.exit_code == 1
    assert "Repository config invalid" in result.output
    assert "path does not exist" in result.output
