"""Persona-level toolset config: `toolsets:` / `tools:` + the deprecated alias (#241).

`cabinet.tools` was a naming accident ported from ClaudeClaw's `warroom_tools`.
The name claimed to scope cabinet meetings; the code gated EVERY persona turn on
EVERY surface. It shipped empty and nobody noticed, because a persona that never
had tools never visibly lost any.

Surveyed 2026-07-27 across all 25 live profiles under `~/.homie/profiles/`:

    16  cabinet.tools = []        (explicitly empty)
     4  cabinet section, no tools key
     5  no cabinet section at all

**Not one profile has ever granted a tool.** So there is no data to migrate —
the alias exists for forward-compat and for any profile edited before the rename
lands, not to carry existing values. The tests below pin that the rename cannot
silently change any persona's scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from personas.services import (  # noqa: E402
    ConfigShapeError,
    PersonaToolScope,
    resolve_persona_tool_scope,
    validate_config_dict,
)


# ---------------------------------------------------------------------------
# Resolution + precedence
# ---------------------------------------------------------------------------


def test_new_keys_resolve():
    scope = resolve_persona_tool_scope({"toolsets": ["crypto", "browser"], "tools": ["extra"]})
    assert scope.toolsets == ("crypto", "browser")
    assert scope.tools == ("extra",)
    assert scope.used_deprecated_alias is False


def test_absent_config_grants_nothing():
    """Default-deny survives the rename.

    An absent key has never granted anything and must not start now — the whole
    point of the epic is scoped capability, not blanket capability.
    """
    assert resolve_persona_tool_scope({}).is_empty
    assert resolve_persona_tool_scope({"persona": {"name": "x"}}).is_empty


def test_deprecated_alias_still_parses_as_individual_grants():
    """`cabinet.tools` held TOOL names, never toolset names.

    Reading it as `toolsets` would silently reinterpret every legacy value as a
    scope name, and an unknown toolset resolves to nothing — a persona would
    lose tools it thought it had, quietly.
    """
    scope = resolve_persona_tool_scope({"cabinet": {"tools": ["Read", "Bash"]}})
    assert scope.tools == ("Read", "Bash")
    assert scope.toolsets == ()
    assert scope.used_deprecated_alias is True


def test_new_keys_win_as_a_pair_over_the_alias():
    """The new keys replace the alias outright; they never merge with it.

    Merging would give a profile mid-migration an effective scope that appears
    in NEITHER key alone — invisible in the file the operator is reading.
    """
    scope = resolve_persona_tool_scope(
        {"toolsets": ["crypto"], "cabinet": {"tools": ["Bash"]}}
    )
    assert scope.toolsets == ("crypto",)
    assert scope.tools == (), "the alias leaked in despite a new key being present"
    assert scope.used_deprecated_alias is False


def test_an_empty_new_key_still_beats_the_alias():
    """`tools: []` is an explicit statement, not an absence.

    An operator who empties the new key is REVOKING; falling back to the alias
    would resurrect what they just removed.
    """
    scope = resolve_persona_tool_scope({"tools": [], "cabinet": {"tools": ["Bash"]}})
    assert scope.is_empty
    assert scope.used_deprecated_alias is False


def test_alias_use_is_reported_not_hidden():
    """`used_deprecated_alias` exists so an operator can see WHY a scope is what
    it is, and so a migration has something to report."""
    assert resolve_persona_tool_scope({"cabinet": {"tools": []}}).used_deprecated_alias is True
    assert resolve_persona_tool_scope({"cabinet": {}}).used_deprecated_alias is False


def test_names_are_cleaned_but_order_and_duplicates_are_preserved():
    """Order is meaningful downstream; silent dedup would hide a config mistake."""
    scope = resolve_persona_tool_scope(
        {"toolsets": ["  crypto  ", "", "research", "crypto", 42, None]}
    )
    assert scope.toolsets == ("crypto", "research", "crypto")


def test_malformed_config_degrades_to_empty_rather_than_raising():
    """Resolution is a read path — validation is where shape errors belong."""
    assert resolve_persona_tool_scope({"toolsets": "crypto"}).is_empty
    assert resolve_persona_tool_scope(None).is_empty  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["toolsets", "tools"])
def test_valid_shapes_accepted(key):
    validate_config_dict({key: ["crypto", "browser"]})
    validate_config_dict({key: []})


@pytest.mark.parametrize("key", ["toolsets", "tools"])
@pytest.mark.parametrize("bad", ["crypto", {"a": 1}, 42, None])
def test_non_list_rejected(key, bad):
    with pytest.raises(ConfigShapeError, match=key):
        validate_config_dict({key: bad})


@pytest.mark.parametrize("key", ["toolsets", "tools"])
def test_non_string_entries_rejected(key):
    with pytest.raises(ConfigShapeError, match=rf"{key}\[1\]"):
        validate_config_dict({key: ["ok", 42]})


@pytest.mark.parametrize("key", ["toolsets", "tools"])
def test_blank_entries_rejected(key):
    """A blank name is always a typo, and it would resolve to nothing silently."""
    with pytest.raises(ConfigShapeError, match="must not be blank"):
        validate_config_dict({key: ["ok", "   "]})


def test_unknown_toolset_names_are_NOT_a_config_error():
    """Shape is validated here; existence is a RUNTIME question.

    `runtime.toolsets.TOOLSETS` is a live registry that plugins extend, so
    failing config load on an unimported toolset would make profile loading
    depend on import order. Unknown names resolve to nothing at assembly time —
    fail-closed, per the registry's silent-on-missing contract.
    """
    validate_config_dict({"toolsets": ["definitely_not_registered_yet"]})


def test_cabinet_tools_still_validates_unchanged():
    """25 live profiles carry this key. It must keep parsing."""
    validate_config_dict({"cabinet": {"tools": []}})
    validate_config_dict({"cabinet": {"tools": ["Read"]}})
    with pytest.raises(ConfigShapeError, match="cabinet.tools"):
        validate_config_dict({"cabinet": {"tools": "Read"}})


# ---------------------------------------------------------------------------
# The 25 live profiles
# ---------------------------------------------------------------------------


def _live_configs():
    import yaml

    root = Path.home() / ".homie" / "profiles"
    if not root.is_dir():
        pytest.skip("no ~/.homie/profiles on this machine")
    out = []
    for d in sorted(root.iterdir()):
        cfg = d / "config.yaml"
        if cfg.is_file():
            out.append((d.name, yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}))
    if not out:
        pytest.skip("no profiles found")
    return out


def test_every_live_profile_still_validates():
    """The new validators must not reject a single existing profile.

    Runs against the REAL profiles, not fixtures — the whole risk of this ticket
    is a schema change that bricks profile loading on the operator's machine.
    """
    for name, data in _live_configs():
        try:
            validate_config_dict(data)
        except ConfigShapeError as exc:
            pytest.fail(f"profile {name!r} no longer validates: {exc}")


def test_every_live_grant_names_a_REAL_toolset():
    """Grants landed 2026-07-27 — every declared name must actually resolve.

    Supersedes the pre-grant assertion that every profile was empty. That guard
    did its job: it failed loudly the moment the 25 profiles were granted,
    which is exactly what a staleness guard is for. The invariant now worth
    holding is different — a grant naming a toolset that does not exist
    resolves to NOTHING, so the persona looks configured and is powerless, and
    nothing in the config file says so.
    """
    from runtime.toolsets import TOOLSETS

    bad = []
    for name, data in _live_configs():
        scope = resolve_persona_tool_scope(data)
        for toolset in scope.toolsets:
            if toolset not in TOOLSETS:
                bad.append(f"{name} -> {toolset!r}")
    assert bad == [], f"profiles declare unknown toolsets (they resolve to nothing): {bad}"


def test_no_live_profile_depends_on_the_deprecated_alias():
    """After the grant pass, capability comes from the honest key.

    A profile still resolving through `cabinet.tools` would be one the
    migration missed — and it would keep working right up until the alias is
    removed, which is the worst time to find out.
    """
    stragglers = [
        name for name, data in _live_configs()
        if resolve_persona_tool_scope(data).used_deprecated_alias
    ]
    assert stragglers == [], f"still resolving via cabinet.tools: {stragglers}"


def test_live_profiles_resolve_to_a_scope_object():
    for name, data in _live_configs():
        assert isinstance(resolve_persona_tool_scope(data), PersonaToolScope), name
