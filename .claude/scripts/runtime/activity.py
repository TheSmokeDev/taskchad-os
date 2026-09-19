"""Cross-process foreground activity and renewable background-worker leases.

The installation-wide ledger is deliberately independent of a persona's active
environment. A named-profile subprocess must see the same interactive traffic as
the default profile. Leases expire after crashes; no process mutates another
profile's environment to query them.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import uuid4

_logger = logging.getLogger(__name__)


def activity_db_path() -> Path:
    override = os.getenv("SECOND_BRAIN_RUNTIME_ACTIVITY_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    from personas import core

    return core.get_default_paths()["data"] / "runtime-activity.sqlite3"


@contextmanager
def _connection(path: Path | None = None):
    target = Path(path) if path is not None else activity_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=2.0)
    try:
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_activity ("
            "lease_id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, "
            "expires_at REAL NOT NULL)"
        )
        connection.commit()
        yield connection
        connection.commit()
    finally:
        connection.close()


def acquire_lease(
    kind: str,
    *,
    owner: str,
    ttl_seconds: float | None = None,
    exclusive: bool = False,
    path: Path | None = None,
    now: float | None = None,
) -> str | None:
    ttl = 90.0 if ttl_seconds is None else float(ttl_seconds)
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("Lease TTL must be positive")
    instant = time.time() if now is None else now
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM runtime_activity WHERE expires_at<=?", (instant,))
        if (
            exclusive
            and db.execute(
                "SELECT 1 FROM runtime_activity WHERE kind=? LIMIT 1", (kind,)
            ).fetchone()
        ):
            return None
        lease = uuid4().hex
        db.execute(
            "INSERT INTO runtime_activity VALUES(?,?,?,?)", (lease, kind, owner, instant + ttl)
        )
        return lease


def renew_lease(
    lease_id: str,
    *,
    ttl_seconds: float | None = None,
    path: Path | None = None,
    now: float | None = None,
) -> bool:
    ttl = 90.0 if ttl_seconds is None else float(ttl_seconds)
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("Lease TTL must be positive and finite")
    instant = time.time() if now is None else now
    with _connection(path) as db:
        return (
            db.execute(
                "UPDATE runtime_activity SET expires_at=? WHERE lease_id=? AND expires_at>?",
                (instant + ttl, lease_id, instant),
            ).rowcount
            == 1
        )


def release_lease(lease_id: str, *, path: Path | None = None) -> None:
    with _connection(path) as db:
        db.execute("DELETE FROM runtime_activity WHERE lease_id=?", (lease_id,))


def foreground_active(*, path: Path | None = None, now: float | None = None) -> bool:
    instant = time.time() if now is None else now
    with _connection(path) as db:
        return (
            db.execute(
                "SELECT 1 FROM runtime_activity WHERE kind='foreground' AND expires_at>? LIMIT 1",
                (instant,),
            ).fetchone()
            is not None
        )


async def _renew_foreground_lease(lease: str) -> bool:
    return await asyncio.to_thread(renew_lease, lease)


async def _wait_foreground_refresh() -> None:
    await asyncio.sleep(25)


@asynccontextmanager
async def foreground_request(request):
    """Track a foreground runtime request without blocking work on ledger failure."""
    workload = getattr(request, "workload", "auto")
    foreground = workload == "foreground" or (
        workload == "auto" and getattr(request, "conversational", False)
    )
    if not foreground:
        yield
        return
    lease = None
    owner = f"{os.getpid()}:{request.task_name}"
    try:
        lease = await asyncio.to_thread(acquire_lease, "foreground", owner=owner)
    except Exception:
        _logger.warning("Foreground activity coverage failed; request continues", exc_info=True)

    async def refresh():
        nonlocal lease
        while True:
            await _wait_foreground_refresh()
            try:
                if lease and await _renew_foreground_lease(lease):
                    continue
                _logger.warning("Foreground activity lease unavailable; reacquiring")
                lease = await asyncio.to_thread(acquire_lease, "foreground", owner=owner)
            except Exception:
                # A transient lock must not permanently stop protecting a live turn.
                _logger.warning("Foreground activity renewal failed; retrying", exc_info=True)

    renewer = asyncio.create_task(refresh())
    try:
        yield
    finally:
        if renewer is not None:
            renewer.cancel()
            try:
                await renewer
            except asyncio.CancelledError:
                pass
            except Exception:
                _logger.warning("Foreground activity renewal failed", exc_info=True)
        if lease:
            try:
                await asyncio.to_thread(release_lease, lease)
            except Exception:
                _logger.warning("Foreground activity release failed; lease expires", exc_info=True)
