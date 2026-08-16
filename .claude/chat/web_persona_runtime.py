"""Runtime path for dashboard/mobile conversations scoped to a Homie persona.

M5 (Homie Mobile persona switcher): `/api/conversation/{persona_id}/send` used to
run EVERY turn through the shared dashboard router with the default identity —
the path persona only labeled SSE events. This module gives a non-default
persona a real turn: resolve its profile, answer as it, and persist with
`persona_id` attribution so persona turns never contaminate the main
operator-belief corpus (the Act 5 Discord bug class).

Epic #465 1b: the turn now runs the SAME scoped tool loop as the Discord
persona path — config-scoped caller tools with a multi-turn loop, one-time
elevation, the counter-offer proposal, and the pending-request card (text
appended to the reply; this surface has no buttons). SDK built-ins stay
default-deny (`allowed_tools=[]` / `disallowed_tools=["*"]`) exactly like
Discord: scoped tools ride the caller-tools path, never the built-in surface.

Mirrors `discord_persona_runtime.run_discord_persona_channel_turn`; the
persistence and recent-context helpers are shared imports from that module.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from discord_persona_runtime import (
    _PREFETCHED_CONTEXT_PREAMBLE,
    _PREFETCHED_CONTEXT_PREAMBLE_WITH_TOOLS,
    _incoming_display_text,
    _persist_turn,
    _persona_turn_max_turns,
    _recent_conversation_block,
)
from models import IncomingMessage
from session import get_persist_lock
from session_keys import build_session_key, resolve_thread_id


def _web_persona_system_prompt(
    *,
    persona_id: str,
    display_name: str,
    role: str,
    profile_context: str,
    recalled_memory: str,
    persona_prompt: str,
    skill_index: str,
    counter_offer_briefing: str = "",
) -> str:
    blocks = [
        "# Dashboard Persona Chat Contract",
        (
            f"You are `{persona_id}` ({display_name}) in a direct one-on-one chat "
            "with the operator (dashboard/mobile surface)."
        ),
        "Answer as this persona only. Do not say you are Main/default.",
        "Use the profile memory and role below as your brain for this turn.",
        "Stay useful and concrete. Ask a short clarifying question only when the next action is genuinely blocked.",
        (
            "Tools and browser/social writes are default-deny from this chat. "
            "If the task is blocked on a registered tool outside your scope, use "
            "`request_tool` with the exact intended arguments. Dedicated-gate actions "
            "still use their own workflow and can never be elevated."
        ),
        (
            "When the authenticated operator directly authorizes a tool or action, "
            "do not make them discover or relay an internal capability name. If a "
            "grant is still required, immediately call `request_tool` yourself so "
            "the real approval card appears, then continue after approval."
        ),
    ]
    if counter_offer_briefing:
        # Issue #428 — the counter-offer. Prompt guidance only: the marker
        # this teaches is parsed server-side into a PROPOSAL row, and no
        # reply the model can write reaches a config mutation.
        blocks.append(counter_offer_briefing.strip())
    if role:
        blocks.append("# Persona Role\n" + role.strip())
    if profile_context:
        blocks.append("# Persona Memory Context\n" + profile_context.strip())
    if recalled_memory:
        blocks.append("# Persona Recalled Memory\n" + recalled_memory.strip())
    if skill_index:
        blocks.append("# Persona Skill Index\n" + skill_index.strip())
    if persona_prompt:
        blocks.append("# Persona Voice Prompt\n" + persona_prompt.strip())
    return "\n\n".join(blocks)


async def run_web_persona_turn(
    *,
    incoming: IncomingMessage,
    persona_id: str,
    session_store: Any,
    project_root: Path,
) -> str:
    """Run one dashboard/mobile message as the named persona; return reply text."""

    import personas
    from cognition.skills import build_skill_index
    from personas.capabilities import (
        build_capability_scoped_env,
        resolve_skill_allowlist,
    )
    from personas.lifecycle import show_profile
    from runtime.base import RuntimeRequest
    from runtime.bootstrap import build_session_start_context
    from runtime.capabilities import TEXT_REASONING
    from runtime.errors import RuntimeCallerToolTransportError
    from runtime.lane_router import run_with_runtime_lanes

    info = show_profile(persona_id)
    cfg = personas.load_persona_config(persona_id)
    paths = personas.get_persona_paths(persona_id)
    persona_section = cfg.get("persona", {}) if isinstance(cfg.get("persona"), dict) else {}
    cabinet = cfg.get("cabinet", {}) if isinstance(cfg.get("cabinet"), dict) else {}
    display_name = (
        persona_section.get("display_name")
        or persona_section.get("name")
        or persona_id
    )
    role = persona_section.get("role") or ""
    persona_prompt = cabinet.get("voice_persona_prompt") or ""
    profile_context = build_session_start_context(
        "web_persona_chat",
        memory_dir=paths["memory"],
        daily_dir=paths["memory"] / "daily",
    ).strip()

    # Per-persona semantic recall (issue #110) — same as the Discord persona
    # path. ``memory_dir=paths["memory"]`` → config.resolve_db_path routes it to
    # ``~/.homie/profiles/<name>/data/memory.db`` (per-persona-unique, NEVER the
    # main vault). AUTO mode tier-gates cost; fail-open (failure OR empty index
    # → briefing-only turn). One-time bulk build: ``memory_index.py -p <name>``.
    recalled_memory = ""
    try:
        from recall_service import recall as recall_memory_service

        recall_response = await recall_memory_service(
            query=incoming.text,
            memory_dir=paths["memory"],
            caller="web_persona_chat",
            max_results=5,
            has_prefetched=bool(incoming.prefetched_context),
        )
        recalled_memory = recall_response.formatted_text or ""
    except Exception as exc:  # noqa: BLE001 — recall is best-effort, never turn-killing
        print(
            f"[{datetime.now()}] [WebPersonaRecall] "
            f"{persona_id}: recall failed (non-blocking): {exc}"
        )

    try:
        skill_index = build_skill_index(
            project_root / ".claude" / "skills",
            allowlist=resolve_skill_allowlist(persona_id),
            extra_skill_dirs=[paths["skills"]],
            reader_persona=persona_id,
        )
    except Exception:
        skill_index = ""
    # Issue #428 — teach the counter-offer. Reads the live toolset registry, so
    # it fails open to no guidance rather than costing the turn its prompt.
    try:
        from personas import grant_proposals as _grant_proposals

        counter_offer_briefing = _grant_proposals.counter_offer_briefing()
    except Exception as exc:  # noqa: BLE001 — guidance is additive
        print(
            f"[{datetime.now()}] [GrantProposals] briefing unavailable "
            f"(non-blocking): {type(exc).__name__}: {exc}",
            flush=True,
        )
        counter_offer_briefing = ""
    system_prompt = _web_persona_system_prompt(
        persona_id=persona_id,
        display_name=display_name,
        role=role,
        profile_context=profile_context,
        recalled_memory=recalled_memory,
        persona_prompt=persona_prompt,
        skill_index=skill_index,
        counter_offer_briefing=counter_offer_briefing,
    )

    platform_str = incoming.platform.value
    channel_id = incoming.channel.platform_id
    thread_id = resolve_thread_id(
        channel_id,
        incoming.thread.thread_id if incoming.thread else None,
    )
    session_key = build_session_key(platform_str, channel_id, thread_id)
    recent = _recent_conversation_block(session_store, session_key)

    from runtime import persona_elevation

    elevation_context = persona_elevation.build_turn_context(
        persona_id,
        incoming,
        session_key=session_key,
        project_root=project_root,
    )
    elevation_grant = None
    elevation_claim_error = ""
    raw_event = getattr(incoming, "raw_event", None)
    raw_event = raw_event if isinstance(raw_event, dict) else {}
    resume_request_id = str(raw_event.get("elevation_resume_request_id") or "").strip()
    if resume_request_id:
        elevation_grant, elevation_claim_error = persona_elevation.claim_grant(
            resume_request_id,
            persona_id=persona_id,
            platform=platform_str,
            channel_id=channel_id,
        )

    # Epic #465 1b — the web persona surface joins the scoped tool loop the
    # Discord path already runs. Resolved HERE rather than at the request,
    # because the prompt blocks below have to know whether tools exist before
    # they tell the persona what it may do.
    persona_tool_defs = None
    persona_tool_dispatch = None
    persona_scope_version = None
    try:
        from runtime.persona_tools import (
            PERSONA_CHAT_BASE_TOOLS,
            build_persona_tool_payload,
            persona_tool_scope_version,
        )

        _payload = build_persona_tool_payload(
            persona_id,
            cfg,
            request_context=elevation_context,
            elevation_grant=elevation_grant,
        )
        if _payload is not None:
            persona_tool_defs, persona_tool_dispatch = _payload
            persona_scope_version = persona_tool_scope_version(
                persona_id, persona_tool_defs
            )
    except Exception:  # noqa: BLE001 — a scope failure must never kill the turn
        print(
            f"[web_persona_runtime] tool scope resolution failed for "
            f"{persona_id}; answering without tools",
            flush=True,
        )

    # The authorization bridge can ask for a capability but cannot fetch or
    # mutate anything itself. Keep prefetch wording based on operational tools,
    # so adding the universal bridge does not falsely tell legacy personas they
    # already possess a data-gathering surface.
    has_operational_tools = any(
        str((definition.get("function") or {}).get("name") or "")
        not in PERSONA_CHAT_BASE_TOOLS
        for definition in (persona_tool_defs or [])
    )

    prompt_parts = []
    if recent:
        prompt_parts.append(recent)
    if incoming.prefetched_context:
        prompt_parts.append(
            "# Prefetched Context\n"
            + (
                _PREFETCHED_CONTEXT_PREAMBLE_WITH_TOOLS
                if has_operational_tools
                else _PREFETCHED_CONTEXT_PREAMBLE
            )
            + incoming.prefetched_context
        )
    if elevation_grant is not None:
        prompt_parts.append(
            "# One-Time Approved Capability\n"
            f"The operator approved exactly one `{elevation_grant.tool_name}` call for "
            "this retry. Call it once with these exact arguments, then complete the "
            "original task. Any different or second call will be refused.\n"
            + persona_elevation.canonical_arguments(
                elevation_grant.intended_arguments
            )
        )
    elif resume_request_id and elevation_claim_error:
        prompt_parts.append(
            "# One-Time Capability Unavailable\n"
            + elevation_claim_error
            + ". Do not claim the tool ran."
        )
    prompt_parts.append("# Current User Message\n" + incoming.text.strip())
    prompt = "\n\n".join(prompt_parts)

    request = RuntimeRequest(
        prompt=prompt,
        cwd=project_root,
        task_name="web_persona_turn",
        capability=TEXT_REASONING,
        conversational=True,
        # A tool loop needs room to call, read the result, and answer. One turn
        # is correct for the no-tools path and would truncate a persona
        # mid-investigation, so the bound moves only when tools exist.
        max_turns=_persona_turn_max_turns() if persona_tool_defs else 1,
        tool_defs=persona_tool_defs,
        tool_dispatch=persona_tool_dispatch,
        tool_scope_version=persona_scope_version,
        # `allowed_tools` stays EMPTY on purpose. It is the SDK-NATIVE tool
        # list; the scoped tools ride `tool_defs`/`tool_dispatch` (the
        # caller-tools path). Populating both would hand a scoped persona the
        # built-in surface as well — granting a toolset must never silently
        # also grant Bash.
        allowed_tools=[],
        disallowed_tools=["*"],
        permission_mode="bypassPermissions",
        allow_fallback=True,
        env=build_capability_scoped_env(persona_id, profile_root=info.path),
        system_prompt=system_prompt,
        metadata={
            "caller": "web_persona_chat",
            "persona_id": persona_id,
            **(
                {"tool_scope_version": persona_scope_version}
                if persona_scope_version is not None
                else {}
            ),
            "conversation_id": channel_id,
        },
    )
    tools_degraded = False
    try:
        result = await run_with_runtime_lanes(request)
    except RuntimeCallerToolTransportError as exc:
        # Persona chats are conversation surfaces first. If every selected
        # runtime refuses or loses the caller-tool transport, retry exactly once
        # as a declared text-only turn. This never supplies the dispatcher and
        # never claims an action happened; other runtime/config/security errors
        # still propagate normally.
        tools_degraded = True
        print(
            f"[web_persona_runtime] scoped tools unavailable for {persona_id}; "
            f"retrying text-only: {exc}",
            flush=True,
        )
        degraded_prompt = (
            "# Tool Availability\n"
            "Your scoped tools are unavailable for this turn. Respond "
            "conversationally from the context you already have. Do not claim "
            "you checked, changed, sent, searched, or executed anything. If the "
            "request requires a tool, say what could not be verified.\n\n"
            + prompt
        )
        degraded_metadata = dict(request.metadata or {})
        degraded_metadata.pop("tool_scope_version", None)
        degraded_metadata["caller_tools_degraded"] = True
        result = await run_with_runtime_lanes(
            replace(
                request,
                prompt=degraded_prompt,
                max_turns=1,
                tool_defs=None,
                tool_dispatch=None,
                tool_scope_version=None,
                metadata=degraded_metadata,
            )
        )
    response_text = (result.text or "").strip() or "No response returned."
    if tools_degraded:
        response_text += "\n\n_(Scoped tools were unavailable; no tool action was performed.)_"

    # ── Counter-offer (#428). Same flow as the Discord persona path: one
    # `<<GRANT_REQUEST: …>>` marker at the end of the reply becomes a PENDING
    # PROPOSAL plus a card. The marker is stripped here — before persistence,
    # so the transcript shows the reply the operator saw. `persona_id` comes
    # from the request path, never from the reply, so a persona can only ever
    # propose for itself. Sync sqlite, so it rides `to_thread`: the dashboard
    # never does blocking IO on its event loop.
    try:
        from personas import grant_proposals as _grant_proposals

        counter_offer = await asyncio.to_thread(
            _grant_proposals.tee_up_from_reply,
            persona_id,
            response_text,
            requested_by=str(getattr(incoming.user, "platform_id", "") or ""),
            trigger_text=_incoming_display_text(incoming),
            surface=platform_str,
            channel_id=channel_id,
            thread_id=thread_id,
        )
    except Exception as exc:  # noqa: BLE001 — an affordance never costs the answer
        print(
            f"[{datetime.now()}] [GrantProposals] counter-offer failed "
            f"(non-blocking): {type(exc).__name__}: {exc}",
            flush=True,
        )
        counter_offer = None
    if counter_offer is not None:
        response_text = (
            counter_offer.reply_text.rstrip() + "\n\n" + counter_offer.card_text
        ).strip()
        if counter_offer.approve_custom_id:
            # This surface has no buttons, and typed text in a persona
            # conversation never reaches the router's `/grant` handler — the
            # card's commands only land from the main chat. Same dead-end
            # failure mode the Cabinet wording in grant_proposals.card_text
            # names.
            response_text += (
                "\nRun the approve/deny command from the main chat — this "
                "persona conversation cannot decide it."
            )

    pending_elevation = persona_elevation.pending_request_for_turn(
        persona_id,
        str(elevation_context["turn_id"]),
    )
    if pending_elevation is not None:
        response_text = (
            response_text.rstrip()
            + "\n\n"
            + persona_elevation.request_card_text(pending_elevation)
            # No `capability:approve:` buttons exist on this surface and typed
            # text can never synthesize an approval (the router's provenance
            # gate), so name where the decision actually happens.
            + "\nThis chat cannot approve it — decide it where the bot's "
            "approval buttons live (Telegram/Discord)."
        )

    # Serialize + offload the sync persist off the DASHBOARD process loop under
    # this process's own per-conversation lock (#131) — same correctness class
    # as the Discord path, zero dashboard_api edits.
    async with get_persist_lock(session_key):
        await asyncio.to_thread(
            _persist_turn,
            session_store=session_store,
            incoming=incoming,
            response_text=response_text,
            result=result,
            session_key=session_key,
            platform_str=platform_str,
            channel_id=channel_id,
            thread_id=thread_id,
            persona_id=persona_id,
        )
    return response_text


__all__ = ["run_web_persona_turn"]
