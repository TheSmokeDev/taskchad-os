"""PRP-7b WS4 — R1 M2 Click registry collision tests.

Disposition: profile name MUST be validated against the LIVE Click registry,
not just the static `_HOMIE_SUBCOMMANDS_SEED`. After Phase 2 adds the `profile`
group + 11 subcommands to `cli.main`, names like `profile`, `chat`, etc. must
be rejected by `validate_persona_name(registered_subcommands=...)`.

Verified two ways:
    1. Direct call: pass an explicit `registered_subcommands` set with one
       entry; verify create raises ValueError matching "collides with a CLI
       subcommand".
    2. Via CliRunner — confirms the CLI handler actually routes the live
       Click registry into the validator.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from cli import main
from personas.lifecycle import LifecycleError, create_profile


def test_create_profile_collides_with_explicit_subcommand(empty_homie_root):
    """Direct API: passing `registered_subcommands={"chat"}` rejects `chat`."""
    with pytest.raises(ValueError, match="collides with a CLI subcommand"):
        create_profile(
            "chat",
            no_alias=True,
            registered_subcommands=frozenset({"chat", "profile"}),
        )


def test_create_profile_passes_through_when_not_in_registry(empty_homie_root):
    """A name not in the registry passes the collision check."""
    info = create_profile(
        "sales",
        no_alias=True,
        registered_subcommands=frozenset({"chat", "profile"}),
    )
    assert info.path.exists()


def test_cli_create_rejects_subcommand_collision(empty_homie_root):
    """CLI handler wires `frozenset(cli_root.commands.keys())` into validate.

    The live Click registry contains `profile` (the group itself) plus
    every other top-level subcommand (`chat`, `convoy`, `team`, ...). All
    must be rejected as profile names.
    """
    runner = CliRunner()
    # Pick a few names KNOWN to be Click subcommands.
    for subcmd in ("chat", "convoy", "team", "profile", "doctor", "status"):
        if subcmd not in main.commands:
            continue
        result = runner.invoke(main, ["profile", "create", subcmd])
        assert result.exit_code != 0, (
            f"name '{subcmd}' was accepted but should collide with subcommand"
        )
        # Error message surfaces in stdout (cli echoes via click.echo).
        combined = result.output
        assert (
            "collides" in combined.lower() or "Error" in combined
        ), f"unexpected output for '{subcmd}': {combined!r}"


def test_cli_create_accepts_non_collision_name(empty_homie_root):
    """CLI handler accepts a name that is NOT a registered subcommand."""
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "salestest"])
    # Should NOT collide — exit code 0 (lifecycle may succeed; if it fails
    # for other reasons, error message must NOT mention "collides").
    if result.exit_code != 0:
        assert "collides" not in result.output.lower(), result.output


def test_validate_persona_name_with_empty_registered_subcommands_falls_back():
    """`registered_subcommands=frozenset()` (empty, not None) — empty set
    means NO collision possible. The static seed is ONLY consulted when
    the kwarg is None.
    """
    from personas.core import validate_persona_name

    # `chat` is in the static seed, but we override with an empty set.
    # No collision check should fire.
    validate_persona_name("chat", registered_subcommands=frozenset())
    # Sanity: same name with the seed default raises.
    with pytest.raises(ValueError, match="collides|reserved"):
        validate_persona_name("chat")
