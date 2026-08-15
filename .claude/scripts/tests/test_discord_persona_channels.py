"""Discord channel bindings route to real persona profile context."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

from discord_channel_bindings import (  # noqa: E402
    DiscordChannelBinding,
    load_discord_channel_bindings,
    resolve_discord_channel_binding,
    watched_channel_ids,
)
from discord_persona_runtime import run_discord_persona_channel_turn  # noqa: E402
from models import Channel, IncomingMessage, Platform, Thread, User  # noqa: E402
from session import get_session_store  # noqa: E402

from personas.discord_bindings import (  # noqa: E402
    DiscordBindingError,
    load_binding_document,
    reconcile_persona_bindings,
)
from runtime.base import RuntimeResult  # noqa: E402
from runtime.errors import RuntimeCallerToolTransportError  # noqa: E402


def _write_profile(homie_root: Path, persona_id: str) -> Path:
    profile_root = homie_root / "profiles" / persona_id
    memory_dir = profile_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (profile_root / "run").mkdir(parents=True, exist_ok=True)
    (profile_root / "skills").mkdir(parents=True, exist_ok=True)
    (profile_root / "config.yaml").write_text(
        "\n".join(
            [
                "persona:",
                f"  display_name: {persona_id.title()} Homie",
                f"  role: {persona_id} role marker",
                "cabinet:",
                "  tools: []",
                "  voice_persona_prompt: |",
                f"    {persona_id.upper()}_VOICE_PROMPT",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (memory_dir / "SOUL.md").write_text(
        f"# Soul\n{persona_id.upper()}_SOUL_MARKER", encoding="utf-8"
    )
    (memory_dir / "MEMORY.md").write_text(
        f"# Memory\n{persona_id.upper()}_MEMORY_MARKER", encoding="utf-8"
    )
    return profile_root


def _incoming(channel_id: str, guild_id: str = "guild-1") -> IncomingMessage:
    return IncomingMessage(
        text="what should we do next?",
        user=User(Platform.DISCORD, "user-1", "Operator"),
        channel=Channel(Platform.DISCORD, channel_id, is_dm=False),
        platform=Platform.DISCORD,
        thread=Thread(channel_id),
        raw_event={"guild": guild_id},
    )


def test_load_bindings_and_watched_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_file = tmp_path / "bindings.json"
    binding_file.write_text(
        json.dumps(
            {
                "guild_id": "guild-1",
                "channels": {
                    "1": {"name": "default", "kind": "default"},
                    "2": {"name": "sales", "persona": "sales"},
                    "4": {
                        "name": "staged",
                        "kind": "persona",
                        "persona": "staged",
                        "enabled": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_CHANNEL_BINDINGS_FILE", str(binding_file))
    monkeypatch.setenv("DISCORD_WATCHED_CHANNELS", "3")

    bindings = load_discord_channel_bindings()
    assert bindings["2"].persona_id == "sales"
    assert watched_channel_ids() == ["1", "2", "3"]
    assert resolve_discord_channel_binding(_incoming("1")) is None
    assert resolve_discord_channel_binding(_incoming("2")).persona_id == "sales"
    assert resolve_discord_channel_binding(_incoming("4")) is None
    assert resolve_discord_channel_binding(_incoming("2", guild_id="other")) is None


def test_strict_mutation_reader_refuses_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "bindings.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(DiscordBindingError, match="invalid Discord"):
        load_binding_document(path, strict=True)
    assert load_discord_channel_bindings(path) == {}


def test_fail_soft_reader_skips_only_the_malformed_row(tmp_path: Path) -> None:
    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "channels": {
                    "1": {"persona": "sales"},
                    "2": "malformed",
                }
            }
        ),
        encoding="utf-8",
    )

    assert set(load_discord_channel_bindings(path)) == {"1"}


def test_binding_reconcile_preserves_unknown_and_guild_fields() -> None:
    document = {
        "guild_id": "guild-1",
        "operator_note": "keep",
        "channels": {
            "2": {
                "kind": "persona",
                "persona": "sales",
                "guild_id": "guild-override",
                "custom": {"keep": True},
            }
        },
    }
    updated = reconcile_persona_bindings(
        document,
        persona_id="sales",
        channels=[
            type(
                "ChannelIntent",
                (),
                {"kind": "discord", "channel_id": "2", "name": "sales-room"},
            )()
        ],
    )

    assert updated["guild_id"] == "guild-1"
    assert updated["operator_note"] == "keep"
    assert updated["channels"]["2"]["guild_id"] == "guild-override"
    assert updated["channels"]["2"]["custom"] == {"keep": True}
    assert updated["channels"]["2"]["name"] == "sales-room"
    assert "enabled" not in updated["channels"]["2"]


def test_binding_reconcile_preserves_legacy_ownership_and_removes_legacy_rows() -> None:
    owned = {"channels": {"2": {"persona": "other"}}}
    intent = type(
        "ChannelIntent",
        (),
        {"kind": "discord", "channel_id": "2", "name": "sales-room"},
    )()
    with pytest.raises(DiscordBindingError, match="already bound"):
        reconcile_persona_bindings(
            owned,
            persona_id="sales",
            channels=[intent],
        )

    legacy = {"channels": {"2": {"persona": "sales"}}}
    removed = reconcile_persona_bindings(
        legacy,
        persona_id="sales",
        channels=[],
    )
    assert removed["channels"] == {}


@pytest.mark.asyncio
async def test_bound_channel_turn_uses_target_profile_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    matrix_path = tmp_path / "persona-capability-matrix.yaml"
    matrix_path.write_text(
        "\n".join(
            [
                "env_groups:",
                "  runtime_core: [OPENAI_API_KEY]",
                "skill_groups:",
                "  sales_lane: [sales-skill]",
                "profiles:",
                "  sales:",
                "    env_groups: [runtime_core]",
                "    skill_groups: [sales_lane]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMIE_PERSONA_CAPABILITY_MATRIX", str(matrix_path))
    profile_root = _write_profile(homie_root, "sales")
    skills_root = tmp_path / ".claude" / "skills"
    for skill_name in ("sales-skill", "marketing-skill"):
        skill_dir = skills_root / skill_name
        skill_dir.mkdir(parents=True)
        skill_dir.joinpath("SKILL.md").write_text(
            (
                "---\n"
                f"name: {skill_name}\n"
                f"description: {skill_name} description\n"
                "---\n"
            ),
            encoding="utf-8",
        )
    db_path = tmp_path / "chat.db"
    store = get_session_store(db_path)
    captured = []
    observed_progress: list[str] = []
    progress: dict[str, object] = {}

    async def fake_run(req):
        captured.append(req)
        observed_progress.append(str(progress.get("status") or ""))
        return RuntimeResult(
            text="sales answer",
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
            profile_key="test-profile",
            session_id="runtime-1",
        )

    binding = load_discord_channel_bindings(
        path=tmp_path / "missing.json"
    ).get("nope")
    assert binding is None
    incoming = _incoming("2")
    incoming.prefetched_context = "# Crypto Desk Live Snapshot\nOpen plays: 1"
    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        outgoing = await run_discord_persona_channel_turn(
            incoming=incoming,
            binding=DiscordChannelBinding(
                channel_id="2",
                name="sales",
                kind="persona",
                persona_id="sales",
                guild_id="guild-1",
            ),
            session_store=store,
            project_root=tmp_path,
            progress=progress,
        )

    assert outgoing.text == "sales answer"
    request = captured[0]
    assert request.env["HOMIE_HOME"] == str(profile_root)
    assert request.metadata["persona_id"] == "sales"
    assert len(captured) == 1
    assert "The data below was already gathered via direct API calls." in request.prompt
    assert "Do NOT run any commands, tools, or scripts" in request.prompt
    assert "# Crypto Desk Live Snapshot\nOpen plays: 1" in request.prompt
    assert "SALES_SOUL_MARKER" in request.system_prompt
    assert "SALES_MEMORY_MARKER" in request.system_prompt
    assert "SALES_VOICE_PROMPT" in request.system_prompt
    assert "sales-skill" in request.system_prompt
    assert "marketing-skill" not in request.system_prompt
    assert "dedicated Discord channel `#sales`" in request.system_prompt
    assert request.allowed_tools == []
    assert request.disallowed_tools == ["*"]
    assert observed_progress == ["Sales Homie is reasoning"]
    assert progress["status"] == "Sales Homie is reasoning"
    assert progress["tool_calls"] == 0
    assert "current_tool" not in progress
    session = store.get("discord", "2", "2")
    assert session is not None
    assert session.runtime_profile_key == "test-profile"
    assert [m.role for m in store.list_messages(session.session_id)] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_tool_transport_failure_retries_once_as_declared_text_only_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    _write_profile(homie_root, "sales")
    store = get_session_store(tmp_path / "chat.db")
    captured = []
    dispatched = []
    definition = {
        "type": "function",
        "function": {
            "name": "safe_lookup",
            "description": "Read a harmless scoped value.",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    async def fake_run(request):
        captured.append(request)
        if len(captured) == 1:
            raise RuntimeCallerToolTransportError("no safe caller-tool lane")
        return RuntimeResult(
            text="I can still talk, but I did not run anything.",
            runtime_lane="generic_runtime",
            provider="openai-codex",
            model="gpt-5.6-sol",
            profile_key="primary-openai-codex",
        )

    with (
        patch(
            "runtime.persona_tools.build_persona_tool_payload",
            return_value=(
                [definition],
                lambda name, arguments: dispatched.append((name, arguments)),
            ),
        ),
        patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run),
    ):
        outgoing = await run_discord_persona_channel_turn(
            incoming=_incoming("2"),
            binding=DiscordChannelBinding(
                channel_id="2",
                name="sales",
                kind="persona",
                persona_id="sales",
                guild_id="guild-1",
            ),
            session_store=store,
            project_root=tmp_path,
        )

    assert len(captured) == 2
    assert captured[0].tool_defs == [definition]
    assert captured[1].tool_defs is None
    assert captured[1].tool_dispatch is None
    assert captured[1].tool_scope_version is None
    assert captured[1].metadata["caller_tools_degraded"] is True
    assert "Do not claim" in captured[1].prompt
    assert dispatched == []
    assert "no tool action was performed" in outgoing.text


@pytest.mark.asyncio
async def test_counter_offer_marker_reaches_a_real_tap_that_grants_and_the_next_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #428's acceptance bar, driven through REAL production wiring.

    Round-2 confirmed this test's prior shape was vacuous: it hard-coded both
    model replies, called ``core_handlers.decide_grant_proposal`` directly
    instead of routing an authenticated adapter BUTTON tap, never read the
    persisted transcript, and never inspected the second turn's actual
    resolved tool scope. Deleting the guidance, the router button wiring, or
    the granted-toolset resolution could all still pass the old assertions.
    This version closes each gap:

    1. The approve tap goes through ``router.ChatRouter._handle_button`` on a
       REAL ``ChatRouter`` holding the SAME session store the persona turns
       write to — the same entrypoint (and the same custom-id prefix routing)
       a real Discord button interaction reaches — with a real
       ``IncomingMessage`` carrying the button provenance markers
       (``interaction_type``/``source_message_is_own``) and ``user_role``
       stamped the way the canonical ingress seam
       (``models.resolve_ingress_role``, #424/#449) stamps a real
       ``DiscordAdapter`` button interaction: "viewer" for a sender off
       ``DISCORD_ALLOWED_USERS``, "admin" for one on it.
    2. The persisted transcript (``session_store.list_messages``) is read
       back and asserted to carry the WHOLE sequence the ticket's acceptance
       demands — counter-offer card → refused tap receipt → authenticated
       approval receipt naming the granted toolset → the completed task — in
       that order, marker-free. Round 3 caught the prior shape here: the tap
       replied and mutated config but persisted NOTHING, so the durable
       transcript jumped from the card straight to the later task and the
       approval that caused the grant existed only in the ledger. Reverting
       the router's persist call empties the receipts and fails this leg.
    3. Turn 2's captured ``RuntimeRequest.tool_defs`` is inspected for a real
       ``research_read`` tool (the intersection with the toolset's own
       registered tool list, not one hardcoded name — which of that list's
       providers register successfully depends on test-env credentials) —
       proving the grant is LIVE in the next turn's real resolved scope, not
       merely that the reply text differs. Turn 1's tool_defs is asserted to
       carry NONE of them, so the turn-2 assertion cannot pass because the
       tool was already there for an unrelated reason.
    """
    import router as router_mod
    from personas import grant_proposals
    from runtime.base import RuntimeResult
    from runtime.toolsets import TOOLSETS

    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_GRANT_PROPOSALS", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    _write_profile(homie_root, "sales")
    store = get_session_store(tmp_path / "chat.db")
    binding = DiscordChannelBinding(
        channel_id="2",
        name="sales",
        kind="persona",
        persona_id="sales",
        guild_id="guild-1",
    )
    replies = iter(
        [
            "I can't pull that without web research. <<GRANT_REQUEST: research_read>>",
            "Done — pulled the competitor pages.",
        ]
    )
    captured_requests: list = []

    async def fake_run(request):
        captured_requests.append(request)
        return RuntimeResult(
            text=next(replies),
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
            profile_key="test-profile",
        )

    # Turn 1: the persona's reply carries the marker. Its own tool scope
    # must NOT already carry the toolset being asked for.
    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        first_turn = await run_discord_persona_channel_turn(
            incoming=_incoming("2"),
            binding=binding,
            session_store=store,
            project_root=tmp_path,
        )

    def _tool_names(request) -> set[str]:
        return {
            (td.get("function") or {}).get("name")
            for td in (request.tool_defs or [])
        }

    research_read_tools = set(TOOLSETS["research_read"]["tools"])
    assert not (_tool_names(captured_requests[0]) & research_read_tools)
    assert "<<GRANT_REQUEST" not in first_turn.text
    assert "research_read" in first_turn.text
    approve = next(
        (c for c in first_turn.components if "approve" in c.custom_id), None
    )
    assert approve is not None
    action, persona_id, code = grant_proposals.parse_custom_id(approve.custom_id)
    assert (action, persona_id) == (grant_proposals.ACTION_APPROVE, "sales")

    # A real router over the REAL session store the persona turns write to,
    # so the tap's persistence lands in the same transcript (and runs the
    # real `_persist_router_turn` body) instead of being swallowed by a
    # router built with `object.__new__`, which has no engine at all.
    class _TapManager:
        def get_all_command_names(self):
            return ["grant"]

    class _TapEngine:
        def __init__(self, session_store):
            self.session_store = session_store

    tap_router = router_mod.ChatRouter(_TapEngine(store), _TapManager())

    async def _tap(user_id: str, user_role: str) -> str:
        sent: list = []

        class _Adapter:
            async def send(self, outgoing):
                sent.append(outgoing)

        incoming = IncomingMessage(
            text=f"__button:{approve.custom_id}",
            user=User(Platform.DISCORD, user_id),
            channel=Channel(Platform.DISCORD, "2"),
            platform=Platform.DISCORD,
            thread=Thread("2"),
            # What the REAL DiscordAdapter stamps at ingress via
            # `resolve_ingress_role` for a button interaction it admitted —
            # the router trusts this field verbatim (round-2 BLOCKER fix).
            user_role=user_role,
            raw_event={"interaction_type": "button", "source_message_is_own": True},
        )
        await tap_router._handle_button(_Adapter(), incoming, approve.custom_id)
        return sent[-1].text

    # A non-operator's authenticated tap (the seam stamped "viewer" — off
    # DISCORD_ALLOWED_USERS) is refused through the router's real button path.
    stranger_reply = await _tap("not-the-operator", "viewer")
    assert "admin" in stranger_reply.lower()

    # The configured operator's tap (the seam stamped "admin") reaches the
    # #426 executor through the SAME router entrypoint and grants.
    operator_reply = await _tap("operator-1", "admin")
    assert "Granted" in operator_reply
    import personas

    live_config = personas.load_persona_config("sales")
    assert "research_read" in live_config.get("toolsets", [])

    # The persisted transcript — not just the in-memory reply — is
    # marker-free and carries the approve/deny card.
    from session_keys import build_session_key

    session_key = build_session_key("discord", "2", "2")
    persisted = store.list_messages(session_key)
    assistant_turn_1 = next(m for m in persisted if m.role == "assistant")
    assert "<<GRANT_REQUEST" not in assistant_turn_1.content
    assert f"/grant approve sales {code}" in assistant_turn_1.content

    # Both taps left DURABLE, sanitized receipts in that same transcript, in
    # order, so the session's own history explains why the persona's reach
    # grew: the card, the refused stranger tap, then the authenticated
    # approval naming who approved and what landed.
    contents = [m.content for m in persisted]
    card_at = contents.index(assistant_turn_1.content)
    refused_at = next(
        i for i, c in enumerate(contents) if "[grant receipt]" in c and "outcome=refused" in c
    )
    granted_at = next(
        i for i, c in enumerate(contents) if "[grant receipt]" in c and "outcome=granted" in c
    )
    assert card_at < refused_at < granted_at
    assert f"[server command] grant approve -> persona=sales code={code}" in contents
    assert "role=viewer" in contents[refused_at]
    granted_receipt = contents[granted_at]
    for field in (
        "decision=approve",
        "persona=sales",
        "toolset=research_read",
        f"code={code}",
        "by=operator-1",
        "role=admin",
    ):
        assert field in granted_receipt
    # The receipt is what persists — never the operator-facing reply, which
    # can carry an executor exception or the live toolset list into a prompt
    # that `recent_conversation` later replays.
    assert not any("Toolsets now:" in c for c in contents)

    # Turn 2: the channel keeps working — task-completed, no repeated card,
    # and the grant is LIVE in the real resolved tool scope for this turn.
    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        second_turn = await run_discord_persona_channel_turn(
            incoming=_incoming("2"),
            binding=binding,
            session_store=store,
            project_root=tmp_path,
        )
    assert second_turn.text == "Done — pulled the competitor pages."
    assert second_turn.components == []
    assert _tool_names(captured_requests[1]) & research_read_tools

    # The full acceptance sequence, in one durable transcript:
    # counter-offer → authenticated approval receipt → task completed.
    final = [m.content for m in store.list_messages(session_key)]
    assert final.index(second_turn.text) > final.index(granted_receipt)
