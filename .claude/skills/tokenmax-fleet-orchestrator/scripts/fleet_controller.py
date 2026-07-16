#!/usr/bin/env python3
"""Resumable, fail-closed controller for sequential TokenMax site fleets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_SCHEMA_VERSION = 1
TERMINAL_SITE_STATES = {"complete", "disabled"}
ELIGIBLE_SITE_STATES = {"queued", "deferred"}


class FleetError(RuntimeError):
    """Operator-visible controller error."""


class LockBusy(FleetError):
    """Raised when another controller owns the fleet lock."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise FleetError("PyYAML is required: python -m pip install PyYAML") from exc

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FleetError(f"Config not found: {path}") from exc
    except Exception as exc:
        raise FleetError(f"Unable to parse fleet config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FleetError("Fleet config root must be a mapping")
    return payload


def config_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def validate_config(config: dict[str, Any], config_path: Path) -> None:
    if config.get("schema_version") != 1:
        raise FleetError("schema_version must be 1")

    fleet = config.get("fleet")
    if not isinstance(fleet, dict):
        raise FleetError("fleet must be a mapping")
    for key in ("id", "state_dir", "lock_file"):
        if not isinstance(fleet.get(key), str) or not fleet[key].strip():
            raise FleetError(f"fleet.{key} must be a non-empty string")

    stages = config.get("stages")
    if not isinstance(stages, list) or not stages:
        raise FleetError("stages must be a non-empty list")
    stage_names: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise FleetError(f"stages[{index}] must be a mapping")
        name = stage.get("name")
        if not isinstance(name, str) or not name:
            raise FleetError(f"stages[{index}].name must be a non-empty string")
        if name in stage_names:
            raise FleetError(f"Duplicate stage name: {name}")
        stage_names.add(name)
        command = stage.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise FleetError(f"Stage {name} command must be a non-empty string array")
        failure_policy = stage.get("failure_policy", "block_site")
        if failure_policy not in {"block_site", "freeze_fleet", "defer_site"}:
            raise FleetError(f"Stage {name} has invalid failure_policy: {failure_policy}")
        retries = stage.get("retries", 0)
        if not isinstance(retries, int) or retries < 0:
            raise FleetError(f"Stage {name} retries must be a non-negative integer")

    sites = config.get("sites")
    if not isinstance(sites, list) or not sites:
        raise FleetError("sites must be a non-empty list")
    site_ids: set[str] = set()
    for index, site in enumerate(sites):
        if not isinstance(site, dict):
            raise FleetError(f"sites[{index}] must be a mapping")
        site_id = site.get("id")
        if not isinstance(site_id, str) or not site_id:
            raise FleetError(f"sites[{index}].id must be a non-empty string")
        if site_id in site_ids:
            raise FleetError(f"Duplicate site id: {site_id}")
        site_ids.add(site_id)
        priority = site.get("priority", 1000)
        if not isinstance(priority, int):
            raise FleetError(f"Site {site_id} priority must be an integer")
        initial_status = site.get("initial_status", "queued" if site.get("enabled", True) else "disabled")
        if initial_status not in {"queued", "complete", "disabled"}:
            raise FleetError(f"Site {site_id} has invalid initial_status: {initial_status}")
        skip_stages = site.get("skip_stages", [])
        if not isinstance(skip_stages, list) or any(name not in stage_names for name in skip_stages):
            raise FleetError(f"Site {site_id} skip_stages contains an unknown stage")

    base = config_path.parent
    state_dir = resolve_path(fleet["state_dir"], base)
    lock_file = resolve_path(fleet["lock_file"], base)
    if state_dir == lock_file:
        raise FleetError("fleet.state_dir and fleet.lock_file must differ")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class FleetLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None
        self.directory_lock: Path | None = None

    def __enter__(self) -> "FleetLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            self.directory_lock = Path(f"{self.path}.hold")
            try:
                self.directory_lock.mkdir()
            except FileExistsError as exc:
                raise LockBusy(f"Fleet lock is already held: {self.path}") from exc
            (self.directory_lock / "owner").write_text(
                f"pid={os.getpid()} acquired={utc_now()}\n", encoding="utf-8"
            )
            return self

        self.path.touch(exist_ok=True)
        self.handle = self.path.open("r+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"\n")
            self.handle.flush()
        self.handle.seek(0)
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self.handle.close()
            self.handle = None
            raise LockBusy(f"Fleet lock is already held: {self.path}") from exc

        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} acquired={utc_now()}\n".encode("utf-8"))
        self.handle.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.directory_lock is not None:
            (self.directory_lock / "owner").unlink(missing_ok=True)
            self.directory_lock.rmdir()
            self.directory_lock = None
            return
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class FleetController:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.config = load_yaml(self.config_path)
        validate_config(self.config, self.config_path)
        fleet = self.config["fleet"]
        self.state_dir = resolve_path(fleet["state_dir"], self.config_path.parent)
        self.lock_path = resolve_path(fleet["lock_file"], self.config_path.parent)
        self.state_path = self.state_dir / "fleet-state.json"
        self.logs_dir = self.state_dir / "logs"
        self.results_dir = self.state_dir / "results"
        self.indexing_dir = self.state_dir / "indexing"

    def fresh_state(self) -> dict[str, Any]:
        now = utc_now()
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "fleet_id": self.config["fleet"]["id"],
            "config_path": str(self.config_path),
            "config_sha256": config_digest(self.config_path),
            "created_at": now,
            "updated_at": now,
            "paused": False,
            "frozen": False,
            "freeze_reason": None,
            "sites": {},
        }

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            state = self.fresh_state()
        else:
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise FleetError(f"Invalid state ledger {self.state_path}: {exc}") from exc
            if state.get("schema_version") != STATE_SCHEMA_VERSION:
                raise FleetError("Unsupported fleet state schema")
            if state.get("fleet_id") != self.config["fleet"]["id"]:
                raise FleetError("Fleet state belongs to a different fleet id")

        configured_ids: set[str] = set()
        for site in self.config["sites"]:
            site_id = site["id"]
            configured_ids.add(site_id)
            existing = state["sites"].get(site_id)
            if existing is None:
                initial_status = site.get(
                    "initial_status", "queued" if site.get("enabled", True) else "disabled"
                )
                state["sites"][site_id] = {
                    "id": site_id,
                    "priority": site.get("priority", 1000),
                    "status": initial_status,
                    "current_stage": len(self.config["stages"]) if initial_status == "complete" else 0,
                    "run_id": None,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "completed_at": utc_now() if initial_status == "complete" else None,
                    "blocked_reason": None,
                    "stages": {},
                    "metadata": site.get("metadata", {}),
                }
            else:
                existing["priority"] = site.get("priority", 1000)
                existing["metadata"] = site.get("metadata", {})
                if not site.get("enabled", True) and existing["status"] != "complete":
                    existing["status"] = "disabled"
                elif site.get("enabled", True) and existing["status"] == "disabled":
                    existing["status"] = "queued"
        for site_id, site_state in state["sites"].items():
            if site_id not in configured_ids and site_state["status"] not in TERMINAL_SITE_STATES:
                site_state["status"] = "disabled"
        state["config_path"] = str(self.config_path)
        state["config_sha256"] = config_digest(self.config_path)
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_write_json(self.state_path, state)

    def initialize(self) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.indexing_dir.mkdir(parents=True, exist_ok=True)
        state = self.load_state()
        self.save_state(state)
        return state

    def site_config(self, site_id: str) -> dict[str, Any]:
        for site in self.config["sites"]:
            if site["id"] == site_id:
                return site
        raise FleetError(f"Unknown site: {site_id}")

    def context(self, site: dict[str, Any], site_state: dict[str, Any], stage: dict[str, Any]) -> dict[str, str]:
        run_id = site_state["run_id"] or "unassigned"
        values: dict[str, str] = {
            "config": str(self.config_path),
            "config_dir": str(self.config_path.parent),
            "state_dir": str(self.state_dir),
            "site_id": site["id"],
            "stage": stage["name"],
            "run_id": run_id,
        }
        for source in (self.config.get("variables", {}), site.get("metadata", {}), site.get("variables", {})):
            if isinstance(source, dict):
                for key, value in source.items():
                    if isinstance(value, (str, int, float, bool)):
                        values[str(key)] = str(value)
        return values

    @staticmethod
    def render(value: str, context: dict[str, str]) -> str:
        try:
            return value.format_map(context)
        except KeyError as exc:
            raise FleetError(f"Unknown command placeholder: {exc.args[0]}") from exc

    def stage_command(
        self, site: dict[str, Any], site_state: dict[str, Any], stage: dict[str, Any]
    ) -> tuple[list[str], Path, dict[str, str]]:
        context = self.context(site, site_state, stage)
        command = [self.render(part, context) for part in stage["command"]]
        cwd_value = stage.get("cwd", self.config_path.parent.as_posix())
        cwd = Path(self.render(str(cwd_value), context)).expanduser()
        if not cwd.is_absolute():
            cwd = (self.config_path.parent / cwd).resolve()
        env = os.environ.copy()
        for source in (self.config.get("env", {}), site.get("env", {}), stage.get("env", {})):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                env[str(key)] = self.render(str(value), context)
        return command, cwd, env

    def result_path(self, site_id: str, run_id: str, stage_name: str) -> Path:
        return self.results_dir / site_id / run_id / f"{stage_name}.json"

    def log_path(self, site_id: str, run_id: str, stage_name: str, attempt: int) -> Path:
        return self.logs_dir / site_id / run_id / f"{stage_name}.attempt-{attempt}.log"

    def execute_command(
        self,
        site: dict[str, Any],
        site_state: dict[str, Any],
        stage: dict[str, Any],
        attempt: int,
    ) -> tuple[int, Path, Path, dict[str, Any]]:
        command, cwd, env = self.stage_command(site, site_state, stage)
        if not cwd.exists():
            raise FleetError(f"Stage cwd does not exist: {cwd}")
        run_id = site_state["run_id"]
        result_path = self.result_path(site["id"], run_id, stage["name"])
        log_path = self.log_path(site["id"], run_id, stage["name"], attempt)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.unlink(missing_ok=True)
        env.update(
            {
                "TOKENMAX_FLEET_ID": self.config["fleet"]["id"],
                "TOKENMAX_SITE_ID": site["id"],
                "TOKENMAX_STAGE": stage["name"],
                "TOKENMAX_STAGE_RESULT": str(result_path),
                "TOKENMAX_RUN_ID": run_id,
                "TOKENMAX_STATE_DIR": str(self.state_dir),
                "TOKENMAX_CONFIG": str(self.config_path),
            }
        )
        timeout = int(stage.get("timeout_seconds", 43200))
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            log.write(f"started_at={utc_now()}\n")
            log.write(f"cwd={cwd}\n")
            log.write("command=" + json.dumps(command) + "\n")
            log.flush()
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=timeout,
                )
                return_code = completed.returncode
            except subprocess.TimeoutExpired:
                log.write(f"\nTIMEOUT after {timeout} seconds\n")
                return_code = 124
            log.write(f"\nfinished_at={utc_now()}\n")
            log.write(f"duration_seconds={time.monotonic() - started:.3f}\n")
            log.write(f"return_code={return_code}\n")

        result: dict[str, Any] = {}
        if result_path.exists():
            try:
                loaded = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise FleetError(f"Invalid stage result {result_path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise FleetError(f"Stage result must be a JSON object: {result_path}")
            result = loaded
        return return_code, log_path, result_path, result

    def execute_site(self, state: dict[str, Any], site_id: str) -> bool:
        site = self.site_config(site_id)
        site_state = state["sites"][site_id]
        if site_state["status"] in TERMINAL_SITE_STATES:
            return True
        if not site_state.get("run_id"):
            site_state["run_id"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        site_state["status"] = "running"
        site_state["blocked_reason"] = None
        site_state["updated_at"] = utc_now()
        self.save_state(state)

        stages = self.config["stages"]
        skip_stages = set(site.get("skip_stages", []))
        for index in range(int(site_state.get("current_stage", 0)), len(stages)):
            stage = stages[index]
            stage_name = stage["name"]
            stage_state = site_state["stages"].setdefault(stage_name, {})
            if stage_name in skip_stages:
                stage_state.update({"status": "skipped", "completed_at": utc_now()})
                site_state["current_stage"] = index + 1
                self.save_state(state)
                continue

            attempts_allowed = int(stage.get("retries", 0)) + 1
            success = False
            final_result: dict[str, Any] = {}
            for attempt in range(1, attempts_allowed + 1):
                stage_state.update(
                    {
                        "status": "running",
                        "attempt": attempt,
                        "started_at": utc_now(),
                        "completed_at": None,
                    }
                )
                self.save_state(state)
                try:
                    return_code, log_path, result_path, result = self.execute_command(
                        site, site_state, stage, attempt
                    )
                except FleetError as exc:
                    return_code = 125
                    log_path = self.log_path(site_id, site_state["run_id"], stage_name, attempt)
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(f"controller_error={exc}\n", encoding="utf-8")
                    result_path = self.result_path(site_id, site_state["run_id"], stage_name)
                    result = {"outcome": "failed", "summary": str(exc)}
                outcome = result.get("outcome", "passed" if return_code == 0 else "failed")
                stage_state.update(
                    {
                        "return_code": return_code,
                        "outcome": outcome,
                        "log": str(log_path),
                        "result": str(result_path) if result_path.exists() else None,
                        "summary": result.get("summary"),
                        "metrics": result.get("metrics", {}),
                        "artifacts": result.get("artifacts", {}),
                        "completed_at": utc_now(),
                    }
                )
                self.save_state(state)
                final_result = result
                if return_code == 0 and outcome in {"passed", "complete_site"}:
                    success = True
                    break
                if return_code == 0 and outcome == "deferred":
                    stage_state["status"] = "deferred"
                    site_state["status"] = "deferred"
                    site_state["updated_at"] = utc_now()
                    self.save_state(state)
                    return False
                if attempt < attempts_allowed:
                    delay = int(stage.get("retry_delay_seconds", 30))
                    stage_state["status"] = "retry_wait"
                    self.save_state(state)
                    time.sleep(delay)

            if success:
                stage_state["status"] = "passed"
                site_state["current_stage"] = index + 1
                site_state["updated_at"] = utc_now()
                if final_result.get("outcome") == "complete_site" or final_result.get("site_status") == "complete":
                    site_state["status"] = "complete"
                    site_state["completed_at"] = utc_now()
                    site_state["current_stage"] = len(stages)
                    self.save_state(state)
                    return True
                self.save_state(state)
                continue

            stage_state["status"] = "failed"
            summary = final_result.get("summary") or f"Stage {stage_name} failed"
            site_state["blocked_reason"] = summary
            failure_policy = stage.get("failure_policy", "block_site")
            if failure_policy == "defer_site":
                site_state["status"] = "deferred"
            else:
                site_state["status"] = "blocked"
            if failure_policy == "freeze_fleet":
                state["frozen"] = True
                state["freeze_reason"] = f"{site_id}:{stage_name}: {summary}"
            site_state["updated_at"] = utc_now()
            self.save_state(state)
            return False

        site_state["status"] = "complete"
        site_state["completed_at"] = utc_now()
        site_state["updated_at"] = utc_now()
        self.save_state(state)
        return True

    def eligible_sites(self, state: dict[str, Any]) -> list[str]:
        sites = [
            site
            for site in state["sites"].values()
            if site.get("status") in ELIGIBLE_SITE_STATES
        ]
        sites.sort(key=lambda item: (int(item.get("priority", 1000)), item["id"]))
        return [site["id"] for site in sites]

    def recover_interrupted_sites(self, state: dict[str, Any]) -> list[str]:
        recovered: list[str] = []
        timestamp = utc_now()
        ordered = sorted(state["sites"].values(), key=lambda item: (item["priority"], item["id"]))
        for site_state in ordered:
            if site_state.get("status") != "running":
                continue
            for stage_state in site_state.get("stages", {}).values():
                if stage_state.get("status") == "running":
                    stage_state.update(
                        {
                            "status": "interrupted",
                            "outcome": "interrupted",
                            "completed_at": timestamp,
                            "summary": "Runner stopped before this stage completed",
                        }
                    )
            site_state["status"] = "queued"
            site_state["blocked_reason"] = None
            site_state["updated_at"] = timestamp
            recovered.append(site_state["id"])
        return recovered

    def dry_plan(self, state: dict[str, Any], max_sites: int) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        for site_id in self.eligible_sites(state)[:max_sites]:
            site = self.site_config(site_id)
            site_state = state["sites"][site_id]
            if not site_state.get("run_id"):
                site_state = dict(site_state)
                site_state["run_id"] = "DRY_RUN"
            rendered_stages = []
            for stage in self.config["stages"][int(site_state.get("current_stage", 0)) :]:
                if stage["name"] in set(site.get("skip_stages", [])):
                    rendered_stages.append({"name": stage["name"], "skipped": True})
                    continue
                command, cwd, _ = self.stage_command(site, site_state, stage)
                rendered_stages.append({"name": stage["name"], "cwd": str(cwd), "command": command})
            plan.append({"site": site_id, "priority": site_state["priority"], "stages": rendered_stages})
        return plan

    def run_next(self, max_sites: int, dry_run: bool = False) -> int:
        if max_sites < 1:
            raise FleetError("max-sites must be at least 1")
        with FleetLock(self.lock_path):
            state = self.initialize()
            if state.get("paused"):
                raise FleetError("Fleet is paused")
            if state.get("frozen"):
                raise FleetError(f"Fleet is frozen: {state.get('freeze_reason')}")
            recovered = self.recover_interrupted_sites(state)
            if recovered:
                self.save_state(state)
                print("RECOVERED_INTERRUPTED_SITES=" + ",".join(recovered), flush=True)
            eligible = self.eligible_sites(state)
            if dry_run:
                print(json.dumps({"fleet_id": state["fleet_id"], "plan": self.dry_plan(state, max_sites)}, indent=2))
                return 0
            if not eligible:
                print("NO_ELIGIBLE_SITES")
                return 0
            completed = 0
            for site_id in eligible[:max_sites]:
                print(f"RUN_SITE={site_id}", flush=True)
                if self.execute_site(state, site_id):
                    completed += 1
                if state.get("frozen"):
                    break
            print(f"RUN_COMPLETE completed={completed} attempted={min(max_sites, len(eligible))}")
            return 0 if not state.get("frozen") else 2

    def resume(self, site_id: str) -> int:
        with FleetLock(self.lock_path):
            state = self.initialize()
            if state.get("frozen"):
                raise FleetError(f"Fleet is frozen: {state.get('freeze_reason')}")
            site_state = state["sites"].get(site_id)
            if site_state is None:
                raise FleetError(f"Unknown site: {site_id}")
            if site_state["status"] == "complete":
                print(f"SITE_ALREADY_COMPLETE={site_id}")
                return 0
            if site_state["status"] == "disabled":
                raise FleetError(f"Site is disabled: {site_id}")
            site_state["status"] = "queued"
            self.save_state(state)
            return 0 if self.execute_site(state, site_id) else 1

    def retry(self, site_id: str, stage_name: str | None, unfreeze: bool) -> None:
        with FleetLock(self.lock_path):
            state = self.initialize()
            site_state = state["sites"].get(site_id)
            if site_state is None:
                raise FleetError(f"Unknown site: {site_id}")
            if stage_name is not None:
                stage_names = [stage["name"] for stage in self.config["stages"]]
                if stage_name not in stage_names:
                    raise FleetError(f"Unknown stage: {stage_name}")
                index = stage_names.index(stage_name)
                site_state["current_stage"] = index
                for name in stage_names[index:]:
                    site_state["stages"].pop(name, None)
            site_state["status"] = "queued"
            site_state["blocked_reason"] = None
            site_state["completed_at"] = None
            if unfreeze:
                state["frozen"] = False
                state["freeze_reason"] = None
            self.save_state(state)

    def set_flag(self, flag: str, value: bool, reason: str | None = None) -> None:
        with FleetLock(self.lock_path):
            state = self.initialize()
            state[flag] = value
            if flag == "frozen":
                state["freeze_reason"] = reason if value else None
            self.save_state(state)

    def status(self, as_json: bool) -> None:
        state = self.initialize()
        if as_json:
            print(json.dumps(state, indent=2, sort_keys=True))
            return
        print(
            f"fleet={state['fleet_id']} paused={state['paused']} frozen={state['frozen']} "
            f"updated={state['updated_at']}"
        )
        print(f"{'SITE':30} {'STATUS':12} {'STAGE':24} {'PRIORITY':8}")
        stage_names = [stage["name"] for stage in self.config["stages"]]
        ordered = sorted(state["sites"].values(), key=lambda item: (item["priority"], item["id"]))
        for site in ordered:
            index = int(site.get("current_stage", 0))
            stage = "done" if index >= len(stage_names) else stage_names[index]
            print(f"{site['id']:30} {site['status']:12} {stage:24} {site['priority']:8}")

    def indexing_queue(self, as_json: bool) -> None:
        self.initialize()
        entries: list[dict[str, Any]] = []
        for path in sorted(self.indexing_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                entries.append({"path": str(path), "error": str(exc)})
                continue
            entries.append({"path": str(path), "payload": payload})
        if as_json:
            print(json.dumps({"entries": entries}, indent=2))
            return
        if not entries:
            print("INDEXING_QUEUE_EMPTY")
            return
        for entry in entries:
            payload = entry.get("payload", {})
            print(
                f"{payload.get('site_id', '?'):30} status={payload.get('status', '?'):12} "
                f"urls={len(payload.get('urls', []))} path={entry['path']}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config")
    subparsers.add_parser("init")

    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true")

    run_next = subparsers.add_parser("run-next")
    run_next.add_argument("--max-sites", type=int, default=1)
    run_next.add_argument("--dry-run", action="store_true")

    resume = subparsers.add_parser("resume")
    resume.add_argument("--site", required=True)

    retry = subparsers.add_parser("retry")
    retry.add_argument("--site", required=True)
    retry.add_argument("--stage")
    retry.add_argument("--unfreeze", action="store_true")

    subparsers.add_parser("pause")
    subparsers.add_parser("unpause")
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--reason", required=True)
    subparsers.add_parser("unfreeze")

    indexing = subparsers.add_parser("index-queue")
    indexing.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        controller = FleetController(args.config)
        if args.command == "validate-config":
            print(f"CONFIG_OK fleet={controller.config['fleet']['id']} sites={len(controller.config['sites'])} stages={len(controller.config['stages'])}")
            return 0
        if args.command == "init":
            state = controller.initialize()
            print(f"STATE_OK path={controller.state_path} sites={len(state['sites'])}")
            return 0
        if args.command == "status":
            controller.status(args.json)
            return 0
        if args.command == "run-next":
            return controller.run_next(args.max_sites, args.dry_run)
        if args.command == "resume":
            return controller.resume(args.site)
        if args.command == "retry":
            controller.retry(args.site, args.stage, args.unfreeze)
            print(f"SITE_QUEUED={args.site}")
            return 0
        if args.command == "pause":
            controller.set_flag("paused", True)
            print("FLEET_PAUSED")
            return 0
        if args.command == "unpause":
            controller.set_flag("paused", False)
            print("FLEET_UNPAUSED")
            return 0
        if args.command == "freeze":
            controller.set_flag("frozen", True, args.reason)
            print("FLEET_FROZEN")
            return 0
        if args.command == "unfreeze":
            controller.set_flag("frozen", False)
            print("FLEET_UNFROZEN")
            return 0
        if args.command == "index-queue":
            controller.indexing_queue(args.json)
            return 0
        raise FleetError(f"Unhandled command: {args.command}")
    except LockBusy as exc:
        print(f"LOCK_BUSY: {exc}", file=sys.stderr)
        return 75
    except FleetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
