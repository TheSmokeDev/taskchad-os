"""Regression tests for the staged, rollback-capable framework updater."""

from __future__ import annotations

import subprocess
from pathlib import Path

from framework_update import FrameworkUpdater, UpdateFailedError
from shared import file_lock


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def make_repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    live = tmp_path / "live"
    git(tmp_path, "init", "--bare", str(remote))
    seed.mkdir()
    git(seed, "init", "-b", "master")
    git(seed, "config", "user.name", "Test Operator")
    git(seed, "config", "user.email", "test@example.com")
    write(seed, ".claude/scripts/pyproject.toml", '[project]\nversion = "1.0.1"\n')
    write(seed, "base.txt", "base\n")
    commit(seed, "base")
    git(seed, "tag", "v1.0.1")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "master", "--tags")
    git(tmp_path, "clone", str(remote), str(live))
    git(live, "config", "user.name", "Deployment Operator")
    git(live, "config", "user.email", "deploy@example.com")
    return seed, live, remote


def publish_release(seed: Path, *, path: str = "release.txt", value: str = "release\n") -> str:
    write(seed, path, value)
    write(seed, ".claude/scripts/pyproject.toml", '[project]\nversion = "1.1.0"\n')
    revision = commit(seed, "release 1.1.0")
    git(seed, "tag", "v1.1.0")
    git(seed, "push", "origin", "master", "--tags")
    return revision


def updater(live: Path, tmp_path: Path, **kwargs) -> FrameworkUpdater:
    return FrameworkUpdater(
        live,
        state_dir=tmp_path / "state",
        release_lookup=lambda: {"tag": "v1.1.0", "version": "1.1.0"},
        validator=kwargs.pop("validator", lambda _root: []),
        dependency_installer=kwargs.pop("dependency_installer", lambda _root: []),
        **kwargs,
    )


def test_dependency_sync_is_frozen_only_when_release_ships_a_lockfile(tmp_path: Path) -> None:
    root = tmp_path / "package"
    scripts = root / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    calls: list[list[str]] = []
    tracked_lock = False

    def runner(argv, _cwd):
        if list(argv[:2]) == ["git", "ls-files"]:
            return subprocess.CompletedProcess(argv, 0 if tracked_lock else 1, "", "")
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    manager = FrameworkUpdater(root, state_dir=tmp_path / "state", runner=runner)
    manager._default_install_dependencies(root)
    assert calls[-1][-1] == "sync"

    (scripts / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    tracked_lock = True
    manager._default_install_dependencies(root)
    assert calls[-1][-2:] == ["sync", "--frozen"]


def test_clean_upgrade_fast_forwards_after_candidate_validation(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    target = publish_release(seed)
    result = updater(live, tmp_path).apply()

    assert result.status == "applied"
    assert result.deployment_mode == "clean"
    assert git(live, "rev-parse", "HEAD") == target
    assert (live / "release.txt").read_text() == "release\n"
    assert result.rollback_ref
    assert git(live, "rev-parse", result.rollback_ref) == result.baseline_revision


def test_custom_branch_is_merged_in_candidate_without_losing_local_commit(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    write(live, "deployment.txt", "YourBusiness\n")
    local = commit(live, "deployment customization")
    publish_release(seed)

    result = updater(live, tmp_path).apply()

    assert result.status == "applied"
    assert result.deployment_mode == "customized"
    assert (live / "deployment.txt").read_text() == "YourBusiness\n"
    assert (live / "release.txt").read_text() == "release\n"
    parents = git(live, "show", "-s", "--format=%P", "HEAD").split()
    assert parents[0] == local
    assert len(parents) == 2


def test_custom_merge_conflict_blocks_without_touching_live_head(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    write(live, "base.txt", "local\n")
    baseline = commit(live, "local conflict")
    publish_release(seed, path="base.txt", value="upstream\n")

    result = updater(live, tmp_path).apply()

    assert result.status == "blocked"
    assert "conflicts" in (result.blocker or "")
    assert git(live, "rev-parse", "HEAD") == baseline
    assert (live / "base.txt").read_text() == "local\n"


def test_tracked_dirt_refuses_before_fetch_or_mutation(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    baseline = git(live, "rev-parse", "HEAD")
    write(live, "base.txt", "dirty\n")

    result = updater(live, tmp_path).apply()

    assert result.status == "blocked"
    assert "tracked worktree" in (result.blocker or "")
    assert git(live, "rev-parse", "HEAD") == baseline


def test_untracked_release_collision_refuses_and_preserves_file(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    collision = ".claude/skills/operator-only/SKILL.md"
    publish_release(seed, path=collision, value="public\n")
    write(live, collision, "private\n")

    result = updater(live, tmp_path).apply()

    assert result.status == "blocked"
    assert "untracked operator paths" in (result.blocker or "")
    assert (live / collision).read_text() == "private\n"


def test_untracked_skill_hash_is_preserved_across_upgrade(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    skill = ".claude/skills/YourBusiness-private/SKILL.md"
    write(live, skill, "deployment only\n")

    result = updater(live, tmp_path).apply()

    assert result.status == "applied"
    assert result.protected_hashes_before == result.protected_hashes_after
    assert skill in result.protected_hashes_after
    assert (live / skill).read_text() == "deployment only\n"


def test_concurrent_update_is_blocked_by_exclusive_lock(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    manager = updater(live, tmp_path)

    with file_lock(manager.lock_file, timeout=0.1):
        result = manager.apply(lock_timeout=0.1)

    assert result.status == "blocked"
    assert result.blocker == "another update is already running"


def test_candidate_validation_failure_never_changes_live_head(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    baseline = git(live, "rev-parse", "HEAD")

    def fail(_root: Path) -> list[dict]:
        raise UpdateFailedError("tests failed")

    result = updater(live, tmp_path, validator=fail).apply()

    assert result.status == "failed"
    assert result.rollback_state == "not_needed"
    assert git(live, "rev-parse", "HEAD") == baseline


def test_dependency_failure_rolls_live_head_back(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    baseline = git(live, "rev-parse", "HEAD")
    calls = 0

    def install(_root: Path) -> list[dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UpdateFailedError("dependency install failed")
        return []

    result = updater(live, tmp_path, dependency_installer=install).apply()

    assert result.status == "rolled_back"
    assert result.rollback_state == "restored"
    assert git(live, "rev-parse", "HEAD") == baseline


def test_restart_failure_rolls_live_head_back(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    baseline = git(live, "rev-parse", "HEAD")

    def restart() -> None:
        raise RuntimeError("restart failed")

    result = updater(live, tmp_path).apply(restart=restart)

    assert result.status == "rolled_back"
    assert result.rollback_state == "restored"
    assert git(live, "rev-parse", "HEAD") == baseline


def test_health_failure_rolls_live_head_back(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    baseline = git(live, "rev-parse", "HEAD")

    result = updater(live, tmp_path).apply(
        restart=lambda: {"ok": True},
        health_check=lambda: False,
    )

    assert result.status == "rolled_back"
    assert result.rollback_state == "restored"
    assert git(live, "rev-parse", "HEAD") == baseline


def test_history_returns_latest_state_once_per_receipt(tmp_path: Path) -> None:
    seed, live, _ = make_repos(tmp_path)
    publish_release(seed)
    manager = updater(live, tmp_path)
    result = manager.apply()

    history = manager.history()

    assert len(history) == 1
    assert history[0]["receipt_id"] == result.receipt_id
    assert history[0]["status"] == "applied"
