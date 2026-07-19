"""Tests for health — HealthStatus dataclass and serialization."""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

import pytest

# Add chat dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "chat"))

from health import HealthStatus
from update_check import get_current_version


def test_health_status_defaults():
    status = HealthStatus(
        status="ok",
        uptime_seconds=100.0,
        adapters={"telegram": True},
        sessions_active=5,
        cognition_available=True,
    )
    assert status.status == "ok"
    assert status.uptime_seconds == 100.0
    assert status.adapters == {"telegram": True}
    assert status.sessions_active == 5
    assert status.cognition_available is True
    assert status.version == get_current_version()
    assert status.timestamp == ""


def test_health_status_serialization():
    status = HealthStatus(
        status="ok",
        uptime_seconds=42.5,
        adapters={"telegram": True, "discord": False},
        sessions_active=3,
        cognition_available=True,
        timestamp="2026-03-19T00:00:00",
    )
    d = asdict(status)
    assert "status" in d
    assert "adapters" in d
    assert d["status"] == "ok"
    assert d["adapters"]["telegram"] is True
    assert d["adapters"]["discord"] is False
    assert d["sessions_active"] == 3
    assert d["timestamp"] == "2026-03-19T00:00:00"


def test_health_status_degraded():
    status = HealthStatus(
        status="degraded",
        uptime_seconds=0.0,
        adapters={},
        sessions_active=0,
        cognition_available=False,
    )
    assert status.status == "degraded"
    assert status.adapters == {}


def test_health_status_version():
    status = HealthStatus(
        status="ok",
        uptime_seconds=1.0,
        adapters={},
        sessions_active=0,
        cognition_available=True,
        version="2.0.0",
    )
    assert status.version == "2.0.0"


def test_version_is_frozen_not_reread_per_instance(monkeypatch, tmp_path):
    """#132: constructing HealthStatus must not re-read pyproject.toml.

    The version is frozen at import (health._VERSION); pointing
    update_check at a missing pyproject would make a per-request read
    yield the '0.0.0' failure sentinel. Post-fix, construction never
    touches disk, so the frozen value stands.
    """
    import health
    import update_check

    monkeypatch.setattr(update_check, "_PYPROJECT_PATH", tmp_path / "missing.toml")
    status = HealthStatus(
        status="ok",
        uptime_seconds=0.0,
        adapters={},
        sessions_active=0,
        cognition_available=False,
    )
    assert status.version == health._VERSION
    assert status.version != "0.0.0"  # the get_current_version() failure sentinel
