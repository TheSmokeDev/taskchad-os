"""OpenAI-compatible runtime adapter — text plus caller-supplied tool calling.

The ``chat_completions`` wire carries caller-supplied OpenAI-format tool
definitions natively; the ``responses`` wire does not yet, and says so rather
than dropping them silently. See ``supports_caller_tool_defs``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from typing import Any

from . import base as _base
from .base import RUNTIME_LANE_GENERIC, RuntimeRequest, RuntimeResult, RuntimeToolCall
from .capabilities import TEXT_REASONING, TOOL_REASONING
from .errors import (
    RuntimeConfigError,
    RuntimeExecutionError,
    RuntimeRetryableError,
    RuntimeUnsupportedCapabilityError,
)
from .profiles import RuntimeProfile

_logger = logging.getLogger(__name__)

# How many model round trips one tool-carrying turn may take. Deliberately NOT
# `request.max_turns`: that is a Claude-Agent-SDK concept and defaults to 1,
# which would cap every generic tool turn at "call a tool, never see the
# result" — the model would be cut off before it could use what it asked for.
_DEFAULT_TOOL_LOOP_ITERATIONS = 8


def _tool_loop_max_iterations() -> int:
    """Resolved at CALL time (Rule 1) so tests and live tuning take effect."""
    raw = os.getenv("SECOND_BRAIN_GENERIC_TOOL_MAX_ITERATIONS", "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_TOOL_LOOP_ITERATIONS
    except ValueError:
        value = _DEFAULT_TOOL_LOOP_ITERATIONS
    return max(1, value)


def _accumulate_usage(usage: dict[str, int], usage_raw: Any) -> None:
    """Sum token counts ACROSS loop iterations.

    A tool turn is several round trips; reporting only the last one would
    under-report a multi-call turn by most of its real cost. Cost stays unset —
    no price table is configured, and an invented number is worse than none.
    """
    if usage_raw is None:
        return
    for field_name in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        value = getattr(usage_raw, field_name, None)
        if isinstance(value, int):
            usage[field_name] = usage.get(field_name, 0) + value


def _assistant_message_with_tool_calls(message: Any, raw_calls: list[Any]) -> dict[str, Any]:
    """Rebuild the assistant turn as a plain dict for the next request.

    The SDK hands back a model object; the API wants JSON. Each tool result
    must reference the assistant message that requested it, so this echo is
    required, not cosmetic.
    """
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": [
            {
                "id": getattr(c, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(getattr(c, "function", None), "name", ""),
                    "arguments": getattr(getattr(c, "function", None), "arguments", "") or "{}",
                },
            }
            for c in raw_calls
        ],
    }


class OpenAICompatibleRuntime:
    """Minimal OpenAI-compatible adapter for safe text-only fallback."""

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile

    def _wire_api(self) -> str:
        """Which wire this profile speaks. Resolved at CALL time (Rule 1).

        One adapter class serves several providers (``openai-compatible``,
        ``openrouter``, ``kimi``) and they do NOT share a wire — so capability
        is a per-PROFILE question, never a per-class one.
        """
        from .profiles import GENERIC_PROVIDER_REGISTRY

        provider = getattr(self.profile, "provider", None)
        if not provider:
            # No profile (capability probed before binding) — fall back to the
            # NON-carrying wire. The router's probe catches exceptions and
            # fails closed anyway, but relying on that is luck: an adapter must
            # be able to answer "can you carry tools?" without a profile, and
            # the safe answer when it cannot tell is no.
            return "responses"
        overlay = GENERIC_PROVIDER_REGISTRY.get(provider)
        return overlay.wire_api if overlay is not None else "responses"

    def supports_caller_tool_defs(self) -> bool:
        """True on the ``chat_completions`` wire ONLY.

        This answers "will this adapter EXECUTE caller-supplied tool
        definitions", not "could the provider in principle". An adapter
        returning True while quietly dropping definitions is exactly the Codex
        failure mode, reproduced in our own code.

        ``chat_completions`` — TRUE. The loop below sends ``tools=[...]``,
        parses ``tool_calls``, executes through ``request.tool_dispatch``, feeds
        results back as ``role: "tool"`` messages, and iterates. Measured
        2026-07-27: Kimi K3 returned ``finish_reason: tool_calls`` with a
        structured ``get_weather({"city": "Reykjavik"})`` for an
        OpenAI-format definition.

        ``responses`` — FALSE, deliberately. That branch builds a request with
        no tools parameter at all and would return a confident tool-free
        answer: a polite drop, indistinguishable from a persona refusing to
        act. Declaring False means the router skips this lane for tool turns
        instead. Wiring the Responses tool format is separate work, and until
        it exists the honest answer is no.
        """
        return self._wire_api() == "chat_completions"

    def supports_model_only(self) -> bool:
        """False until this adapter enforces token/cost ceilings for strict jobs."""
        return False

    def supports(self, request: RuntimeRequest) -> bool:
        if _base.request_carries_tools(request):
            # A tool-carrying request the wire cannot execute must never reach
            # the body. NOT covered by the `allowed_tools` clause — `tool_defs`
            # is a different field, and a caller may legitimately send tool_defs
            # on a TEXT_REASONING turn.
            if not self.supports_caller_tool_defs():
                return False
            # Tool turns are TOOL_REASONING by convention but the tier is not
            # what makes them executable here — carrying the definitions is.
            # Accept both tiers rather than forcing callers to pick one.
            if request.capability not in {TEXT_REASONING, TOOL_REASONING}:
                return False
            return request.resume is None and request.hooks is None
        return (
            request.capability == TEXT_REASONING
            and not request.allowed_tools
            and request.resume is None
            and request.hooks is None
        )

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        if not self.supports(request):
            raise RuntimeUnsupportedCapabilityError(
                f"OpenAI-compatible runtime does not support capability {request.capability}"
            )
        if not self.profile.api_key:
            raise RuntimeConfigError(
                f"API key is not configured for {self.profile.provider}"
            )

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeConfigError("openai package is not installed") from exc

        client = AsyncOpenAI(api_key=self.profile.api_key, base_url=self.profile.base_url)
        model = request.fallback_model or request.model or self.profile.model
        instructions: str | None = None
        usage: dict[str, int] = {}
        if isinstance(request.system_prompt, str):
            instructions = request.system_prompt
        elif isinstance(request.system_prompt, dict):
            instructions = str(request.system_prompt.get("append", "")).strip() or None

        wire_api = self._wire_api()
        tool_calls: list[RuntimeToolCall] = []

        try:
            if wire_api == "chat_completions":
                # request.model/fallback_model carry the Claude-lane value
                # (engine.py builds it from SECOND_BRAIN_CLAUDE_MODEL). Generic
                # providers use their own pinned model — the profiles contract
                # ("model names are provider-specific").
                model = self.profile.model
                messages: list[dict[str, Any]] = []
                if instructions:
                    messages.append({"role": "system", "content": instructions})
                messages.append({"role": "user", "content": request.prompt})
                text, tool_calls = await self._run_chat_completions(
                    client, model, request, messages, usage
                )
            else:
                response = await client.responses.create(
                    model=model,
                    input=request.prompt,
                    instructions=instructions,
                )
                text = getattr(response, "output_text", "").strip()
                if not text:
                    text = _extract_response_text(response)
        except Exception as exc:
            error_text = str(exc).lower()
            if any(
                token in error_text
                for token in ("rate limit", "quota", "429", "overloaded", "unavailable")
            ):
                raise RuntimeRetryableError(str(exc)) from exc
            if "auth" in error_text or "api key" in error_text or "401" in error_text:
                raise RuntimeConfigError(str(exc)) from exc
            raise

        return RuntimeResult(
            text=text.strip(),
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider=self.profile.provider,
            model=model,
            profile_key=self.profile.key,
            usage=usage or None,
            tool_call_count=len(tool_calls),
            tool_names_used=sorted({c.name for c in tool_calls if c.name}),
            tool_calls=tool_calls,
        )

    async def _run_chat_completions(
        self,
        client: Any,
        model: str,
        request: RuntimeRequest,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> tuple[str, list[RuntimeToolCall]]:
        """Drive a chat_completions turn, looping while the model calls tools.

        Without caller tool defs this is one round trip and behaves exactly as
        before. With them: send ``tools=[...]``, execute any returned
        ``tool_calls`` through ``request.tool_dispatch``, append the results as
        ``role: "tool"`` messages, and go again until the model answers in text.

        Every execution goes through ``request.tool_dispatch`` — the ONE
        chokepoint. This adapter never calls a handler directly, so guardrails,
        the kill switch, and the audit row (#242) fire identically here and for
        the disclosure bridge (#245). Two execution paths would mean two places
        to forget a guardrail.
        """
        tool_defs = list(request.tool_defs or [])
        carries_tools = _base.request_carries_tools(request)
        # Names the model is permitted to call THIS turn. A model can emit a
        # name it was never offered (hallucination, or a stale name from
        # history); executing that would reach past the toolset scope the
        # persona was granted.
        offered: set[str] = {
            name for td in tool_defs if (name := ((td.get("function") or {}) or {}).get("name"))
        }
        collected: list[RuntimeToolCall] = []
        text = ""

        for _ in range(_tool_loop_max_iterations() if carries_tools else 1):
            kwargs: dict[str, Any] = {"model": model, "messages": messages}
            if carries_tools:
                kwargs["tools"] = tool_defs

            completion = await client.chat.completions.create(**kwargs)
            _accumulate_usage(usage, getattr(completion, "usage", None))

            choice = (getattr(completion, "choices", None) or [None])[0]
            message = getattr(choice, "message", None)
            text = str(getattr(message, "content", "") or "").strip()
            raw_calls = list(getattr(message, "tool_calls", None) or [])

            if not carries_tools or not raw_calls:
                return text, collected

            if request.tool_dispatch is None:
                # Carrying definitions without a dispatcher is a caller bug, and
                # a LOUD one: the model has already decided to call a tool, so
                # returning its empty text would look like a considered answer.
                raise RuntimeExecutionError(
                    "model requested tool calls but the request carries no "
                    "tool_dispatch — the caller supplied definitions it cannot "
                    "execute"
                )

            # Echo the assistant turn back verbatim; the API requires each
            # tool result to reference the assistant message that requested it.
            messages.append(_assistant_message_with_tool_calls(message, raw_calls))

            for raw in raw_calls:
                record, result_text = await self._execute_one(raw, offered, request)
                collected.append(record)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": record.id,
                        "content": result_text,
                    }
                )

        # Loop bound reached with the model still calling tools. Return the last
        # text rather than raising — the turn produced real work, and the caller
        # sees the full tool_calls trail. Silence here would look like a hang.
        _logger.warning(
            "chat_completions tool loop hit its iteration bound with tools still "
            "pending (%d call(s) executed); returning the last assistant text",
            len(collected),
        )
        return text, collected

    async def _execute_one(
        self,
        raw: Any,
        offered: set[str],
        request: RuntimeRequest,
    ) -> tuple[RuntimeToolCall, str]:
        """Execute one model-requested call. Never raises to the loop.

        A failing tool is normal conversational input — the model should SEE
        the error and get a chance to recover, exactly as it would on the
        Claude lane. Raising instead would turn a recoverable tool error into a
        failed turn and a lane fallback.
        """
        fn = getattr(raw, "function", None)
        name = str(getattr(fn, "name", "") or "")
        raw_args = getattr(fn, "arguments", None)
        call_id = str(getattr(raw, "id", "") or "")

        arguments: dict[str, Any] | str | None = raw_args
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = raw_args  # keep the raw string for the audit trail

        record = RuntimeToolCall(
            id=call_id,
            name=name,
            arguments=arguments,
            provider_type="function",
        )

        if name not in offered:
            # Scope guard: the model asked for something it was not offered.
            record.status = "refused"
            _logger.warning(
                "refusing tool call %r — not in the definitions offered this turn (offered: %s)",
                name,
                ", ".join(sorted(offered)) or "none",
            )
            return record, json.dumps({"error": f"tool {name!r} was not offered for this turn"})

        try:
            # Sync dispatchers do blocking work (SQLite, filesystem, browser
            # subprocesses — e.g. the persona action gate's proposal write).
            # Running one directly here would stall this runtime's event loop
            # for the full duration (Codex R1), so sync dispatch hops a
            # thread; an async dispatcher is awaited in place. Mirrors the
            # codex_app_server_gate carrier.
            if inspect.iscoroutinefunction(request.tool_dispatch):
                outcome = request.tool_dispatch(name, arguments)
            else:
                outcome = await asyncio.to_thread(request.tool_dispatch, name, arguments)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            record.status = "completed"
            return record, outcome if isinstance(outcome, str) else json.dumps(outcome, default=str)
        except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed
            record.status = "failed"
            _logger.warning("tool %r raised during dispatch: %s", name, exc)
            return record, json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _extract_response_text(response: Any) -> str:
    """Best-effort text extraction across OpenAI client response variants."""

    outputs = getattr(response, "output", None) or []
    parts: list[str] = []
    for item in outputs:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(text, dict):
                value = text.get("value")
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(part for part in parts if part.strip())
