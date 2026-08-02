from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from personas.blueprint_migration import (
    analyze_scope,
    inventory_profile_migrations,
    preview_existing_profile,
)
from personas.blueprints import build_builtin_blueprint

_REGISTRY = {
    "memory_search": ("safe_core", True),
    "memory_recall": ("safe_core", True),
    "memory_store": ("safe_core", True),
    "skill_list": ("safe_core", True),
    "skill_read": ("safe_core", True),
    "todo_read": ("safe_core", True),
    "todo_write": ("safe_core", True),
    "terminal_exec": ("operator_exec", True),
    "null_tool": ("safe_core", False),
}


def test_scope_analysis_preserves_alias_unknown_and_null_handler() -> None:
    alias = analyze_scope(
        {"cabinet": {"tools": ["terminal_exec", "missing_tool"]}},
        registered_inventory=_REGISTRY,
    )
    assert alias.source == "cabinet-alias"
    assert alias.individual_tools == ("terminal_exec", "missing_tool")
    assert alias.offered_names == ("terminal_exec",)
    assert alias.unregistered_names == ("missing_tool",)

    unknown = analyze_scope(
        {"toolsets": ["future-plugin"], "tools": ["null_tool"]},
        registered_inventory=_REGISTRY,
    )
    assert unknown.unknown_toolsets == ("future-plugin",)
    assert unknown.offered_names == ("null_tool",)
    assert unknown.uncallable_names == ("null_tool",)


def test_absent_scope_migration_stays_empty(tmp_path: Path) -> None:
    profile = tmp_path / "ai-engineer"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        yaml.safe_dump({"persona": {"id": "ai-engineer"}}),
        encoding="utf-8",
    )
    preview = preview_existing_profile(
        "ai-engineer",
        build_builtin_blueprint("ai-engineer"),
        profile_root=profile,
        registered_inventory=_REGISTRY,
    )
    assert preview.current_scope.source == "absent"
    assert preview.preserved_scope.toolsets == ()
    assert preview.exact_intent_preserved
    assert preview.offered_names_preserved
    assert preview.recommended_scope.toolsets == (
        "safe_core",
        "ai_engineering",
    )


def test_inventory_is_read_only_and_keeps_repo_scout_separate(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    for persona_id, config in {
        "repo-scout": {
            "persona": {"id": "repo-scout"},
            "toolsets": ["safe_core"],
            "tools": [],
        },
        "ai-engineer": {
            "persona": {"id": "wrong-id"},
            "toolsets": [],
            "tools": [],
        },
    }.items():
        root = profiles / persona_id
        root.mkdir(parents=True)
        (root / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "channels": {
                    "1": {
                        "kind": "persona",
                        "persona": "missing-persona",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "repo-scout": {},
                    "ghost-profile": {},
                }
            }
        ),
        encoding="utf-8",
    )
    before = {
        path: path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    inventory = inventory_profile_migrations(
        profiles,
        bindings_file=bindings,
        capability_matrix_file=matrix,
        registered_inventory=_REGISTRY,
    )
    after = {
        path: path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert inventory.dangling_binding_personas == ("missing-persona",)
    assert inventory.mismatched_config_ids == ("ai-engineer",)
    assert inventory.dangling_capability_rows == ("ghost-profile",)
    repo = next(
        profile for profile in inventory.profiles
        if profile.persona_id == "repo-scout"
    )
    assert repo.protected
    assert "protected_repo_scout" in repo.findings


def test_inventory_uses_existing_profile_blueprint(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profile = profiles / "ai-engineer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        yaml.safe_dump({"persona": {"id": "ai-engineer"}}),
        encoding="utf-8",
    )
    blueprint = build_builtin_blueprint(
        "general-specialist",
        persona_id="ai-engineer",
        display_name="Custom AI Desk",
    )
    (profile / "blueprint.yaml").write_text(
        yaml.safe_dump(blueprint, sort_keys=False),
        encoding="utf-8",
    )

    inventory = inventory_profile_migrations(
        profiles,
        registered_inventory=_REGISTRY,
    )

    assert inventory.errors == ()
    assert inventory.profiles[0].template == "general-specialist"


def test_default_scope_analysis_populates_runtime_registry() -> None:
    scripts_root = Path(__file__).resolve().parents[1]
    code = (
        "import json\n"
        "from personas.blueprint_migration import analyze_scope\n"
        "scope = analyze_scope({'toolsets': ['safe_core']})\n"
        "print(json.dumps({"
        "'offered': scope.offered_names, "
        "'unregistered': scope.unregistered_names"
        "}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=scripts_root,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "memory_search" in payload["offered"]
    assert "memory_search" not in payload["unregistered"]
    assert len(payload["offered"]) > 1
