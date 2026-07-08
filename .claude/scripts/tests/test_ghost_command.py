"""Ghost Phone P4.1 A1 — the /ghost chat command (status | up | down)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import commands  # type: ignore[import-not-found]  # noqa: E402
import config  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402
import ghost_control  # type: ignore[import-not-found]  # noqa: E402


def test_ghost_command_registered_all_four() -> None:
    # 1. COMMANDS tuple  2. CORE_HANDLERS dispatch  3. the handler  4. native menu
    assert "ghost" in [row[0] for row in commands.COMMANDS]
    assert core_handlers.CORE_HANDLERS["ghost"] is core_handlers.handle_ghost
    assert "ghost" in commands.TELEGRAM_NATIVE_COMMANDS


@pytest.mark.asyncio
async def test_ghost_status_renders_physical_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setattr(
        ghost_control,
        "ghost_status",
        lambda **_k: {
            "running": True,
            "booted": True,
            "serial": "emulator-5554",
            "avd": "homie_pixel",
            "detail": "ok",
        },
    )
    out = await core_handlers.handle_ghost(None, None, "status")
    assert "Ghost Phone" in out
    assert "emulator-5554" in out
    assert "running: True" in out and "booted: True" in out


@pytest.mark.asyncio
async def test_ghost_up_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.delenv("HOMIE_KILLSWITCH_GHOST", raising=False)
    seen: list[str] = []
    monkeypatch.setattr(
        ghost_control,
        "ensure_ghost_running",
        lambda **_k: seen.append("up") or {"ok": True, "status": "booted", "detail": "ghost AVD booted"},
    )
    out = await core_handlers.handle_ghost(None, None, "up")
    assert seen == ["up"]
    assert "Ghost is up" in out and "booted" in out


@pytest.mark.asyncio
async def test_ghost_down_shuts_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setattr(
        ghost_control,
        "ghost_shutdown",
        lambda **_k: {"ok": True, "status": "killed", "detail": "emu kill sent"},
    )
    out = await core_handlers.handle_ghost(None, None, "down")
    assert "shut down" in out and "killed" in out


@pytest.mark.asyncio
async def test_ghost_disabled_when_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    monkeypatch.setattr(
        ghost_control,
        "ghost_status",
        lambda **_k: pytest.fail("disabled ghost must not touch adb"),
    )
    out = await core_handlers.handle_ghost(None, None, "status")
    assert "Ghost is disabled" in out


@pytest.mark.asyncio
async def test_ghost_up_refused_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setenv("HOMIE_KILLSWITCH_GHOST", "disabled")
    monkeypatch.setattr(
        ghost_control,
        "ensure_ghost_running",
        lambda **_k: pytest.fail("kill-switched /ghost up must not boot"),
    )
    out = await core_handlers.handle_ghost(None, None, "up")
    assert "kill-switch" in out.lower()


# ---------------------------------------------------------------------------
# A3 — natural-language intent routing (registration + detect_intents behavior)
# ---------------------------------------------------------------------------

def test_ghost_lifecycle_intent_registered() -> None:
    ghost_kw = [kw for kws, cmd, _ in commands.CORE_INTENTS if cmd == "ghost" for kw in kws]
    assert "boot the ghost" in ghost_kw
    assert "ghost status" in ghost_kw


def test_ghost_drive_phrases_route_to_browserops() -> None:
    browserops_kw = [
        kw for kws, cmd, _ in commands.CORE_INTENTS if cmd == "browserops" for kw in kws
    ]
    assert "on the ghost" in browserops_kw


def test_detect_intents_boot_the_ghost_routes_to_ghost() -> None:
    # conftest pins INTENT_AUTODISPATCH_ENABLED=True (framework default).
    from extension_manager import ExtensionManager

    mgr = ExtensionManager()
    mgr.register_core_intents(commands.CORE_INTENTS)
    detected = mgr.detect_intents("can you boot the ghost")
    assert "ghost" in detected


def test_detect_intents_drive_on_the_ghost_routes_to_browserops() -> None:
    from extension_manager import ExtensionManager

    mgr = ExtensionManager()
    mgr.register_core_intents(commands.CORE_INTENTS)
    detected = mgr.detect_intents("check my twitter on the ghost")
    assert "browserops" in detected
