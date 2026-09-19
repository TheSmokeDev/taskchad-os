"""One supervised, installation-elected wake source for the existing learner.

Minute ticks discover durable work; model execution is a separately supervised
profile child, never a process-global persona environment switch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from runtime import activity

from .errors import LearningUnavailableError

log = logging.getLogger(__name__)
INTERVAL_SECONDS = 60
LEASE_SECONDS = 90


def dispatcher_status(*, path: Path | None = None, now: float | None = None) -> dict:
    target = path or activity.activity_db_path()
    absent = {"state": "not_started", "interval_seconds": INTERVAL_SECONDS}
    if not target.exists():
        return absent
    try:
        with sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT payload FROM learning_dispatcher_status WHERE id=1").fetchone()
        if row is None:
            return absent
        state = json.loads(row[0])
        instant = time.time() if now is None else now
        if (
            state.get("state") not in {"stopped", "disabled"}
            and instant - state.get("updated_at", 0) > 150
        ):
            state["state"] = "stale"
        return state
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return absent
        return {"state": "unavailable", "error_type": type(exc).__name__}


def _write_status(state: dict, path: Path, lease: str) -> None:
    # Uses the existing installation activity database, never another ledger.
    with activity._connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM runtime_activity WHERE lease_id=? AND expires_at>?", (lease, time.time())
        ).fetchone():
            return
        db.execute(
            "CREATE TABLE IF NOT EXISTS learning_dispatcher_status "
            "(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO learning_dispatcher_status VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (json.dumps(state, sort_keys=True),),
        )


def _services() -> list:
    from personas.lifecycle import list_profiles

    from .service import get_learning_service

    names = sorted({"default", *(profile.name for profile in list_profiles())})
    services, errors = [], []
    for name in names:
        try:
            services.append(get_learning_service(name))
        except Exception as exc:
            errors.append({"persona_id": name, "error_type": type(exc).__name__})
    return services, errors


def _discover(services: list) -> list:
    from .queue import LearningQueue
    from .worker import discover_work

    pending, errors = [], []
    instant = time.time()
    for service in services:
        try:
            if not service.enabled():
                continue
            discover_work(service)
            if any(
                job["available_at"] <= instant
                and (job["status"] != "running" or (job.get("expires_at") or 0) <= instant)
                for job in LearningQueue(service).list()
            ):
                pending.append(service)
        except Exception as exc:
            errors.append(
                {"persona_id": service.target.persona_id, "error_type": type(exc).__name__}
            )
    return pending, errors


def _child_env(service) -> dict:
    from personas.capabilities import build_capability_scoped_env
    from runtime.subprocess_env import get_scrubbed_sdk_env

    name = service.target.persona_id
    if name == "default":
        env = get_scrubbed_sdk_env(
            parent_env=dict(os.environ), profile_root=service.target.memory_dir.parent
        )
        env.pop("HOMIE_HOME", None)
        env.pop("HOMIE_PROFILE", None)
    else:
        env = build_capability_scoped_env(name, profile_root=service.target.memory_dir.parent)
    from personas import core

    env["HOMIE_DEFAULT_PROFILE_ROOT"] = str(core.get_default_paths()["workspace"])
    env["SECOND_BRAIN_RUNTIME_ACTIVITY_DB"] = str(activity.activity_db_path())
    for key in (
        "HOMIE_DEFAULT_PROFILE_ROOT",
        "PERSONA_LEARNING_ENABLED",
        "PERSONA_LEARNING_MODEL_BUDGET_USD",
        "PERSONA_LEARNING_STAGE_TIMEOUT_SECONDS",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    if "ORCHESTRATION_DB_PATH" in os.environ:
        env.setdefault("ORCHESTRATION_DB_PATH", os.environ["ORCHESTRATION_DB_PATH"])
    return env


async def _stop_child(child) -> None:
    if child.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(child.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        await killer.wait()
    else:
        child.terminate()
    try:
        await asyncio.wait_for(child.wait(), timeout=10)
    except TimeoutError:
        child.kill()
        await child.wait()


async def _run_child(service) -> dict:
    script = Path(__file__).resolve().parents[2] / "persona_learning_worker.py"
    env = await asyncio.to_thread(_child_env, service)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "-p",
        service.target.persona_id,
        "--max-stages",
        "1",
        cwd=str(script.parent),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        stdout, _ = await asyncio.wait_for(child.communicate(), timeout=900)
        if child.returncode:
            raise LearningUnavailableError("learning child exited unsuccessfully")
        # Ignore logs, parse only the final quiet worker receipt.
        lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
        result = json.loads(lines[-1]) if lines else {}
        if result.get("status") in {"failed", "retry", "lease_lost"}:
            raise LearningUnavailableError("learning child did not finish its stage")
        return result
    finally:
        await _stop_child(child)


class CognitiveDispatcher:
    def __init__(self, *, path=None, service_loader=None, child_runner=None, report_runner=None):
        self.path = path or activity.activity_db_path()
        self.service_loader = service_loader or _services
        self.child_runner = child_runner or _run_child
        self.report_runner = report_runner
        self.lease = None
        self.worker = None
        self.last_persona = ""
        self.owner = f"{os.getpid()}:cognition-dispatcher"
        self.state = {
            "state": "starting",
            "interval_seconds": INTERVAL_SECONDS,
            "tick_count": 0,
            "consecutive_failures": 0,
            "adapter_coverage": "framework_surface; native_adapter_reported_per_request",
        }

    async def save(self, **changes):
        self.state.update(changes, updated_at=time.time())
        if self.lease:
            await asyncio.to_thread(_write_status, self.state, self.path, self.lease)

    async def elect(self):
        if self.lease:
            if await asyncio.to_thread(
                activity.renew_lease, self.lease, path=self.path, ttl_seconds=LEASE_SECONDS
            ):
                return True
            # Election can observe expiry before the periodic renewal task.
            # Stop and join the old child before dropping ownership or acquiring
            # another lease; standby must never retain a live previous child.
            self.lease = None
            if self.worker is not None:
                previous_worker, self.worker = self.worker, None
                previous_worker.cancel()
                await asyncio.gather(previous_worker, return_exceptions=True)
        self.lease = await asyncio.to_thread(
            activity.acquire_lease,
            "learning_dispatcher",
            owner=self.owner,
            exclusive=True,
            path=self.path,
            ttl_seconds=LEASE_SECONDS,
        )
        if self.lease:
            previous = await asyncio.to_thread(dispatcher_status, path=self.path)
            self.last_persona = previous.get("selected_persona", self.last_persona)
        return self.lease is not None

    async def tick(self):
        if not await self.elect():
            return {"state": "standby"}
        if self.worker and self.worker.done():
            completed, self.worker = self.worker, None
            if completed.cancelled():
                self.state["error_type"] = "LearningDeferredError"
            else:
                completed.result()  # Supervision catches exceptions and retries on later wake.
        await self.save(
            state="discovering", last_tick_at=time.time(), tick_count=self.state["tick_count"] + 1
        )
        loaded = await asyncio.to_thread(self.service_loader)
        services, load_errors = loaded if isinstance(loaded, tuple) else (loaded, [])
        pending, discovery_errors = await asyncio.to_thread(_discover, services)
        errors = load_errors + discovery_errors
        self.state["profile_errors"] = errors
        failed_ids = {error["persona_id"] for error in errors}
        services = [s for s in services if s.target.persona_id not in failed_ids]
        if await asyncio.to_thread(activity.foreground_active, path=self.path):
            await self.save(state="foreground", last_success_at=time.time())
            return self.state
        if self.worker is None and pending:
            ordered = sorted(pending, key=lambda service: service.target.persona_id)
            selected = next(
                (s for s in ordered if s.target.persona_id > self.last_persona), ordered[0]
            )
            self.last_persona = selected.target.persona_id
            self.worker = asyncio.create_task(self.child_runner(selected))
            await self.save(selected_persona=self.last_persona)
        if self.report_runner is None:
            from .reporting import dispatch_learning_reports

            report_runner = dispatch_learning_reports
        else:
            report_runner = self.report_runner
        # Report errors remain visible; no report can monopolize the dispatch loop.
        await asyncio.wait_for(report_runner(services), timeout=50)
        await self.save(
            state="degraded" if errors else "working" if self.worker else "idle",
            last_success_at=time.time(),
            consecutive_failures=0,
            error_type="ProfileDiscoveryError" if errors else None,
        )
        return self.state

    async def renew(self):
        while True:
            await asyncio.sleep(25)
            if self.lease:
                try:
                    if not await asyncio.to_thread(
                        activity.renew_lease, self.lease, path=self.path, ttl_seconds=LEASE_SECONDS
                    ):
                        self.lease = None
                        if self.worker:
                            self.worker.cancel()
                        await self.save(state="lease_lost")
                except Exception as exc:
                    # Retry renewal; the child also holds the exclusive learning-worker lease.
                    log.warning("Cognitive dispatcher lease renewal failed: %s", type(exc).__name__)

    async def run(self):
        renewal = asyncio.create_task(self.renew())
        try:
            while True:
                started = time.monotonic()
                try:
                    await self.tick()
                    delay = max(0.1, INTERVAL_SECONDS - (time.monotonic() - started))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures = self.state["consecutive_failures"] + 1
                    self.state.update(
                        state="degraded",
                        error_type=type(exc).__name__,
                        consecutive_failures=failures,
                    )
                    try:
                        await self.save()
                    except Exception:
                        pass
                    log.warning("Cognitive dispatcher wake failed: %s", type(exc).__name__)
                    delay = min(300, 10 * 2 ** min(failures - 1, 5))
                await asyncio.sleep(delay)
        finally:
            renewal.cancel()
            if self.worker:
                self.worker.cancel()
            await asyncio.gather(
                renewal, *([self.worker] if self.worker else []), return_exceptions=True
            )
            if self.lease:
                try:
                    await self.save(state="stopped")
                    await asyncio.to_thread(activity.release_lease, self.lease, path=self.path)
                except Exception:
                    log.warning("Cognitive dispatcher shutdown lease will expire")


async def run_dispatcher() -> None:
    """Bot lifecycle entry point; periodic ticks themselves survive failures."""
    await CognitiveDispatcher().run()
