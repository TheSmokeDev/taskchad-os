"""Shared prompt rendering for CLI-backed runtime providers (Codex, Gemini).

Extracted from openai_codex.py / gemini_cli.py to eliminate duplication.
Both CLI providers call render_cli_prompt() instead of maintaining local copies.
"""

from __future__ import annotations

from .base import RuntimeRequest
from .capabilities import TOOL_REASONING

INTEGRATION_HINTS = (
    "Key integrations available via shell commands (run from .claude/scripts/):\n"
    "- Email: uv run python "
    "../skills/direct-integrations/scripts/query.py gmail list --max 5\n"
    "- Calendar: uv run python "
    "../skills/direct-integrations/scripts/query.py calendar today\n"
    "- Search Console: uv run python -m integrations.search_console_api overview\n"
    "- Analytics: uv run python -m integrations.analytics_api overview\n"
    "- Memory search: uv run python memory_search.py "
    '"query" --mode keyword --limit 5\n'
    "- Deployment-specific integrations may also be configured. "
    "Inspect .claude/scripts/integrations/registry.py and "
    ".claude/skills/direct-integrations/scripts/query.py --help"
)

# Default preambles — callers can override via kwargs.
# Internal \n\n matches the original list-item-per-sentence structure
# that was joined with "\n\n".join(parts).
_TOOL_PREAMBLE = (
    "You are an AI agent running through The Homie runtime layer.\n\n"
    "You have full access to read and write files, run shell commands, "
    "and use any tools needed to complete the task.\n\n"
    "Work in the project directory and return a complete, self-contained response."
)

_TEXT_PREAMBLE = (
    "You are running a safe text-only reasoning task for The Homie runtime layer.\n\n"
    "Do not edit files, run tools, or take destructive actions.\n\n"
    "Return only the final response text for the requested task."
)


def render_cli_prompt(
    request: RuntimeRequest,
    *,
    tool_preamble: str = _TOOL_PREAMBLE,
    text_preamble: str = _TEXT_PREAMBLE,
    integration_hints: str = INTEGRATION_HINTS,
) -> str:
    """Flatten RuntimeRequest into a single text prompt for CLI subprocess stdin.

    Extracted from openai_codex.py:150-177 / gemini_cli.py:146-173.
    """

    if request.capability == TOOL_REASONING:
        parts: list[str] = [tool_preamble, integration_hints]
    else:
        parts = [text_preamble]

    if isinstance(request.system_prompt, str) and request.system_prompt.strip():
        parts.append("System context:\n" + request.system_prompt.strip())
    elif isinstance(request.system_prompt, dict):
        append = str(request.system_prompt.get("append", "")).strip()
        if append:
            parts.append("System context:\n" + append)

    parts.append(f"Task name: {request.task_name}")
    parts.append("User task:\n" + request.prompt.strip())
    return "\n\n".join(parts)
