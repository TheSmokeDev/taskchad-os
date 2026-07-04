"""Test Phase 2 / WS-chat — /recap, /blueprints, /suggestions handlers.

Exercises the three ``core_handlers`` entrypoints against the REAL orchestration
contract (blueprint_catalog, suggestions, suggestion_catalog) with an isolated
``config.STATE_DIR`` (Rule 1 — the suggestions store writes there at call time)
and a fake session store for /recap. The scheduled_api create call is
monkeypatched so no live orchestration API process is required:

  * accept happy path  → fake create_fn returns a row → "Scheduled"
  * accept refused     → fake create_fn raises ScheduledCreateRefused → the
                         handler returns the guard's verbatim friendly_message
                         (never a 500)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import core_handlers  # type: ignore[import-not-found]  # noqa: E402

import config  # type: ignore[import-not-found]  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.STATE_DIR at a temp dir so the suggestions store is clean."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    return tmp_path


def _incoming(platform: str = "telegram") -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        channel=SimpleNamespace(platform_id="chan1"),
        thread=None,
        user_role="admin",
        chat_id="chan1",
    )


class _FakeStore:
    def __init__(self, existing=None, messages=None) -> None:
        self._existing = existing
        self._messages = messages or []

    def get(self, platform, channel_id, thread_id):
        return self._existing

    def list_recent_messages(self, session_id, limit=80):
        return self._messages


class _FakeEngine:
    def __init__(self, store: _FakeStore) -> None:
        self.session_store = store


def _set_engine(store: _FakeStore) -> None:
    core_handlers.set_context(engine=_FakeEngine(store))


# ---------------------------------------------------------------------------
# /recap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_recap_no_session() -> None:
    _set_engine(_FakeStore(existing=None))
    reply = await core_handlers.handle_recap(None, _incoming(), "")
    assert "Session recap" in reply
    assert "nothing to recap" in reply


@pytest.mark.asyncio
async def test_handle_recap_over_seeded_session() -> None:
    session = SimpleNamespace(session_id="telegram:1:2")
    messages = [
        SimpleNamespace(role="user", content="edit the file", tool_calls=[]),
        SimpleNamespace(
            role="assistant",
            content="done",
            tool_calls=[{"name": "Write", "arguments": {"file_path": "foo.py"}}],
        ),
    ]
    _set_engine(_FakeStore(existing=session, messages=messages))
    reply = await core_handlers.handle_recap(None, _incoming(), "")
    assert "Session recap" in reply
    assert "Write×1" in reply
    assert "foo.py" in reply


# ---------------------------------------------------------------------------
# /blueprints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_blueprints_list() -> None:
    reply = await core_handlers.handle_blueprints(None, _incoming(), "")
    assert "Automation Blueprints" in reply
    assert "morning-brief" in reply


@pytest.mark.asyncio
async def test_handle_blueprints_show_detail() -> None:
    reply = await core_handlers.handle_blueprints(None, _incoming(), "morning-brief")
    assert "morning-brief" in reply
    assert "Slots" in reply
    # The pre-filled command uses the /blueprints prefix.
    assert "/blueprints morning-brief" in reply


@pytest.mark.asyncio
async def test_handle_blueprints_fill_registers_pending(isolated_state: Path) -> None:
    from orchestration import suggestions

    reply = await core_handlers.handle_blueprints(
        None, _incoming(), "morning-brief time=07:30 deliver=origin"
    )
    assert "Proposed" in reply
    pending = suggestions.list_pending()
    assert len(pending) == 1
    assert pending[0]["source"] == "blueprint"
    # The filled schedule resolved to a 5-field cron.
    assert pending[0]["job_spec"]["schedule"] == "30 7 * * *"


@pytest.mark.asyncio
async def test_handle_blueprints_fill_bad_slot_is_friendly(isolated_state: Path) -> None:
    reply = await core_handlers.handle_blueprints(
        None, _incoming(), "morning-brief tiem=07:30"
    )
    assert "Could not fill" in reply
    assert "unknown slot" in reply.lower()


@pytest.mark.asyncio
async def test_handle_blueprints_unknown_key(isolated_state: Path) -> None:
    reply = await core_handlers.handle_blueprints(None, _incoming(), "nope")
    assert "No blueprint 'nope'" in reply


# ---------------------------------------------------------------------------
# /suggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_suggestions_list_seeds_catalog(isolated_state: Path) -> None:
    from orchestration import suggestions

    reply = await core_handlers.handle_suggestions(None, _incoming(), "")
    assert "Automation Suggestions" in reply
    pending = suggestions.list_pending()
    assert len(pending) == 4
    assert all(s["source"] == "catalog" for s in pending)


@pytest.mark.asyncio
async def test_handle_suggestions_accept_invokes_create(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from integrations import scheduled_api
    from orchestration import suggestions

    # Seed one pending suggestion via the list path.
    await core_handlers.handle_suggestions(None, _incoming(), "list")
    assert len(suggestions.list_pending()) == 4

    calls: list[dict] = []

    async def _fake_create(spec, *, client=None):
        calls.append(spec)
        return {"id": 99, **spec}

    monkeypatch.setattr(scheduled_api, "create_scheduled_task", _fake_create)

    reply = await core_handlers.handle_suggestions(None, _incoming(), "accept 1")
    assert "Scheduled" in reply
    assert len(calls) == 1
    # Accepted → dropped from pending.
    assert len(suggestions.list_pending()) == 3


@pytest.mark.asyncio
async def test_handle_suggestions_accept_refused_returns_friendly(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from integrations import scheduled_api
    from orchestration import suggestions

    await core_handlers.handle_suggestions(None, _incoming(), "list")

    detail = "Blocked: job contains a bot lifecycle command (run_chat.sh)."

    async def _refuse(spec, *, client=None):
        raise scheduled_api.ScheduledCreateRefused(detail)

    monkeypatch.setattr(scheduled_api, "create_scheduled_task", _refuse)

    reply = await core_handlers.handle_suggestions(None, _incoming(), "accept 1")
    assert reply == detail
    # Refused create leaves the suggestion PENDING (not latched accepted).
    assert len(suggestions.list_pending()) == 4


@pytest.mark.asyncio
async def test_handle_suggestions_dismiss_latches(isolated_state: Path) -> None:
    from orchestration import suggestions

    await core_handlers.handle_suggestions(None, _incoming(), "list")
    assert len(suggestions.list_pending()) == 4

    reply = await core_handlers.handle_suggestions(None, _incoming(), "dismiss 1")
    assert "Dismissed" in reply
    assert len(suggestions.list_pending()) == 3

    # Re-seeding does NOT re-offer the dismissed one (dedup latch).
    await core_handlers.handle_suggestions(None, _incoming(), "list")
    assert len(suggestions.list_pending()) == 3


@pytest.mark.asyncio
async def test_handle_suggestions_accept_missing_ref() -> None:
    reply = await core_handlers.handle_suggestions(None, _incoming(), "accept")
    assert "Usage" in reply
