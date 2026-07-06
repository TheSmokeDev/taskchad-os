"""US-016 — inline pause/approve buttons on co-founder notify cards.

Path map (one test per distinct path, adversarial first):

* notify card side (cofounder/notify.py):
  - _build_reply_markup carries BOTH callback ids, one row, each under the
    64-byte callback_data limit (boundary locked at exactly 64)
  - overlong slug drops the buttons (None), never the card
  - notify() send includes reply_markup with both ids; overlong slug sends
    the card WITHOUT reply_markup and still returns True
* adapter round-trip (the EXISTING hashed-callback pipeline, no new code):
  - a >64-byte cofounder custom_id is hashed on send and _on_callback
    resolves it back to the original id (hash round-trip to handler)
  - a short id (the realistic cron-card tap — never in the bot's map)
    passes through intact with interaction_type=button
* router dispatch (router.py):
  - _handle_button routes the cofounder: prefix to _handle_cofounder_button
  - pause/approve taps execute the REAL manager.dispatch -> handle_cofounder
    path with disk-level effects matching the slash-command tests
  - viewer role is denied by the manager's role gate (button can never do
    more than the slash command)
  - malformed callbacks (2 parts / empty slug / 4 parts) and unknown actions
    are refused gracefully, dispatch never called
  - unregistered command and a raising dispatch both fail to friendly text
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure both .claude/scripts and .claude/chat are importable.
_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import adapters.telegram as telegram_adapter  # type: ignore[import-not-found]  # noqa: E402
import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402
from adapters.telegram import TelegramAdapter  # noqa: E402
from extension_manager import ExtensionManager  # noqa: E402
from models import MessageComponent  # noqa: E402
from router import ChatRouter  # noqa: E402

import config  # noqa: E402
from cofounder import notify as notify_mod  # noqa: E402
from cofounder import project_model  # noqa: E402
from cofounder import state as state_mod  # noqa: E402

COFOUNDER_ENV_KEYS = (
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_MAX_ITERATIONS",
    "COFOUNDER_MAX_WALL_CLOCK_HOURS",
    "COFOUNDER_MAX_CONCURRENT",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_ZOMBIE_STALE_MINUTES",
    "COFOUNDER_ARCHON_DB",
    "COFOUNDER_WORKFLOW_PROVIDER",
    "COFOUNDER_WORKFLOW_MODEL",
)

PROJECT_TEMPLATE = """---
tags: [system, cofounder]
status: {status}
created: 2026-07-01T09:00:00
last_run: null
repo: greenfield
branch: null
current_job_id: null
iterations: 1
max_iterations: 5
max_wall_clock_hours: 72
completion_check: "echo ok"
subjective_gate: {subjective_gate}
archon_workflow: null
chat_thread: null
---
# Widget Factory

## Spec (STATIC - orchestrator MUST NOT rewrite)
Build the widget factory.

## Plan / Working Memory
- [ ] first step

## Activity Log
- 2026-07-01T09:00:00 [note] project created
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No COFOUNDER_*/kill-switch/Telegram env leaks from the operator .env
    (config runs load_dotenv(override=True) at import)."""
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    yield


@pytest.fixture
def cofounder_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point projects dir + state dir at tmp; return the projects dir."""
    projects_dir = tmp_path / "cofounder"
    projects_dir.mkdir()
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(projects_dir))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    return projects_dir


def _write_project(
    projects_dir: Path,
    slug: str = "alpha",
    status: str = "building",
    subjective_gate: bool = False,
) -> Path:
    path = projects_dir / f"{slug}.md"
    path.write_text(
        PROJECT_TEMPLATE.format(
            status=status,
            subjective_gate="true" if subjective_gate else "false",
        ),
        encoding="utf-8",
    )
    return path


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_fake_telegram(monkeypatch, *, message_id: int = 4242) -> dict:
    captured: dict = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["params"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return _FakeResponse({"ok": True, "result": {"message_id": message_id}})

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    return captured


def set_creds(monkeypatch, token: str = "tok123", user_ids: str = "555"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", user_ids)


class _RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message) -> None:
        self.sent.append(message.text)


def _incoming(**extra) -> SimpleNamespace:
    return SimpleNamespace(channel=None, thread=None, **extra)


def _real_manager() -> ExtensionManager:
    """A real ExtensionManager with the real /cofounder registration — the
    button path dispatches through the SAME machinery as the slash command."""
    manager = ExtensionManager()
    rows = [row for row in commands.COMMANDS if row[0] == "cofounder"]
    assert rows, "cofounder COMMANDS row missing (US-015 registration)"
    manager.register_core_commands(
        rows, commands.CATEGORIES, {"cofounder": core_handlers.handle_cofounder}
    )
    return manager


def _shim(manager) -> SimpleNamespace:
    obj = SimpleNamespace()
    obj._handle_cofounder_button = ChatRouter._handle_cofounder_button.__get__(obj)
    obj.manager = manager
    return obj


def _recording_manager() -> tuple[SimpleNamespace, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    async def dispatch(command, adapter, incoming, args, **kwargs):
        calls.append((command, args))
        return f"dispatched {command} {args}"

    return SimpleNamespace(dispatch=dispatch), calls


# ---------------------------------------------------------------------------
# Notify card — reply_markup carries both callback ids within 64 bytes
# ---------------------------------------------------------------------------


def test_build_reply_markup_carries_both_callback_ids_within_limit() -> None:
    markup = notify_mod._build_reply_markup("alpha")
    assert markup is not None
    rows = markup["inline_keyboard"]
    assert len(rows) == 1
    ids = [btn["callback_data"] for btn in rows[0]]
    assert ids == ["cofounder:pause:alpha", "cofounder:approve:alpha"]
    for cid in ids:
        assert len(cid.encode("utf-8")) <= 64


def test_build_reply_markup_boundary_at_exactly_64_bytes() -> None:
    # "cofounder:approve:" is 18 bytes -> a 46-char slug hits exactly 64.
    fits = "s" * 46
    assert notify_mod._build_reply_markup(fits) is not None
    assert notify_mod._build_reply_markup("s" * 47) is None


def test_notify_send_carries_reply_markup(tmp_path, monkeypatch) -> None:
    set_creds(monkeypatch)
    captured = install_fake_telegram(monkeypatch)
    project = SimpleNamespace(slug="alpha", path=None)
    ok = notify_mod.notify(project, "check green", "done", audit_path=tmp_path / "a.jsonl")
    assert ok is True
    markup = json.loads(captured["params"]["reply_markup"])
    ids = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "cofounder:pause:alpha" in ids
    assert "cofounder:approve:alpha" in ids


def test_notify_overlong_slug_sends_card_without_buttons(tmp_path, monkeypatch) -> None:
    """A slug past the callback_data limit must drop the BUTTONS, not the card
    (Telegram rejects the whole sendMessage on >64-byte callback_data)."""
    set_creds(monkeypatch)
    captured = install_fake_telegram(monkeypatch)
    project = SimpleNamespace(slug="s" * 60, path=None)
    ok = notify_mod.notify(project, "stuck", "blocked", audit_path=tmp_path / "a.jsonl")
    assert ok is True
    assert "reply_markup" not in captured["params"]


# ---------------------------------------------------------------------------
# Adapter — the EXISTING hashed-callback pipeline round-trips cofounder ids
# ---------------------------------------------------------------------------


def _adapter_shim() -> TelegramAdapter:
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._queue = telegram_adapter.asyncio.Queue()
    adapter.allowed_user_ids = []
    adapter._callback_id_map = {}
    return adapter


class _FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=777, first_name="Op")
        self.message = SimpleNamespace(
            chat_id=42, chat=SimpleNamespace(type="private"), reply_markup=None
        )

    async def answer(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_hashed_callback_round_trips_to_the_cofounder_custom_id() -> None:
    """>64-byte custom_id: hashed on send, resolved back on tap — the full
    hash round-trip through _callback_id_map ends at __button:cofounder:*."""
    adapter = _adapter_shim()
    long_id = f"cofounder:pause:{'s' * 60}"  # 76 bytes -> hash map engages
    markup = adapter._build_reply_markup([MessageComponent("Pause", long_id, "secondary")])
    callback_data = markup.inline_keyboard[0][0].callback_data
    assert callback_data.startswith("h:")
    assert adapter._callback_id_map[callback_data] == long_id

    await adapter._on_callback(SimpleNamespace(callback_query=_FakeQuery(callback_data)), None)
    incoming = adapter._queue.get_nowait()
    assert incoming.text == f"__button:{long_id}"
    assert incoming.raw_event["interaction_type"] == "button"
    assert incoming.raw_event["custom_id"] == long_id


@pytest.mark.asyncio
async def test_short_callback_passes_through_intact() -> None:
    """The realistic cron-card tap: the id was sent by ANOTHER process, is
    absent from the bot's map, and must pass through unchanged."""
    adapter = _adapter_shim()
    await adapter._on_callback(
        SimpleNamespace(callback_query=_FakeQuery("cofounder:approve:alpha")), None
    )
    incoming = adapter._queue.get_nowait()
    assert incoming.text == "__button:cofounder:approve:alpha"
    assert incoming.raw_event["interaction_type"] == "button"


# ---------------------------------------------------------------------------
# Router — _handle_button routes the cofounder: prefix to the button handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_button_routes_cofounder_prefix() -> None:
    obj = SimpleNamespace()
    obj._handle_button = ChatRouter._handle_button.__get__(obj)
    routed: list[str] = []

    async def record(adapter, incoming, custom_id):
        routed.append(custom_id)

    obj._handle_cofounder_button = record
    await obj._handle_button(_RecordingAdapter(), _incoming(), "cofounder:pause:alpha")
    assert routed == ["cofounder:pause:alpha"]


# ---------------------------------------------------------------------------
# Button press == slash command (same dispatch path, disk-level effects)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_pause_matches_slash_command_effects(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha", status="building")
    adapter = _RecordingAdapter()
    shim = _shim(_real_manager())

    await shim._handle_cofounder_button(adapter, _incoming(), "cofounder:pause:alpha")

    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "awaiting-human"
    entry = state_mod.get_project_state(state_mod.load_state(), "alpha")
    assert entry.get("paused_from") == "building"
    assert "paused" in adapter.sent[-1]


@pytest.mark.asyncio
async def test_button_approve_matches_slash_command_effects(cofounder_env: Path) -> None:
    path = _write_project(
        cofounder_env, "alpha", status="awaiting-human", subjective_gate=True
    )
    adapter = _RecordingAdapter()
    shim = _shim(_real_manager())

    await shim._handle_cofounder_button(adapter, _incoming(), "cofounder:approve:alpha")

    assert not path.exists(), "approve must move the file (Rule 4: disk proof)"
    archived = cofounder_env / "done" / "alpha.md"
    assert archived.exists()
    assert project_model.parse_project_file(archived).frontmatter.status == "done"
    assert "archived" in adapter.sent[-1]


@pytest.mark.asyncio
async def test_button_unknown_slug_gets_friendly_error(cofounder_env: Path) -> None:
    adapter = _RecordingAdapter()
    shim = _shim(_real_manager())
    await shim._handle_cofounder_button(adapter, _incoming(), "cofounder:pause:ghost")
    assert "Unknown co-founder project" in adapter.sent[-1]


@pytest.mark.asyncio
async def test_viewer_role_is_denied_by_the_dispatch_gate(cofounder_env: Path) -> None:
    """The button rides manager.dispatch, so the /cofounder admin role gate
    applies — a button can never do more than the slash command."""
    path = _write_project(cofounder_env, "alpha", status="building")
    adapter = _RecordingAdapter()
    shim = _shim(_real_manager())

    await shim._handle_cofounder_button(
        adapter, _incoming(user_role="viewer"), "cofounder:pause:alpha"
    )

    assert "Permission denied" in adapter.sent[-1]
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "building", "denied tap must not flip status"


# ---------------------------------------------------------------------------
# Malformed / unknown callbacks — refused gracefully, dispatch never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "custom_id",
    [
        "cofounder:pause",  # missing slug segment
        "cofounder:pause:",  # empty slug
        "cofounder:approve:alpha:extra",  # too many segments
    ],
)
async def test_malformed_callback_refused_without_dispatch(custom_id: str) -> None:
    manager, calls = _recording_manager()
    adapter = _RecordingAdapter()
    shim = _shim(manager)
    await shim._handle_cofounder_button(adapter, _incoming(), custom_id)
    assert calls == []
    assert "Malformed co-founder action" in adapter.sent[0]


@pytest.mark.asyncio
async def test_unknown_action_refused_without_dispatch() -> None:
    """Only pause/approve ride notify cards; steer/resume/etc. must not be
    synthesizable through a callback id."""
    manager, calls = _recording_manager()
    adapter = _RecordingAdapter()
    shim = _shim(manager)
    await shim._handle_cofounder_button(adapter, _incoming(), "cofounder:steer:alpha")
    assert calls == []
    assert "Unknown co-founder action" in adapter.sent[0]


@pytest.mark.asyncio
async def test_valid_callback_dispatches_the_slash_args() -> None:
    manager, calls = _recording_manager()
    adapter = _RecordingAdapter()
    shim = _shim(manager)
    await shim._handle_cofounder_button(adapter, _incoming(), "cofounder:pause:alpha")
    assert calls == [("cofounder", "pause alpha")]
    assert adapter.sent == ["dispatched cofounder pause alpha"]


# ---------------------------------------------------------------------------
# Fail-open seams — unregistered command, raising dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unregistered_command_falls_back_to_friendly_text() -> None:
    """dispatch returns None when /cofounder is not registered (manager
    contract) — the tap still gets an answer."""
    adapter = _RecordingAdapter()
    shim = _shim(ExtensionManager())  # nothing registered
    await shim._handle_cofounder_button(adapter, _incoming(), "cofounder:pause:alpha")
    assert adapter.sent == ["Co-founder command is not available."]


@pytest.mark.asyncio
async def test_raising_dispatch_never_leaves_the_tap_unanswered() -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("manager exploded")

    adapter = _RecordingAdapter()
    shim = _shim(SimpleNamespace(dispatch=boom))
    await shim._handle_cofounder_button(adapter, _incoming(), "cofounder:approve:alpha")
    assert "Co-founder action failed" in adapter.sent[0]
    assert "RuntimeError" in adapter.sent[0]
