"""The ingress-role seam: every producer stamps, and the stamp means something.

`IncomingMessage.user_role` is authorization data. Three populations touch it —
adapters (the authorities), consumers (the gates), and internal producers (code
that builds a message and hands it to the same gate). The #424 round-3 fix swept
the first two; the design re-verdict found the third unswept and the live /talk
voice surface demoted to viewer as a result.

These tests lock the seam itself rather than any one call site, because three
sibling tickets (#427/#428/#429) delete their bespoke role resolvers and inherit
it.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Vendored / generated / irrelevant trees. `tests` is excluded on purpose: a
#: test may legitimately construct an UNSTAMPED message to prove the
#: fail-closed default is what an unstamped surface gets.
_PRUNED_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", "_archive", ".git",
    "site-packages", "tests", ".mypy_cache", ".pytest_cache", "data", "models",
}

_SOURCE_ROOTS = (
    ".claude/chat",
    ".claude/scripts",
    ".claude/extensions",
    ".claude/hooks",
    "dashboard",
)


def _iter_source_files():
    for root in _SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
            for name in filenames:
                if name.endswith(".py"):
                    yield pathlib.Path(dirpath) / name


def _construction_sites():
    """Every non-test `IncomingMessage(...)` call, with its keyword names."""
    for path in _iter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - vendored junk
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "IncomingMessage":
                continue
            kwargs = {k.arg for k in node.keywords}
            yield path.relative_to(REPO_ROOT).as_posix(), node.lineno, kwargs


def test_every_producer_stamps_an_explicit_role() -> None:
    """The self-enforcing half of the seam.

    `IncomingMessage.user_role` is fail-closed, which makes a FORGOTTEN stamp
    silent: the message just quietly loses privilege until someone notices a
    live surface answering "Permission denied" (which is exactly how the /talk
    voice regression shipped). Position and convention are not enough — every
    construction site outside tests must state the authority it carries, so a
    new producer cannot inherit a default in either direction.
    """
    sites = list(_construction_sites())
    assert sites, "guard found no construction sites — the walk is broken"

    unstamped = [
        f"{path}:{line}"
        for path, line, kwargs in sites
        # `None` is a **splat; those forward an already-built kwargs mapping.
        if "user_role" not in kwargs and None not in kwargs
    ]

    assert unstamped == [], (
        "these IncomingMessage producers do not pass an explicit user_role= "
        "(fail-closed means they silently become `viewer`; state the authority "
        "they carry, with a comment saying whose it is): " + ", ".join(unstamped)
    )


# ── the empty-allowlist survival property (re-verdict note 4) ──────────────


@pytest.mark.parametrize(
    ("sender", "allowlist"),
    [
        (12345, []),          # Telegram: list[int], unset
        ("12345", []),        # Discord / Slack / WhatsApp: list[str], unset
        ("12345", ()),        # any falsy container
        ("12345", set()),
    ],
)
def test_empty_allowlist_grants_nothing(sender, allowlist) -> None:
    """R4 BLOCKER: an empty allowlist must not mint admin.

    These adapters ADMIT everyone when their list is unset, so an empty list is
    not a statement that the sender is the operator — it is the absence of any
    statement. Deriving `admin` from it made every stranger who found the bot an
    admin on a default install. Identity comes only from an explicit entry.
    """
    from models import resolve_ingress_role

    assert resolve_ingress_role(sender, allowlist) == "viewer"


@pytest.mark.parametrize(
    ("surface", "env_var"),
    [
        ("telegram", "TELEGRAM_ALLOWED_USER_IDS"),
        ("discord", "DISCORD_ALLOWED_USERS"),
        ("slack", "CHAT_ALLOWED_USERS"),
        ("whatsapp", "WHATSAPP_ALLOWED_NUMBERS"),
    ],
)
def test_unset_allowlist_warns_the_operator_by_name(surface, env_var) -> None:
    """The refusal must be explainable from the log.

    Fail-closed silence is the failure mode that shipped the /talk regression:
    the operator meets it as a mystery "Permission denied". Each remote adapter
    says at startup which env var turns its commands back on.
    """
    from models import ingress_allowlist_warning

    warning = ingress_allowlist_warning(surface, [], env_var)
    assert warning is not None
    assert env_var in warning
    assert "viewer" in warning
    # Configured -> silent.
    assert ingress_allowlist_warning(surface, ["123"], env_var) is None


@pytest.mark.parametrize(
    ("sender", "allowlist", "expected"),
    [
        (555, [555], "admin"),
        (999, [555], "viewer"),
        ("555", ["555"], "admin"),
        ("999", ["555"], "viewer"),
    ],
)
def test_configured_allowlist_is_the_authenticated_identity_list(
    sender, allowlist, expected
) -> None:
    """Configured → on-list is admin, off-list is viewer, same compare as the gate."""
    from models import resolve_ingress_role

    assert resolve_ingress_role(sender, allowlist) == expected


# ── the /talk voice surface carries its join operator's authority ──────────


@contextlib.contextmanager
def _null_lock(*_args, **_kwargs):
    """Stand-in for shared.file_lock (no real lock file in a unit test)."""
    yield


def _real_command_manager():
    """A REAL ExtensionManager with the REAL core registry (real min_roles)."""
    from commands import CATEGORIES, COMMANDS, CORE_INTENTS
    from core_handlers import CORE_HANDLERS
    from extension_manager import ExtensionManager

    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)
    manager.register_core_intents(CORE_INTENTS)
    return manager



def test_join_threads_the_operator_role_to_the_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middle hops: lifecycle -> sidecar control `/join`.

    `handle_talk` puts the stamped role in the API payload (asserted in
    test_core_handlers_talk); this covers the rest of the chain, so the role
    cannot be dropped between the API boundary and the process that stamps the
    IncomingMessage.
    """
    import discord_voice_lifecycle

    posted: dict[str, dict] = {}

    monkeypatch.setattr(discord_voice_lifecycle, "_read_state", lambda: {"pid": 1})
    monkeypatch.setattr(discord_voice_lifecycle, "_is_alive", lambda _pid: True)
    monkeypatch.setattr(discord_voice_lifecycle, "_sweep_transcripts", lambda **_kw: None)
    monkeypatch.setattr(discord_voice_lifecycle, "_sidecar_status", lambda: None)
    monkeypatch.setattr(discord_voice_lifecycle, "_write_state", lambda _state: None)
    monkeypatch.setattr(discord_voice_lifecycle.shared, "file_lock", _null_lock)

    def fake_control_post(path: str, body: dict, timeout: float = 0.0):
        posted[path] = body
        return {"ok": True}

    monkeypatch.setattr(discord_voice_lifecycle, "_control_post", fake_control_post)

    discord_voice_lifecycle.start_session(
        guild_id=1, channel_id=2, text_channel_id=3, operator_role="admin"
    )

    assert posted["/join"]["operatorRole"] == "admin"

    # And the default is fail-closed, so a caller that forgets cannot mint admin.
    posted.clear()
    discord_voice_lifecycle.start_session(guild_id=1, channel_id=2)
    assert posted["/join"]["operatorRole"] == "viewer"


def test_voice_api_forwards_the_body_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTP boundary hands the role to the lifecycle rather than dropping it."""
    import discord_voice_api

    seen: dict = {}

    def fake_start(*, guild_id, channel_id, text_channel_id, operator_role):
        seen.update(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "text_channel_id": text_channel_id,
                "operator_role": operator_role,
            }
        )
        return {"status": "ready"}

    monkeypatch.setattr(discord_voice_api.discord_voice_lifecycle, "start_session", fake_start)

    body = discord_voice_api.DiscordVoiceJoinBody(
        guildId=1, channelId=2, textChannelId=3, operatorRole="admin"
    )
    discord_voice_api.join_voice(body)
    assert seen["operator_role"] == "admin"

    # Omitted -> fail-closed, not admin.
    assert discord_voice_api.DiscordVoiceJoinBody(guildId=1, channelId=2).operatorRole == "viewer"


# ── R5 BLOCKER: the join authorizes the SESSION, never another speaker ─────


@pytest.fixture
def voice_room(monkeypatch: pytest.MonkeyPatch):
    """The MAIN process serving Discord-voice tool calls.

    Returns `(speak, reached)`. `speak(user_id)` is one relayed tool call
    carrying that speaker on the wire — exactly the shape the sidecar POSTs —
    run through the REAL `execute_talk_tool` against the REAL command registry
    (real min_roles). `reached` records whether the admin-gated handler ran.

    Nothing here reads a module global for authority: that was the R6 blocker.
    The sidecar is a separate process, so its memory is not this one's, and the
    only thing that crosses is the request.
    """
    import talk_tools

    manager = _real_command_manager()
    reached: list[str] = []

    async def sentinel(_adapter, _incoming, _args, *, collect_only=False):
        reached.append("diagnostics")
        return "diagnostics ran"

    assert manager.get_command_min_role("diagnostics") == "admin"
    manager._commands["diagnostics"].handler = sentinel

    monkeypatch.setattr(talk_tools, "_COMMAND_MANAGER", manager)
    monkeypatch.setattr(talk_tools, "_COMMAND_MANAGER_FAILED", False)
    # A browser mint has already happened in this process — the exact
    # precondition that made the old bug hand admin to Discord speakers.
    monkeypatch.setattr(talk_tools, "_BROWSER_SESSION_ROLE", "admin")

    # Only 555 is on the Discord allowlist — the same seam the chat adapter
    # stamps from, read at call time in THIS process.
    import config

    monkeypatch.setattr(config, "DISCORD_ALLOWED_USERS", ["555"])

    def speak(user_id: int | None, *, bound: bool = True) -> str:
        # `bound` is the sidecar's interval verdict: it resolved this speaker
        # from the audio the model answered. The default is the ordinary case;
        # the R7 tests below pass bound=False for an utterance nobody can own.
        binding = {
            "token": "tok-test",
            "trusted": bool(bound and user_id is not None),
            "reason": "resolved immutable Discord user ID"
            if bound and user_id is not None
            else "ambiguous speakers",
        }
        return talk_tools.execute_talk_tool(
            "homie_command",
            {"command": "diagnostics", "args": ""},
            transport=talk_tools.TRANSPORT_DISCORD_VOICE,
            speaker_id=str(user_id) if user_id is not None else None,
            binding=binding,
        )

    return speak, reached


def test_the_opener_can_drive_their_own_voice_session(voice_room) -> None:
    """The allowlisted operator's spoken command still works."""
    speak, reached = voice_room

    assert speak(555) == "diagnostics ran"
    assert reached == ["diagnostics"]


def test_a_second_speaker_cannot_borrow_the_openers_admin(voice_room) -> None:
    """R5/R6 BLOCKER: a voice channel is a room, and `/talk join` is admin-gated,
    so the opener is an admin. Another member speaking an admin-gated command
    must not dispatch as them.

    R6 made this real across the PROCESS boundary: the sidecar knows the speaker
    but authorization happens here, so the id has to arrive on the request. The
    fixture also leaves a prior browser mint at `admin` — under the old code
    that global was what a Discord call read, which is precisely how a stranger
    got admin.
    """
    speak, reached = voice_room

    assert speak(555) == "diagnostics ran"
    reached.clear()

    # R8 moved WHERE this refusal happens: the chokepoint now enforces the
    # resolved role before dispatch, so the stranger no longer reaches the
    # command registry's own "Permission denied". The property under test is
    # unchanged and is the `reached` assertion — no admin handler ran.
    assert "was not run" in speak(999)
    assert reached == [], "an unallowlisted speaker reached an admin handler"

    # Per-utterance, not a session-wide downgrade.
    assert speak(555) == "diagnostics ran"


def test_an_unidentified_speaker_is_a_viewer(voice_room) -> None:
    """No id on the wire is not an operator — even with a browser mint at admin."""
    speak, reached = voice_room

    assert "was not run" in speak(None)
    assert reached == []


def test_an_unbound_utterance_is_refused_even_when_it_names_an_admin(voice_room) -> None:
    """R7: naming a speaker is not the same as proving they said it.

    The sidecar names 555 — the allowlisted operator — but reports that it
    could not tie that id to the utterance the model answered (two people
    talked over each other). Authorizing off the NAME alone is exactly the
    race: a stranger authors the command, an operator's stray packet supplies
    the id. The main process must refuse from the request it was handed.
    """
    speak, reached = voice_room

    denial = speak(555, bound=False)

    assert "was not run" in denial
    assert "Read-only requests are still available" in denial
    assert reached == [], "an unbound utterance reached an admin handler"
    # ...and the same speaker on a clean turn still works.
    assert speak(555) == "diagnostics ran"


def test_read_only_voice_tools_survive_a_lost_binding(monkeypatch) -> None:
    """Losing attribution costs mutation, not the assistant.

    hermes-talk's shape: the operator can still ask what is on the calendar in
    a noisy room; only the tools that spend or change something refuse.
    """
    import talk_tools

    monkeypatch.setitem(
        talk_tools._HANDLERS, "calendar_events", lambda _args: "standup at 9"
    )

    assert (
        talk_tools.execute_talk_tool(
            "calendar_events",
            {},
            transport=talk_tools.TRANSPORT_DISCORD_VOICE,
            speaker_id=None,
            binding={"trusted": False, "reason": "ambiguous speakers"},
        )
        == "standup at 9"
    )


def test_every_talk_tool_is_classified_read_only_or_mutating() -> None:
    """Completeness invariant, same shape as the route-policy CI check.

    Unclassified is ENFORCED as mutating at runtime, which is safe but silent —
    without this test a tool could sit in the strict bucket by accident forever
    and nobody would learn whether that was the intent. Every registered tool
    belongs to exactly one set, chosen deliberately.
    """
    import talk_tools

    classified = talk_tools.READ_ONLY_TALK_TOOLS | talk_tools.MUTATING_TALK_TOOLS

    assert set(talk_tools._HANDLERS) == classified
    assert not (talk_tools.READ_ONLY_TALK_TOOLS & talk_tools.MUTATING_TALK_TOOLS)


def test_an_unclassified_voice_tool_is_enforced_as_mutating(monkeypatch) -> None:
    """R8: the runtime half — a missing classification picks the STRICT bucket.

    A new tool nobody classified must not reach the room on a stranger's word.
    It inherits the mutating rules (trusted binding AND an authorized role)
    rather than a free pass; the completeness test above is what turns the
    omission itself into a failing build.
    """
    import talk_tools

    ran: list[str] = []
    monkeypatch.setitem(
        talk_tools._HANDLERS,
        "future_state_changer",
        lambda _a: ran.append("x") or "ran",
    )
    monkeypatch.setattr(_config_module(), "DISCORD_ALLOWED_USERS", ["555"])
    trusted = {"trusted": True, "reason": "resolved immutable Discord user ID"}

    def call(speaker: str) -> str:
        return talk_tools.execute_talk_tool(
            "future_state_changer",
            {},
            transport=talk_tools.TRANSPORT_DISCORD_VOICE,
            speaker_id=speaker,
            binding=trusted,
        )

    assert "was not run" in call("999"), "a stranger ran an unclassified tool"
    assert ran == []
    # The operator still gets it — strict, not bricked.
    assert call("555") == "ran"
    assert ran == ["x"]


def _config_module():
    import config

    return config


# ── R8 BLOCKER: the resolved role must be ENFORCED, not merely resolved ─────


@pytest.fixture
def voice_tool_probe(monkeypatch: pytest.MonkeyPatch):
    """One mutating tool with a sentinel handler, driven through the REAL gate.

    Returns `(call, ran)`. `call(speaker_id)` is a Discord-voice tool call with
    a fully TRUSTED binding — the sidecar could prove exactly who spoke — so
    the only thing standing between the caller and the handler is the resolved
    role. `ran` records whether the handler executed.
    """
    import talk_tools

    ran: list[str] = []
    monkeypatch.setitem(
        talk_tools._HANDLERS, "run_shell", lambda _a: ran.append("run_shell") or "done"
    )
    monkeypatch.setattr(_config_module(), "DISCORD_ALLOWED_USERS", ["555"])
    # A browser Talk session already minted admin in this process — the standing
    # precondition that has turned every previous gap in this chain into
    # stranger-admin.
    monkeypatch.setattr(talk_tools, "_BROWSER_SESSION_ROLE", "admin")

    def call(speaker_id: str | None) -> str:
        return talk_tools.execute_talk_tool(
            "run_shell",
            {"command": "echo hi"},
            transport=talk_tools.TRANSPORT_DISCORD_VOICE,
            speaker_id=speaker_id,
            binding={
                "token": "tok-test",
                "trusted": True,
                "reason": "resolved immutable Discord user ID",
            },
        )

    return call, ran


def test_a_trusted_stranger_cannot_run_a_mutating_tool(voice_tool_probe) -> None:
    """THE R8 BLOCKER, reproduced.

    Attribution is perfect: the sidecar bound this call to speaker 999 and can
    prove it. 999 is simply not on the allowlist, so `resolve_request_role`
    returns `viewer` — and before this fix that verdict went into a ContextVar
    that only `_handle_homie_command` ever read. `run_shell` and its six
    siblings executed anyway. Proving who spoke is worth nothing if nothing
    checks what they may do.
    """
    call, ran = voice_tool_probe

    denial = call("999")

    assert ran == [], "a viewer-resolved speaker executed a mutating tool"
    assert "was not run" in denial
    assert "Read-only requests are still available" in denial
    # The refusal must not leak who IS allowed.
    assert "555" not in denial


def test_the_allowlisted_operator_still_runs_the_same_tool(voice_tool_probe) -> None:
    """The other direction — enforcement that refuses everyone is not a fix."""
    call, ran = voice_tool_probe

    assert call("555") == "done"
    assert ran == ["run_shell"]


def test_the_denial_names_the_authority_problem_not_the_attribution_one(
    voice_tool_probe,
) -> None:
    """Two different failures deserve two different sentences.

    A stranger IS identified — telling them we could not verify which speaker
    asked would be false, and would send the operator chasing a mic problem
    that does not exist.
    """
    import talk_tools

    call, _ran = voice_tool_probe

    assert call("999") == talk_tools.ROLE_DENIAL.format(tool="run_shell")
    assert talk_tools.ROLE_DENIAL != talk_tools.MUTATION_DENIAL


@pytest.mark.parametrize(
    "tool",
    sorted(
        {
            "run_python",
            "run_shell",
            "delegate_task",
            "run_skill",
            "run_archon",
            "computer",
            "manage_run",
        }
    ),
)
def test_every_receipted_direct_handler_is_gated(monkeypatch, tool) -> None:
    """All seven handlers codex receipted, each proven at the chokepoint.

    They are covered as a set rather than by seven copies of the check, which
    is the point: the eighth handler someone adds is gated the day it lands.
    """
    import talk_tools

    ran: list[str] = []
    monkeypatch.setitem(talk_tools._HANDLERS, tool, lambda _a: ran.append(tool) or "ok")
    monkeypatch.setattr(_config_module(), "DISCORD_ALLOWED_USERS", ["555"])

    output = talk_tools.execute_talk_tool(
        tool,
        {},
        transport=talk_tools.TRANSPORT_DISCORD_VOICE,
        speaker_id="999",
        binding={"trusted": True, "reason": "resolved immutable Discord user ID"},
    )

    assert ran == [], f"{tool} executed for a viewer-resolved speaker"
    assert "was not run" in output


def test_the_browser_transport_is_deliberately_unaffected(monkeypatch) -> None:
    """The scoping is a decision, not an oversight.

    Discord voice is the surface where ONE authorized session serves MANY
    identities, which is why the role is resolved per call there. The browser
    page has a single participant and authorizes at session mint; both
    `/api/talk/session` and `/api/talk/tool` are admin-classified in
    `orchestration/route_policy.py`, so a browser call has already cleared an
    admin route gate. Gating it again here would refuse the operator's own page
    for a bar it never claimed to meet.
    """
    import talk_tools

    ran: list[str] = []
    monkeypatch.setitem(
        talk_tools._HANDLERS, "run_shell", lambda _a: ran.append("x") or "done"
    )

    assert talk_tools.execute_talk_tool("run_shell", {}) == "done"
    assert ran == ["x"]


def test_a_browser_mint_cannot_leak_into_a_discord_call() -> None:
    """The global is scoped to its transport BY NAME.

    The browser session role is real authority for the browser page, and
    meaningless for a voice channel. Asserted directly on the resolver so the
    scoping cannot quietly regress into "whatever was set last".
    """
    import talk_tools

    try:
        talk_tools.set_browser_session_role("admin")

        assert (
            talk_tools.resolve_request_role(
                transport=talk_tools.TRANSPORT_BROWSER, speaker_id=None
            )
            == "admin"
        )
        # Same process, same instant, Discord transport: the mint is invisible.
        assert (
            talk_tools.resolve_request_role(
                transport=talk_tools.TRANSPORT_DISCORD_VOICE, speaker_id=None
            )
            == "viewer"
        )
        # A named speaker with no interval binding is not a free pass either —
        # the sidecar always sends one, so its absence means an unknown client.
        assert (
            talk_tools.resolve_request_role(
                transport=talk_tools.TRANSPORT_DISCORD_VOICE, speaker_id="555"
            )
            == "viewer"
        )
        assert (
            talk_tools.resolve_request_role(
                transport=talk_tools.TRANSPORT_DISCORD_VOICE,
                speaker_id="555",
                binding={"trusted": False, "reason": "ambiguous speakers"},
            )
            == "viewer"
        )
        # An unrecognized transport is not a free pass either.
        assert talk_tools.resolve_request_role(transport="bogus", speaker_id="555") == "viewer"
    finally:
        talk_tools.end_talk_session()


@pytest.mark.parametrize("bogus", ["", None, "root", "ADMIN; DROP", "superuser"])
def test_unknown_browser_session_roles_fail_closed(bogus) -> None:
    """The setter is an authorization boundary: anything it does not recognize
    becomes `viewer`, never the value it was handed."""
    import talk_tools

    try:
        assert talk_tools.set_browser_session_role(bogus) == "viewer"
        assert (
            talk_tools.resolve_request_role(
                transport=talk_tools.TRANSPORT_BROWSER, speaker_id=None
            )
            == "viewer"
        )
    finally:
        talk_tools.end_talk_session()


def test_the_request_role_does_not_outlive_its_call(voice_room) -> None:
    """The bound identity is per-call, so one caller cannot inherit another's.

    This process serves both transports concurrently; a module global here was
    the original defect, and a ContextVar that leaked would reintroduce it.
    """
    import talk_tools

    speak, _reached = voice_room

    speak(555)  # an admin call completes ...
    # ... and leaves nothing behind for the next one.
    assert talk_tools._REQUEST_ROLE.get() == "viewer"
