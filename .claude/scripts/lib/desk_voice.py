"""Let the desk SPEAK, and let it stay quiet.

The card chrome is a template and its content came from a standalone analyst
prompt that never loaded the persona's SOUL.md -- so there was no code path
where the crypto homie's actual voice reached Discord unless the operator spoke
first. Cards arrived as a wall of forms, and reading them was homework.

This composes the turn that wraps them: he sees what the scan surfaced, decides
whether it is worth the operator's attention at all, and says so in his own
voice. Two outputs the template could never produce:

  * ``post`` -- permission to stay QUIET. A desk that must speak every two hours
    is a desk that pads. Silence is a valid, and often correct, answer.
  * ``ping`` -- an @ that costs the operator an interruption, so it is a
    separate decision from "worth posting".

Fail-open in one direction: any failure returns None and the caller posts the
cards exactly as it shipped. The homie layer can go dark without taking the
desk with it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

KILL_SWITCH_NAME = "desk_voice"

_DEFAULT_MAX_TURNS = 8
_MESSAGE_MAX = 1200


@dataclass(frozen=True)
class DeskMessage:
    """What the homie decided to do with this scan."""

    text: str
    post: bool
    ping: bool


def voice_max_turns() -> int:
    """Turn budget for the voice turn. CALL time (Rule 1)."""
    raw = os.getenv("DESK_VOICE_MAX_TURNS", "").strip()
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        parsed = int(raw)
    except ValueError:
        return _DEFAULT_MAX_TURNS
    return parsed if parsed > 0 else _DEFAULT_MAX_TURNS


def voice_enabled() -> bool:
    """Ships ON; the kill switch only revokes. Absence is not a denial."""
    try:
        from security import kill_switches

        if kill_switches.is_disabled(KILL_SWITCH_NAME):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def _soul(persona_id: str) -> str:
    """The persona's own identity text, or '' -- never a raised exception.

    Without this the voice turn is just another analyst prompt wearing his name,
    which is exactly the bug this module exists to fix.
    """
    try:
        from cognition.identity_payload import build_identity_payload
        from personas.core import get_persona_paths

        paths = get_persona_paths(persona_id)
        memory_dir = getattr(paths, "memory_dir", None) or getattr(paths, "memory", None)
        if memory_dir is None:
            return ""
        payload = build_identity_payload(memory_dir, include=("SOUL", "SELF"))
        return "\n\n".join(v for v in payload.values() if v).strip()
    except Exception:  # noqa: BLE001
        _logger.info("desk voice: could not load identity for %s", persona_id, exc_info=True)
        return ""


def _build_prompt(persona_id: str, plays_block: str, awake: bool) -> str:
    soul = _soul(persona_id)
    identity = f"{soul}\n\n---\n\n" if soul else ""
    ping_rule = (
        "The operator is AWAKE right now, so a ping is allowed if it earns one."
        if awake
        else
        "The operator is ASLEEP right now. Set ping=false no matter how good it is "
        "-- he will read it when he is up. Nothing here is worth waking him."
    )
    return f"""{identity}You just finished your two-hour scan. Below is what it surfaced.

You are writing to Smoke, one person, in his own Discord. Not an audience, not a
newsletter. Talk to him the way you would if you'd been up all night watching
this and he just walked in.

{plays_block}

Decide three things, in this order:

1. post -- is ANY of this worth his attention? A quiet scan is a real outcome and
   he would rather hear nothing than read filler. If nothing here would change
   what he does today, set post=false and stop. Do not pad to justify the run.

2. message -- if you are posting, open like a person. What was actually cooking,
   what you checked, what you think. Lead with the thing that matters. If you
   verified something with a tool, say what you found -- "liquidity's thin, I'd
   skip it" beats restating the room's hype. If you already called one of these
   before, say how that went. Under {_MESSAGE_MAX} characters, no headers, no
   bullet-point report format. The cards carry the detail; you carry the read.

3. ping -- does this need him NOW, or can it wait until he scrolls up? Reserve it
   for something live and time-sensitive, or a call of yours that just moved
   hard. {ping_rule}

Output ONLY this JSON object, nothing else, no code fences:
{{"post": true|false, "ping": true|false, "message": "str"}}"""


def _parse(raw: str) -> DeskMessage | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        # Fenced despite the instruction -- salvage rather than lose the turn.
        nl = text.find("\n")
        text = text[nl + 1 :] if nl != -1 else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    message = str(data.get("message") or "").strip()
    post = bool(data.get("post", False))
    # A "post" with nothing to say is a no-post. Trusting the flag alone would
    # publish an empty message and look like a bug to the operator.
    if post and not message:
        post = False
    return DeskMessage(
        text=message[:_MESSAGE_MAX],
        post=post,
        # Ping only ever rides a real post -- an @ with no message is noise.
        ping=bool(data.get("ping", False)) and post,
    )


async def compose_desk_message(
    plays_block: str,
    *,
    persona_id: str = "crypto",
    task_name: str = "desk_voice",
) -> DeskMessage | None:
    """Run the voice turn. ``None`` => caller behaves exactly as it shipped."""
    if not voice_enabled() or not str(plays_block or "").strip():
        return None

    try:
        from config import PROJECT_ROOT, get_background_models, is_within_waking_window
        from lib.agentic_turn import agentic_model_tier, resolve_desk_tools
        from runtime.base import RuntimeRequest
        from runtime.capabilities import TEXT_REASONING
        from runtime.lane_router import run_with_runtime_lanes
        from security import kill_switches

        kill_switches.requireEnabled("llm", caller="desk_voice")

        awake = is_within_waking_window()
        tool_defs, tool_dispatch = resolve_desk_tools(persona_id)
        models = get_background_models()
        model = models.get(agentic_model_tier(), models["fast"])

        result = await run_with_runtime_lanes(
            RuntimeRequest(
                prompt=_build_prompt(persona_id, plays_block, awake),
                cwd=PROJECT_ROOT,
                task_name=task_name,
                capability=TEXT_REASONING,
                model=model,
                max_turns=voice_max_turns() if tool_defs else 1,
                allowed_tools=[],
                tool_defs=tool_defs,
                tool_dispatch=tool_dispatch,
            )
        )
    except Exception:  # noqa: BLE001 -- the homie layer never kills the desk
        _logger.warning("desk voice turn failed; posting cards unwrapped", exc_info=True)
        return None

    parsed = _parse(str(getattr(result, "text", "") or ""))
    if parsed is None:
        _logger.info("desk voice: unparseable response; posting cards unwrapped")
    return parsed


__all__ = [
    "DeskMessage",
    "KILL_SWITCH_NAME",
    "compose_desk_message",
    "voice_enabled",
    "voice_max_turns",
]
