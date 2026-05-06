"""PRP-7e Phase 5 WS2b — `thehomie archon list/run/status` runner subgroup.

Covers the WS2b operator surface added in `chat/cli.py:archon` (Click subgroup
declared at root level via `@main.group()`). The subgroup is a thin wrapper
over the real `archon` binary — these tests assert the WRAPPER contract:

    1. Subprocess invocation shape — argv + env passed to ``archon`` is exactly
       the R4 ARCHON_HOME pivot shape: ``["archon", "workflow", <verb>, ...,
       "--cwd", <git_repo>]`` with ``env["ARCHON_HOME"] == <profile>/.archon``
       AND ``env["HOMIE_HOME"]`` AND ``env["HOMIE_NAME"]`` injected via
       ``personas.get_subprocess_env()``.

    2. Exit code mapping — ``ArchonNotInstalledError`` -> 4 (PRD §12.3),
       generic init/profile errors -> 1, ``archon status`` ALWAYS -> 0
       (R3 NM-minor diagnostic-only).

    3. R4 NB1 regression guard — argv MUST include ``--cwd <git_repo>`` AND
       ``env["ARCHON_HOME"]`` (NOT ``--cwd <profile_root>`` which was the
       rejected R3 design).

    4. R3 NB2 regression guard — `archon status` reports STALE when the
       on-disk config has stale R2-era derived values (e.g. ``root: archon``
       without the dot) even though every key is present.

Two test patterns (per PRP §"WS2b runner tests"):

    - Pattern A — Pure `CliRunner` (no `-p` flag, default profile only).
      Monkeypatched ``subprocess.run`` / ``subprocess.check_output`` /
      ``personas.archon.detect_archon_binary`` so no real binary needed.

    - Pattern B (deferred) — Subprocess via console_scripts entrypoint for
      `-p <name>` profile-flag tests, since `apply_persona_override()` strips
      `-p` from `sys.argv` BEFORE Click sees it. Click's `main` group
      declares no `-p` option; `CliRunner().invoke(main, ["-p", "sales", ...])`
      fails with "no such option". WS2b smoke runs (`uv run thehomie -p
      ws2a-test archon list/status`) cover the production profile-selection
      path end-to-end.

R-Notes (which Codex review round each test addresses):
    - R1 B1 — PRP-7e originally dropped the runner subgroup; WS2b reinstates.
    - R3 NB1 — argv must use ``--cwd <git_repo>`` not ``--cwd <profile_root>``.
    - R3 NB2 — shape validator must reject stale R2 derived values.
    - R3 NM-minor — `archon status` is diagnostic-only (always exits 0).
    - R4 — ARCHON_HOME pivot superseded R3 Option C (no ARCHON_SOURCE_REPO).

Sign-off: YourAgent (Phase 5 WS2b executor).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

# `chat/cli.py` runs `apply_persona_override()` at import time. Tests must
# import `cli` AFTER any test-specific environment monkeypatching, but the
# module-level import here is fine because the precedence chain falls through
# to rank-4 default in a clean test env. Per-test fixtures clear HOMIE_HOME.
from cli import main


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_archon_repo(tmp_path: Path) -> Path:
    """Build a minimal tmp git repo with one commit.

    Used as the `--cwd` value for archon subprocess calls (Archon's git-probe
    refuses non-git directories). Local copy — WS3 owns the canonical copy
    in `conftest.py` but WS2b runs in parallel so we keep our own.
    """
    repo = tmp_path / "tmp_archon_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "master"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "README.md").write_text("# tmp\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "smoke@example.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Smoke Test"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


@pytest.fixture
def isolated_default_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Pin HOMIE_HOME to a tmp dir layout where the active profile is 'default'.

    `get_active_profile_name()` returns "default" when HOMIE_HOME is unset or
    equals ~/.homie. We unset HOMIE_HOME entirely so the resolver returns
    "default", and pin the install-root paths via HOMIE_VAULT_DIR so any
    archon resolver call lands inside `tmp_path` instead of the real install.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    monkeypatch.delenv("HOMIE_NAME", raising=False)
    return tmp_path


@pytest.fixture
def fake_archon_binary(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Replace `personas.archon.detect_archon_binary` so no real binary needed.

    Returns (binary_path, version) — same shape the real function returns.
    Patches the module-attr lookup so any call site that does
    ``from personas.archon import detect_archon_binary`` AND any later call
    via ``personas.archon.detect_archon_binary`` both see the fake.
    """
    fake = (Path("/fake/archon"), "0.3.10")

    def _fake_detect(*, expected_version: str | None = None):
        # Mirror the real function's expected_version handling.
        if expected_version is not None and expected_version != fake[1]:
            from personas.archon import ArchonVersionMismatchError
            raise ArchonVersionMismatchError(
                f"version mismatch: installed {fake[1]!r}, "
                f"expected {expected_version!r}"
            )
        return fake

    monkeypatch.setattr(
        "personas.archon.detect_archon_binary", _fake_detect
    )
    # Also patch `cli.archon_list`/`archon_run`/`archon_status`'s local-import
    # path: those handlers do `from personas.archon import detect_archon_binary`
    # at call time, so the module-level patch above is what they observe.
    return fake


@pytest.fixture
def compliant_default_archon_config(
    fake_archon_binary: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Synthesize a PRD §11.1-compliant config.yaml for the default profile.

    Returns the path to `<archon_home>/config.yaml`. Patches
    `personas.archon._archon_root_for("default")` to point at tmp_path so we
    don't have to write to the real install dir. Detection is faked.
    """
    archon_home = tmp_path / ".archon"
    archon_home.mkdir(parents=True, exist_ok=True)
    (archon_home / "workflows").mkdir(exist_ok=True)
    config_path = archon_home / "config.yaml"

    payload = {
        "capabilities": {
            "archon": {
                "enabled": True,
                "binary": "archon",
                "archon_version": "0.3.10",
                "root": ".archon",
                "workflows_dir": ".archon/workflows",
                "commands_dir": ".archon/commands",
                "artifacts_dir": ".archon/artifacts",
                "ralph_dir": ".archon/ralph",
                "worktrees_dir": ".archon/worktrees",
                "default_workflow": "archon-assist",
            }
        },
        "worktree": {
            "baseBranch": "master",
            "base_path": ".archon/worktrees",
        },
    }
    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    # Re-route resolver to our tmp archon_home for the runner under test.
    def _fake_root(name: str) -> Path:
        return archon_home

    monkeypatch.setattr("personas.archon._archon_root_for", _fake_root)
    # Also re-route `cli._resolve_archon_home_for_runner` so the tests can
    # assert the env var via `_resolve_archon_home_for_runner(...)` indirectly.
    # The cli helper uses `personas.get_persona_paths`, which doesn't go
    # through `_archon_root_for`. So patch the cli helper directly.
    import cli

    monkeypatch.setattr(
        cli, "_resolve_archon_home_for_runner", lambda name: str(archon_home)
    )
    # F2 post-build fix: also re-route the new `_resolve_homie_home_for_runner`
    # helper so tests get the tmp install root (parent of archon_home) instead
    # of the real repo root. The default-profile design pick is "HOMIE_HOME =
    # parent of ARCHON_HOME"; mirror that here.
    monkeypatch.setattr(
        cli, "_resolve_homie_home_for_runner", lambda name: str(archon_home.parent)
    )
    return config_path


@pytest.fixture
def stale_r2_archon_config(
    fake_archon_binary: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Synthesize a stale R2-era config.yaml — every key present BUT
    ``root: archon`` (no leading dot). This is the R3 NB2 regression case:
    presence-only validators pass; canonical-value validators reject.
    """
    archon_home = tmp_path / ".archon"
    archon_home.mkdir(parents=True, exist_ok=True)
    config_path = archon_home / "config.yaml"

    payload = {
        "capabilities": {
            "archon": {
                "enabled": True,
                "binary": "archon",
                "archon_version": "0.3.10",
                "root": "archon",  # STALE R2 — no leading dot
                "workflows_dir": "archon/workflows",
                "commands_dir": "archon/commands",
                "artifacts_dir": "archon/artifacts",
                "ralph_dir": "archon/ralph",
                "worktrees_dir": "archon/worktrees",
                "default_workflow": "archon-assist",
            }
        },
        "worktree": {
            "baseBranch": "master",
            "base_path": "archon/worktrees",
        },
    }
    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    def _fake_root(name: str) -> Path:
        return archon_home

    monkeypatch.setattr("personas.archon._archon_root_for", _fake_root)
    import cli

    monkeypatch.setattr(
        cli, "_resolve_archon_home_for_runner", lambda name: str(archon_home)
    )
    return config_path


# =============================================================================
# Subgroup wiring + help
# =============================================================================


def test_archon_group_is_registered_at_root_level():
    """`archon` is a top-level subgroup of `main` (NOT under `profile`)."""
    assert "archon" in main.commands, (
        f"`archon` group not registered. Top-level commands: "
        f"{sorted(main.commands.keys())!r}"
    )
    archon_grp = main.commands["archon"]
    expected = {"list", "run", "status"}
    assert expected.issubset(archon_grp.commands.keys()), (
        f"missing archon subcommands: "
        f"{expected - set(archon_grp.commands.keys())}"
    )


def test_archon_is_NOT_under_profile_subgroup():
    """Regression: `archon` lives at root, NOT under `profile`."""
    profile_grp = main.commands["profile"]
    assert "archon" not in profile_grp.commands, (
        "PRP-7e WS2b regression: `archon` must be a top-level group "
        "(`thehomie archon ...`), not nested under `profile`."
    )


def test_archon_subcommands_help_text():
    """Each archon subcommand has --help wired."""
    runner = CliRunner()
    for sub in ("list", "run", "status"):
        result = runner.invoke(main, ["archon", sub, "--help"])
        assert result.exit_code == 0, (
            f"`archon {sub} --help` failed: {result.output!r}"
        )
        # `run` takes a positional arg, so its help mentions WORKFLOW.
        if sub == "run":
            assert "WORKFLOW" in result.output


# =============================================================================
# `thehomie archon list` — Pattern A (CliRunner)
# =============================================================================


def test_archon_list_invokes_archon_with_archon_home_and_git_cwd(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_archon_repo: Path,
):
    """Happy path — archon list invokes the binary with R4 shape."""
    captured: dict = {}

    def _fake_run(cmd, env=None, check=False, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = env

        class _Res:
            returncode = 0

        return _Res()

    def _fake_check_output(cmd, **kwargs):
        # `git rev-parse --show-toplevel` -> tmp_archon_repo
        return str(tmp_archon_repo) + "\n"

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "check_output", _fake_check_output)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "list"])
    assert result.exit_code == 0, result.output

    # argv shape — R4 ARCHON_HOME pivot
    assert captured["cmd"][:3] == ["archon", "workflow", "list"], (
        f"expected ['archon', 'workflow', 'list', ...], got {captured['cmd']!r}"
    )
    assert "--cwd" in captured["cmd"]
    assert str(tmp_archon_repo) in captured["cmd"]

    # env shape — ARCHON_HOME points at <profile>/.archon
    archon_home = compliant_default_archon_config.parent
    assert captured["env"]["ARCHON_HOME"] == str(archon_home), (
        f"expected ARCHON_HOME={archon_home}, "
        f"got {captured['env'].get('ARCHON_HOME')!r}"
    )
    assert captured["env"].get("ARCHON_SUPPRESS_NESTED_CLAUDE_WARNING") == "1"
    # HOMIE_HOME and HOMIE_NAME forwarded.
    assert "HOMIE_HOME" in captured["env"]
    assert "HOMIE_NAME" in captured["env"]


def test_archon_list_cmd_shape_includes_archon_home_env_NB1_regression(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_archon_repo: Path,
):
    """R3 NB1 regression — argv MUST use `--cwd <git_repo>` AND env MUST
    set `ARCHON_HOME=<profile>/.archon`.

    The rejected R3 design used `--cwd <profile_root>`, which is non-git and
    failed Archon's git-probe. The R4 ARCHON_HOME pivot moves per-profile
    state isolation into the env var and points --cwd at the operator's git
    repo. This test catches any regression that drops the env or reverts to
    the profile-root --cwd shape.
    """
    captured: dict = {}

    def _fake_run(cmd, env=None, check=False, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = env

        class _Res:
            returncode = 0

        return _Res()

    def _fake_check_output(cmd, **kwargs):
        return str(tmp_archon_repo) + "\n"

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "check_output", _fake_check_output)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "list"])
    assert result.exit_code == 0, result.output

    # --cwd value is the GIT REPO, NOT the profile root.
    cwd_idx = captured["cmd"].index("--cwd")
    cwd_val = captured["cmd"][cwd_idx + 1]
    assert cwd_val == str(tmp_archon_repo), (
        f"R3 NB1 regression: --cwd must be the operator's git repo "
        f"({tmp_archon_repo}), got {cwd_val!r}"
    )
    # ARCHON_HOME env MUST be present and equal <profile>/.archon.
    archon_home = compliant_default_archon_config.parent
    assert captured["env"].get("ARCHON_HOME") == str(archon_home), (
        f"R4 pivot regression: env must include "
        f"ARCHON_HOME={archon_home}, got {captured['env'].get('ARCHON_HOME')!r}"
    )
    # ARCHON_SOURCE_REPO MUST NOT be set (R4 dropped it).
    assert "ARCHON_SOURCE_REPO" not in captured["env"], (
        "R4 regression: ARCHON_SOURCE_REPO was dropped — should not be "
        "in subprocess env"
    )


def test_archon_list_passes_through_json_flag(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_archon_repo: Path,
):
    """`archon list --json` appends `--json` to the archon binary call."""
    captured: dict = {}

    def _fake_run(cmd, env=None, check=False, **kwargs):
        captured["cmd"] = cmd

        class _Res:
            returncode = 0

        return _Res()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        subprocess, "check_output", lambda cmd, **kw: str(tmp_archon_repo) + "\n"
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert "--json" in captured["cmd"]


def test_archon_list_exit_4_when_archon_missing(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
):
    """archon binary not installed -> exit 4 (PRD §12.3)."""
    from personas.archon import ArchonNotInstalledError

    def _fake_detect(*, expected_version=None):
        raise ArchonNotInstalledError("archon binary not found on PATH")

    monkeypatch.setattr("personas.archon.detect_archon_binary", _fake_detect)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "list"])
    assert result.exit_code == 4, (
        f"expected exit 4 (archon not installed), got {result.exit_code}: "
        f"{result.output!r}"
    )
    assert "archon" in result.output.lower()


def test_archon_list_exit_1_when_profile_not_initialized(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_path: Path,
):
    """Profile config absent -> exit 1 with init-archon hint."""
    # Point the resolver at a tmp dir with NO config.yaml.
    archon_home = tmp_path / "missing_archon"

    def _fake_root(name: str) -> Path:
        return archon_home

    monkeypatch.setattr("personas.archon._archon_root_for", _fake_root)
    import cli

    monkeypatch.setattr(
        cli, "_resolve_archon_home_for_runner", lambda name: str(archon_home)
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "list"])
    assert result.exit_code == 1, (
        f"expected exit 1 (not initialized), got {result.exit_code}: "
        f"{result.output!r}"
    )
    assert "init-archon" in result.output, (
        f"expected hint to run init-archon, got: {result.output!r}"
    )


def test_archon_list_exit_1_when_not_in_git_repo(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
):
    """Non-git CWD -> exit 1 with explicit "must be invoked from inside a
    git repo" message. Catches the R3 NB1 class of bug at the wrapper layer.
    """

    def _fake_check_output(cmd, **kwargs):
        # `git rev-parse --show-toplevel` outside a repo -> non-zero exit.
        raise subprocess.CalledProcessError(
            128, cmd, output="", stderr="not a git repository\n"
        )

    monkeypatch.setattr(subprocess, "check_output", _fake_check_output)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "list"])
    assert result.exit_code == 1, (
        f"expected exit 1 (not in git repo), got {result.exit_code}"
    )
    assert "git repo" in result.output.lower()


# =============================================================================
# `thehomie archon run` — Pattern A
# =============================================================================


def test_archon_run_passes_workflow_args_through(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_archon_repo: Path,
):
    """`archon run myflow --foo bar` passes trailing args through to archon
    binary AFTER the --cwd insertion.
    """
    captured: dict = {}

    def _fake_run(cmd, env=None, check=False, **kwargs):
        captured["cmd"] = cmd

        class _Res:
            returncode = 0

        return _Res()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        subprocess, "check_output", lambda cmd, **kw: str(tmp_archon_repo) + "\n"
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "run", "myflow", "--foo", "bar"])
    assert result.exit_code == 0, result.output
    assert captured["cmd"][:4] == ["archon", "workflow", "run", "myflow"]
    # Trailing args appear after --cwd <git_repo>.
    assert "--foo" in captured["cmd"]
    assert "bar" in captured["cmd"]
    # --cwd <git_repo> still present.
    cwd_idx = captured["cmd"].index("--cwd")
    assert captured["cmd"][cwd_idx + 1] == str(tmp_archon_repo)


def test_archon_run_injects_ARCHON_HOME_HOMIE_HOME_HOMIE_NAME(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_archon_repo: Path,
):
    """env passed to subprocess includes ARCHON_HOME, HOMIE_HOME, HOMIE_NAME.

    Critically does NOT include ARCHON_SOURCE_REPO (R4 dropped it).

    F2 post-build fix: HOMIE_HOME must be a concrete non-empty path that
    points at a real existing dir AND satisfies the smoke YAML's strict
    ``${HOMIE_HOME:?...}`` expansion. Empty string used to be silently
    accepted — that's the gas-station behavior the F2 tighten removes.
    """
    captured: dict = {}

    def _fake_run(cmd, env=None, check=False, **kwargs):
        captured["env"] = env

        class _Res:
            returncode = 0

        return _Res()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        subprocess, "check_output", lambda cmd, **kw: str(tmp_archon_repo) + "\n"
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "run", "smoke"])
    assert result.exit_code == 0, result.output

    env = captured["env"]
    archon_home = compliant_default_archon_config.parent
    assert env["ARCHON_HOME"] == str(archon_home)
    # F2 post-build fix: HOMIE_HOME must be present, non-empty, AND point
    # at a real on-disk dir.
    assert "HOMIE_HOME" in env
    assert env["HOMIE_HOME"] != "", (
        "F2 regression: HOMIE_HOME must NOT be empty — the shipped smoke "
        "YAML uses strict ``${HOMIE_HOME:?...}`` expansion which would "
        "hard-fail an empty value"
    )
    assert Path(env["HOMIE_HOME"]).is_dir(), (
        f"F2 contract: HOMIE_HOME={env['HOMIE_HOME']!r} must point at an "
        f"existing on-disk dir so smoke ``mkdir -p $HOMIE_HOME/.archon/...`` "
        f"succeeds"
    )
    # The smoke YAML writes under ``$HOMIE_HOME/.archon/...`` and ARCHON_HOME
    # is ``<profile>/.archon``; the two must agree on the same profile root.
    assert str(archon_home) == str(Path(env["HOMIE_HOME"]) / ".archon"), (
        f"F2 contract: HOMIE_HOME ({env['HOMIE_HOME']!r}) and ARCHON_HOME "
        f"({env['ARCHON_HOME']!r}) must point at the same profile root — "
        f"smoke output would otherwise land in a different dir than archon "
        f"state"
    )
    assert "HOMIE_NAME" in env
    # HOMIE_NAME falls back to active profile name when env unset.
    assert env["HOMIE_NAME"] == "default"

    # R4: ARCHON_SOURCE_REPO is dropped — should NOT be in env.
    assert "ARCHON_SOURCE_REPO" not in env, (
        "R4 regression: ARCHON_SOURCE_REPO was dropped — should not be in "
        "subprocess env"
    )


def test_archon_run_default_profile_homie_home_resolves_to_install_root(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_archon_repo: Path,
):
    """F2 post-build fix — default-profile runner resolves HOMIE_HOME to the
    install repo root (= parent of ``get_default_paths()["archon"]``).

    Pre-fix: ``HOMIE_HOME=os.environ.get("HOMIE_HOME", "")`` → empty
    string for default profile (boot.py rank-4 leaves the env unset).
    Post-fix: ``_resolve_homie_home_for_runner("default")`` returns the
    install root, which is the parent of ARCHON_HOME.
    """
    captured: dict = {}

    def _fake_run(cmd, env=None, check=False, **kwargs):
        captured["env"] = env

        class _Res:
            returncode = 0

        return _Res()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        subprocess, "check_output", lambda cmd, **kw: str(tmp_archon_repo) + "\n"
    )

    # Invoke without -p (rank-4 default). Default to no inherited HOMIE_HOME.
    runner = CliRunner()
    result = runner.invoke(main, ["archon", "run", "smoke"])
    assert result.exit_code == 0, result.output

    env = captured["env"]
    archon_home = compliant_default_archon_config.parent  # tmp_path / ".archon"
    # F2 design pick: default profile HOMIE_HOME = parent of ARCHON_HOME
    # (the install repo root that hosts ``.archon/``).
    expected_homie_home = str(archon_home.parent)
    assert env["HOMIE_HOME"] == expected_homie_home, (
        f"F2 contract: default profile HOMIE_HOME must equal "
        f"{expected_homie_home!r} (parent of ARCHON_HOME), "
        f"got {env['HOMIE_HOME']!r}"
    )
    # HOMIE_NAME also published.
    assert env["HOMIE_NAME"] == "default"
    # The smoke YAML's contract is satisfied: HOMIE_HOME exists on disk.
    assert Path(env["HOMIE_HOME"]).is_dir()


def test_archon_run_fails_when_not_in_git_repo(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
):
    """Run from non-git CWD -> exit 1 with explicit message."""

    def _fake_check_output(cmd, **kwargs):
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(subprocess, "check_output", _fake_check_output)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "run", "smoke"])
    assert result.exit_code == 1
    assert "git" in result.output.lower()


def test_archon_run_exit_4_when_archon_missing(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
):
    """archon binary missing -> exit 4 (same as `list`)."""
    from personas.archon import ArchonNotInstalledError

    def _fake_detect(*, expected_version=None):
        raise ArchonNotInstalledError("archon binary not found on PATH")

    monkeypatch.setattr("personas.archon.detect_archon_binary", _fake_detect)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "run", "smoke"])
    assert result.exit_code == 4


def test_archon_run_subprocess_returncode_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_archon_repo: Path,
):
    """archon binary's exit code is passed through unchanged.

    Critical for Paperclip / CI/CD — a workflow failing inside archon must
    surface as a non-zero exit at `thehomie archon run` so the harness knows.
    """

    def _fake_run(cmd, env=None, check=False, **kwargs):
        class _Res:
            returncode = 42

        return _Res()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        subprocess, "check_output", lambda cmd, **kw: str(tmp_archon_repo) + "\n"
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "run", "failing-flow"])
    assert result.exit_code == 42, (
        f"subprocess returncode passthrough broken: archon returned 42, "
        f"wrapper exited {result.exit_code}"
    )


# =============================================================================
# `thehomie archon status` — DIAGNOSTIC-ONLY (always exits 0)
# =============================================================================


def test_archon_status_reports_OK_when_compliant(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
):
    """Compliant config + binary present -> output contains "OK", exit 0."""
    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "matches" in result.output


def test_archon_status_reports_STALE_when_phase2_stub(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_path: Path,
):
    """Phase-2-stub config -> output contains "STALE", exit 0
    (R3 NM-minor: status is diagnostic-only).
    """
    archon_home = tmp_path / ".archon"
    archon_home.mkdir(parents=True, exist_ok=True)
    config_path = archon_home / "config.yaml"
    # Phase 2 stub: only `archon: enabled: true, version: "stub"`.
    config_path.write_text(
        'archon:\n  enabled: true\n  version: "stub"\n', encoding="utf-8"
    )

    def _fake_root(name: str) -> Path:
        return archon_home

    monkeypatch.setattr("personas.archon._archon_root_for", _fake_root)
    import cli

    monkeypatch.setattr(
        cli, "_resolve_archon_home_for_runner", lambda name: str(archon_home)
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status"])
    assert result.exit_code == 0, (
        f"R3 NM-minor: status is diagnostic-only, must always exit 0. "
        f"Got {result.exit_code}: {result.output!r}"
    )
    assert "STALE" in result.output


def test_archon_status_reports_STALE_when_stale_R2_derived_values(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    stale_r2_archon_config: Path,
):
    """R3 NB2 regression — stale R2-era config (every key present, but
    `root: archon` without the dot) -> STALE, exit 0.

    Proves the value-aware shape validator rejects stale derived values
    even when key presence passes. The R3 design hardened
    `_validate_config_shape` to compare canonical strings against the
    `_CANONICAL_DERIVED_VALUES` map.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status"])
    assert result.exit_code == 0, (
        f"status must always exit 0 (diagnostic-only). "
        f"Got {result.exit_code}: {result.output!r}"
    )
    assert "STALE" in result.output, (
        f"R3 NB2 regression: stale R2 config (root=archon, no dot) must "
        f"report STALE. Output: {result.output!r}"
    )


def test_archon_status_reports_MISSING_when_no_config(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_path: Path,
):
    """No config.yaml -> output contains "MISSING", exit 0."""
    archon_home = tmp_path / "missing_archon"
    # Don't even create the directory — `is_file()` -> False.

    def _fake_root(name: str) -> Path:
        return archon_home

    monkeypatch.setattr("personas.archon._archon_root_for", _fake_root)
    import cli

    monkeypatch.setattr(
        cli, "_resolve_archon_home_for_runner", lambda name: str(archon_home)
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status"])
    assert result.exit_code == 0
    assert "MISSING" in result.output


def test_archon_status_diagnostic_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
):
    """archon binary not installed -> output contains "NOT INSTALLED", exit 0
    (R3 NM-minor: status is diagnostic, doesn't gate on binary presence).
    """
    from personas.archon import ArchonNotInstalledError

    def _fake_detect(*, expected_version=None):
        raise ArchonNotInstalledError("archon binary not found on PATH")

    monkeypatch.setattr("personas.archon.detect_archon_binary", _fake_detect)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status"])
    assert result.exit_code == 0, (
        f"R3 NM-minor: status must always exit 0 even when binary missing. "
        f"Got {result.exit_code}: {result.output!r}"
    )
    assert "NOT INSTALLED" in result.output


def test_archon_status_diagnostic_when_version_lock_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    tmp_path: Path,
):
    """Config locked to v0.3.10 but binary reports v0.4.0 -> output contains
    "MISMATCH", exit 0 (R3 NM-minor: status is diagnostic).
    """
    # Synthesize compliant config locked to v0.3.10.
    archon_home = tmp_path / ".archon"
    archon_home.mkdir(parents=True, exist_ok=True)
    config_path = archon_home / "config.yaml"
    payload = {
        "capabilities": {
            "archon": {
                "enabled": True,
                "binary": "archon",
                "archon_version": "0.3.10",
                "root": ".archon",
                "workflows_dir": ".archon/workflows",
                "commands_dir": ".archon/commands",
                "artifacts_dir": ".archon/artifacts",
                "ralph_dir": ".archon/ralph",
                "worktrees_dir": ".archon/worktrees",
                "default_workflow": "archon-assist",
            }
        },
        "worktree": {
            "baseBranch": "master",
            "base_path": ".archon/worktrees",
        },
    }
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def _fake_root(name: str) -> Path:
        return archon_home

    monkeypatch.setattr("personas.archon._archon_root_for", _fake_root)
    import cli

    monkeypatch.setattr(
        cli, "_resolve_archon_home_for_runner", lambda name: str(archon_home)
    )

    # Detection returns DIFFERENT version than locked.
    def _fake_detect(*, expected_version=None):
        return (Path("/fake/archon"), "0.4.0")

    monkeypatch.setattr("personas.archon.detect_archon_binary", _fake_detect)

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status"])
    assert result.exit_code == 0, (
        f"R3 NM-minor: status must exit 0 on version mismatch. "
        f"Got {result.exit_code}"
    )
    assert "MISMATCH" in result.output


def test_archon_status_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    compliant_default_archon_config: Path,
    fake_archon_binary: tuple[Path, str],
):
    """`archon status --json` emits structured JSON, exit 0."""
    import json as json_mod

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status", "--json"])
    assert result.exit_code == 0, result.output

    payload = json_mod.loads(result.output)
    assert payload["profile"] == "default"
    assert payload["config_state"] == "OK"
    assert payload["initialized"] is True
    assert payload["installed_version"] == "0.3.10"
    assert payload["locked_version"] == "0.3.10"
    assert payload["version_match"] is True


def test_archon_status_json_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    isolated_default_profile: Path,
    fake_archon_binary: tuple[Path, str],
    tmp_path: Path,
):
    """`archon status --json` reports config_state=MISSING when no config."""
    import json as json_mod

    archon_home = tmp_path / "missing_archon"

    def _fake_root(name: str) -> Path:
        return archon_home

    monkeypatch.setattr("personas.archon._archon_root_for", _fake_root)
    import cli

    monkeypatch.setattr(
        cli, "_resolve_archon_home_for_runner", lambda name: str(archon_home)
    )

    runner = CliRunner()
    result = runner.invoke(main, ["archon", "status", "--json"])
    assert result.exit_code == 0
    payload = json_mod.loads(result.output)
    assert payload["config_state"] == "MISSING"
    assert payload["initialized"] is False
