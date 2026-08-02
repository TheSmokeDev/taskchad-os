"""Explicit, confined filesystem roots for one persona curriculum."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CurriculumPaths:
    persona_id: str
    profile_root: Path
    data_root: Path
    memory_root: Path
    curriculum_data: Path
    bundle_root: Path
    artifacts_root: Path
    raw_root: Path
    vendor_root: Path
    ledger_path: Path
    staging_path: Path

    def confine_data(self, path: Path | str) -> Path:
        return _confine(path, self.curriculum_data)

    def confine_memory(self, path: Path | str) -> Path:
        return _confine(path, self.bundle_root)


def resolve_curriculum_paths(persona_id: str, domain: str) -> CurriculumPaths:
    import personas
    from personas.lifecycle import validate_persona_name

    validate_persona_name(persona_id)
    paths = personas.get_persona_paths(persona_id)
    data_root = Path(paths["data"]).resolve(strict=False)
    memory_root = Path(paths["memory"]).resolve(strict=False)
    profile_root = data_root.parent.resolve(strict=False)
    curriculum_data = (data_root / "curricula").resolve(strict=False)
    bundle_root = (memory_root / "curricula" / domain).resolve(strict=False)
    resolved = CurriculumPaths(
        persona_id=persona_id,
        profile_root=profile_root,
        data_root=data_root,
        memory_root=memory_root,
        curriculum_data=curriculum_data,
        bundle_root=bundle_root,
        artifacts_root=curriculum_data / "artifacts",
        raw_root=curriculum_data / "raw",
        vendor_root=curriculum_data / "vendor",
        ledger_path=curriculum_data / "curriculum.db",
        staging_path=Path(paths["state"]) / "memory-candidates.jsonl",
    )
    # Construction-time assertions catch an unexpected persona path resolver.
    _confine(resolved.curriculum_data, data_root)
    _confine(resolved.bundle_root, memory_root)
    _confine(resolved.staging_path, profile_root)
    return resolved


def _confine(path: Path | str, root: Path | str) -> Path:
    target = Path(path).resolve(strict=False)
    boundary = Path(root).resolve(strict=False)
    try:
        target.relative_to(boundary)
    except ValueError as exc:
        raise ValueError(f"Curriculum path escapes profile boundary: {target}") from exc
    return target
