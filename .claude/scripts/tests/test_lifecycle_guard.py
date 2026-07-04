"""Tests for orchestration/lifecycle_guard.py + its two wire seams.

Covers Deliverable 3 of the Hermes v0.18 Tier-1 Ports (Phase 1):
  * the re-anchored, bot-SPECIFIC ``_BOT_LIFECYCLE_PATTERN`` — REJECT each
    command shape, ALLOW bare ``python``/``main.py``/benign prose (B2);
  * prompt+script combined scan with ``errors="replace"`` bytes decode;
  * convoy_service create_convoy / add_subtasks wiring (raise → api.py 400);
  * fail-open contract (M3 guard RuntimeError, R2 NM1 import failure) — a
    benign convoy always creates.

The dashboard ``/api/scheduled`` seam (B1) is tested in test_dashboard_api.py.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from orchestration import lifecycle_guard  # noqa: E402
from orchestration.convoy_service import ConvoyService  # noqa: E402
from orchestration.db import OrchestrationDB  # noqa: E402
from orchestration.lifecycle_guard import (  # noqa: E402
    BotLifecycleBlocked,
    check_bot_lifecycle,
    contains_bot_lifecycle_command,
)
from orchestration.models import (  # noqa: E402
    AddSubtaskInput,
    CreateConvoyInput,
    CreateSubtaskInput,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def svc():
    db = OrchestrationDB(":memory:")
    yield ConvoyService(db)
    db.close()


# ── REJECT: each bot-SPECIFIC command shape must raise ────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "bash run_chat.sh",
        "cd .claude/chat && bash run_chat.sh",
        "python ../chat/main.py",
        "python .claude/chat/main.py --fg",
        "py chat\\main.py",
        "uv run python ../chat/main.py",
        "uv run ../chat/main.py",
        "taskkill /IM run_chat",
        "taskkill /F /IM chat/main.py",
        "Stop-Process -Name thehomie -Force",
        "stop-process ... chat/main.py",
        "Restart-Service thehomie",
        "nssm restart homie-bot",
        "sc stop thehomie",
        "pkill -f chat/main.py",
        "kill run_chat",
        "pkill -f thehomie",
    ],
)
def test_rejects_bot_lifecycle_shapes(text):
    assert contains_bot_lifecycle_command(text) is True
    with pytest.raises(BotLifecycleBlocked):
        check_bot_lifecycle(text)


# ── ALLOW: bare python/main.py/bot + benign prose must NOT raise (B2) ─────


@pytest.mark.parametrize(
    "text",
    [
        "python app/main.py",
        "python main.py",
        "taskkill /IM python.exe",
        "edit main.py",
        "restart behavior of the API gateway",
        "Investigate why the bot keeps crashing on boot",
        "run the chat test suite",
        "kill the flaky python process holding port 4322",
        "Deploy v2 and report the merge commit",
        "",
        "update run_chat documentation in the README",
        # F2 — words ending in "kill" must NOT read as the POSIX `kill` command
        # (left \b anchor); "thehomie" alone is not a two-token kill shape.
        "upskill thehomie onboarding",
        "reskill thehomie",
        "skill thehomie docs",
    ],
)
def test_allows_benign_text(text):
    assert contains_bot_lifecycle_command(text) is False
    check_bot_lifecycle(text)  # must not raise


def test_bare_run_chat_word_does_not_match_without_sh():
    # ``run_chat.sh`` is the launcher; a bare ``run_chat`` mention in prose is
    # not branch A. (It only trips branches C/D/E behind a kill/service verb.)
    assert contains_bot_lifecycle_command("the run_chat script logs to bot.log") is False


# ── prompt + script combined scan (errors="replace" bytes decode) ─────────


def test_command_in_script_is_caught(tmp_path):
    script = tmp_path / "deploy.sh"
    script.write_text("#!/bin/bash\nbash run_chat.sh\n")
    with pytest.raises(BotLifecycleBlocked):
        check_bot_lifecycle("benign prompt text", script=str(script))


def test_command_in_binary_script_is_caught(tmp_path):
    # Non-UTF-8 noise must NOT let the command slip through — errors="replace".
    script = tmp_path / "obf.bin"
    script.write_bytes(b"\xff\xfe\x00 pkill -f chat/main.py \x80\x81")
    with pytest.raises(BotLifecycleBlocked):
        check_bot_lifecycle("benign", script=str(script))


def test_benign_prompt_and_script_allowed(tmp_path):
    script = tmp_path / "ok.sh"
    script.write_text("echo hello\nls -la\n")
    check_bot_lifecycle("run the deploy", script=str(script))  # must not raise


def test_missing_script_path_does_not_crash(tmp_path):
    missing = tmp_path / "does-not-exist.sh"
    # OSError → empty string; prompt-only scan applies (benign here).
    check_bot_lifecycle("benign prompt", script=str(missing))


def test_command_split_across_prompt_and_script(tmp_path):
    # Prompt alone benign, script alone benign, but the guard scans BOTH so a
    # command in the script is still caught.
    script = tmp_path / "s.sh"
    script.write_text("Stop-Process -Name thehomie\n")
    with pytest.raises(BotLifecycleBlocked):
        check_bot_lifecycle("please run the maintenance job", script=str(script))


# ── Convoy service wiring (seam 1) ────────────────────────────────────────


def test_benign_convoy_creates(svc):
    result = svc.create_convoy(
        CreateConvoyInput(title="Deploy v2", description="Ship the release", created_by="sb")
    )
    assert result.convoy.title == "Deploy v2"


def test_convoy_title_lifecycle_command_blocked(svc):
    with pytest.raises(BotLifecycleBlocked):
        svc.create_convoy(
            CreateConvoyInput(title="bash run_chat.sh", created_by="sb")
        )


def test_convoy_description_lifecycle_command_blocked(svc):
    with pytest.raises(BotLifecycleBlocked):
        svc.create_convoy(
            CreateConvoyInput(
                title="Maintenance",
                description="then pkill -f chat/main.py to reset",
                created_by="sb",
            )
        )


def test_convoy_subtask_lifecycle_command_blocked(svc):
    with pytest.raises(BotLifecycleBlocked):
        svc.create_convoy(
            CreateConvoyInput(
                title="Deploy",
                created_by="sb",
                subtasks=[
                    CreateSubtaskInput(title="ok task"),
                    CreateSubtaskInput(
                        title="cleanup", description="taskkill /IM run_chat"
                    ),
                ],
            )
        )


def test_convoy_blocked_before_db_write(svc):
    # A blocked create must not persist a partial convoy row.
    with pytest.raises(BotLifecycleBlocked):
        svc.create_convoy(CreateConvoyInput(title="bash run_chat.sh", created_by="sb"))
    assert svc.list_convoys() == []


def test_add_subtasks_lifecycle_command_blocked(svc):
    result = svc.create_convoy(CreateConvoyInput(title="Base", created_by="sb"))
    cid = result.convoy.id
    with pytest.raises(BotLifecycleBlocked):
        svc.add_subtasks(
            cid,
            [AddSubtaskInput(title="kill it", description="Stop-Process -Name thehomie")],
        )


def test_add_subtasks_benign_succeeds(svc):
    result = svc.create_convoy(CreateConvoyInput(title="Base", created_by="sb"))
    cid = result.convoy.id
    added = svc.add_subtasks(cid, [AddSubtaskInput(title="benign task")])
    assert len(added) == 1


# ── api.py maps BotLifecycleBlocked (a ValueError) → HTTP 400 ─────────────


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from unittest.mock import patch

    db_path = tmp_path / "orch_api.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, ts = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = ts
        yield TestClient(api_mod.app)
        db.close()


def test_convoy_api_lifecycle_command_returns_400(api_client):
    r = api_client.post(
        "/api/convoy",
        json={"title": "bash run_chat.sh", "created_by": "sb"},
    )
    assert r.status_code == 400


def test_convoy_api_benign_returns_200(api_client):
    r = api_client.post(
        "/api/convoy",
        json={"title": "Deploy v2", "created_by": "sb"},
    )
    assert r.status_code == 200


# ── Fail-open: M3 (guard raises) + R2 NM1 (import failure) ─────────────────


def test_fail_open_when_guard_raises(svc, monkeypatch):
    # M3: a non-BotLifecycleBlocked raise from check_bot_lifecycle is caught +
    # logged so a legit convoy still creates.
    def _boom(*a, **k):
        raise RuntimeError("regex engine exploded")

    monkeypatch.setattr(lifecycle_guard, "check_bot_lifecycle", _boom)
    result = svc.create_convoy(CreateConvoyInput(title="Deploy v2", created_by="sb"))
    assert result.convoy.title == "Deploy v2"


def test_fail_open_when_guard_import_fails(svc, monkeypatch):
    # R2 NM1: the guard import lives INSIDE the fail-open wrapper, so an
    # import-time failure of lifecycle_guard ALLOWS the create (never closes).
    class _RaisingModule:
        def __getattr__(self, name):
            raise RuntimeError("simulated import failure")

    monkeypatch.setitem(
        sys.modules, "orchestration.lifecycle_guard", _RaisingModule()
    )
    result = svc.create_convoy(CreateConvoyInput(title="Deploy v2", created_by="sb"))
    assert result.convoy.title == "Deploy v2"


def test_fail_open_guard_raises_still_lets_lifecycle_text_through(svc, monkeypatch):
    # When the guard itself is broken, even a real lifecycle command is ALLOWED
    # (fail-open beats fail-closed for a broken guard — the create must not wedge).
    def _boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(lifecycle_guard, "check_bot_lifecycle", _boom)
    result = svc.create_convoy(
        CreateConvoyInput(title="bash run_chat.sh", created_by="sb")
    )
    assert result.convoy.id >= 1
