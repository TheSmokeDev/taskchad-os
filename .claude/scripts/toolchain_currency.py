"""Compatibility-aware currency checks for Homie's agent toolchain.

The framework updater owns Homie releases.  This module owns the external
CLIs and SDKs Homie relies on, with a deliberately narrower mutation policy:
global CLIs may be advanced automatically when their compatibility policy
allows it; project SDK locks are reported and proposed, never rewritten by a
scheduled job.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.openai_codex_app_server import SUPPORTED_CODEX_VERSION
from shared import file_lock

_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?!\d)")
SUPPORTED_CLAUDE_CODE_VERSION = "2.1.220"
_PINNED_CLI_VERSIONS = {
    "codex-cli": SUPPORTED_CODEX_VERSION,
    "claude-code-cli": SUPPORTED_CLAUDE_CODE_VERSION,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _version_tuple(value: str | None) -> tuple[int, int, int]:
    match = _VERSION_RE.search(value or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.group(1).split(".")[:3])  # type: ignore[return-value]


def _extract_version(value: str) -> str | None:
    match = _VERSION_RE.search(value)
    return match.group(1) if match else None


def _compatibility_series(value: str | None) -> tuple[int, int]:
    major, minor, _patch = _version_tuple(value)
    return (major, minor if major == 0 else 0)


@dataclass(frozen=True, slots=True)
class ToolchainSpec:
    id: str
    display_name: str
    kind: str
    package: str
    usage: str
    command: tuple[str, ...] | None = None
    distribution: str | None = None
    auto_policy: str = "proposal"


@dataclass(slots=True)
class ToolchainItem:
    id: str
    display_name: str
    kind: str
    package: str
    usage: str
    current_version: str | None
    latest_version: str | None
    desired_version: str | None
    state: str
    update_available: bool
    auto_apply: bool
    blocker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolchainReport:
    success: bool
    checked_at: str
    items: list[ToolchainItem]
    current_count: int
    actionable_count: int
    migration_count: int

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        return payload


@dataclass(slots=True)
class ToolchainReceipt:
    receipt_id: str
    started_at: str
    finished_at: str
    scheduled: bool
    success: bool
    attempted: list[dict[str, Any]] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    blocker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Runner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]
LatestLookup = Callable[[ToolchainSpec], str | None]


SPECS = (
    ToolchainSpec(
        id="codex-cli",
        display_name="Codex CLI",
        kind="npm-cli",
        package="@openai/codex",
        usage="persona caller-tool runtime",
        command=("codex", "--version"),
        auto_policy="compatibility-gate",
    ),
    ToolchainSpec(
        id="claude-code-cli",
        display_name="Claude Code CLI",
        kind="npm-cli",
        package="@anthropic-ai/claude-code",
        usage="operator and native-runtime toolchain",
        command=("claude", "--version"),
        auto_policy="compatibility-gate",
    ),
    ToolchainSpec(
        id="codex-sdk-js",
        display_name="Codex SDK (JavaScript)",
        kind="npm-sdk",
        package="@openai/codex-sdk",
        usage="not used by Homie; app-server protocol is used instead",
        auto_policy="not-in-use",
    ),
    ToolchainSpec(
        id="claude-agent-sdk-python",
        display_name="Claude Agent SDK (Python)",
        kind="python-sdk",
        package="claude-agent-sdk",
        usage="Homie runtime dependency",
        distribution="claude-agent-sdk",
        auto_policy="sdk-lock",
    ),
    ToolchainSpec(
        id="openai-sdk-python",
        display_name="OpenAI SDK (Python)",
        kind="python-sdk",
        package="openai",
        usage="Talk mode and direct OpenAI integrations",
        distribution="openai",
        auto_policy="sdk-lock",
    ),
    ToolchainSpec(
        id="mcp-python",
        display_name="MCP SDK (Python)",
        kind="python-sdk",
        package="mcp",
        usage="paired compatibility dependency for Claude Agent SDK",
        distribution="mcp",
        auto_policy="sdk-lock",
    ),
)


def _default_runner(
    argv: Sequence[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(argv[0]) or argv[0]
    return subprocess.run(
        [executable, *argv[1:]],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


class ToolchainCurrency:
    """Inspect and safely advance the external agent toolchain."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        state_dir: str | Path | None = None,
        runner: Runner | None = None,
        latest_lookup: LatestLookup | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = (
            Path(state_dir).resolve()
            if state_dir
            else self.repo_root / ".claude" / "data" / "state"
        )
        self.runner = runner or _default_runner
        self.latest_lookup = latest_lookup or self._latest_registry_version
        self.history_file = self.state_dir / "toolchain-update-history.jsonl"
        self.lock_file = self.state_dir / "toolchain-update"

    def _latest_registry_version(self, spec: ToolchainSpec) -> str | None:
        if spec.kind.startswith("npm"):
            encoded = urllib.parse.quote(spec.package, safe="")
            url = f"https://registry.npmjs.org/{encoded}"
            key_path = ("dist-tags", "latest")
        else:
            encoded = urllib.parse.quote(spec.package, safe="")
            url = f"https://pypi.org/pypi/{encoded}/json"
            key_path = ("info", "version")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "thehomie-toolchain-currency"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            value: Any = payload
            for key in key_path:
                value = value[key]
            return _extract_version(str(value))
        except (KeyError, OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def _locked_versions(self) -> dict[str, str]:
        lock = self.repo_root / ".claude" / "scripts" / "uv.lock"
        try:
            payload = tomllib.loads(lock.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        versions: dict[str, str] = {}
        for package in payload.get("package", []):
            name = str(package.get("name") or "").lower()
            version = _extract_version(str(package.get("version") or ""))
            if name and version:
                versions[name] = version
        return versions

    def _installed_cli_version(self, spec: ToolchainSpec) -> str | None:
        if not spec.command:
            return None
        try:
            result = self.runner(spec.command, self.repo_root, 15)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return _extract_version(f"{result.stdout}\n{result.stderr}")

    @staticmethod
    def _classify(
        spec: ToolchainSpec,
        current: str | None,
        latest: str | None,
    ) -> ToolchainItem:
        desired = latest
        blocker = None
        update_available = bool(
            current and latest and _version_tuple(latest) > _version_tuple(current)
        )
        auto_apply = False

        if spec.auto_policy == "not-in-use":
            return ToolchainItem(
                spec.id,
                spec.display_name,
                spec.kind,
                spec.package,
                spec.usage,
                None,
                latest,
                None,
                "not_in_use",
                False,
                False,
                "not installed or mutated by Homie",
            )

        if latest is None:
            state = "unverified"
            blocker = "registry version unavailable"
        elif spec.auto_policy == "compatibility-gate":
            supported = _PINNED_CLI_VERSIONS[spec.id]
            desired = supported
            if current != supported:
                state = "compatibility_update_required"
                update_available = True
                auto_apply = True
            elif _version_tuple(latest) > _version_tuple(supported):
                state = "compatible_current_newer_ungated"
                update_available = True
                blocker = (
                    f"{spec.display_name} {latest} must pass the runtime gate before production use"
                )
            else:
                state = "current"
                update_available = False
        elif current is None:
            state = "missing"
            update_available = False
            auto_apply = update_available
        elif not update_available:
            state = "current"
        elif spec.id in {"claude-agent-sdk-python", "mcp-python"} and (
            _compatibility_series(latest) > _compatibility_series(current)
        ):
            state = "migration_required"
            blocker = "major SDK migration requires lockfile change and runtime regression proof"
        else:
            state = "dependency_update_proposal"
            blocker = "project SDK locks change only through a tested framework PR"

        return ToolchainItem(
            spec.id,
            spec.display_name,
            spec.kind,
            spec.package,
            spec.usage,
            current,
            latest,
            desired,
            state,
            update_available,
            auto_apply,
            blocker,
        )

    def check(self, *, latest_versions: dict[str, str | None] | None = None) -> ToolchainReport:
        locked = self._locked_versions()
        items: list[ToolchainItem] = []
        for spec in SPECS:
            if spec.command:
                current = self._installed_cli_version(spec)
            elif spec.distribution:
                current = locked.get(spec.distribution.lower())
            else:
                current = None
            latest = (
                latest_versions.get(spec.id)
                if latest_versions is not None
                else self.latest_lookup(spec)
            )
            items.append(self._classify(spec, current, latest))
        return ToolchainReport(
            success=all(item.state != "unverified" for item in items if item.id != "codex-sdk-js"),
            checked_at=_now(),
            items=items,
            current_count=sum(item.state == "current" for item in items),
            actionable_count=sum(item.auto_apply for item in items),
            migration_count=sum(item.state == "migration_required" for item in items),
        )

    def _append_history(self, receipt: ToolchainReceipt) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt.to_dict(), sort_keys=True) + "\n")

    def history(self, *, limit: int = 10) -> list[dict[str, Any]]:
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def apply_safe_cli_updates(self, *, scheduled: bool = False) -> ToolchainReceipt:
        started = _now()
        receipt = ToolchainReceipt(
            receipt_id=f"toolchain-{uuid.uuid4().hex[:12]}",
            started_at=started,
            finished_at=started,
            scheduled=scheduled,
            success=False,
        )
        try:
            with file_lock(self.lock_file, timeout=0.1):
                before = self.check()
                receipt.before = before.to_dict()
                latest = {item.id: item.latest_version for item in before.items}
                for item in before.items:
                    if not item.auto_apply or not item.desired_version:
                        continue
                    argv = ["npm", "install", "-g", f"{item.package}@{item.desired_version}"]
                    try:
                        result = self.runner(argv, self.repo_root, 600)
                        detail = (result.stderr or result.stdout or "").strip()[-2000:]
                        receipt.attempted.append(
                            {
                                "id": item.id,
                                "desired_version": item.desired_version,
                                "returncode": result.returncode,
                                "detail": detail,
                            }
                        )
                    except (OSError, subprocess.SubprocessError) as exc:
                        receipt.attempted.append(
                            {
                                "id": item.id,
                                "desired_version": item.desired_version,
                                "returncode": -1,
                                "detail": str(exc),
                            }
                        )
                after = self.check(latest_versions=latest)
                receipt.after = after.to_dict()
                after_by_id = {item.id: item for item in after.items}
                receipt.success = all(
                    attempt["returncode"] == 0
                    and after_by_id[attempt["id"]].current_version
                    == attempt["desired_version"]
                    for attempt in receipt.attempted
                )
                if not receipt.attempted:
                    receipt.success = before.success
        except TimeoutError as exc:
            receipt.blocker = f"another toolchain update is active: {exc}"
        receipt.finished_at = _now()
        self._append_history(receipt)
        return receipt


__all__ = [
    "SPECS",
    "SUPPORTED_CLAUDE_CODE_VERSION",
    "ToolchainCurrency",
    "ToolchainItem",
    "ToolchainReceipt",
    "ToolchainReport",
    "ToolchainSpec",
]
