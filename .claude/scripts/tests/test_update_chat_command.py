from __future__ import annotations

import pytest
from commands import COMMANDS, get_command_min_role
from core_handlers import handle_update
from router import _FRAMEWORK_UPDATE_NOW_RE


@pytest.mark.parametrize(
    "text",
    [
        "update yourself",
        "pull the latest " + "Task" + "Chad OS",
        "pull the lastest update on " + "task" + "chad os",
        "upgrade the homie",
    ],
)
def test_direct_framework_update_phrases_are_tightly_routed(text: str) -> None:
    assert _FRAMEWORK_UPDATE_NOW_RE.fullmatch(text)


@pytest.mark.parametrize(
    "text",
    [
        "update the manual",
        "install the dependency",
        "pull the customer report",
        "should you update yourself?",
    ],
)
def test_other_update_discussion_is_not_auto_executed(text: str) -> None:
    assert not _FRAMEWORK_UPDATE_NOW_RE.fullmatch(text)


def test_update_command_is_admin_only() -> None:
    assert get_command_min_role("update") == "admin"
    assert any(row[0] == "update" and row[2:] == ("router", "admin") for row in COMMANDS)


@pytest.mark.asyncio
async def test_mutating_update_commands_refuse_chaining() -> None:
    assert await handle_update(None, None, "now", collect_only=True) == (
        "Cannot chain /update now — use it alone."
    )
    assert await handle_update(None, None, "auto on", collect_only=True) == (
        "Cannot chain /update auto — use it alone."
    )
