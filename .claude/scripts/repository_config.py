"""Profile-owned repository runtime config validation.

This module reads the active profile's existing ``config.yaml`` and validates
an optional ``repositories:`` section. It never starts workflows, creates
worktrees, triages issues, or dispatches Archon.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personas import get_active_profile_name
from personas.services import (
    ConfigShapeError,
    get_profile_config_path,
    read_profile_config,
)

DISPATCH_MODES = frozenset({"manual", "archon-preferred"})
REQUIRED_ITEM_FIELDS = (
    "slug",
    "github_repo",
    "default_branch",
    "local_path",
    "archon_enabled",
    "dispatch_mode",
)


@dataclass(frozen=True)
class RepositoryItem:
    slug: str
    github_repo: str
    default_branch: str
    local_path: str
    archon_enabled: bool
    dispatch_mode: str


@dataclass(frozen=True)
class RepositoryConfigReport:
    profile: str
    config_path: Path
    config_exists: bool
    enabled: bool = False
    items: tuple[RepositoryItem, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(item.slug for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "config_path": str(self.config_path),
            "config_exists": self.config_exists,
            "enabled": self.enabled,
            "valid": self.valid,
            "items": [
                {
                    "slug": item.slug,
                    "github_repo": item.github_repo,
                    "default_branch": item.default_branch,
                    "local_path": item.local_path,
                    "archon_enabled": item.archon_enabled,
                    "dispatch_mode": item.dispatch_mode,
                }
                for item in self.items
            ],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _field_error(index: int, field: str, message: str) -> str:
    return f"repositories.items[{index}].{field}: {message}"


def _require_string(item: dict[str, Any], index: int, field: str, errors: list[str]) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(_field_error(index, field, "must be a non-empty string"))
        return ""
    return value.strip()


def _require_bool(item: dict[str, Any], index: int, field: str, errors: list[str]) -> bool:
    value = item.get(field)
    if not isinstance(value, bool):
        errors.append(_field_error(index, field, "must be boolean"))
        return False
    return value


def parse_repository_config(
    data: dict[str, Any],
    *,
    profile: str,
    config_path: Path,
    config_exists: bool,
    check_paths: bool = True,
) -> RepositoryConfigReport:
    errors: list[str] = []
    warnings: list[str] = []

    section = data.get("repositories")
    if section is None:
        return RepositoryConfigReport(
            profile=profile,
            config_path=config_path,
            config_exists=config_exists,
        )

    if not isinstance(section, dict):
        return RepositoryConfigReport(
            profile=profile,
            config_path=config_path,
            config_exists=config_exists,
            errors=("repositories: must be mapping",),
        )

    if "enabled" not in section:
        errors.append("repositories.enabled: required")
        enabled = False
    else:
        enabled_value = section.get("enabled")
        if not isinstance(enabled_value, bool):
            errors.append("repositories.enabled: must be boolean")
            enabled = False
        else:
            enabled = enabled_value

    if not enabled:
        return RepositoryConfigReport(
            profile=profile,
            config_path=config_path,
            config_exists=config_exists,
            enabled=False,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    raw_items = section.get("items", [])
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        errors.append("repositories.items: must be list")
        raw_items = []

    if not raw_items:
        errors.append("repositories.items: required when repositories.enabled is true")

    seen_slugs: set[str] = set()
    parsed_items: list[RepositoryItem] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            errors.append(f"repositories.items[{index}]: must be mapping")
            continue

        missing = [field for field in REQUIRED_ITEM_FIELDS if field not in raw_item]
        for field in missing:
            errors.append(_field_error(index, field, "required"))

        slug = _require_string(raw_item, index, "slug", errors)
        github_repo = _require_string(raw_item, index, "github_repo", errors)
        default_branch = _require_string(raw_item, index, "default_branch", errors)
        local_path = _require_string(raw_item, index, "local_path", errors)
        archon_enabled = _require_bool(raw_item, index, "archon_enabled", errors)
        dispatch_mode = _require_string(raw_item, index, "dispatch_mode", errors)

        if slug:
            if slug in seen_slugs:
                errors.append(_field_error(index, "slug", f"duplicate slug {slug!r}"))
            seen_slugs.add(slug)
        if dispatch_mode and dispatch_mode not in DISPATCH_MODES:
            allowed = ", ".join(sorted(DISPATCH_MODES))
            errors.append(_field_error(index, "dispatch_mode", f"must be one of: {allowed}"))
        if local_path and check_paths and not Path(local_path).expanduser().exists():
            errors.append(_field_error(index, "local_path", "path does not exist"))

        if all((slug, github_repo, default_branch, local_path, dispatch_mode)):
            parsed_items.append(
                RepositoryItem(
                    slug=slug,
                    github_repo=github_repo,
                    default_branch=default_branch,
                    local_path=local_path,
                    archon_enabled=archon_enabled,
                    dispatch_mode=dispatch_mode,
                )
            )

    return RepositoryConfigReport(
        profile=profile,
        config_path=config_path,
        config_exists=config_exists,
        enabled=enabled,
        items=tuple(parsed_items),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def load_repository_config(
    profile_name: str | None = None,
    *,
    check_paths: bool = True,
) -> RepositoryConfigReport:
    profile = profile_name or get_active_profile_name()
    config_path = get_profile_config_path(profile)
    config_exists = config_path.is_file()
    if not config_exists:
        return RepositoryConfigReport(
            profile=profile,
            config_path=config_path,
            config_exists=False,
        )

    try:
        data = read_profile_config(profile, strict=True)
    except ConfigShapeError as exc:
        return RepositoryConfigReport(
            profile=profile,
            config_path=config_path,
            config_exists=True,
            errors=(str(exc),),
        )

    return parse_repository_config(
        data,
        profile=profile,
        config_path=config_path,
        config_exists=True,
        check_paths=check_paths,
    )


def build_repository_config_briefing(max_items: int = 6) -> str:
    report = load_repository_config()
    if not report.enabled or not report.valid or not report.items:
        return ""

    lines = ["### Configured Repositories"]
    for item in report.items[:max_items]:
        archon = "yes" if item.archon_enabled else "no"
        lines.append(
            "- "
            f"{item.slug}: {item.github_repo} "
            f"(branch: {item.default_branch}; dispatch: {item.dispatch_mode}; "
            f"Archon: {archon})"
        )
    if len(report.items) > max_items:
        lines.append(f"- ... {len(report.items) - max_items} more configured repositories")
    return "\n".join(lines)
