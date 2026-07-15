"""Safe, staged framework updates for The Homie.

The updater deliberately treats a release as untrusted until it has been
merged in a disposable worktree and validated.  The live checkout is changed
only after that candidate succeeds.  Tracked dirt, merge conflicts, protected
untracked collisions, and concurrent runs all fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared import file_lock

DEFAULT_RELEASE_REPO = "TheSmokeDev/taskchad-os"
PROTECTED_ROOTS = (
    ".claude/skills/",
    ".claude/extensions/",
    ".claude/chat/extensions/",
    ".claude/scripts/integrations/",
)
RECEIPT_LIMIT = 100


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts[:3]) or (0,)


@dataclass(slots=True)
class UpdateStatus:
    success: bool
    current_version: str
    current_revision: str
    latest_version: str | None
    latest_revision: str | None
    target_tag: str | None
    update_available: bool
    deployment_mode: str
    branch: str | None
    tracked_dirty: bool
    untracked_count: int
    blocker: str | None
    schedule: dict[str, Any] | None
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class UpdateReceipt:
    receipt_id: str
    status: str
    started_at: str
    finished_at: str | None
    current_version: str
    target_version: str | None
    target_tag: str | None
    deployment_mode: str
    baseline_revision: str
    candidate_revision: str | None = None
    applied_revision: str | None = None
    rollback_ref: str | None = None
    blocker: str | None = None
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    protected_hashes_before: dict[str, str] = field(default_factory=dict)
    protected_hashes_after: dict[str, str] = field(default_factory=dict)
    rollback_state: str = "not_needed"
    requester: dict[str, str] | None = None
    scheduled: bool = False

    @property
    def success(self) -> bool:
        return self.status in {"applied", "up_to_date"}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["success"] = self.success
        return payload


class UpdateBlockedError(RuntimeError):
    """A safe precondition prevented the update before live mutation."""


class UpdateFailedError(RuntimeError):
    """Candidate or live validation failed."""


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]
Validator = Callable[[Path], list[dict[str, Any]]]
Callback = Callable[[], Any]


def _default_runner(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


class FrameworkUpdater:
    """Canonical update manager shared by chat, CLI, and schedulers."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        state_dir: str | Path | None = None,
        release_repo: str | None = None,
        remote: str | None = None,
        runner: Runner | None = None,
        validator: Validator | None = None,
        dependency_installer: Validator | None = None,
        release_lookup: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else self._default_state_dir()
        self.release_repo = release_repo or os.getenv(
            "HOMIE_UPDATE_RELEASE_REPO", DEFAULT_RELEASE_REPO
        )
        self.remote = remote or os.getenv("HOMIE_UPDATE_REMOTE", "origin")
        self.runner = runner or _default_runner
        self.validator = validator or self._default_validate_candidate
        self.dependency_installer = dependency_installer or self._default_install_dependencies
        self.release_lookup = release_lookup or self._lookup_latest_release
        self.history_file = self.state_dir / "framework-update-history.jsonl"
        self.lock_file = self.state_dir / "framework-update"

    def _default_state_dir(self) -> Path:
        try:
            import config

            return Path(config.STATE_DIR).resolve()
        except Exception:
            return self.repo_root / ".claude" / "data" / "state"

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(argv, cwd or self.repo_root)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise UpdateFailedError(f"{' '.join(argv)}: {detail}")
        return result

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        result = self._run(["git", *args], cwd=cwd, check=check)
        return (result.stdout or "").strip()

    def _lookup_latest_release(self) -> dict[str, str]:
        url = f"https://api.github.com/repos/{self.release_repo}/releases/latest"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "thehomie-safe-updater",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise UpdateBlockedError(f"GitHub release lookup failed: {exc}") from exc
        tag = str(payload.get("tag_name") or "").strip()
        if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", tag):
            raise UpdateBlockedError("GitHub latest release did not return a stable semantic tag")
        return {
            "tag": tag,
            "version": tag.removeprefix("v"),
            "published_at": str(payload.get("published_at") or ""),
        }

    def _current_version(self, root: Path | None = None) -> str:
        pyproject = (root or self.repo_root) / ".claude" / "scripts" / "pyproject.toml"
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            return "0.0.0"
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return match.group(1) if match else "0.0.0"

    def _tracked_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain", "--untracked-files=no"))

    def _untracked_paths(self) -> list[str]:
        raw = self._git("ls-files", "--others", "--exclude-standard", "-z")
        return sorted(path for path in raw.split("\0") if path)

    def _branch(self) -> str | None:
        value = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return value or None

    def _is_ancestor(self, older: str, newer: str) -> bool:
        result = self._run(
            ["git", "merge-base", "--is-ancestor", older, newer],
            cwd=self.repo_root,
            check=False,
        )
        return result.returncode == 0

    def _deployment_mode(self, baseline: str, target_revision: str | None) -> str:
        if self._branch() is None:
            return "detached"
        if not target_revision:
            return "unknown"
        if self._is_ancestor(baseline, target_revision):
            return "clean"
        if self._is_ancestor(target_revision, baseline):
            return "ahead"
        return "customized"

    def _resolve_local_tag(self, tag: str) -> str | None:
        value = self._git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}", check=False)
        return value or None

    def _fetch_release(self, tag: str) -> str:
        self._git("fetch", "--force", self.remote, f"refs/tags/{tag}:refs/tags/{tag}")
        revision = self._resolve_local_tag(tag)
        if not revision:
            raise UpdateBlockedError(f"release tag {tag} was not fetched")
        return revision

    def _release_status(self, *, fetch: bool) -> tuple[dict[str, str], str | None]:
        release = self.release_lookup()
        tag = release["tag"]
        target_revision = self._fetch_release(tag) if fetch else self._resolve_local_tag(tag)
        return release, target_revision

    def status(self, *, refresh: bool = True, include_schedule: bool = True) -> UpdateStatus:
        baseline = self._git("rev-parse", "HEAD")
        current = self._current_version()
        latest: dict[str, str] | None = None
        target_revision: str | None = None
        blocker: str | None = None
        try:
            latest, target_revision = self._release_status(fetch=False)
        except (UpdateBlockedError, UpdateFailedError) as exc:
            blocker = str(exc)
        tracked_dirty = self._tracked_dirty()
        if tracked_dirty:
            blocker = "tracked worktree changes must be committed or removed"
        schedule: dict[str, Any] | None = None
        if include_schedule:
            try:
                import update_scheduler

                schedule = update_scheduler.status(self.repo_root)
            except Exception as exc:
                schedule = {"supported": False, "enabled": False, "detail": str(exc)}
        latest_version = latest["version"] if latest else None
        available = bool(
            latest_version and _version_tuple(latest_version) > _version_tuple(current)
        )
        return UpdateStatus(
            success=blocker is None,
            current_version=current,
            current_revision=baseline,
            latest_version=latest_version,
            latest_revision=target_revision,
            target_tag=latest["tag"] if latest else None,
            update_available=available,
            deployment_mode=self._deployment_mode(baseline, target_revision),
            branch=self._branch(),
            tracked_dirty=tracked_dirty,
            untracked_count=len(self._untracked_paths()),
            blocker=blocker,
            schedule=schedule,
            checked_at=_now(),
        )

    def _protected_hashes(self, paths: Iterable[str] | None = None) -> dict[str, str]:
        selected = paths if paths is not None else self._untracked_paths()
        hashes: dict[str, str] = {}
        for rel in selected:
            normalized = rel.replace("\\", "/")
            if not normalized.startswith(PROTECTED_ROOTS):
                continue
            path = self.repo_root / rel
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[normalized] = digest.hexdigest()
        return hashes

    def _target_paths(self, target_revision: str) -> set[str]:
        raw = self._git("ls-tree", "-r", "--name-only", "-z", target_revision)
        return {path for path in raw.split("\0") if path}

    def _check_untracked_collisions(
        self, untracked: Sequence[str], target_revision: str
    ) -> None:
        collisions = sorted(set(untracked) & self._target_paths(target_revision))
        if collisions:
            shown = ", ".join(collisions[:5])
            suffix = f" (+{len(collisions) - 5} more)" if len(collisions) > 5 else ""
            raise UpdateBlockedError(
                f"release would overwrite untracked operator paths: {shown}{suffix}"
            )

    def _run_commands(
        self, root: Path, commands: list[tuple[Sequence[str], Path]]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for argv, cwd in commands:
            result = self.runner(argv, cwd)
            record = {
                "command": list(argv),
                "cwd": str(cwd.relative_to(root)) if cwd != root else ".",
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "output": ((result.stdout or "") + (result.stderr or ""))[-4000:],
            }
            results.append(record)
            if result.returncode != 0:
                raise UpdateFailedError(
                    f"validation failed: {' '.join(argv)} (exit {result.returncode})"
                )
        return results

    def _default_install_dependencies(self, root: Path) -> list[dict[str, Any]]:
        scripts = root / ".claude" / "scripts"
        if not (scripts / "pyproject.toml").is_file():
            return []
        uv = shutil.which("uv") or "uv"
        argv = [uv, "sync"]
        lockfile = scripts / "uv.lock"
        lock_is_tracked = False
        if lockfile.is_file():
            tracked = self.runner(
                ["git", "ls-files", "--error-unmatch", ".claude/scripts/uv.lock"],
                root,
            )
            lock_is_tracked = tracked.returncode == 0
        if lock_is_tracked:
            argv.append("--frozen")
        return self._run_commands(root, [(argv, scripts)])

    def _default_validate_candidate(self, root: Path) -> list[dict[str, Any]]:
        results = self._default_install_dependencies(root)
        scripts = root / ".claude" / "scripts"
        tests = [
            "tests/test_framework_update.py",
            "tests/test_update_scheduler.py",
            "tests/test_update_chat_command.py",
            "tests/test_chat_runtime_engine.py",
            "tests/test_core_handlers.py",
            "tests/test_cli.py",
        ]
        existing = [name for name in tests if (scripts / name).is_file()]
        if existing:
            uv = shutil.which("uv") or "uv"
            results.extend(
                self._run_commands(root, [([uv, "run", "pytest", *existing, "-q"], scripts)])
            )
        return results

    def _append_receipt(self, receipt: UpdateReceipt) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt.to_dict(), sort_keys=True) + "\n")

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        receipts_by_id: dict[str, dict[str, Any]] = {}
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            receipts_by_id[str(item.get("receipt_id") or uuid.uuid4())] = item
        receipts = list(receipts_by_id.values())[-max(1, min(limit, RECEIPT_LIMIT)):]
        return list(reversed(receipts))

    def _new_receipt(
        self,
        *,
        current_version: str,
        target_version: str | None,
        target_tag: str | None,
        deployment_mode: str,
        baseline: str,
        requester: dict[str, str] | None,
        scheduled: bool,
    ) -> UpdateReceipt:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return UpdateReceipt(
            receipt_id=f"{stamp}-{uuid.uuid4().hex[:8]}",
            status="preparing",
            started_at=_now(),
            finished_at=None,
            current_version=current_version,
            target_version=target_version,
            target_tag=target_tag,
            deployment_mode=deployment_mode,
            baseline_revision=baseline,
            requester=requester,
            scheduled=scheduled,
        )

    def apply(
        self,
        *,
        requester: dict[str, str] | None = None,
        scheduled: bool = False,
        restart: Callback | None = None,
        health_check: Callback | None = None,
        lock_timeout: float = 0.1,
    ) -> UpdateReceipt:
        """Apply the latest stable release or return a durable failure receipt."""

        self.state_dir.mkdir(parents=True, exist_ok=True)
        baseline = self._git("rev-parse", "HEAD")
        current_version = self._current_version()
        receipt = self._new_receipt(
            current_version=current_version,
            target_version=None,
            target_tag=None,
            deployment_mode="unknown",
            baseline=baseline,
            requester=requester,
            scheduled=scheduled,
        )
        try:
            with file_lock(self.lock_file, timeout=lock_timeout):
                return self._apply_locked(
                    receipt,
                    restart=restart,
                    health_check=health_check,
                )
        except TimeoutError:
            receipt.status = "blocked"
            receipt.blocker = "another update is already running"
            receipt.finished_at = _now()
            self._append_receipt(receipt)
            return receipt
        except Exception as exc:  # last-resort receipt; never hide updater failures
            receipt.status = "failed"
            receipt.blocker = f"{type(exc).__name__}: {exc}"
            receipt.finished_at = _now()
            self._append_receipt(receipt)
            return receipt

    def _apply_locked(
        self,
        receipt: UpdateReceipt,
        *,
        restart: Callback | None,
        health_check: Callback | None,
    ) -> UpdateReceipt:
        candidate_dir: Path | None = None
        live_changed = False
        try:
            if self._tracked_dirty():
                raise UpdateBlockedError("tracked worktree changes must be committed or removed")

            release, target_revision = self._release_status(fetch=True)
            if not target_revision:
                raise UpdateBlockedError(f"release tag {release['tag']} is unavailable locally")
            receipt.target_tag = release["tag"]
            receipt.target_version = release["version"]
            receipt.deployment_mode = self._deployment_mode(
                receipt.baseline_revision, target_revision
            )

            if _version_tuple(release["version"]) <= _version_tuple(receipt.current_version):
                receipt.status = "up_to_date"
                receipt.finished_at = _now()
                self._append_receipt(receipt)
                return receipt

            if receipt.deployment_mode in {"detached", "unknown"}:
                raise UpdateBlockedError(
                    f"deployment mode '{receipt.deployment_mode}' cannot be updated automatically"
                )

            untracked = self._untracked_paths()
            receipt.protected_hashes_before = self._protected_hashes(untracked)
            self._check_untracked_collisions(untracked, target_revision)

            candidate_dir = Path(tempfile.mkdtemp(prefix="thehomie-update-candidate-"))
            self._git("worktree", "add", "--detach", str(candidate_dir), receipt.baseline_revision)
            if receipt.deployment_mode == "clean":
                self._git("reset", "--hard", target_revision, cwd=candidate_dir)
            else:
                result = self._run(
                    [
                        "git",
                        "-c",
                        "user.name=The Homie Updater",
                        "-c",
                        "user.email=updater@localhost",
                        "merge",
                        "--no-edit",
                        "--no-ff",
                        target_revision,
                    ],
                    cwd=candidate_dir,
                    check=False,
                )
                if result.returncode != 0:
                    conflicts = self._git(
                        "diff", "--name-only", "--diff-filter=U", cwd=candidate_dir, check=False
                    )
                    detail = ", ".join(conflicts.splitlines()) or (
                        result.stderr or "merge failed"
                    ).strip()
                    raise UpdateBlockedError(
                        f"candidate merge conflicts require an operator: {detail}"
                    )

            receipt.candidate_revision = self._git("rev-parse", "HEAD", cwd=candidate_dir)
            receipt.validation_results = self.validator(candidate_dir)

            receipt.rollback_ref = (
                "refs/thehomie-update-backups/" + receipt.receipt_id
            )
            self._git(
                "update-ref",
                receipt.rollback_ref,
                receipt.baseline_revision,
            )
            # Persist a pre-mutation receipt so a power loss still leaves the
            # operator with an exact rollback ref and candidate revision.
            self._append_receipt(receipt)

            self._git("reset", "--hard", receipt.candidate_revision)
            live_changed = True
            receipt.applied_revision = self._git("rev-parse", "HEAD")
            receipt.validation_results.extend(self.dependency_installer(self.repo_root))

            receipt.protected_hashes_after = self._protected_hashes(untracked)
            if receipt.protected_hashes_after != receipt.protected_hashes_before:
                raise UpdateFailedError(
                    "protected skill or extension hashes changed during update"
                )

            if restart is not None:
                restart_result = restart()
                receipt.validation_results.append(
                    {"command": ["restart"], "cwd": ".", "ok": True, "result": restart_result}
                )
            if health_check is not None:
                health_result = health_check()
                if health_result is False:
                    raise UpdateFailedError("post-restart health verification failed")
                receipt.validation_results.append(
                    {"command": ["health-check"], "cwd": ".", "ok": True, "result": health_result}
                )

            receipt.status = "applied"
            receipt.rollback_state = "available"
            receipt.finished_at = _now()
            self._append_receipt(receipt)
            return receipt
        except Exception as exc:
            receipt.blocker = str(exc)
            if live_changed:
                try:
                    self._git("reset", "--hard", receipt.baseline_revision)
                    self.dependency_installer(self.repo_root)
                    receipt.rollback_state = "restored"
                    receipt.status = "rolled_back"
                    if restart is not None:
                        try:
                            restart()
                        except Exception as restart_exc:
                            receipt.validation_results.append(
                                {
                                    "command": ["rollback-restart"],
                                    "cwd": ".",
                                    "ok": False,
                                    "result": str(restart_exc),
                                }
                            )
                    if health_check is not None:
                        try:
                            health_check()
                        except Exception as health_exc:
                            receipt.validation_results.append(
                                {
                                    "command": ["rollback-health-check"],
                                    "cwd": ".",
                                    "ok": False,
                                    "result": str(health_exc),
                                }
                            )
                except Exception as rollback_exc:
                    receipt.rollback_state = f"failed: {rollback_exc}"
                    receipt.status = "failed"
            else:
                receipt.status = "blocked" if isinstance(exc, UpdateBlockedError) else "failed"
            receipt.finished_at = _now()
            self._append_receipt(receipt)
            return receipt
        finally:
            if candidate_dir is not None:
                self._git("worktree", "remove", "--force", str(candidate_dir), check=False)
                shutil.rmtree(candidate_dir, ignore_errors=True)


def resolve_repo_root(start: str | Path | None = None) -> Path:
    candidate = Path(start or os.getenv("HOMIE_UPDATE_REPO_ROOT") or Path.cwd()).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=candidate,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise UpdateBlockedError(f"not a Git checkout: {candidate}")
    return Path(result.stdout.strip()).resolve()


def get_updater(repo_root: str | Path | None = None) -> FrameworkUpdater:
    return FrameworkUpdater(resolve_repo_root(repo_root))
