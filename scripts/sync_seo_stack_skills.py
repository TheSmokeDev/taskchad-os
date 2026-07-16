#!/usr/bin/env python3
"""Install or verify the tracked TokenMax SEO authority skill packages."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / ".claude" / "skills"
SKILLS = (
    "tokenmax-site-factory",
    "tokenmax-fleet-orchestrator",
    "ai-citation-authority-wave",
)
IGNORED_DIRS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


class SyncError(RuntimeError):
    """Raised when a requested sync would violate the package boundary."""


@dataclass(frozen=True)
class Comparison:
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not (self.missing or self.extra or self.changed)


def _ignored(relative: Path) -> bool:
    return any(part in IGNORED_DIRS for part in relative.parts) or relative.suffix in IGNORED_SUFFIXES


def package_manifest(root: Path) -> dict[str, str]:
    """Return a stable SHA-256 manifest for one package."""
    if not root.is_dir():
        raise SyncError(f"skill package does not exist: {root}")

    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _ignored(relative):
            continue
        if path.is_symlink():
            raise SyncError(f"skill packages may not contain symlinks: {path}")
        if path.is_file():
            manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def compare_packages(source: Path, target: Path) -> Comparison:
    source_manifest = package_manifest(source)
    if not target.is_dir():
        return Comparison(tuple(source_manifest), (), ())

    target_manifest = package_manifest(target)
    source_paths = set(source_manifest)
    target_paths = set(target_manifest)
    return Comparison(
        missing=tuple(sorted(source_paths - target_paths)),
        extra=tuple(sorted(target_paths - source_paths)),
        changed=tuple(
            sorted(path for path in source_paths & target_paths if source_manifest[path] != target_manifest[path])
        ),
    )


def selected_skills(requested: list[str] | None) -> tuple[str, ...]:
    if not requested:
        return SKILLS
    unknown = sorted(set(requested) - set(SKILLS))
    if unknown:
        raise SyncError(f"unknown skill(s): {', '.join(unknown)}")
    return tuple(dict.fromkeys(requested))


def target_directory(target_root: Path, skill: str) -> Path:
    if skill not in SKILLS:
        raise SyncError(f"unknown skill: {skill}")
    root = target_root.expanduser().resolve()
    target = (root / skill).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SyncError(f"target escaped root: {target}") from exc
    if target == (SOURCE_ROOT / skill).resolve():
        raise SyncError("source package cannot be used as an install target")
    return target


def _remove_tree(path: Path, allowed_root: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(allowed_root.resolve())
    except ValueError as exc:
        raise SyncError(f"refusing to remove path outside target root: {resolved}") from exc
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def install_skill(skill: str, target_root: Path) -> Path:
    source = SOURCE_ROOT / skill
    source_manifest = package_manifest(source)
    target = target_directory(target_root, skill)
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)

    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise SyncError(f"target must be a normal directory or absent: {target}")

    temporary = root / f".{skill}.sync-{uuid.uuid4().hex}"
    backup = root / f".{skill}.backup-{uuid.uuid4().hex}"
    try:
        for relative in source_manifest:
            source_file = source / relative
            target_file = temporary / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)

        if package_manifest(temporary) != source_manifest:
            raise SyncError(f"temporary package verification failed: {skill}")

        if target.exists():
            target.rename(backup)
        try:
            temporary.rename(target)
        except Exception:
            if backup.exists() and not target.exists():
                backup.rename(target)
            raise
        if backup.exists():
            _remove_tree(backup, root)
    finally:
        if temporary.exists():
            _remove_tree(temporary, root)

    comparison = compare_packages(source, target)
    if not comparison.matches:
        raise SyncError(f"installed package does not match source: {skill}")
    return target


def _format_mismatch(comparison: Comparison) -> str:
    fields = []
    if comparison.missing:
        fields.append(f"missing={','.join(comparison.missing)}")
    if comparison.extra:
        fields.append(f"extra={','.join(comparison.extra)}")
    if comparison.changed:
        fields.append(f"changed={','.join(comparison.changed)}")
    return " ".join(fields)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "check"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--target-root",
            action="append",
            required=True,
            type=Path,
            help="Directory that contains installed skill folders; repeatable.",
        )
        subparser.add_argument(
            "--skill",
            action="append",
            choices=SKILLS,
            help="Skill to process; repeatable. Defaults to all three.",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        skills = selected_skills(args.skill)
        failed = False
        for target_root in args.target_root:
            for skill in skills:
                source = SOURCE_ROOT / skill
                target = target_directory(target_root, skill)
                if args.command == "install":
                    install_skill(skill, target_root)
                    print(f"INSTALLED {skill} {target}")
                    continue

                comparison = compare_packages(source, target)
                if comparison.matches:
                    print(f"OK {skill} {target}")
                else:
                    failed = True
                    print(f"MISMATCH {skill} {target} {_format_mismatch(comparison)}")
        return 1 if failed else 0
    except SyncError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
