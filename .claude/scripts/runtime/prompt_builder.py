"""Shared prompt rendering for CLI-backed runtime providers (Codex, Gemini).

Extracted from openai_codex.py / gemini_cli.py to eliminate duplication.
Both CLI providers call render_cli_prompt() instead of maintaining local copies.
"""

from __future__ import annotations

from .base import RuntimeRequest
from .capabilities import TOOL_REASONING
from .framework_registry import render_framework_tool_map

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
    "Use .claude/scripts/integrations/capabilities.py as the canonical "
    "action/effect policy; registry.py reports availability and "
    "query.py --help shows wrapper syntax."
)

# Default preambles — callers can override via kwargs.
# Internal \n\n matches the original list-item-per-sentence structure
# that was joined with "\n\n".join(parts).
_TOOL_PREAMBLE = (
    "You are an AI agent running through The Homie runtime layer.\n\n"
    "You have full access to read and write files, run shell commands, "
    "and use any tools needed to complete the task.\n\n"
    "Match the length of your response to the request: keep explanatory text "
    "brief and focus on actions and results over narration. Do not pad or "
    "restate context the user did not ask for.\n\n"
    "Work in the project directory and return a complete, self-contained response."
)

_TEXT_PREAMBLE = (
    "You are running a safe text-only reasoning task for The Homie runtime layer.\n\n"
    "Do not edit files, run tools, or take destructive actions.\n\n"
    "Match the length of your response to the request: answer short or casual "
    "messages briefly (a sentence or two), and reserve longer responses for "
    "tasks that genuinely need them. Do not pad, restate the prompt, or dump "
    "context the user did not ask for.\n\n"
    "Return only the final response text for the requested task."
)


# Gemini-specific operational guidance, ported from Hermes'
# GOOGLE_MODEL_OPERATIONAL_GUIDANCE (hermes-agent/agent/prompt_builder.py),
# adapted for The Homie's CLI lane and injected via
# render_cli_prompt(model_guidance=...) by gemini_cli.py. The conciseness
# directive is the primary guard against Gemini models that dump multi-thousand
# character replies to trivial messages.
GEMINI_GUIDANCE = (
    "# Gemini operational directives\n"
    "Follow these rules strictly:\n"
    "- Conciseness: keep explanatory text brief — a few sentences, not "
    "paragraphs. Match response length to the request; a short or casual "
    "message gets a short reply. Do not pad, restate the prompt, or dump "
    "context the user did not ask for.\n"
    "- Verify first: read files / check structure before changing them; never "
    "guess at file contents.\n"
    "- Absolute paths: construct absolute file paths for file-system operations.\n"
    "- Dependency checks: never assume a library is available; check the "
    "manifest before importing.\n"
    "- Non-interactive commands: use flags like -y / --yes / --non-interactive "
    "so CLI tools do not hang on prompts.\n"
    "- Keep going: work autonomously until the task is resolved; execute, do "
    "not just plan.\n"
)


def render_cli_prompt(
    request: RuntimeRequest,
    *,
    tool_preamble: str = _TOOL_PREAMBLE,
    text_preamble: str = _TEXT_PREAMBLE,
    integration_hints: str = INTEGRATION_HINTS,
    framework_tool_map: str | None = None,
    model_guidance: str | None = None,
) -> str:
    """Flatten RuntimeRequest into a single text prompt for CLI subprocess stdin.

    Extracted from openai_codex.py:150-177 / gemini_cli.py:146-173.

    ``model_guidance`` lets a provider adapter inject model-specific operational
    directives (e.g. Gemini brevity discipline) without coupling this shared
    renderer to any one provider.
    """

    if request.capability == TOOL_REASONING:
        parts: list[str] = [tool_preamble]
        if model_guidance and model_guidance.strip():
            parts.append(model_guidance.strip())
        parts.append(integration_hints)
        tool_map = (
            render_framework_tool_map(request.cwd)
            if framework_tool_map is None
            else framework_tool_map.strip()
        )
        if tool_map:
            parts.append(tool_map)
    else:
        parts = [text_preamble]
        if model_guidance and model_guidance.strip():
            parts.append(model_guidance.strip())

    if isinstance(request.system_prompt, str) and request.system_prompt.strip():
        parts.append("System context:\n" + request.system_prompt.strip())
    elif isinstance(request.system_prompt, dict):
        append = str(request.system_prompt.get("append", "")).strip()
        if append:
            parts.append("System context:\n" + append)

    parts.append(f"Task name: {request.task_name}")
    parts.append("User task:\n" + request.prompt.strip())
    return "\n\n".join(parts)
