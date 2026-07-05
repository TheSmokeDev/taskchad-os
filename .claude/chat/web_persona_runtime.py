"""Runtime path for dashboard/mobile conversations scoped to a Homie persona.

M5 (Homie Mobile persona switcher): `/api/conversation/{persona_id}/send` used to
run EVERY turn through the shared dashboard router with the default identity —
the path persona only labeled SSE events. This module gives a non-default
persona a real turn: resolve its profile, answer as it (no tools, same
default-deny posture as Discord persona channels and Cabinet participants),
and persist with `persona_id` attribution so persona turns never contaminate
the main operator-belief corpus (the Act 5 Discord bug class).

Mirrors `discord_persona_runtime.run_discord_persona_channel_turn`; the
persistence and recent-context helpers are shared imports from that module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from discord_persona_runtime import _persist_turn, _recent_conversation_block
from models import IncomingMessage
from session_keys import build_session_key, resolve_thread_id


def _web_persona_system_prompt(
    *,
    persona_id: str,
    display_name: str,
    role: str,
    profile_context: str,
    persona_prompt: str,
    skill_index: str,
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
            "If the request needs a gated workflow, name the exact workflow or approval needed."
        ),
    ]
    if role:
        blocks.append("# Persona Role\n" + role.strip())
    if profile_context:
        blocks.append("# Persona Memory Context\n" + profile_context.strip())
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
    try:
        skill_index = build_skill_index(
            project_root / ".claude" / "skills",
            allowlist=resolve_skill_allowlist(persona_id),
            extra_skill_dirs=[paths["skills"]],
        )
    except Exception:
        skill_index = ""
    system_prompt = _web_persona_system_prompt(
        persona_id=persona_id,
        display_name=display_name,
        role=role,
        profile_context=profile_context,
        persona_prompt=persona_prompt,
        skill_index=skill_index,
    )

    platform_str = incoming.platform.value
    channel_id = incoming.channel.platform_id
    thread_id = resolve_thread_id(
        channel_id,
        incoming.thread.thread_id if incoming.thread else None,
    )
    session_key = build_session_key(platform_str, channel_id, thread_id)
    recent = _recent_conversation_block(session_store, session_key)
    prompt_parts = []
    if recent:
        prompt_parts.append(recent)
    if incoming.prefetched_context:
        prompt_parts.append("# Prefetched Context\n" + incoming.prefetched_context)
    prompt_parts.append("# Current User Message\n" + incoming.text.strip())
    prompt = "\n\n".join(prompt_parts)

    request = RuntimeRequest(
        prompt=prompt,
        cwd=project_root,
        task_name="web_persona_turn",
        capability=TEXT_REASONING,
        conversational=True,
        max_turns=1,
        allowed_tools=[],
        disallowed_tools=["*"],
        permission_mode="bypassPermissions",
        allow_fallback=True,
        env=build_capability_scoped_env(persona_id, profile_root=info.path),
        system_prompt=system_prompt,
        metadata={
            "caller": "web_persona_chat",
            "persona_id": persona_id,
            "conversation_id": channel_id,
        },
    )
    result = await run_with_runtime_lanes(request)
    response_text = (result.text or "").strip() or "No response returned."

    _persist_turn(
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
