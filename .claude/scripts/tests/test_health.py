"""Tests for health — HealthStatus dataclass and serialization."""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

import pytest

# Add chat dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "chat"))

from health import HealthStatus


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
    assert status.version == "1.0.0"
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
