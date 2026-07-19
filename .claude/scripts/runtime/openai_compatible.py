"""OpenAI-compatible runtime adapter for safe no-tool paths."""

from __future__ import annotations

from typing import Any

from .base import RUNTIME_LANE_GENERIC, RuntimeRequest, RuntimeResult
from .capabilities import TEXT_REASONING
from .errors import RuntimeConfigError, RuntimeRetryableError, RuntimeUnsupportedCapabilityError
from .profiles import RuntimeProfile


class OpenAICompatibleRuntime:
    """Minimal OpenAI-compatible adapter for safe text-only fallback."""

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile

    def supports(self, request: RuntimeRequest) -> bool:
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
                "OPENAI_API_KEY is not configured for OpenAI-compatible fallback"
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

        # Deferred import (no cycle): profiles never imports this module.
        from .profiles import GENERIC_PROVIDER_REGISTRY

        overlay = GENERIC_PROVIDER_REGISTRY.get(self.profile.provider)
        wire_api = overlay.wire_api if overlay is not None else "responses"

        try:
            if wire_api == "chat_completions":
                # request.model/fallback_model carry the Claude-lane value
                # (engine.py builds it from SECOND_BRAIN_CLAUDE_MODEL). Generic
                # providers use their own pinned model — the profiles contract
                # ("model names are provider-specific").
                model = self.profile.model
                messages: list[dict[str, str]] = []
                if instructions:
                    messages.append({"role": "system", "content": instructions})
                messages.append({"role": "user", "content": request.prompt})
                completion = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                )
                choice = (getattr(completion, "choices", None) or [None])[0]
                message = getattr(choice, "message", None)
                text = str(getattr(message, "content", "") or "").strip()
                # Usage telemetry rides the result (token counts only — no
                # price table is configured, so cost stays unset rather than
                # invented).
                usage_raw = getattr(completion, "usage", None)
                if usage_raw is not None:
                    for field_name in (
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "cached_tokens",
                    ):
                        value = getattr(usage_raw, field_name, None)
                        if isinstance(value, int):
                            usage[field_name] = value
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
        )


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
