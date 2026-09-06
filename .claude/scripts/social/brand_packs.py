"""Immutable, provider-neutral brand intent loaded from approved roots.

Brand packs contain references to operator-owned files, never the referenced
contents or provider credentials.  The loader resolves every path physically
before checking containment so ``..`` segments and symlink escapes fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
SUPPORTED_IMAGE_ASPECTS = frozenset({"1:1", "16:9", "4:5", "9:16"})
SUPPORTED_VIDEO_ASPECTS = frozenset({"1:1", "16:9", "9:16"})
SUPPORTED_MEDIA_POLICIES = frozenset({"auto", "image", "video", "none"})

_PACK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CONSUMER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOCUMENT_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
_ALLOWED_FIELDS = frozenset(
    {
        "pack_id",
        "schema_version",
        "display_name",
        "version",
        "voice_profile",
        "design_file",
        "persona_pack",
        "image_aspect",
        "video_aspect",
        "default_media_policy",
        "compliance_policy",
        "asset_policy",
        "provenance",
        "allowed_consumers",
    }
)
_REQUIRED_FIELDS = frozenset(
    {
        "pack_id",
        "schema_version",
        "display_name",
        "version",
        "provenance",
        "allowed_consumers",
    }
)


class BrandPackError(ValueError):
    """A redacted BrandPack validation or resolution failure."""


@dataclass(frozen=True, slots=True)
class BrandPack:
    """Resolved, immutable brand/media intent.

    Reference fields are physical paths contained by ``source_path.parent``.
    ``to_dict`` intentionally emits portable relative references and never
    reads or serializes referenced file contents.
    """

    pack_id: str
    schema_version: int
    display_name: str
    version: str
    source_hash: str
    provenance: str
    allowed_consumers: tuple[str, ...]
    source_path: Path = field(repr=False, compare=False)
    _approved_root: Path = field(repr=False, compare=False)
    voice_profile: Path | None = field(default=None, repr=False)
    design_file: Path | None = field(default=None, repr=False)
    persona_pack: Path | None = field(default=None, repr=False)
    image_aspect: str = "1:1"
    video_aspect: str = "9:16"
    default_media_policy: str = "auto"
    compliance_policy: Path | None = field(default=None, repr=False)
    asset_policy: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Revalidate scalar and physical-path invariants at consumption time."""

        if not isinstance(self.pack_id, str) or not _PACK_ID_PATTERN.fullmatch(
            self.pack_id
        ):
            raise BrandPackError("pack_id is invalid")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version not in SUPPORTED_SCHEMA_VERSIONS
        ):
            raise BrandPackError("schema_version is unsupported")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise BrandPackError("display_name is required")
        if not isinstance(self.version, str) or not self.version.strip():
            raise BrandPackError("version is required")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise BrandPackError("provenance is required")
        if not isinstance(self.source_hash, str) or not _HASH_PATTERN.fullmatch(
            self.source_hash
        ):
            raise BrandPackError("source_hash is invalid")
        if (
            not isinstance(self.image_aspect, str)
            or self.image_aspect not in SUPPORTED_IMAGE_ASPECTS
        ):
            raise BrandPackError("image_aspect is unsupported")
        if (
            not isinstance(self.video_aspect, str)
            or self.video_aspect not in SUPPORTED_VIDEO_ASPECTS
        ):
            raise BrandPackError("video_aspect is unsupported")
        if (
            not isinstance(self.default_media_policy, str)
            or self.default_media_policy not in SUPPORTED_MEDIA_POLICIES
        ):
            raise BrandPackError("default_media_policy is unsupported")
        if not isinstance(self.allowed_consumers, tuple) or not self.allowed_consumers:
            raise BrandPackError("allowed_consumers must not be empty")
        if any(not isinstance(item, str) for item in self.allowed_consumers):
            raise BrandPackError("allowed_consumers contains an invalid identifier")
        if len(set(self.allowed_consumers)) != len(self.allowed_consumers):
            raise BrandPackError("allowed_consumers contains duplicates")
        if any(not _CONSUMER_PATTERN.fullmatch(item) for item in self.allowed_consumers):
            raise BrandPackError("allowed_consumers contains an invalid identifier")

        approved_root = self._validated_physical_path(
            self._approved_root,
            field_name="approved root",
            expected="directory",
        )
        source = self._validated_physical_path(
            self.source_path,
            field_name="source_path",
            expected="file",
        )
        if not source.is_relative_to(approved_root):
            raise BrandPackError("source_path is outside the approved root")

        pack_root = source.parent
        for field_name, reference, expected in (
            ("voice_profile", self.voice_profile, "file"),
            ("design_file", self.design_file, "file"),
            ("persona_pack", self.persona_pack, "directory"),
            ("compliance_policy", self.compliance_policy, "file"),
            ("asset_policy", self.asset_policy, "file"),
        ):
            if reference is None:
                continue
            physical = self._validated_physical_path(
                reference,
                field_name=field_name,
                expected=expected,
            )
            if not physical.is_relative_to(pack_root):
                raise BrandPackError(f"{field_name} is outside the pack root")

    @staticmethod
    def _validated_physical_path(
        value: Path,
        *,
        field_name: str,
        expected: str,
    ) -> Path:
        if not isinstance(value, Path) or not value.is_absolute():
            raise BrandPackError(f"{field_name} must be a resolved absolute path")
        try:
            physical = value.resolve(strict=True)
        except (OSError, RuntimeError):
            raise BrandPackError(f"{field_name} is missing or unavailable") from None
        if physical != value:
            raise BrandPackError(f"{field_name} must be physically resolved")
        if expected == "file" and not physical.is_file():
            raise BrandPackError(f"{field_name} must be a file")
        if expected == "directory" and not physical.is_dir():
            raise BrandPackError(f"{field_name} must be a directory")
        return physical

    def to_dict(self) -> dict[str, object]:
        """Return a portable, content-free representation safe for receipts."""

        self.validate()
        pack_root = self.source_path.parent

        def portable(reference: Path | None) -> str | None:
            if reference is None:
                return None
            try:
                return reference.relative_to(pack_root).as_posix()
            except ValueError:  # defensive: the loader prevents this
                raise BrandPackError(
                    "resolved reference is outside the pack root"
                ) from None

        return {
            "pack_id": self.pack_id,
            "schema_version": self.schema_version,
            "display_name": self.display_name,
            "version": self.version,
            "source_hash": self.source_hash,
            "voice_profile": portable(self.voice_profile),
            "design_file": portable(self.design_file),
            "persona_pack": portable(self.persona_pack),
            "image_aspect": self.image_aspect,
            "video_aspect": self.video_aspect,
            "default_media_policy": self.default_media_policy,
            "compliance_policy": portable(self.compliance_policy),
            "asset_policy": portable(self.asset_policy),
            "provenance": self.provenance,
            "allowed_consumers": list(self.allowed_consumers),
        }


def _resolved_approved_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    if not roots:
        raise BrandPackError("at least one approved root is required")
    resolved: list[Path] = []
    for root in roots:
        try:
            physical = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise BrandPackError("an approved root is unavailable") from None
        if not physical.is_dir():
            raise BrandPackError("an approved root is not a directory")
        if physical not in resolved:
            resolved.append(physical)
    return tuple(resolved)


def _is_contained(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _read_document(path: Path) -> dict[str, Any]:
    if path.suffix.lower() not in _DOCUMENT_SUFFIXES:
        raise BrandPackError("brand pack format must be JSON or YAML")
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            parsed = json.loads(text)
        else:
            parsed = yaml.safe_load(text)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError):
        raise BrandPackError("brand pack document is unreadable or invalid") from None
    if not isinstance(parsed, Mapping):
        raise BrandPackError("brand pack document must be an object")
    if any(not isinstance(key, str) for key in parsed):
        raise BrandPackError("brand pack field names must be strings")
    return dict(parsed)


def _validate_document_shape(document: Mapping[str, Any]) -> None:
    unknown = sorted(set(document) - _ALLOWED_FIELDS)
    if unknown:
        # Do not echo arbitrary keys: a malformed document can put a credential
        # in either the key or value position.
        raise BrandPackError("brand pack contains unknown field(s)")
    missing = sorted(_REQUIRED_FIELDS - set(document))
    if missing:
        raise BrandPackError(f"missing required field(s): {', '.join(missing)}")


def _required_text(document: Mapping[str, Any], field_name: str) -> str:
    value = document.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise BrandPackError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(
    document: Mapping[str, Any],
    field_name: str,
    *,
    default: str,
) -> str:
    value = document.get(field_name, default)
    if not isinstance(value, str) or not value.strip():
        raise BrandPackError(f"{field_name} must be a non-empty string")
    return value.strip()


def _allowed_consumers(document: Mapping[str, Any]) -> tuple[str, ...]:
    value = document.get("allowed_consumers")
    if not isinstance(value, list) or not value:
        raise BrandPackError("allowed_consumers must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise BrandPackError("allowed_consumers must contain non-empty strings")
    consumers = tuple(item.strip() for item in value)
    if len(set(consumers)) != len(consumers):
        raise BrandPackError("allowed_consumers contains duplicates")
    return consumers


def _resolve_reference(
    document: Mapping[str, Any],
    field_name: str,
    *,
    pack_root: Path,
    expected: str,
) -> Path | None:
    value = document.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BrandPackError(f"{field_name} must be a non-empty relative path")
    reference = Path(value.strip())
    if reference.is_absolute():
        raise BrandPackError(f"{field_name} reference must stay inside the pack root")
    try:
        physical = (pack_root / reference).resolve(strict=True)
    except (OSError, RuntimeError):
        raise BrandPackError(f"{field_name} reference is missing or unavailable") from None
    if not physical.is_relative_to(pack_root):
        raise BrandPackError(f"{field_name} reference must stay inside the pack root")
    if expected == "file" and not physical.is_file():
        raise BrandPackError(f"{field_name} reference must be a file")
    if expected == "directory" and not physical.is_dir():
        raise BrandPackError(f"{field_name} reference must be a directory")
    return physical


def _source_hash(document: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise BrandPackError("brand pack contains a non-portable value") from None
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_brand_pack(path: Path, *, approved_roots: Sequence[Path]) -> BrandPack:
    """Load one strict JSON/YAML pack contained by an approved physical root."""

    roots = _resolved_approved_roots(approved_roots)
    try:
        source = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise BrandPackError("brand pack source is missing or unavailable") from None
    if not source.is_file():
        raise BrandPackError("brand pack source must be a file")
    if not _is_contained(source, roots):
        raise BrandPackError("brand pack source is outside approved roots")

    document = _read_document(source)
    _validate_document_shape(document)

    pack_id = _required_text(document, "pack_id")
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise BrandPackError("schema_version must be an integer")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BrandPackError("schema_version is unsupported")

    image_aspect = _optional_text(document, "image_aspect", default="1:1")
    video_aspect = _optional_text(document, "video_aspect", default="9:16")
    media_policy = _optional_text(document, "default_media_policy", default="auto")
    if image_aspect not in SUPPORTED_IMAGE_ASPECTS:
        raise BrandPackError("image_aspect is unsupported")
    if video_aspect not in SUPPORTED_VIDEO_ASPECTS:
        raise BrandPackError("video_aspect is unsupported")
    if media_policy not in SUPPORTED_MEDIA_POLICIES:
        raise BrandPackError("default_media_policy is unsupported")

    pack_root = source.parent
    approved_root = max(
        (root for root in roots if source.is_relative_to(root)),
        key=lambda root: len(root.parts),
    )
    return BrandPack(
        pack_id=pack_id,
        schema_version=schema_version,
        display_name=_required_text(document, "display_name"),
        version=_required_text(document, "version"),
        source_hash=_source_hash(document),
        provenance=_required_text(document, "provenance"),
        allowed_consumers=_allowed_consumers(document),
        source_path=source,
        _approved_root=approved_root,
        voice_profile=_resolve_reference(
            document, "voice_profile", pack_root=pack_root, expected="file"
        ),
        design_file=_resolve_reference(
            document, "design_file", pack_root=pack_root, expected="file"
        ),
        persona_pack=_resolve_reference(
            document, "persona_pack", pack_root=pack_root, expected="directory"
        ),
        image_aspect=image_aspect,
        video_aspect=video_aspect,
        default_media_policy=media_policy,
        compliance_policy=_resolve_reference(
            document, "compliance_policy", pack_root=pack_root, expected="file"
        ),
        asset_policy=_resolve_reference(
            document, "asset_policy", pack_root=pack_root, expected="file"
        ),
    )


def resolve_brand_pack(pack_id: str, *, roots: Sequence[Path]) -> BrandPack:
    """Resolve one unique pack ID across approved roots.

    Candidate documents without a ``pack_id`` are ignored. A matching malformed
    document is validated by ``load_brand_pack`` and fails closed; two distinct
    matching physical files are always ambiguous.
    """

    if not isinstance(pack_id, str) or not _PACK_ID_PATTERN.fullmatch(pack_id):
        raise BrandPackError("pack_id is invalid")
    approved = _resolved_approved_roots(roots)
    matches: dict[Path, Path] = {}
    for root in approved:
        for candidate in root.rglob("*"):
            if candidate.suffix.lower() not in _DOCUMENT_SUFFIXES or not candidate.is_file():
                continue
            try:
                physical = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not physical.is_relative_to(root):
                continue
            try:
                document = _read_document(physical)
            except BrandPackError:
                continue
            if document.get("pack_id") == pack_id:
                matches[physical] = physical

    if not matches:
        raise BrandPackError("brand pack was not found")
    if len(matches) > 1:
        raise BrandPackError("brand pack ID is ambiguous across approved roots")
    return load_brand_pack(next(iter(matches.values())), approved_roots=approved)
