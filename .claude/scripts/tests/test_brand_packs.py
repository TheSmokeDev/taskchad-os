"""Strict, tenant-safe BrandPack contract tests."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
import yaml

from social.brand_packs import (
    BrandPackError,
    load_brand_pack,
    resolve_brand_pack,
)


def _pack_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "pack_id": "orbit-labs",
        "schema_version": 1,
        "display_name": "Orbit Labs",
        "version": "2026.09",
        "voice_profile": "voice.md",
        "design_file": "design.json",
        "persona_pack": "persona",
        "image_aspect": "4:5",
        "video_aspect": "9:16",
        "default_media_policy": "image",
        "compliance_policy": "compliance.md",
        "asset_policy": "assets.md",
        "provenance": "operator-authored",
        "allowed_consumers": ["social", "content"],
    }
    document.update(overrides)
    return document


def _write_pack(
    root: Path,
    *,
    name: str = "pack.json",
    document: dict[str, object] | None = None,
) -> Path:
    pack_root = root / "orbit-labs"
    pack_root.mkdir(parents=True, exist_ok=True)
    (pack_root / "voice.md").write_text("Invented public voice.", encoding="utf-8")
    (pack_root / "design.json").write_text("{}", encoding="utf-8")
    (pack_root / "compliance.md").write_text("Public-safe claims only.", encoding="utf-8")
    (pack_root / "assets.md").write_text("Use owned assets.", encoding="utf-8")
    (pack_root / "persona").mkdir(exist_ok=True)
    path = pack_root / name
    payload = document or _pack_document()
    if path.suffix == ".json":
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("filename", ["pack.json", "pack.yaml", "pack.yml"])
def test_load_brand_pack_accepts_json_and_yaml(tmp_path: Path, filename: str):
    source = _write_pack(tmp_path, name=filename)

    pack = load_brand_pack(source, approved_roots=[tmp_path])

    assert pack.pack_id == "orbit-labs"
    assert pack.schema_version == 1
    assert pack.image_aspect == "4:5"
    assert pack.video_aspect == "9:16"
    assert pack.voice_profile == (source.parent / "voice.md").resolve()
    assert pack.design_file == (source.parent / "design.json").resolve()
    assert pack.persona_pack == (source.parent / "persona").resolve()
    assert pack.allowed_consumers == ("social", "content")
    assert pack.source_hash.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        pack.display_name = "Changed"  # type: ignore[misc]


def test_load_minimal_pack_uses_provider_neutral_defaults(tmp_path: Path):
    minimal = {
        "pack_id": "minimal",
        "schema_version": 1,
        "display_name": "Minimal",
        "version": "1",
        "provenance": "operator-authored",
        "allowed_consumers": ["social"],
    }
    root = tmp_path / "minimal"
    root.mkdir()
    source = root / "pack.json"
    source.write_text(json.dumps(minimal), encoding="utf-8")

    pack = load_brand_pack(source, approved_roots=[tmp_path])

    assert pack.voice_profile is None
    assert pack.design_file is None
    assert pack.persona_pack is None
    assert pack.image_aspect == "1:1"
    assert pack.video_aspect == "9:16"
    assert pack.default_media_policy == "auto"


def test_load_brand_pack_rejects_unknown_fields_without_echoing_values(tmp_path: Path):
    secret_value = "do-not-echo-this-provider-token"
    source = _write_pack(
        tmp_path,
        document=_pack_document(provider_api_key=secret_value),
    )

    with pytest.raises(BrandPackError) as caught:
        load_brand_pack(source, approved_roots=[tmp_path])

    assert "unknown field" in str(caught.value).lower()
    assert secret_value not in str(caught.value)


def test_load_brand_pack_rejects_unsupported_schema_version(tmp_path: Path):
    source = _write_pack(tmp_path, document=_pack_document(schema_version=99))

    with pytest.raises(BrandPackError, match="schema_version"):
        load_brand_pack(source, approved_roots=[tmp_path])


def test_load_brand_pack_rejects_missing_reference(tmp_path: Path):
    source = _write_pack(tmp_path)
    (source.parent / "design.json").unlink()

    with pytest.raises(BrandPackError, match="design_file"):
        load_brand_pack(source, approved_roots=[tmp_path])


def test_load_brand_pack_rejects_reference_path_escape(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    source = _write_pack(
        tmp_path,
        document=_pack_document(voice_profile="../outside.md"),
    )

    with pytest.raises(BrandPackError, match="voice_profile"):
        load_brand_pack(source, approved_roots=[tmp_path])


def test_direct_brand_pack_construction_revalidates_reference_containment(tmp_path: Path):
    source = _write_pack(tmp_path / "approved")
    pack = load_brand_pack(source, approved_roots=[tmp_path / "approved"])
    outside = tmp_path / "outside-voice.md"
    outside.write_text("must never reach a model prompt", encoding="utf-8")

    with pytest.raises(BrandPackError, match="voice_profile.*outside"):
        replace(pack, voice_profile=outside.resolve())


def test_load_brand_pack_rejects_unapproved_source_root(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    unapproved = tmp_path / "unapproved"
    source = _write_pack(unapproved)

    with pytest.raises(BrandPackError, match="approved"):
        load_brand_pack(source, approved_roots=[approved])


def test_load_brand_pack_rejects_symlink_escape(tmp_path: Path):
    source = _write_pack(tmp_path)
    outside = tmp_path / "outside-design.json"
    outside.write_text("{}", encoding="utf-8")
    link = source.parent / "linked-design.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    document = _pack_document(design_file="linked-design.json")
    source.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(BrandPackError, match="design_file"):
        load_brand_pack(source, approved_roots=[tmp_path])


def test_source_hash_is_deterministic_across_json_and_yaml(tmp_path: Path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = _write_pack(first_root, name="pack.json")
    second = _write_pack(second_root, name="pack.yaml")

    first_pack = load_brand_pack(first, approved_roots=[first_root])
    second_pack = load_brand_pack(second, approved_roots=[second_root])

    assert first_pack.source_hash == second_pack.source_hash


def test_resolve_brand_pack_rejects_duplicate_ids(tmp_path: Path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_pack(first_root)
    _write_pack(second_root)

    with pytest.raises(BrandPackError, match="ambiguous"):
        resolve_brand_pack("orbit-labs", roots=[first_root, second_root])


def test_resolve_brand_pack_finds_unique_id(tmp_path: Path):
    root = tmp_path / "packs"
    source = _write_pack(root)

    pack = resolve_brand_pack("orbit-labs", roots=[root])

    assert pack.source_path == source.resolve()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_aspect", "3:2"),
        ("video_aspect", "2:3"),
        ("default_media_policy", "publish"),
    ],
)
def test_load_brand_pack_rejects_unsupported_media_contract(
    tmp_path: Path,
    field: str,
    value: str,
):
    source = _write_pack(tmp_path, document=_pack_document(**{field: value}))

    with pytest.raises(BrandPackError, match=field):
        load_brand_pack(source, approved_roots=[tmp_path])


def test_serialization_is_portable_and_contains_no_reference_contents(tmp_path: Path):
    source = _write_pack(tmp_path)
    voice_contents = (source.parent / "voice.md").read_text(encoding="utf-8")

    serialized = load_brand_pack(source, approved_roots=[tmp_path]).to_dict()

    assert serialized["voice_profile"] == "voice.md"
    assert serialized["design_file"] == "design.json"
    assert voice_contents not in json.dumps(serialized)
    assert str(tmp_path.resolve()) not in json.dumps(serialized)
    assert str(tmp_path.resolve()) not in repr(
        load_brand_pack(source, approved_roots=[tmp_path])
    )


def test_optional_private_fixture_parity_uses_only_temp_copy(tmp_path: Path):
    """Opt-in local proof; the tracked test contains no tenant name or path."""

    configured = os.environ.get("HOMIE_PRIVATE_BRAND_SOURCE", "").strip()
    if not configured:
        pytest.skip("private fixture path is not configured")
    private_source = Path(configured).resolve(strict=True)
    if not private_source.is_file():
        pytest.skip("configured private fixture is unavailable")

    pack_root = tmp_path / "private-parity"
    pack_root.mkdir()
    copied_reference = pack_root / "source-reference.json"
    shutil.copy2(private_source, copied_reference)
    manifest = {
        "pack_id": "private-parity",
        "schema_version": 1,
        "display_name": "Private Parity",
        "version": "local-proof",
        "voice_profile": copied_reference.name,
        "design_file": copied_reference.name,
        "image_aspect": "4:5",
        "video_aspect": "9:16",
        "default_media_policy": "image",
        "provenance": "private-temp-copy",
        "allowed_consumers": ["social"],
    }
    manifest_path = pack_root / "brand-pack.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    pack = load_brand_pack(manifest_path, approved_roots=[tmp_path])

    assert pack.voice_profile == copied_reference.resolve()
    assert str(private_source.parent) not in json.dumps(pack.to_dict())
