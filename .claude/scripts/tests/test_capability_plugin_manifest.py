from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from runtime.capability_plugin_manifest import (
    ENTRY_POINT_GROUP,
    CapabilityManifestError,
    CapabilityPluginArtifact,
    CapabilityPluginCandidate,
    ExportPosture,
    FilesystemPluginSource,
    ManifestClassification,
    ManifestSource,
    classify_manifest,
    contribution_topological_order,
    discover_capability_plugins,
    parse_capability_manifest,
    redact_detail,
)


# Build synthetic credential shapes from fragments so the private-to-public
# sanitizer does not mistake the test corpus for live secrets and rewrite the
# exported regression itself. These values are intentionally fake.
_FAKE_BEARER = "Bearer" + " " + "abcdefghijklmnopqrstuvwxyz0123456789"
_FAKE_JWT = (
    "ey"
    + "JhbGciOiJIUzI1NiJ9"
    + "."
    + "ey"
    + "JzdWIiOiIxMjM0NTY3ODkwIn0"
    + "."
    + "signatureabcdefghijklmnop"
)
_FAKE_OPENAI_PROJECT = "sk" + "-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"
_FAKE_GITHUB_PAT = "github" + "_pat_11AAabcdefghijklmnopqrstuvwxyz012345"
_FAKE_SLACK = "xo" + "xb-123456789012-abcdefghijklmnopqrstuvwxyz"
_FAKE_GOOGLE = "AI" + "zaSyabcdefghijklmnopqrstuvwxyz123456789"
_FAKE_AWS = "AK" + "IAABCDEFGHIJKLMNOP"
_FAKE_STRIPE = "sk" + "_live_abcdefghijklmnopqrstuvwxyz012345"
_FAKE_HUGGING_FACE = "hf" + "_abcdefghijklmnopqrstuvwxyz012345"
_FAKE_OPENAI_VERSION = "1.0.0+" + "sk" + "-abcdefghijklmnop"
_FAKE_STANDALONE_CREDENTIALS = (
    _FAKE_BEARER,
    _FAKE_JWT,
    _FAKE_OPENAI_PROJECT,
    _FAKE_GITHUB_PAT,
    _FAKE_SLACK,
    _FAKE_GOOGLE,
    _FAKE_AWS,
    _FAKE_STRIPE,
    _FAKE_HUGGING_FACE,
)


def manifest(
    plugin_id: str = "example.plugin",
    *,
    source: str = "bundled",
    contribution_ids: tuple[str, ...] = ("example.value",),
    dependencies: dict[str, tuple[str, ...]] | None = None,
    replaces: dict[str, object] | None = None,
    contract_version: int = 1,
    export: str = "public",
    entrypoint: str = "plugin:register",
) -> dict[str, object]:
    dependencies = dependencies or {}
    return {
        "manifestVersion": 2,
        "id": plugin_id,
        "name": "Example plugin",
        "version": "1.2.3",
        "description": "A deterministic capability fixture.",
        "source": source,
        "entrypoint": entrypoint,
        "requirements": {"coreVersion": ">=1.6,<2", "env": [], "plugins": []},
        "contributions": [
            {"id": item, "type": "generic", "dependsOn": list(dependencies.get(item, ()))}
            for item in contribution_ids
        ],
        "enabledByDefault": False,
        "replaces": replaces,
        "contractVersion": contract_version,
        "export": export,
    }


def write_plugin(
    root: Path,
    directory: str,
    raw: dict[str, object],
    *,
    tripwire: Path | None = None,
) -> Path:
    plugin_dir = root / directory
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "extension.json").write_text(json.dumps(raw), encoding="utf-8")
    body = "def register(registrar):\n    return None\n"
    if tripwire is not None:
        body = f"from pathlib import Path\nPath({str(tripwire)!r}).write_text('imported')\n{body}"
    (plugin_dir / "plugin.py").write_text(body, encoding="utf-8")
    return plugin_dir


def test_strict_manifest_parses_to_frozen_value_objects() -> None:
    parsed = parse_capability_manifest(manifest(), physical_source=ManifestSource.BUNDLED)

    assert parsed.id == "example.plugin"
    assert parsed.contribution_ids == ("example.value",)
    assert parsed.requirements.core_version == ">=1.6,<2"
    assert parsed.export_posture is ExportPosture.PUBLIC
    with pytest.raises(FrozenInstanceError):
        parsed.version = "9.9.9"  # type: ignore[misc]


def test_candidate_provenance_covers_complete_manifest_lifecycle_identity() -> None:
    base = manifest()
    original = parse_capability_manifest(base, physical_source=ManifestSource.BUNDLED)
    changed = json.loads(json.dumps(base))
    changed["entrypoint"] = "replacement:register"
    changed["requirements"]["env"] = ["REQUIRED_VALUE"]  # type: ignore[index]
    changed["contributions"][0]["type"] = "tool"  # type: ignore[index]
    replacement = parse_capability_manifest(
        changed,
        physical_source=ManifestSource.BUNDLED,
    )

    assert original.contract_fingerprint != replacement.contract_fingerprint
    assert original.manifest_fingerprint != replacement.manifest_fingerprint
    assert CapabilityPluginCandidate(original, "same-location").provenance_id != (
        CapabilityPluginCandidate(replacement, "same-location").provenance_id
    )
    artifact_a = CapabilityPluginCandidate(
        original,
        "same-location",
        artifacts=(CapabilityPluginArtifact("plugin.py", b"artifact-a"),),
    )
    artifact_b = CapabilityPluginCandidate(
        original,
        "same-location",
        artifacts=(CapabilityPluginArtifact("plugin.py", b"artifact-b"),),
    )
    assert artifact_a.artifact_fingerprint != artifact_b.artifact_fingerprint
    assert artifact_a.provenance_id != artifact_b.provenance_id


def test_unmarked_extension_is_legacy_but_marked_non_v2_is_rejected() -> None:
    legacy = {"id": "blog", "name": "Blog", "version": "1.0.0"}
    assert (
        classify_manifest(legacy, physical_source=ManifestSource.BUNDLED)
        is ManifestClassification.LEGACY
    )

    with pytest.raises(CapabilityManifestError) as raised:
        classify_manifest(
            {**legacy, "manifestVersion": 1}, physical_source=ManifestSource.BUNDLED
        )
    assert raised.value.code == "unsupported_manifest_version"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda raw: raw.update({"surprise": True}), "manifest_unknown_fields"),
        (lambda raw: raw.__setitem__("source", "project"), "source_mismatch"),
        (lambda raw: raw.__setitem__("entrypoint", "../plugin.py:register"), "invalid_entrypoint"),
        (lambda raw: raw.__setitem__("entrypoint", "C:\\plugin:register"), "invalid_entrypoint"),
        (lambda raw: raw.__setitem__("version", "latest"), "invalid_version"),
        (lambda raw: raw.__setitem__("version", "1.0.0-01"), "invalid_version"),
        (lambda raw: raw.__setitem__("name", "line one\nline two"), "invalid_name"),
        (lambda raw: raw.__setitem__("enabledByDefault", 1), "invalid_enabled_by_default"),
        (lambda raw: raw.__setitem__("contractVersion", True), "invalid_contract_version"),
        (
            lambda raw: raw["requirements"].update({"token": "do-not-store"}),  # type: ignore[union-attr]
            "requirements_unknown_fields",
        ),
        (
            lambda raw: raw.__setitem__("requirements", {"env": ["API_KEY=secret"]}),
            "invalid_env_requirements",
        ),
        (
            lambda raw: raw["contributions"][0].update({"handler": "x"}),  # type: ignore[index,union-attr]
            "contribution_unknown_fields",
        ),
        (
            lambda raw: raw.__setitem__(
                "replaces",
                {
                    "id": "old.plugin",
                    "contractVersion": 1,
                    "contractFingerprint": "a" * 64,
                    "x": 1,
                },
            ),
            "replacement_unknown_fields",
        ),
    ],
)
def test_hostile_or_open_manifest_shapes_fail_closed(mutate, code: str) -> None:
    raw = manifest()
    mutate(raw)

    with pytest.raises(CapabilityManifestError) as raised:
        parse_capability_manifest(raw, physical_source=ManifestSource.BUNDLED)
    assert raised.value.code == code
    assert len(raised.value.detail) <= 400


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Example api_key=CATALOG_SECRET_123"),
        ("description", "Connect with password:CATALOG_SECRET_456"),
        ("description", _FAKE_BEARER),
        ("description", _FAKE_JWT),
        ("description", _FAKE_OPENAI_PROJECT),
        ("description", _FAKE_GITHUB_PAT),
        ("description", _FAKE_SLACK),
        ("description", _FAKE_GOOGLE),
        ("description", _FAKE_AWS),
        ("description", _FAKE_STRIPE),
        ("description", _FAKE_HUGGING_FACE),
        ("version", _FAKE_OPENAI_VERSION),
    ],
)
def test_secret_shaped_name_and_description_are_rejected_before_catalog(
    field: str, value: str
) -> None:
    raw = manifest()
    raw[field] = value

    with pytest.raises(CapabilityManifestError) as raised:
        parse_capability_manifest(raw, physical_source=ManifestSource.BUNDLED)

    assert raised.value.code == "secret_shaped_metadata"
    assert "CATALOG_SECRET" not in raised.value.detail
    assert value not in str(raised.value)


@pytest.mark.parametrize("field", ["source", "export", "contribution_type"])
def test_secret_bearing_manifest_enum_failure_has_no_retained_exception(
    field: str,
) -> None:
    secret = _FAKE_OPENAI_PROJECT
    raw = manifest()
    if field == "contribution_type":
        raw["contributions"][0]["type"] = secret  # type: ignore[index]
    else:
        raw[field] = secret

    with pytest.raises(CapabilityManifestError) as rejected:
        parse_capability_manifest(raw, physical_source=ManifestSource.BUNDLED)

    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert secret not in str(rejected.value)


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("plugin", "invalid_plugin_id"),
        ("contribution", "invalid_contribution_id"),
        ("dependency", "invalid_plugin_requirements"),
        ("replacement", "invalid_replacement_id"),
    ],
)
def test_secret_shaped_persistable_manifest_identifiers_are_rejected(
    field: str,
    expected_code: str,
) -> None:
    secret = _FAKE_OPENAI_PROJECT
    raw = manifest()
    if field == "plugin":
        raw["id"] = secret
    elif field == "contribution":
        raw["contributions"][0]["id"] = secret  # type: ignore[index]
    elif field == "dependency":
        raw["requirements"]["plugins"] = [secret]  # type: ignore[index]
    else:
        raw["replaces"] = {
            "id": secret,
            "contractVersion": 1,
            "contractFingerprint": "a" * 64,
        }

    with pytest.raises(CapabilityManifestError) as rejected:
        parse_capability_manifest(raw, physical_source=ManifestSource.BUNDLED)

    assert rejected.value.code == expected_code
    assert secret not in str(rejected.value)


@pytest.mark.parametrize(
    "credential",
    _FAKE_STANDALONE_CREDENTIALS,
)
def test_standalone_credentials_are_redacted_from_runtime_detail(
    credential: str,
) -> None:
    redacted = redact_detail(f"plugin failure: {credential}")

    assert credential not in redacted
    assert "[REDACTED]" in redacted


def test_required_secret_values_are_redacted_shortest_and_longest_without_suffix_leaks(
) -> None:
    redacted = redact_detail(
        "short abc overlap abcdef",
        secret_values=("abc", "abcd", "abcdef"),
    )

    assert "abc" not in redacted
    assert "def" not in redacted
    assert redacted == "short [REDACTED] overlap [REDACTED]"


def test_hostile_detail_stringification_cannot_escape_redaction() -> None:
    class HostileDetail:
        def __str__(self) -> str:
            raise SystemExit("hostile stringification escaped")

    assert redact_detail(HostileDetail()) == "Unprintable detail"


def test_required_secret_with_control_bytes_is_redacted_before_normalization() -> None:
    secret = "prefix\x01suffix"

    redacted = redact_detail(f"failure {secret}", secret_values=(secret,))

    assert redacted == "failure [REDACTED]"
    assert "prefix" not in redacted
    assert "suffix" not in redacted


def test_contribution_graph_requires_declared_acyclic_dependencies() -> None:
    raw = manifest(
        contribution_ids=("fixture.base", "fixture.dependent"),
        dependencies={
            "fixture.base": ("fixture.dependent",),
            "fixture.dependent": ("fixture.base",),
        },
    )
    with pytest.raises(CapabilityManifestError) as raised:
        parse_capability_manifest(raw, physical_source=ManifestSource.BUNDLED)
    assert raised.value.code == "contribution_dependency_cycle"

    raw = manifest(dependencies={"example.value": ("missing.value",)})
    with pytest.raises(CapabilityManifestError) as raised:
        parse_capability_manifest(raw, physical_source=ManifestSource.BUNDLED)
    assert raised.value.code == "unknown_contribution_dependency"


def test_deep_contribution_graph_is_validated_iteratively_without_recursion_escape() -> None:
    contribution_ids = tuple(f"node{i}" for i in reversed(range(1_100)))
    dependencies = {
        f"node{i}": (() if i == 0 else (f"node{i - 1}",))
        for i in range(1_100)
    }

    parsed = parse_capability_manifest(
        manifest(
            contribution_ids=contribution_ids,
            dependencies=dependencies,
        ),
        physical_source=ManifestSource.BUNDLED,
    )

    ordered = contribution_topological_order(parsed)
    assert len(ordered) == 1_100
    assert ordered[0] == "node0"
    assert ordered[-1] == "node1099"


def test_filesystem_discovery_reads_manifests_without_importing_code(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    tripwire = tmp_path / "IMPORTED"
    write_plugin(bundled, "fixture", manifest(), tripwire=tripwire)

    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, bundled)]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["example.plugin"]
    assert not tripwire.exists()
    assert discovery.errors == ()


def test_deep_manifest_json_isolated_without_aborting_unrelated_discovery(
    tmp_path: Path,
) -> None:
    bundled = tmp_path / "bundled"
    deep = bundled / "a-deep"
    deep.mkdir(parents=True)
    (deep / "extension.json").write_text(
        "[" * 5_000 + "0" + "]" * 5_000,
        encoding="utf-8",
    )
    (deep / "plugin.py").write_text(
        "raise AssertionError('discovery must not import')\n",
        encoding="utf-8",
    )
    write_plugin(
        bundled,
        "b-good",
        manifest("good.plugin", contribution_ids=("good.value",)),
    )

    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, bundled)]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["good.plugin"]
    assert [(item.location, item.code) for item in discovery.errors] == [
        ("a-deep", "manifest_nesting_too_deep")
    ]


def test_source_precedence_and_project_opt_in_are_explicit(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    write_plugin(bundled, "first", manifest("same.plugin", contribution_ids=("first.value",)))
    write_plugin(
        global_root,
        "second",
        manifest(
            "same.plugin",
            source="operator_global",
            contribution_ids=("second.value",),
        ),
    )
    write_plugin(
        project,
        "project-only",
        manifest("project.plugin", source="project", contribution_ids=("project.value",)),
    )
    sources = [
        FilesystemPluginSource(ManifestSource.PROJECT, project),
        FilesystemPluginSource(ManifestSource.OPERATOR_GLOBAL, global_root),
        FilesystemPluginSource(ManifestSource.BUNDLED, bundled),
    ]

    discovery = discover_capability_plugins(sources)
    assert [item.manifest.id for item in discovery.active_candidates] == ["same.plugin"]
    assert discovery.active_candidates[0].manifest.source is ManifestSource.BUNDLED
    assert {item.code for item in discovery.errors} == {
        "duplicate_plugin_id",
        "project_source_opt_in_required",
    }

    opted_in = discover_capability_plugins(sources, include_project=True)
    assert {item.manifest.id for item in opted_in.active_candidates} == {
        "same.plugin",
        "project.plugin",
    }


def test_contribution_conflict_isolated_from_unrelated_candidate(tmp_path: Path) -> None:
    root = tmp_path / "bundled"
    tripwire = tmp_path / "CONFLICT_IMPORTED"
    write_plugin(root, "a", manifest("a.plugin", contribution_ids=("shared.value",)))
    write_plugin(
        root,
        "b",
        manifest("b.plugin", contribution_ids=("shared.value",)),
        tripwire=tripwire,
    )
    write_plugin(root, "c", manifest("c.plugin", contribution_ids=("unique.value",)))

    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["a.plugin", "c.plugin"]
    assert [item.code for item in discovery.errors] == ["duplicate_contribution_id"]
    assert not tripwire.exists()


def test_exact_compatible_replacement_supersedes_prior_candidate(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    global_root = tmp_path / "global"
    base = manifest("base.plugin", contribution_ids=("shared.value",))
    fingerprint = parse_capability_manifest(
        base, physical_source=ManifestSource.BUNDLED
    ).contract_fingerprint
    write_plugin(bundled, "base", base)
    write_plugin(
        global_root,
        "replacement",
        manifest(
            "replacement.plugin",
            source="operator_global",
            contribution_ids=("shared.value",),
            replaces={
                "id": "base.plugin",
                "contractVersion": 1,
                "contractFingerprint": fingerprint,
            },
        ),
    )

    discovery = discover_capability_plugins(
        [
            FilesystemPluginSource(ManifestSource.BUNDLED, bundled),
            FilesystemPluginSource(ManifestSource.OPERATOR_GLOBAL, global_root),
        ]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["replacement.plugin"]
    (resolution,) = discovery.replacements
    assert (resolution.target_id, resolution.replacement_id) == (
        "base.plugin",
        "replacement.plugin",
    )
    assert resolution.contract_fingerprint == fingerprint
    assert resolution.target_provenance != resolution.replacement_provenance
    assert discovery.errors == ()


def test_incompatible_replacement_leaves_prior_candidate_active(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    global_root = tmp_path / "global"
    base = manifest("base.plugin", contribution_ids=("shared.value",), contract_version=2)
    fingerprint = parse_capability_manifest(
        base, physical_source=ManifestSource.BUNDLED
    ).contract_fingerprint
    write_plugin(
        bundled,
        "base",
        base,
    )
    write_plugin(
        global_root,
        "replacement",
        manifest(
            "replacement.plugin",
            source="operator_global",
            contribution_ids=("shared.value",),
            replaces={
                "id": "base.plugin",
                "contractVersion": 1,
                "contractFingerprint": fingerprint,
            },
        ),
    )

    discovery = discover_capability_plugins(
        [
            FilesystemPluginSource(ManifestSource.BUNDLED, bundled),
            FilesystemPluginSource(ManifestSource.OPERATOR_GLOBAL, global_root),
        ]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["base.plugin"]
    assert [item.code for item in discovery.errors] == ["replacement_contract_mismatch"]


def test_replacement_requires_exact_canonical_contribution_contract(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    global_root = tmp_path / "global"
    base = manifest(
        "base.plugin",
        contribution_ids=("shared.base", "shared.dependent"),
        dependencies={"shared.dependent": ("shared.base",)},
    )
    fingerprint = parse_capability_manifest(
        base, physical_source=ManifestSource.BUNDLED
    ).contract_fingerprint
    write_plugin(bundled, "base", base)
    write_plugin(
        global_root,
        "replacement",
        manifest(
            "replacement.plugin",
            source="operator_global",
            contribution_ids=("shared.base",),
            replaces={
                "id": "base.plugin",
                "contractVersion": 1,
                "contractFingerprint": fingerprint,
            },
        ),
    )

    discovery = discover_capability_plugins(
        [
            FilesystemPluginSource(ManifestSource.BUNDLED, bundled),
            FilesystemPluginSource(ManifestSource.OPERATOR_GLOBAL, global_root),
        ]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["base.plugin"]
    assert [item.code for item in discovery.errors] == [
        "replacement_candidate_fingerprint_mismatch"
    ]


def test_same_id_replacement_catalog_activity_is_candidate_provenanced(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    global_root = tmp_path / "global"
    base = manifest("same.plugin", contribution_ids=("same.value",))
    fingerprint = parse_capability_manifest(
        base, physical_source=ManifestSource.BUNDLED
    ).contract_fingerprint
    write_plugin(bundled, "base", base)
    write_plugin(
        global_root,
        "replacement",
        manifest(
            "same.plugin",
            source="operator_global",
            contribution_ids=("same.value",),
            replaces={
                "id": "same.plugin",
                "contractVersion": 1,
                "contractFingerprint": fingerprint,
            },
        ),
    )

    discovery = discover_capability_plugins(
        [
            FilesystemPluginSource(ManifestSource.BUNDLED, bundled),
            FilesystemPluginSource(ManifestSource.OPERATOR_GLOBAL, global_root),
        ]
    )
    catalog = discovery.catalog()

    assert [record["active"] for record in catalog] == [False, True]
    assert catalog[0]["candidate_provenance"] != catalog[1]["candidate_provenance"]
    assert discovery.active_candidates[0].manifest.source is ManifestSource.OPERATOR_GLOBAL


class FakeDistribution:
    def __init__(self, raw: dict[str, object], root: Path) -> None:
        self.raw = raw
        self.root = root
        self.metadata = {"Name": "fake-distribution"}
        self.read_count = 0
        module_name = str(raw["entrypoint"]).split(":", 1)[0]
        self.files = (Path(f"{module_name}.py"),)
        root.mkdir(parents=True, exist_ok=True)
        (root / self.files[0]).write_text(
            "def register(registrar):\n    return None\n",
            encoding="utf-8",
        )

    def read_text(self, name: str) -> str | None:
        self.read_count += 1
        assert name == "thehomie-capability.json"
        return json.dumps(self.raw)

    def locate_file(self, name: object) -> Path:
        return self.root / Path(str(name))


class FakeEntryPoint:
    group = ENTRY_POINT_GROUP

    def __init__(self, name: str, raw: dict[str, object], root: Path) -> None:
        self.name = name
        self.value = str(raw["entrypoint"])
        self.dist = FakeDistribution(raw, root)
        self.load_count = 0

    def load(self):
        self.load_count += 1
        raise AssertionError("discovery imported entry-point code")


class FakeEntryPoints(tuple):
    def select(self, *, group: str):
        return tuple(item for item in self if item.group == group)


class HostileEntryPoint:
    group = ENTRY_POINT_GROUP

    @property
    def name(self):
        raise SystemExit("hostile entry-point metadata escaped")

    @property
    def value(self):
        raise SystemExit("hostile entry-point metadata escaped")


class HostileEntryPointName:
    def __str__(self) -> str:
        return "hostile-name"

    def __hash__(self) -> int:
        raise SystemExit("hostile entry-point name hash escaped")


class HashHostileEntryPoint:
    group = ENTRY_POINT_GROUP
    name = HostileEntryPointName()
    value = "bad_plugin:register"


def test_approved_entry_point_reads_resource_but_never_calls_load(tmp_path: Path) -> None:
    approved = FakeEntryPoint(
        "approved-fixture",
        manifest(
            "entry.plugin",
            source="python_entry_point",
            entrypoint="entry_plugin:register",
        ),
        tmp_path / "approved-dist",
    )
    unapproved = FakeEntryPoint(
        "not-approved",
        manifest(
            "ignored.plugin",
            source="python_entry_point",
            entrypoint="ignored_plugin:register",
        ),
        tmp_path / "unapproved-dist",
    )

    discovery = discover_capability_plugins(
        [],
        approved_entry_points={"approved-fixture"},
        entry_points_provider=lambda: FakeEntryPoints((approved, unapproved)),
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["entry.plugin"]
    assert approved.dist.read_count == 1
    assert approved.load_count == 0
    assert len(discovery.active_candidates[0].artifacts) == 1
    assert unapproved.dist.read_count == 0
    assert unapproved.load_count == 0
    assert [item.code for item in discovery.errors] == ["entry_point_not_approved"]


def test_hostile_entry_point_metadata_is_isolated_without_poisoning_valid_candidate(
    tmp_path: Path,
) -> None:
    approved = FakeEntryPoint(
        "approved-fixture",
        manifest(
            "entry.plugin",
            source="python_entry_point",
            entrypoint="entry_plugin:register",
        ),
        tmp_path / "approved-dist",
    )
    discovery = discover_capability_plugins(
        [],
        approved_entry_points={"approved-fixture"},
        entry_points_provider=lambda: FakeEntryPoints(
            (HostileEntryPoint(), HashHostileEntryPoint(), approved)
        ),
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["entry.plugin"]
    assert [item.code for item in discovery.errors] == [
        "entry_point_metadata_unavailable",
        "entry_point_metadata_unavailable",
    ]


def test_broken_manifest_is_redacted_and_does_not_poison_good_plugin(tmp_path: Path) -> None:
    root = tmp_path / "bundled"
    write_plugin(root, "good", manifest("good.plugin", contribution_ids=("good.value",)))
    broken = manifest("broken.plugin", contribution_ids=("broken.value",))
    broken["api_key=MANIFEST_SECRET_123"] = "MANIFEST_SECRET_123"
    write_plugin(root, "broken", broken)

    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )
    serialized_errors = json.dumps(
        [
            {"code": item.code, "detail": item.detail, "location": item.location}
            for item in discovery.errors
        ]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["good.plugin"]
    assert "MANIFEST_SECRET_123" not in serialized_errors
    assert [item.code for item in discovery.errors] == ["manifest_unknown_fields"]


def test_public_catalog_omits_private_manifests(tmp_path: Path) -> None:
    root = tmp_path / "bundled"
    write_plugin(root, "public", manifest("public.plugin", contribution_ids=("public.value",)))
    write_plugin(
        root,
        "private",
        manifest("private.plugin", contribution_ids=("private.value",), export="private"),
    )

    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )

    assert {item["id"] for item in discovery.catalog()} == {"public.plugin", "private.plugin"}
    public_catalog = discovery.catalog(public_only=True)
    assert {item["id"] for item in public_catalog} == {"public.plugin"}
    assert "private.plugin" not in json.dumps(public_catalog)


def test_missing_plugin_dependency_fails_only_the_dependent_candidate(tmp_path: Path) -> None:
    root = tmp_path / "bundled"
    dependent = manifest("dependent.plugin", contribution_ids=("dependent.value",))
    dependent["requirements"] = {"plugins": ["missing.plugin"]}
    write_plugin(root, "dependent", dependent)
    write_plugin(root, "good", manifest("good.plugin", contribution_ids=("good.value",)))

    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )

    assert [item.manifest.id for item in discovery.active_candidates] == ["good.plugin"]
    assert [item.code for item in discovery.errors] == ["plugin_dependency_missing"]


def test_plugin_dependency_cycle_fails_every_cycle_member(tmp_path: Path) -> None:
    root = tmp_path / "bundled"
    first = manifest("first.plugin", contribution_ids=("first.value",))
    first["requirements"] = {"plugins": ["second.plugin"]}
    second = manifest("second.plugin", contribution_ids=("second.value",))
    second["requirements"] = {"plugins": ["first.plugin"]}
    write_plugin(root, "first", first)
    write_plugin(root, "second", second)

    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )

    assert discovery.active_candidates == ()
    assert [item.code for item in discovery.errors] == [
        "plugin_dependency_cycle",
        "plugin_dependency_cycle",
    ]
