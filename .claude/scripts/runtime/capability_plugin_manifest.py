"""Strict v2 capability-plugin manifests and import-free trusted discovery.

Legacy command/intent extensions intentionally remain owned by
``chat.extension_manager``.  A file is a capability manifest only when its
top-level ``manifestVersion`` is the integer ``2``.  Discovery reads data and
distribution resources only; importing plugin code is a lifecycle-stage action.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any

MANIFEST_FILENAME = "extension.json"
ENTRY_POINT_GROUP = "thehomie.capability_plugins"
ENTRY_POINT_MANIFEST = "thehomie-capability.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DETAIL_CHARS = 400
MAX_PLUGIN_ARTIFACTS = 256
MAX_PLUGIN_ARTIFACT_BYTES = 1024 * 1024
MAX_PLUGIN_ARTIFACT_TOTAL_BYTES = 8 * 1024 * 1024

_ID_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*$"
)
_VERSION_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_ENTRYPOINT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$"
)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CORE_VERSION_RE = re.compile(r"^[0-9A-Za-z.*<>=!~^,+\- ]{1,128}$")
_CORE_CLAUSE_RE = re.compile(
    r"^(?P<operator>>=|<=|==|!=|~=|>|<|\^|=)?\s*"
    r"(?P<version>(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,2})"
    r"(?P<wildcard>\.\*)?$"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|credential|password|secret|token)"
    r"\b\s*([:=])\s*([^\s,;&]+)"
)
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth|password|secret|token)=)[^&#\s]+"
)
_STANDALONE_CREDENTIAL_RE = re.compile(
    r"""
    (?:
        \bBearer\s+[A-Za-z0-9._~+/=-]{16,}
        |\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b
        |\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b
        |\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b
        |\bxox[baprs]-[A-Za-z0-9-]{10,}\b
        |\bAIza[0-9A-Za-z_-]{20,}\b
        |\bAKIA[0-9A-Z]{16}\b
        |\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b
        |\bhf_[A-Za-z0-9]{20,}\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FIELD_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CapabilityManifestError(ValueError):
    """A stable, operator-safe manifest failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = redact_detail(detail)
        super().__init__(f"{code}: {self.detail}")


class ManifestSource(StrEnum):
    BUNDLED = "bundled"
    OPERATOR_GLOBAL = "operator_global"
    PROJECT = "project"
    PYTHON_ENTRY_POINT = "python_entry_point"


SOURCE_RANK: Mapping[ManifestSource, int] = MappingProxyType(
    {
        ManifestSource.BUNDLED: 0,
        ManifestSource.OPERATOR_GLOBAL: 1,
        ManifestSource.PROJECT: 2,
        ManifestSource.PYTHON_ENTRY_POINT: 3,
    }
)


class ManifestClassification(StrEnum):
    CAPABILITY_V2 = "capability_v2"
    LEGACY = "legacy"


class ExportPosture(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class ContributionType(StrEnum):
    GENERIC = "generic"
    TOOL = "tool"
    TOOLSET = "toolset"
    SKILL = "skill"
    MCP = "mcp"
    MCP_SERVER = "mcp_server"
    COMMAND = "command"
    INTENT = "intent"
    PROMPT_HOOK = "prompt_hook"
    CONTEXT_HOOK = "context_hook"
    PROVIDER_ADAPTER = "provider_adapter"
    HEALTH_PROBE = "health_probe"
    CONFIG_REQUIREMENT = "config_requirement"


@dataclass(frozen=True, slots=True)
class PluginRequirements:
    core_version: str | None = None
    env: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContributionDeclaration:
    id: str
    type: ContributionType
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplacementDeclaration:
    id: str
    contract_version: int
    contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class CapabilityPluginManifest:
    manifest_version: int
    id: str
    name: str
    version: str
    description: str
    source: ManifestSource
    entrypoint: str
    requirements: PluginRequirements
    contributions: tuple[ContributionDeclaration, ...]
    enabled_by_default: bool
    replaces: ReplacementDeclaration | None
    contract_version: int
    export_posture: ExportPosture

    @property
    def contribution_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.contributions)

    @property
    def contract_fingerprint(self) -> str:
        """Return the canonical executable contribution-contract identity.

        Replacements must preserve contribution IDs, contribution types, both
        contribution- and plugin-dependency edges, and the declared contract
        version.  Human-readable metadata and runtime configuration readiness
        intentionally do not participate in this compatibility identity.
        """

        contract = {
            "contract_version": self.contract_version,
            "plugin_dependencies": sorted(self.requirements.plugins),
            "contributions": [
                {
                    "id": item.id,
                    "type": item.type.value,
                    "depends_on": sorted(item.depends_on),
                }
                for item in sorted(self.contributions, key=lambda value: value.id)
            ],
        }
        payload = json.dumps(
            contract,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def manifest_fingerprint(self) -> str:
        """Return the complete canonical lifecycle identity of this manifest."""

        payload = json.dumps(
            {
                "manifest_version": self.manifest_version,
                "id": self.id,
                "name": self.name,
                "version": self.version,
                "description": self.description,
                "source": self.source.value,
                "entrypoint": self.entrypoint,
                "requirements": {
                    "core_version": self.requirements.core_version,
                    "env": list(self.requirements.env),
                    "plugins": list(self.requirements.plugins),
                },
                "contributions": [
                    {
                        "id": item.id,
                        "type": item.type.value,
                        "depends_on": list(item.depends_on),
                    }
                    for item in self.contributions
                ],
                "enabled_by_default": self.enabled_by_default,
                "replaces": (
                    {
                        "id": self.replaces.id,
                        "contract_version": self.replaces.contract_version,
                        "contract_fingerprint": self.replaces.contract_fingerprint,
                    }
                    if self.replaces is not None
                    else None
                ),
                "contract_version": self.contract_version,
                "export": self.export_posture.value,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def catalog_record(self) -> Mapping[str, Any]:
        """Return the bounded, value-free operator catalog representation."""

        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source": self.source.value,
            "contract_version": self.contract_version,
            "contract_fingerprint": self.contract_fingerprint,
            "enabled_by_default": self.enabled_by_default,
            "export": self.export_posture.value,
            "requirements": {
                "core_version": self.requirements.core_version,
                "env": list(self.requirements.env),
                "plugins": list(self.requirements.plugins),
            },
            "contributions": [
                {
                    "id": item.id,
                    "type": item.type.value,
                    "depends_on": list(item.depends_on),
                }
                for item in self.contributions
            ],
            "replaces": (
                {
                    "id": self.replaces.id,
                    "contract_version": self.replaces.contract_version,
                    "contract_fingerprint": self.replaces.contract_fingerprint,
                }
                if self.replaces
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class FilesystemPluginSource:
    source: ManifestSource
    path: Path

    def __post_init__(self) -> None:
        invalid_source = False
        source: ManifestSource | None = None
        try:
            source = ManifestSource(self.source)
        except (TypeError, ValueError):
            invalid_source = True
        if invalid_source:
            raise ValueError("unknown filesystem capability source") from None
        assert source is not None
        if source is ManifestSource.PYTHON_ENTRY_POINT:
            raise ValueError("python entry points are not filesystem sources")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class CapabilityPluginArtifact:
    """One immutable executable source captured without importing plugin code."""

    relative_path: str
    source: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.relative_path) is not str
            or not self.relative_path
            or "\\" in self.relative_path
            or self.relative_path.startswith("/")
            or any(part in {"", ".", ".."} for part in self.relative_path.split("/"))
            or not self.relative_path.endswith(".py")
        ):
            raise ValueError("capability artifact path must be a canonical relative Python file")
        if type(self.source) is not bytes or len(self.source) > MAX_PLUGIN_ARTIFACT_BYTES:
            raise ValueError("capability artifact source is invalid or exceeds its size limit")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.source).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityPluginCandidate:
    manifest: CapabilityPluginManifest
    location_key: str
    plugin_path: Path | None = None
    manifest_path: Path | None = None
    artifact_root: Path | None = None
    artifacts: tuple[CapabilityPluginArtifact, ...] = ()
    entry_point: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.location_key) is not str or not self.location_key:
            raise ValueError("capability candidate location must be a nonempty exact string")
        if type(self.artifacts) is not tuple or not all(
            type(item) is CapabilityPluginArtifact for item in self.artifacts
        ):
            raise ValueError("capability candidate artifacts must be an exact immutable tuple")
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("capability candidate artifact paths must be unique")
        if len(paths) > MAX_PLUGIN_ARTIFACTS or sum(
            len(item.source) for item in self.artifacts
        ) > MAX_PLUGIN_ARTIFACT_TOTAL_BYTES:
            raise ValueError("capability candidate artifact set exceeds its size limit")
        if self.plugin_path is not None:
            object.__setattr__(
                self,
                "plugin_path",
                Path(self.plugin_path).resolve(strict=False),
            )
        if self.manifest_path is not None:
            object.__setattr__(
                self,
                "manifest_path",
                Path(self.manifest_path).resolve(strict=False),
            )
        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                Path(self.artifact_root).resolve(strict=False),
            )

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (
            SOURCE_RANK[self.manifest.source],
            self.location_key.casefold(),
            self.manifest.id,
        )

    @property
    def provenance_id(self) -> str:
        """Return a stable, path-redacted identity for this exact candidate."""

        material = "\0".join(
            (
                self.manifest.source.value,
                self.location_key,
                self.manifest.manifest_fingerprint,
                self.artifact_fingerprint,
            )
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:20]
        return f"{self.manifest.source.value}:{digest}"

    @property
    def artifact_fingerprint(self) -> str:
        payload = json.dumps(
            [
                {"path": item.relative_path, "sha256": item.digest}
                for item in self.artifacts
            ],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplacementResolution:
    target_id: str
    target_provenance: str
    replacement_id: str
    replacement_provenance: str
    contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class LegacyExtensionManifest:
    location: str
    source: ManifestSource


@dataclass(frozen=True, slots=True)
class PluginDiscoveryError:
    code: str
    source: ManifestSource
    location: str
    plugin_id: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "location", _safe_label(self.location))
        object.__setattr__(self, "detail", redact_detail(self.detail))


@dataclass(frozen=True, slots=True)
class CapabilityPluginDiscovery:
    candidates: tuple[CapabilityPluginCandidate, ...]
    active_candidates: tuple[CapabilityPluginCandidate, ...]
    legacy: tuple[LegacyExtensionManifest, ...]
    errors: tuple[PluginDiscoveryError, ...]
    replacements: tuple[ReplacementResolution, ...] = ()

    def catalog(self, *, public_only: bool = False) -> tuple[Mapping[str, Any], ...]:
        records: list[Mapping[str, Any]] = []
        active_provenance = {item.provenance_id for item in self.active_candidates}
        for candidate in self.candidates:
            manifest = candidate.manifest
            if public_only and manifest.export_posture is ExportPosture.PRIVATE:
                continue
            record = dict(manifest.catalog_record())
            record["candidate_provenance"] = candidate.provenance_id
            record["active"] = candidate.provenance_id in active_provenance
            records.append(record)
        return tuple(records)


def redact_detail(detail: object, *, secret_values: Iterable[str] = ()) -> str:
    """Bound hostile detail and remove common credential representations."""

    try:
        rendered = str(detail)
    except BaseException:
        rendered = "Unprintable detail"
    ordered_secrets = sorted(
        {value for value in secret_values if isinstance(value, str) and value},
        key=lambda value: (-len(value), value),
    )
    for value in ordered_secrets:
        rendered = rendered.replace(value, "[REDACTED]")
    text = _CONTROL_RE.sub("?", rendered)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _URL_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = _STANDALONE_CREDENTIAL_RE.sub("[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > MAX_DETAIL_CHARS:
        text = f"{text[: MAX_DETAIL_CHARS - 3]}..."
    return text


def _contains_secret_shaped_text(value: str) -> bool:
    """Reject metadata that would need credential redaction in a catalog."""

    return bool(
        _SECRET_ASSIGNMENT_RE.search(value)
        or _URL_SECRET_RE.search(value)
        or _STANDALONE_CREDENTIAL_RE.search(value)
    )


def core_version_satisfies(current_version: str, constraint: str | None) -> bool:
    """Evaluate the closed v2 core-version constraint language.

    The manifest contract intentionally supports only comma-separated semantic
    comparisons, compatible (``~=``), caret, and trailing-wildcard clauses.  It
    does not silently accept an opaque package-manager expression.
    """

    if constraint is None:
        return True
    current = _parse_core_version(current_version, require_full=True)
    clauses = _parse_core_constraint(constraint)
    return all(_core_clause_matches(current, clause) for clause in clauses)


def parse_capability_manifest(
    raw: Mapping[str, Any],
    *,
    physical_source: ManifestSource | str,
) -> CapabilityPluginManifest:
    """Parse one strict closed v2 manifest.

    Callers must classify unmarked legacy files before invoking this parser.
    """

    invalid_source = False
    source: ManifestSource | None = None
    try:
        source = ManifestSource(physical_source)
    except (TypeError, ValueError):
        invalid_source = True
    if invalid_source:
        raise CapabilityManifestError(
            "invalid_physical_source", "Unknown physical source"
        ) from None
    assert source is not None

    raw = _require_mapping(raw, "manifest_root")
    if type(raw.get("manifestVersion")) is not int or raw.get("manifestVersion") != 2:
        raise CapabilityManifestError("unsupported_manifest_version", "manifestVersion must be 2")

    _require_closed_fields(
        raw,
        required={
            "manifestVersion",
            "id",
            "name",
            "version",
            "description",
            "source",
            "entrypoint",
            "requirements",
            "contributions",
            "enabledByDefault",
            "replaces",
            "contractVersion",
            "export",
        },
        code_prefix="manifest",
    )

    plugin_id = _require_id(raw["id"], "invalid_plugin_id")
    name = _require_text(raw["name"], "invalid_name", maximum=160, allow_empty=False)
    if _contains_secret_shaped_text(name):
        raise CapabilityManifestError(
            "secret_shaped_metadata", "Manifest name contains credential-shaped text"
        )
    version = _require_text(raw["version"], "invalid_version", maximum=128, allow_empty=False)
    if not _VERSION_RE.fullmatch(version):
        raise CapabilityManifestError("invalid_version", "version must be semantic version syntax")
    if _contains_secret_shaped_text(version):
        raise CapabilityManifestError(
            "secret_shaped_metadata", "Manifest version contains credential-shaped text"
        )
    description = _require_text(
        raw["description"], "invalid_description", maximum=2_000, allow_empty=False
    )
    if _contains_secret_shaped_text(description):
        raise CapabilityManifestError(
            "secret_shaped_metadata",
            "Manifest description contains credential-shaped text",
        )

    invalid_declared_source = False
    declared_source: ManifestSource | None = None
    try:
        declared_source = ManifestSource(raw["source"])
    except (TypeError, ValueError):
        invalid_declared_source = True
    if invalid_declared_source:
        raise CapabilityManifestError(
            "invalid_source", "Manifest source is not trusted"
        ) from None
    assert declared_source is not None
    if declared_source is not source:
        raise CapabilityManifestError("source_mismatch", "Manifest source does not match discovery")

    entrypoint = _require_text(
        raw["entrypoint"], "invalid_entrypoint", maximum=256, allow_empty=False
    )
    if not _ENTRYPOINT_RE.fullmatch(entrypoint):
        raise CapabilityManifestError(
            "invalid_entrypoint", "entrypoint must be a relative module:function reference"
        )

    requirements = _parse_requirements(raw["requirements"])
    contributions = _parse_contributions(raw["contributions"])

    if type(raw["enabledByDefault"]) is not bool:
        raise CapabilityManifestError(
            "invalid_enabled_by_default", "enabledByDefault must be a boolean"
        )

    replaces = _parse_replacement(raw["replaces"])
    contract_version = _require_positive_int(raw["contractVersion"], "invalid_contract_version")

    invalid_export = False
    export_posture: ExportPosture | None = None
    try:
        export_posture = ExportPosture(raw["export"])
    except (TypeError, ValueError):
        invalid_export = True
    if invalid_export:
        raise CapabilityManifestError(
            "invalid_export", "export must be public or private"
        ) from None
    assert export_posture is not None

    return CapabilityPluginManifest(
        manifest_version=2,
        id=plugin_id,
        name=name,
        version=version,
        description=description,
        source=source,
        entrypoint=entrypoint,
        requirements=requirements,
        contributions=contributions,
        enabled_by_default=raw["enabledByDefault"],
        replaces=replaces,
        contract_version=contract_version,
        export_posture=export_posture,
    )


def classify_manifest(
    raw: Mapping[str, Any],
    *,
    physical_source: ManifestSource | str,
) -> ManifestClassification:
    """Classify only unmarked manifests as legacy compatibility packages."""

    raw = _require_mapping(raw, "manifest_root")
    if "manifestVersion" not in raw:
        return ManifestClassification.LEGACY
    parse_capability_manifest(raw, physical_source=physical_source)
    return ManifestClassification.CAPABILITY_V2


def discover_capability_plugins(
    sources: Iterable[FilesystemPluginSource],
    *,
    include_project: bool = False,
    approved_entry_points: Iterable[str] | None = None,
    entry_points_provider: Callable[[], Any] | None = None,
) -> CapabilityPluginDiscovery:
    """Discover and conflict-check trusted candidates without importing code."""

    candidates: list[CapabilityPluginCandidate] = []
    legacy: list[LegacyExtensionManifest] = []
    errors: list[PluginDiscoveryError] = []

    for descriptor in sorted(
        tuple(sources),
        key=lambda item: (SOURCE_RANK[item.source], str(item.path).casefold()),
    ):
        if descriptor.source is ManifestSource.PROJECT and not include_project:
            errors.append(
                PluginDiscoveryError(
                    code="project_source_opt_in_required",
                    source=descriptor.source,
                    location=descriptor.path.name or "project",
                    detail="Project-local capability discovery was not opted in",
                )
            )
            continue
        _discover_filesystem_source(descriptor, candidates, legacy, errors)

    if approved_entry_points is not None:
        _discover_entry_points(
            frozenset(approved_entry_points),
            entry_points_provider,
            candidates,
            errors,
        )

    ordered = tuple(sorted(candidates, key=lambda item: item.sort_key))
    active, conflict_errors, replacements = preflight_capability_candidates(ordered)
    errors.extend(conflict_errors)
    return CapabilityPluginDiscovery(
        candidates=ordered,
        active_candidates=active,
        legacy=tuple(legacy),
        errors=tuple(errors),
        replacements=replacements,
    )


def preflight_capability_candidates(
    candidates: Sequence[CapabilityPluginCandidate],
) -> tuple[
    tuple[CapabilityPluginCandidate, ...],
    tuple[PluginDiscoveryError, ...],
    tuple[ReplacementResolution, ...],
]:
    """Resolve duplicates/replacements before any candidate can be imported."""

    active_by_id: dict[str, CapabilityPluginCandidate] = {}
    contribution_owner: dict[str, str] = {}
    errors: list[PluginDiscoveryError] = []
    replacements: list[ReplacementResolution] = []

    for candidate in sorted(candidates, key=lambda item: item.sort_key):
        manifest = candidate.manifest
        replacement = manifest.replaces
        existing_same_id = active_by_id.get(manifest.id)

        if replacement is None:
            if existing_same_id is not None:
                errors.append(_candidate_error(candidate, "duplicate_plugin_id"))
                continue
            conflicts = {
                contribution_owner[item.id]
                for item in manifest.contributions
                if item.id in contribution_owner
            }
            if conflicts:
                errors.append(_candidate_error(candidate, "duplicate_contribution_id"))
                continue
            _activate_candidate(candidate, active_by_id, contribution_owner)
            continue

        target = active_by_id.get(replacement.id)
        if target is None:
            errors.append(_candidate_error(candidate, "replacement_target_missing"))
            continue
        if existing_same_id is not None and existing_same_id is not target:
            errors.append(_candidate_error(candidate, "duplicate_plugin_id"))
            continue
        if target.manifest.contract_version != replacement.contract_version:
            errors.append(_candidate_error(candidate, "replacement_contract_mismatch"))
            continue
        if target.manifest.contract_fingerprint != replacement.contract_fingerprint:
            errors.append(_candidate_error(candidate, "replacement_target_fingerprint_mismatch"))
            continue
        if manifest.contract_fingerprint != replacement.contract_fingerprint:
            errors.append(_candidate_error(candidate, "replacement_candidate_fingerprint_mismatch"))
            continue
        conflicts = {
            contribution_owner[item.id]
            for item in manifest.contributions
            if item.id in contribution_owner
        }
        if conflicts - {target.manifest.id}:
            errors.append(_candidate_error(candidate, "replacement_scope_conflict"))
            continue

        _deactivate_candidate(target, active_by_id, contribution_owner)
        _activate_candidate(candidate, active_by_id, contribution_owner)
        replacements.append(
            ReplacementResolution(
                target_id=target.manifest.id,
                target_provenance=target.provenance_id,
                replacement_id=manifest.id,
                replacement_provenance=candidate.provenance_id,
                contract_fingerprint=replacement.contract_fingerprint,
            )
        )

    _remove_invalid_plugin_dependencies(active_by_id, contribution_owner, errors)
    _remove_plugin_dependency_cycles(active_by_id, contribution_owner, errors)
    _remove_invalid_plugin_dependencies(active_by_id, contribution_owner, errors)

    active = tuple(sorted(active_by_id.values(), key=lambda item: item.sort_key))
    return active, tuple(errors), tuple(replacements)


def contribution_topological_order(
    manifest: CapabilityPluginManifest,
) -> tuple[str, ...]:
    """Return dependencies before dependents for one validated manifest."""

    dependencies = {item.id: item.depends_on for item in manifest.contributions}
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    for root_id in dependencies:
        if root_id in visited:
            continue
        stack: list[tuple[str, bool]] = [(root_id, False)]
        while stack:
            contribution_id, expanded = stack.pop()
            if expanded:
                visiting.remove(contribution_id)
                visited.add(contribution_id)
                ordered.append(contribution_id)
                continue
            if contribution_id in visited:
                continue
            if contribution_id in visiting:
                raise CapabilityManifestError(
                    "contribution_dependency_cycle",
                    "Contribution dependencies must be acyclic",
                )
            visiting.add(contribution_id)
            stack.append((contribution_id, True))
            for dependency in reversed(dependencies[contribution_id]):
                if dependency in visiting:
                    raise CapabilityManifestError(
                        "contribution_dependency_cycle",
                        "Contribution dependencies must be acyclic",
                    )
                if dependency not in visited:
                    stack.append((dependency, False))
    return tuple(ordered)


def _parse_requirements(raw: object) -> PluginRequirements:
    mapping = _require_mapping(raw, "invalid_requirements")
    _require_closed_fields(
        mapping,
        required=set(),
        optional={"coreVersion", "env", "plugins"},
        code_prefix="requirements",
    )
    core_version_raw = mapping.get("coreVersion")
    core_version: str | None = None
    if core_version_raw is not None:
        core_version = _require_text(
            core_version_raw, "invalid_core_version", maximum=128, allow_empty=False
        )
        if not _CORE_VERSION_RE.fullmatch(core_version):
            raise CapabilityManifestError(
                "invalid_core_version", "coreVersion contains unsupported syntax"
            )
        _parse_core_constraint(core_version)
    env = _parse_unique_strings(mapping.get("env", []), "invalid_env_requirements")
    if any(not _ENV_NAME_RE.fullmatch(item) for item in env):
        raise CapabilityManifestError(
            "invalid_env_requirements", "Environment requirements must contain names only"
        )
    plugins = _parse_unique_strings(
        mapping.get("plugins", []), "invalid_plugin_requirements"
    )
    for plugin_id in plugins:
        _require_id(plugin_id, "invalid_plugin_requirements")
    return PluginRequirements(core_version=core_version, env=env, plugins=plugins)


def _parse_contributions(raw: object) -> tuple[ContributionDeclaration, ...]:
    if not isinstance(raw, list) or not raw:
        raise CapabilityManifestError(
            "invalid_contributions", "contributions must be a nonempty list"
        )
    contributions: list[ContributionDeclaration] = []
    seen: set[str] = set()
    for item_raw in raw:
        item = _require_mapping(item_raw, "invalid_contribution")
        _require_closed_fields(
            item,
            required={"id", "type", "dependsOn"},
            code_prefix="contribution",
        )
        contribution_id = _require_id(item["id"], "invalid_contribution_id")
        if contribution_id in seen:
            raise CapabilityManifestError(
                "duplicate_manifest_contribution", "Contribution IDs must be unique"
            )
        seen.add(contribution_id)
        invalid_contribution_type = False
        contribution_type: ContributionType | None = None
        try:
            contribution_type = ContributionType(item["type"])
        except (TypeError, ValueError):
            invalid_contribution_type = True
        if invalid_contribution_type:
            raise CapabilityManifestError(
                "invalid_contribution_type", "Unknown contribution type"
            ) from None
        assert contribution_type is not None
        depends_on = _parse_unique_strings(
            item["dependsOn"], "invalid_contribution_dependencies"
        )
        for dependency in depends_on:
            _require_id(dependency, "invalid_contribution_dependencies")
        contributions.append(
            ContributionDeclaration(
                id=contribution_id,
                type=contribution_type,
                depends_on=depends_on,
            )
        )

    for item in contributions:
        if item.id in item.depends_on:
            raise CapabilityManifestError(
                "contribution_self_dependency", "A contribution cannot depend on itself"
            )
        if any(dependency not in seen for dependency in item.depends_on):
            raise CapabilityManifestError(
                "unknown_contribution_dependency", "Contribution dependency is undeclared"
            )
    manifest_stub = CapabilityPluginManifest(
        manifest_version=2,
        id="validation.stub",
        name="validation",
        version="0.0.0",
        description="validation",
        source=ManifestSource.BUNDLED,
        entrypoint="plugin:register",
        requirements=PluginRequirements(),
        contributions=tuple(contributions),
        enabled_by_default=False,
        replaces=None,
        contract_version=1,
        export_posture=ExportPosture.PUBLIC,
    )
    contribution_topological_order(manifest_stub)
    return tuple(contributions)


def _parse_replacement(raw: object) -> ReplacementDeclaration | None:
    if raw is None:
        return None
    mapping = _require_mapping(raw, "invalid_replacement")
    _require_closed_fields(
        mapping,
        required={"id", "contractVersion", "contractFingerprint"},
        code_prefix="replacement",
    )
    return ReplacementDeclaration(
        id=_require_id(mapping["id"], "invalid_replacement_id"),
        contract_version=_require_positive_int(
            mapping["contractVersion"], "invalid_replacement_contract_version"
        ),
        contract_fingerprint=_require_fingerprint(mapping["contractFingerprint"]),
    )


def _require_fingerprint(raw: object) -> str:
    value = _require_text(
        raw,
        "invalid_replacement_contract_fingerprint",
        maximum=64,
        allow_empty=False,
    )
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise CapabilityManifestError(
            "invalid_replacement_contract_fingerprint",
            "Replacement contract fingerprint must be lowercase SHA-256",
        )
    return value


def _parse_core_version(value: str, *, require_full: bool) -> tuple[int, int, int]:
    parts = value.split(".")
    if require_full and len(parts) != 3:
        raise CapabilityManifestError(
            "invalid_runtime_core_version", "Runtime core version is not semantic version syntax"
        )
    if not 1 <= len(parts) <= 3 or any(
        not re.fullmatch(r"0|[1-9]\d*", part) for part in parts
    ):
        raise CapabilityManifestError(
            "invalid_core_version", "coreVersion contains unsupported semantic version syntax"
        )
    return tuple(int(part) for part in (*parts, *("0",) * (3 - len(parts))))  # type: ignore[return-value]


def _parse_core_constraint(constraint: str) -> tuple[tuple[str, tuple[int, int, int], int], ...]:
    clauses: list[tuple[str, tuple[int, int, int], int]] = []
    for raw_clause in constraint.split(","):
        clause = raw_clause.strip()
        match = _CORE_CLAUSE_RE.fullmatch(clause)
        if match is None:
            raise CapabilityManifestError(
                "invalid_core_version", "coreVersion uses unsupported constraint syntax"
            )
        operator = match.group("operator") or "=="
        version_text = match.group("version")
        precision = len(version_text.split("."))
        if match.group("wildcard"):
            if operator not in {"==", "="}:
                raise CapabilityManifestError(
                    "invalid_core_version", "coreVersion wildcard cannot use a comparator"
                )
            operator = "wildcard"
        clauses.append((operator, _parse_core_version(version_text, require_full=False), precision))
    if not clauses:
        raise CapabilityManifestError("invalid_core_version", "coreVersion is empty")
    return tuple(clauses)


def _core_clause_matches(
    current: tuple[int, int, int],
    clause: tuple[str, tuple[int, int, int], int],
) -> bool:
    operator, required, precision = clause
    if operator == "wildcard":
        compared_parts = max(1, precision)
        return current[:compared_parts] == required[:compared_parts]
    if operator in {"==", "="}:
        return current == required
    if operator == "!=":
        return current != required
    if operator == ">=":
        return current >= required
    if operator == "<=":
        return current <= required
    if operator == ">":
        return current > required
    if operator == "<":
        return current < required
    if operator == "~=":
        upper = (
            (required[0] + 1, 0, 0)
            if precision <= 2
            else (required[0], required[1] + 1, 0)
        )
        return required <= current < upper
    if operator == "^":
        if required[0] > 0:
            upper = (required[0] + 1, 0, 0)
        elif required[1] > 0:
            upper = (0, required[1] + 1, 0)
        else:
            upper = (0, 0, required[2] + 1)
        return required <= current < upper
    raise AssertionError(f"unsupported core comparator: {operator}")


def _validate_artifact_entrypoint(
    manifest: CapabilityPluginManifest,
    artifacts: tuple[CapabilityPluginArtifact, ...],
) -> None:
    module_ref = manifest.entrypoint.rsplit(":", 1)[0].replace(".", "/")
    module_path = f"{module_ref}.py"
    package_path = f"{module_ref}/__init__.py"
    paths = {item.relative_path for item in artifacts}
    matches = int(module_path in paths) + int(package_path in paths)
    if matches == 0:
        raise CapabilityManifestError(
            "entrypoint_module_missing",
            "Entrypoint module is missing from the verified artifact set",
        )
    if matches > 1:
        raise CapabilityManifestError(
            "entrypoint_module_ambiguous",
            "Entrypoint resolves to both a module and a package",
        )

    identities: set[str] = set()
    for item in artifacts:
        identity = (
            item.relative_path[: -len("/__init__.py")]
            if item.relative_path.endswith("/__init__.py")
            else item.relative_path[:-3]
        )
        if identity in identities:
            raise CapabilityManifestError(
                "artifact_module_ambiguous",
                "Verified artifacts contain an ambiguous Python module",
            )
        identities.add(identity)


def _capture_artifacts(
    root: Path,
    relative_paths: Iterable[str],
    manifest: CapabilityPluginManifest,
) -> tuple[CapabilityPluginArtifact, ...]:
    captured: list[CapabilityPluginArtifact] = []
    total_bytes = 0
    for relative_path in sorted(set(relative_paths)):
        if len(captured) >= MAX_PLUGIN_ARTIFACTS:
            raise CapabilityManifestError(
                "artifact_set_too_large", "Plugin contains too many Python artifacts"
            )
        logical_path = root.joinpath(*relative_path.split("/"))
        resolved_path = logical_path.resolve(strict=True)
        if not resolved_path.is_relative_to(root) or not resolved_path.is_file():
            raise CapabilityManifestError(
                "artifact_path_escape", "Plugin artifact escapes its verified root"
            )
        if resolved_path.stat().st_size > MAX_PLUGIN_ARTIFACT_BYTES:
            raise CapabilityManifestError(
                "artifact_too_large", "Plugin artifact exceeds its size limit"
            )
        source = resolved_path.read_bytes()
        total_bytes += len(source)
        if total_bytes > MAX_PLUGIN_ARTIFACT_TOTAL_BYTES:
            raise CapabilityManifestError(
                "artifact_set_too_large", "Plugin artifact set exceeds its size limit"
            )
        captured.append(
            CapabilityPluginArtifact(relative_path=relative_path, source=source)
        )
    result = tuple(captured)
    _validate_artifact_entrypoint(manifest, result)
    return result


def _snapshot_filesystem_artifacts(
    root: Path,
    manifest: CapabilityPluginManifest,
) -> tuple[CapabilityPluginArtifact, ...]:
    relative_paths: list[str] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        relative_paths.append(path.relative_to(root).as_posix())
    return _capture_artifacts(root, relative_paths, manifest)


def _snapshot_entry_point_artifacts(
    entry_point: Any,
    manifest: CapabilityPluginManifest,
) -> tuple[Path, tuple[CapabilityPluginArtifact, ...]]:
    distribution = getattr(entry_point, "dist", None)
    files = getattr(distribution, "files", None) if distribution is not None else None
    locate_file = (
        getattr(distribution, "locate_file", None) if distribution is not None else None
    )
    if files is None or not callable(locate_file):
        raise CapabilityManifestError(
            "entry_point_artifacts_unavailable",
            "Approved entry point has no verifiable distribution artifacts",
        )
    root = Path(locate_file("")).resolve(strict=True)
    top_level = manifest.entrypoint.split(":", 1)[0].split(".", 1)[0]
    relative_paths: list[str] = []
    for item in tuple(files):
        relative_path = str(item).replace("\\", "/")
        if (
            relative_path == f"{top_level}.py"
            or relative_path.startswith(f"{top_level}/")
        ) and relative_path.endswith(".py"):
            if (
                relative_path.startswith("/")
                or any(part in {"", ".", ".."} for part in relative_path.split("/"))
            ):
                raise CapabilityManifestError(
                    "entry_point_artifact_path_invalid",
                    "Distribution artifact path is not canonical",
                )
            located = Path(locate_file(item)).resolve(strict=True)
            if not located.is_relative_to(root):
                raise CapabilityManifestError(
                    "entry_point_artifact_escape",
                    "Distribution artifact escapes its package root",
                )
            canonical = root.joinpath(*relative_path.split("/")).resolve(strict=True)
            if located != canonical:
                raise CapabilityManifestError(
                    "entry_point_artifact_mismatch",
                    "Distribution artifact location does not match its declared path",
                )
            relative_paths.append(relative_path)
    return root, _capture_artifacts(root, relative_paths, manifest)


def _discover_filesystem_source(
    descriptor: FilesystemPluginSource,
    candidates: list[CapabilityPluginCandidate],
    legacy: list[LegacyExtensionManifest],
    errors: list[PluginDiscoveryError],
) -> None:
    root = descriptor.path
    if not root.is_dir():
        return
    try:
        resolved_root = root.resolve(strict=True)
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        errors.append(
            PluginDiscoveryError(
                code="source_unreadable",
                source=descriptor.source,
                location=root.name or descriptor.source.value,
                detail="Capability source could not be read",
            )
        )
        return

    for child in children:
        manifest_path = child / MANIFEST_FILENAME
        if not child.is_dir() or not manifest_path.is_file():
            continue
        location = child.name
        try:
            resolved_child = child.resolve(strict=True)
            if not resolved_child.is_relative_to(resolved_root):
                raise CapabilityManifestError(
                    "source_path_escape", "Plugin directory escapes its trusted source"
                )
            resolved_manifest = manifest_path.resolve(strict=True)
            if not resolved_manifest.is_relative_to(resolved_child):
                raise CapabilityManifestError(
                    "manifest_path_escape", "Manifest escapes its trusted plugin directory"
                )
            raw = _read_manifest_path(resolved_manifest)
            if "manifestVersion" not in raw:
                legacy.append(
                    LegacyExtensionManifest(location=location, source=descriptor.source)
                )
                continue
            manifest = parse_capability_manifest(raw, physical_source=descriptor.source)
            artifacts = _snapshot_filesystem_artifacts(resolved_child, manifest)
            candidates.append(
                CapabilityPluginCandidate(
                    manifest=manifest,
                    location_key=str(resolved_child),
                    plugin_path=resolved_child,
                    manifest_path=resolved_manifest,
                    artifact_root=resolved_child,
                    artifacts=artifacts,
                )
            )
        except CapabilityManifestError as exc:
            errors.append(
                PluginDiscoveryError(
                    code=exc.code,
                    source=descriptor.source,
                    location=location,
                    detail=exc.detail,
                )
            )
        except OSError:
            errors.append(
                PluginDiscoveryError(
                    code="manifest_unreadable",
                    source=descriptor.source,
                    location=location,
                    detail="Manifest could not be read",
                )
            )


def _discover_entry_points(
    approved_names: frozenset[str],
    provider: Callable[[], Any] | None,
    candidates: list[CapabilityPluginCandidate],
    errors: list[PluginDiscoveryError],
) -> None:
    try:
        all_entry_points = provider() if provider is not None else importlib_metadata.entry_points()
        if hasattr(all_entry_points, "select"):
            entry_points = tuple(all_entry_points.select(group=ENTRY_POINT_GROUP))
        else:
            entry_points = tuple(
                item
                for item in all_entry_points
                if getattr(item, "group", None) == ENTRY_POINT_GROUP
            )
    except KeyboardInterrupt:
        raise
    except BaseException:  # third-party metadata cannot terminate discovery
        errors.append(
            PluginDiscoveryError(
                code="entry_point_metadata_unavailable",
                source=ManifestSource.PYTHON_ENTRY_POINT,
                location="python-entry-points",
                detail="Python entry-point metadata could not be read",
            )
        )
        return

    for entry_point in sorted(
        entry_points,
        key=_entry_point_sort_key,
    ):
        try:
            name = getattr(entry_point, "name", "")
        except KeyboardInterrupt:
            raise
        except BaseException:
            errors.append(
                PluginDiscoveryError(
                    code="entry_point_metadata_unavailable",
                    source=ManifestSource.PYTHON_ENTRY_POINT,
                    location="python-entry-point",
                    detail="Installed entry-point metadata could not be read",
                )
            )
            continue
        if type(name) is not str:
            errors.append(
                PluginDiscoveryError(
                    code="entry_point_metadata_unavailable",
                    source=ManifestSource.PYTHON_ENTRY_POINT,
                    location="python-entry-point",
                    detail="Installed entry-point name must be an exact string",
                )
            )
            continue
        if name not in approved_names:
            errors.append(
                PluginDiscoveryError(
                    code="entry_point_not_approved",
                    source=ManifestSource.PYTHON_ENTRY_POINT,
                    location="python-entry-point",
                    detail="Installed entry point is not approved",
                )
            )
            continue
        try:
            raw_text = _read_distribution_manifest(entry_point)
            raw = _decode_manifest_text(raw_text)
            manifest = parse_capability_manifest(
                raw, physical_source=ManifestSource.PYTHON_ENTRY_POINT
            )
            entry_point_value = getattr(entry_point, "value", None)
            if type(entry_point_value) is not str or manifest.entrypoint != entry_point_value:
                raise CapabilityManifestError(
                    "entry_point_mismatch",
                    "Manifest entrypoint does not match installed entry-point metadata",
                )
            artifact_root, artifacts = _snapshot_entry_point_artifacts(
                entry_point, manifest
            )
            distribution_label = _distribution_label(entry_point)
            candidates.append(
                CapabilityPluginCandidate(
                    manifest=manifest,
                    location_key=f"{distribution_label}:{name}",
                    artifact_root=artifact_root,
                    artifacts=artifacts,
                    entry_point=entry_point,
                )
            )
        except CapabilityManifestError as exc:
            errors.append(
                PluginDiscoveryError(
                    code=exc.code,
                    source=ManifestSource.PYTHON_ENTRY_POINT,
                    location="python-entry-point",
                    detail=exc.detail,
                )
            )
        except KeyboardInterrupt:
            raise
        except BaseException:  # distribution objects are third-party metadata
            errors.append(
                PluginDiscoveryError(
                    code="entry_point_manifest_unreadable",
                    source=ManifestSource.PYTHON_ENTRY_POINT,
                    location="python-entry-point",
                    detail="Approved entry-point manifest could not be read",
                )
            )


def _read_distribution_manifest(entry_point: Any) -> str:
    distribution = getattr(entry_point, "dist", None)
    if distribution is None:
        raise CapabilityManifestError(
            "entry_point_distribution_missing", "Entry point has no distribution metadata"
        )
    read_text = getattr(distribution, "read_text", None)
    if callable(read_text):
        text = read_text(ENTRY_POINT_MANIFEST)
        if text is not None:
            if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
                raise CapabilityManifestError("manifest_too_large", "Manifest exceeds size limit")
            return text
    locate_file = getattr(distribution, "locate_file", None)
    if not callable(locate_file):
        raise CapabilityManifestError(
            "entry_point_manifest_missing", "Distribution manifest resource is missing"
        )
    root = Path(locate_file("")).resolve(strict=True)
    path = Path(locate_file(ENTRY_POINT_MANIFEST)).resolve(strict=True)
    if not path.is_relative_to(root):
        raise CapabilityManifestError(
            "entry_point_manifest_escape", "Distribution manifest escapes its package root"
        )
    return _read_manifest_text_path(path)


def _read_manifest_path(path: Path) -> Mapping[str, Any]:
    return _decode_manifest_text(_read_manifest_text_path(path))


def _read_manifest_text_path(path: Path) -> str:
    invalid_encoding = False
    text = ""
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise CapabilityManifestError("manifest_too_large", "Manifest exceeds size limit")
        text = path.read_text(encoding="utf-8")
    except UnicodeError:
        invalid_encoding = True
    if invalid_encoding:
        raise CapabilityManifestError(
            "manifest_encoding", "Manifest must be UTF-8"
        ) from None
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise CapabilityManifestError("manifest_too_large", "Manifest exceeds size limit")
    return text


def _decode_manifest_text(text: str) -> Mapping[str, Any]:
    invalid_code = ""
    try:
        raw = json.loads(text)
    except RecursionError:
        invalid_code = "manifest_nesting_too_deep"
    except json.JSONDecodeError:
        invalid_code = "manifest_json"
    if invalid_code:
        detail = (
            "Manifest JSON nesting exceeds the safe limit"
            if invalid_code == "manifest_nesting_too_deep"
            else "Manifest is not valid JSON"
        )
        raise CapabilityManifestError(invalid_code, detail) from None
    return _require_mapping(raw, "manifest_root")


def _require_mapping(raw: object, code: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise CapabilityManifestError(code, "Expected a JSON object with string keys")
    return raw


def _require_closed_fields(
    raw: Mapping[str, Any],
    *,
    required: set[str],
    code_prefix: str,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - raw.keys()
    if missing:
        raise CapabilityManifestError(
            f"{code_prefix}_missing_fields", "Required fields are missing"
        )
    unknown = raw.keys() - required - optional
    if unknown:
        raise CapabilityManifestError(
            f"{code_prefix}_unknown_fields", "Unknown fields are not allowed"
        )


def _require_text(
    raw: object,
    code: str,
    *,
    maximum: int,
    allow_empty: bool,
) -> str:
    if not isinstance(raw, str):
        raise CapabilityManifestError(code, "Expected a string")
    if raw != raw.strip() or (not allow_empty and not raw):
        raise CapabilityManifestError(code, "String must be nonempty and trimmed")
    if len(raw) > maximum or _FIELD_CONTROL_RE.search(raw):
        raise CapabilityManifestError(code, "String is invalid or exceeds its size limit")
    return raw


def _require_id(raw: object, code: str) -> str:
    value = _require_text(raw, code, maximum=128, allow_empty=False)
    if not _ID_RE.fullmatch(value) or _contains_secret_shaped_text(value):
        raise CapabilityManifestError(code, "Identifier syntax is invalid")
    return value


def _require_positive_int(raw: object, code: str) -> int:
    if type(raw) is not int or raw <= 0:
        raise CapabilityManifestError(code, "Expected a positive integer")
    return raw


def _parse_unique_strings(raw: object, code: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise CapabilityManifestError(code, "Expected a list of strings")
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = _require_text(item, code, maximum=128, allow_empty=False)
        if value in seen:
            raise CapabilityManifestError(code, "Duplicate list values are not allowed")
        seen.add(value)
        values.append(value)
    return tuple(values)


def _safe_label(value: object) -> str:
    text = redact_detail(value)
    text = "".join(char if char.isalnum() or char in "._-" else "_" for char in text)
    return (text[:120] or "unknown").strip(".") or "unknown"


def _entry_point_sort_key(entry_point: Any) -> tuple[str, str]:
    try:
        name = getattr(entry_point, "name", "")
        value = getattr(entry_point, "value", "")
    except KeyboardInterrupt:
        raise
    except BaseException:
        return ("unknown", "unknown")
    return (_safe_label(name).casefold(), _safe_label(value).casefold())


def _distribution_label(entry_point: Any) -> str:
    distribution = getattr(entry_point, "dist", None)
    metadata = getattr(distribution, "metadata", {}) if distribution is not None else {}
    get = getattr(metadata, "get", None)
    name = get("Name") if callable(get) else None
    return _safe_label(name or "distribution")


def _candidate_error(
    candidate: CapabilityPluginCandidate,
    code: str,
) -> PluginDiscoveryError:
    return PluginDiscoveryError(
        code=code,
        source=candidate.manifest.source,
        location=candidate.plugin_path.name if candidate.plugin_path else "python-entry-point",
        plugin_id=candidate.manifest.id,
        detail="Capability candidate failed conflict preflight",
    )


def _activate_candidate(
    candidate: CapabilityPluginCandidate,
    active_by_id: dict[str, CapabilityPluginCandidate],
    contribution_owner: dict[str, str],
) -> None:
    active_by_id[candidate.manifest.id] = candidate
    for declaration in candidate.manifest.contributions:
        contribution_owner[declaration.id] = candidate.manifest.id


def _deactivate_candidate(
    candidate: CapabilityPluginCandidate,
    active_by_id: dict[str, CapabilityPluginCandidate],
    contribution_owner: dict[str, str],
) -> None:
    active_by_id.pop(candidate.manifest.id, None)
    for declaration in candidate.manifest.contributions:
        if contribution_owner.get(declaration.id) == candidate.manifest.id:
            contribution_owner.pop(declaration.id, None)


def _remove_invalid_plugin_dependencies(
    active_by_id: dict[str, CapabilityPluginCandidate],
    contribution_owner: dict[str, str],
    errors: list[PluginDiscoveryError],
) -> None:
    changed = True
    while changed:
        changed = False
        active_ids = set(active_by_id)
        for candidate in tuple(active_by_id.values()):
            if any(
                dependency not in active_ids
                for dependency in candidate.manifest.requirements.plugins
            ):
                errors.append(_candidate_error(candidate, "plugin_dependency_missing"))
                _deactivate_candidate(candidate, active_by_id, contribution_owner)
                changed = True


def _remove_plugin_dependency_cycles(
    active_by_id: dict[str, CapabilityPluginCandidate],
    contribution_owner: dict[str, str],
    errors: list[PluginDiscoveryError],
) -> None:
    state: dict[str, int] = {}
    cycle_ids: set[str] = set()

    for root_id in tuple(active_by_id):
        if state.get(root_id, 0) == 2:
            continue
        path: list[str] = []
        stack: list[tuple[str, int]] = [(root_id, 0)]
        while stack:
            plugin_id, dependency_index = stack[-1]
            if state.get(plugin_id, 0) == 0:
                state[plugin_id] = 1
                path.append(plugin_id)
            dependencies = active_by_id[plugin_id].manifest.requirements.plugins
            if dependency_index < len(dependencies):
                dependency = dependencies[dependency_index]
                stack[-1] = (plugin_id, dependency_index + 1)
                if dependency not in active_by_id:
                    continue
                dependency_state = state.get(dependency, 0)
                if dependency_state == 0:
                    stack.append((dependency, 0))
                elif dependency_state == 1:
                    cycle_ids.update(path[path.index(dependency) :])
                continue
            stack.pop()
            path.pop()
            state[plugin_id] = 2
    for plugin_id in sorted(cycle_ids):
        candidate = active_by_id.get(plugin_id)
        if candidate is None:
            continue
        errors.append(_candidate_error(candidate, "plugin_dependency_cycle"))
        _deactivate_candidate(candidate, active_by_id, contribution_owner)


__all__ = [
    "CapabilityManifestError",
    "CapabilityPluginArtifact",
    "CapabilityPluginCandidate",
    "CapabilityPluginDiscovery",
    "CapabilityPluginManifest",
    "ContributionDeclaration",
    "ContributionType",
    "ENTRY_POINT_GROUP",
    "ENTRY_POINT_MANIFEST",
    "ExportPosture",
    "FilesystemPluginSource",
    "LegacyExtensionManifest",
    "ManifestClassification",
    "ManifestSource",
    "PluginDiscoveryError",
    "PluginRequirements",
    "ReplacementDeclaration",
    "ReplacementResolution",
    "classify_manifest",
    "contribution_topological_order",
    "core_version_satisfies",
    "discover_capability_plugins",
    "parse_capability_manifest",
    "preflight_capability_candidates",
    "redact_detail",
]
