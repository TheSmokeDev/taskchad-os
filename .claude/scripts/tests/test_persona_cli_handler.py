"""PRP-7b WS4 — R2 NM1 / R3 NNM3 — Click handler error contract.

Covers the `chat/cli.py:profile` group's handler boundary:

    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError):
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

Bare `RuntimeError` is intentionally NOT caught — it must propagate as an
uncaught exception (deterministic exit != 0, exception surface in result).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli import main


# ---------------------------------------------------------------------------
# Click sub-command wiring
# ---------------------------------------------------------------------------


def test_profile_group_is_registered():
    """The `profile` Click group exists at the root of `main`."""
    assert "profile" in main.commands
    profile_grp = main.commands["profile"]
    # Standard Click groups expose `.commands`.
    expected = {
        "create", "list", "show", "delete", "use",
        "clone", "clone-all", "init-archon",
        "export", "import", "migrate-default",
    }
    assert expected.issubset(profile_grp.commands.keys()), (
        f"missing subcommands: {expected - set(profile_grp.commands.keys())}"
    )


# ---------------------------------------------------------------------------
# create — error contracts
# ---------------------------------------------------------------------------


def test_create_invalid_name_exits_1_with_error_message(empty_homie_root):
    """ValueError -> exit_code=1, "Error:" prefix."""
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "BADNAME"])
    assert result.exit_code == 1, result.output
    assert "Error" in result.output


def test_create_systemd_on_non_linux_returns_clickexception(
    monkeypatch, empty_homie_root
):
    """R2 NM1 — `--install-systemd` on non-Linux exits 1 with stderr msg."""
    monkeypatch.setattr(sys, "platform", "darwin")
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "ops", "--install-systemd"])
    assert result.exit_code != 0, result.output
    assert "systemd" in result.output.lower()
    assert not (empty_homie_root / "profiles" / "ops").exists()


def test_create_launchd_on_non_darwin_returns_clickexception(
    monkeypatch, empty_homie_root
):
    """R2 NM1 — same shape for `--install-launchd` on non-darwin."""
    monkeypatch.setattr(sys, "platform", "linux")
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "ops", "--install-launchd"])
    assert result.exit_code != 0, result.output
    assert "launchd" in result.output.lower()
    assert not (empty_homie_root / "profiles" / "ops").exists()


def test_create_default_flags_rolls_back_on_wrapper_fail(
    monkeypatch, empty_homie_root
):
    """R2 NM1 — without `--no-alias`, wrapper failure rolls back the profile
    dir and exits non-zero."""
    from personas import wrappers

    def _fail(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(wrappers, "create_wrapper_alias", _fail)
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "ops"])
    assert result.exit_code != 0, result.output
    assert not (empty_homie_root / "profiles" / "ops").exists()


def test_create_best_effort_alias_keeps_profile_on_wrapper_fail(
    monkeypatch, empty_homie_root
):
    """R2 NM1 — `--best-effort-alias` opts into legacy "warn don't fail"
    behavior: wrapper creation failure logs a warning, profile dir survives,
    exit code is 0.
    """
    from personas import wrappers

    def _fail(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(wrappers, "create_wrapper_alias", _fail)
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "ops", "--best-effort-alias"])
    assert result.exit_code == 0, result.output
    assert (empty_homie_root / "profiles" / "ops").is_dir()


def test_bare_runtime_error_is_not_swallowed_as_click_exception(
    monkeypatch, empty_homie_root
):
    """R3 NNM3 — bare RuntimeError MUST propagate as the result.exception.

    The handler's `except (LifecycleError, ValueError, FileExistsError,
    FileNotFoundError):` does NOT include `RuntimeError`. A bug in
    production code surfaces as a real uncaught exception so tests notice.
    """
    from personas import lifecycle

    def _bug(*args, **kwargs):
        raise RuntimeError("simulated implementation bug — not LifecycleError")

    monkeypatch.setattr(lifecycle, "create_profile", _bug)
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "ops"])
    # RuntimeError MUST surface. Click will set result.exit_code != 0 and
    # result.exception will be the RuntimeError (NOT LifecycleError).
    assert result.exit_code != 0
    assert result.exception is not None
    assert isinstance(result.exception, RuntimeError)
    assert not isinstance(result.exception, lifecycle.LifecycleError)
    assert "simulated implementation bug" in str(result.exception)


def test_lifecycle_error_is_caught_and_routed_through_handler(
    monkeypatch, empty_homie_root
):
    """R3 NNM3 — converse: LifecycleError surfaces as exit_code=1 + Error msg."""
    from personas import lifecycle

    def _operator_error(*args, **kwargs):
        raise lifecycle.LifecycleError("simulated operator error")

    monkeypatch.setattr(lifecycle, "create_profile", _operator_error)
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "create", "ops"])
    assert result.exit_code == 1, result.output
    assert "simulated operator error" in result.output


# ---------------------------------------------------------------------------
# list / show — happy path + JSON
# ---------------------------------------------------------------------------


def test_profile_list_empty_root_lists_only_default_when_no_named_profiles(
    empty_homie_root, monkeypatch, tmp_path
):
    """With no named profiles, list still picks up the install-dir default
    via `is_default_profile()` UNLESS HOMIE_VAULT_DIR points elsewhere.

    Override HOMIE_VAULT_DIR to a non-existent dir so `is_default_profile()`
    returns False, then the list should be empty.
    """
    monkeypatch.setenv("HOMIE_VAULT_DIR", str(tmp_path / "no-such-vault"))
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "list"])
    assert result.exit_code == 0, result.output
    # No named profiles AND no default detected -> "No profiles found".
    assert "No profiles" in result.output or "default" not in result.output


def test_profile_list_with_pre_seeded_profile(tmp_homie_home):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "list"])
    assert result.exit_code == 0, result.output
    assert "sales" in result.output


def test_profile_list_json_mode_returns_parseable_json(tmp_homie_home):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    names = [p["name"] for p in payload]
    assert "sales" in names


def test_profile_show_json_mode_returns_parseable_json(tmp_homie_home):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "show", "sales", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["name"] == "sales"


def test_profile_show_nonexistent_exits_1(empty_homie_root):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "show", "doesnotexist"])
    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# use / delete
# ---------------------------------------------------------------------------


def test_profile_use_writes_active_profile(tmp_homie_home):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "use", "sales"])
    assert result.exit_code == 0, result.output
    homie_root = tmp_homie_home.parent.parent
    active = homie_root / "active_profile"
    assert active.exists()
    assert active.read_text(encoding="utf-8").strip() == "sales"


def test_profile_delete_removes_dir(tmp_homie_home):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "delete", "sales", "--yes"])
    assert result.exit_code == 0, result.output
    assert not tmp_homie_home.exists()


def test_profile_delete_default_exits_1(empty_homie_root):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "delete", "default", "--yes"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# clone / clone-all
# ---------------------------------------------------------------------------


def test_profile_clone_creates_destination(source_profile_with_secrets):
    runner = CliRunner()
    result = runner.invoke(
        main, ["profile", "clone", "source", "dest", "--no-alias"]
    )
    assert result.exit_code == 0, result.output
    homie_root = source_profile_with_secrets.parent.parent
    assert (homie_root / "profiles" / "dest").exists()


def test_profile_clone_all_creates_destination(source_profile_with_secrets):
    runner = CliRunner()
    result = runner.invoke(
        main, ["profile", "clone-all", "source", "dest", "--no-alias"]
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# F4 (post-build adversarial review) — clone CLI routes through create_profile
# ---------------------------------------------------------------------------


def test_profile_clone_runs_lifecycle_inventory_backfill(
    source_profile_with_secrets,
):
    """F4 — `profile clone` from an INCOMPLETE source must end up with the
    full PRP-7b inventory in the destination.

    The ``source_profile_with_secrets`` fixture seeds only ``.env`` plus
    ``memory/SOUL.md|MEMORY.md|USER.md``. It is missing every entry in
    ``_REQUIRED_PROFILE_DIRS``, ``_REQUIRED_MEMORY_DIRS``, and most of
    ``_REQUIRED_IDENTITY_FILES``. The pre-fix code path
    (``clone_profile()`` direct) would have left those gaps in the
    destination — codex review measured 31 missing required directories.

    F4 fix: route through ``create_profile(... clone=True ...)`` so the
    bootstrap loops backfill everything the source was missing.
    """
    from personas.lifecycle import (
        _REQUIRED_IDENTITY_FILES,
        _REQUIRED_MEMORY_DIRS,
        _REQUIRED_PROFILE_DIRS,
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["profile", "clone", "source", "dest", "--no-alias"]
    )
    assert result.exit_code == 0, result.output

    homie_root = source_profile_with_secrets.parent.parent
    dst = homie_root / "profiles" / "dest"
    assert dst.exists(), "destination not created"

    missing_dirs = [
        sub for sub in _REQUIRED_PROFILE_DIRS if not (dst / sub).is_dir()
    ]
    assert not missing_dirs, (
        f"F4 violation: dest missing {len(missing_dirs)} required profile "
        f"dirs: {missing_dirs}"
    )

    missing_mem_dirs = [
        m for m in _REQUIRED_MEMORY_DIRS if not (dst / "memory" / m).is_dir()
    ]
    assert not missing_mem_dirs, (
        f"F4 violation: dest missing {len(missing_mem_dirs)} required "
        f"memory subdirs: {missing_mem_dirs}"
    )

    # Identity files: the cloned ones (SOUL/MEMORY/USER) come from source;
    # the rest get seeded with the empty body. ALL must exist.
    missing_identity = [
        f for f in _REQUIRED_IDENTITY_FILES
        if not (dst / "memory" / f).exists()
    ]
    assert not missing_identity, (
        f"F4 violation: dest missing {len(missing_identity)} required "
        f"identity files: {missing_identity}"
    )


def test_profile_clone_creates_wrapper_alias(source_profile_with_secrets):
    """F4 — clone (without --no-alias) materializes a wrapper file under
    HOMIE_BIN_DIR. The previous direct ``clone_profile()`` call path
    NEVER created a wrapper — F4 routes through ``create_profile`` so the
    same wrapper-creation code that runs for ``profile create`` runs here.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "clone", "source", "dest"])
    # Wrapper creation may fail on some fixtureless paths — tolerate by
    # checking exit_code first; if 0, assert wrapper exists.
    assert result.exit_code == 0, result.output

    bin_dir = Path(__import__("os").environ["HOMIE_BIN_DIR"])
    if sys.platform == "win32":
        wrapper_cmd = bin_dir / "dest-homie.cmd"
        wrapper_ps1 = bin_dir / "dest-homie.ps1"
        assert wrapper_cmd.exists() or wrapper_ps1.exists(), (
            f"F4 violation: no wrapper created in {bin_dir}; "
            f"directory contents: {list(bin_dir.iterdir())}"
        )
    else:
        wrapper = bin_dir / "dest-homie"
        assert wrapper.exists(), (
            f"F4 violation: POSIX wrapper not created at {wrapper}; "
            f"directory contents: {list(bin_dir.iterdir())}"
        )


def test_profile_clone_all_runs_lifecycle_inventory_backfill(
    source_profile_with_secrets,
):
    """F4 — clone-all from an INCOMPLETE source backfills the full
    inventory same as light-clone (the lifecycle clone-all branch ALREADY
    ran the bootstrap loop pre-fix; this test pins the contract).
    """
    from personas.lifecycle import (
        _REQUIRED_MEMORY_DIRS,
        _REQUIRED_PROFILE_DIRS,
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["profile", "clone-all", "source", "dest", "--no-alias"]
    )
    assert result.exit_code == 0, result.output

    homie_root = source_profile_with_secrets.parent.parent
    dst = homie_root / "profiles" / "dest"
    assert dst.exists(), "destination not created"

    missing = [
        sub for sub in _REQUIRED_PROFILE_DIRS if not (dst / sub).is_dir()
    ]
    assert not missing, (
        f"F4 violation: clone-all dest missing required dirs: {missing}"
    )
    missing_mem = [
        m for m in _REQUIRED_MEMORY_DIRS if not (dst / "memory" / m).is_dir()
    ]
    assert not missing_mem, (
        f"F4 violation: clone-all dest missing memory subdirs: {missing_mem}"
    )


# ---------------------------------------------------------------------------
# NF1 (post-build adversarial review round 2) — clone-all from "default"
# does NOT recursive-copy the install repo
# ---------------------------------------------------------------------------


def test_profile_clone_all_from_default_does_not_copy_install_repo(
    empty_homie_root, default_profile_install, monkeypatch
):
    """NF1 — ``thehomie profile clone-all default <name>`` MUST NOT
    recursive-copy the install repo into the destination profile.

    Mirrors the direct-API NF1 test, exercised via the Click CLI path so
    we cover both call sites (the CLI handler + the lifecycle layer).
    Pre-fix: the install repo's ``.git/``, nested ``.env``, integrations
    credentials, and workspace code all landed in the destination.
    """
    install_root = default_profile_install

    # Seed install-dir with secret-shaped paths.
    secret_paths = [
        install_root / ".claude" / "scripts" / ".env",
        install_root / ".claude" / "scripts" / "integrations"
            / "google_token.json",
        install_root / "deep" / "nested" / ".env",
        install_root / "private.pem",
    ]
    for p in secret_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("FAKE_SECRET_VALUE_DO_NOT_LEAK", encoding="utf-8")
    git_dir = install_root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")

    # Seed default SOUL.md with a marker so we can prove default memory
    # content reaches dest/memory/SOUL.md after CLI clone-all.
    default_soul = install_root / "TheHomie" / "Memory" / "SOUL.md"
    marker = "NF1-CLI-MARKER-clone-all-default-7e21bd83"
    default_soul.write_text(
        f"# Default SOUL\n\n{marker}\n", encoding="utf-8"
    )

    # default_profile_install delenv'd HOMIE_HOME — re-set it so the CLI
    # writes the destination under the test tree.
    monkeypatch.setenv("HOMIE_HOME", str(empty_homie_root))

    runner = CliRunner()
    result = runner.invoke(
        main, ["profile", "clone-all", "default", "ops", "--no-alias"]
    )
    assert result.exit_code == 0, result.output

    dst = empty_homie_root / "profiles" / "ops"
    assert dst.exists(), "destination profile not created"

    # NF1 — no install-repo trash in dest.
    assert not (dst / ".git").exists(), (
        "NF1 CLI violation: dest contains .git/ from install-repo recursive copy"
    )
    assert not (dst / ".claude").exists(), (
        "NF1 CLI violation: dest contains .claude/ from install repo"
    )
    assert not (dst / "TheHomie").exists(), (
        "NF1 CLI violation: dest contains TheHomie/ — default memory should "
        "be remapped to dest/memory/, not preserved at install layout"
    )
    assert not (dst / "integrations").exists(), (
        "NF1 CLI violation: dest contains integrations/ (credentials root)"
    )
    assert not (dst / ".env").exists(), (
        "NF1 CLI violation: dest/.env exists from install-repo copy"
    )

    # NF1 — no token / .pem / nested .env files anywhere in dest.
    leaks: list[str] = []
    for path in dst.rglob("*"):
        if path.is_dir():
            continue
        name = path.name.lower()
        rel = str(path.relative_to(dst))
        if name == ".env" or name.startswith(".env."):
            leaks.append(rel)
        elif name.endswith(".pem") or name.endswith(".key"):
            leaks.append(rel)
        elif "token" in name and name.endswith(".json"):
            leaks.append(rel)
        elif "credentials" in name and name.endswith(".json"):
            leaks.append(rel)
    assert not leaks, (
        f"NF1 CLI violation: secret-shaped leaks in dest: {leaks}"
    )

    # NF1 + F5 — default memory content landed at dest/memory/SOUL.md.
    dest_soul = dst / "memory" / "SOUL.md"
    assert dest_soul.exists(), (
        "NF1+F5 CLI violation: dest/memory/SOUL.md missing"
    )
    assert marker in dest_soul.read_text(encoding="utf-8"), (
        "NF1+F5 CLI violation: dest/memory/SOUL.md does NOT contain the "
        "default identity marker"
    )


# ---------------------------------------------------------------------------
# init-archon
# ---------------------------------------------------------------------------


def test_profile_init_archon_creates_skeleton(tmp_homie_home):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "init-archon", "sales"])
    assert result.exit_code == 0, result.output
    archon = tmp_homie_home / "archon"
    assert archon.is_dir()
    assert (archon / "config.yaml").exists()


# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------


def test_profile_export_default_path(tmp_homie_home):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "export", "sales"])
    assert result.exit_code == 0, result.output
    # Output line includes target path.
    assert "Exported profile" in result.output


def test_profile_export_custom_output(tmp_homie_home, tmp_path):
    out = tmp_path / "sales-archive.tar.gz"
    runner = CliRunner()
    result = runner.invoke(
        main, ["profile", "export", "sales", "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_profile_import_nonexistent_archive_exits_1(empty_homie_root, tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "import", str(tmp_path / "missing.tar.gz")])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# migrate-default
# ---------------------------------------------------------------------------


def test_profile_migrate_default_dry_run_default_mode(empty_homie_root):
    """Default mode is --dry-run — prints op summary, exits 0."""
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "migrate-default"])
    assert result.exit_code == 0, result.output


def test_profile_migrate_default_apply_writes_journal(empty_homie_root):
    """`--apply` writes the journal stub and exits 0."""
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "migrate-default", "--apply"])
    assert result.exit_code == 0, result.output
    journal = empty_homie_root / "migration-journal.json"
    assert journal.exists()
