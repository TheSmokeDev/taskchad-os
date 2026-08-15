"""Talk mode tool surface — Realtime function-tool schemas + executor.

The voice session (dashboard /talk over WebRTC, or the Discord voice
sidecar) advertises these tools to the Realtime model; when the model
emits a function call, the caller relays it here and speaks the returned
text. Design rules:

- Read-everything goes through what the Homie already is: the memory
  vault, Google Calendar, and the chat router's direct integrations.
- Do-everything goes through delegation: real work is handed to the
  engine's work queue, never improvised inside the voice path.
- ``run_python`` is the raw-code escape hatch and is OFF unless the
  operator sets ``TALK_ENABLE_CODE_EXEC`` explicitly (kill switches
  default to ENABLED, so a fail-closed opt-in env is the right gate for
  a voice-controllable code path — anyone in the room can talk).

Outputs are capped and plain-text: the model summarizes them aloud.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import talk_runs
from security import kill_switches

_log = logging.getLogger(__name__)

_CODE_EXEC_ENV = "TALK_ENABLE_CODE_EXEC"
_CODE_EXEC_TIMEOUT_S = 30
_MAX_OUTPUT_CHARS = 4_000
_MAX_MEMORY_SNIPPET_CHARS = 280

_VALID_SESSION_ROLES = ("viewer", "operator", "admin")

#: TRANSPORTS this tool surface serves. The distinction is load-bearing: the
#: Discord voice sidecar is a SEPARATE PROCESS that relays each tool call over
#: loopback to this one, so nothing it knows is in this interpreter's memory.
TRANSPORT_BROWSER = "browser"
TRANSPORT_DISCORD_VOICE = "discord_voice"

#: Authority for the browser `/talk` page ONLY.
#:
#: `POST /api/talk/session` is classified `admin` in
#: `orchestration/route_policy.py`, so minting the session IS the authorization
#: event, and exactly one participant holds it — the authenticated operator whose
#: browser has the session. Scoped to that transport BY NAME so it can never be
#: read for a Discord-originated call: a browser mint must not silently become
#: the authority for a voice channel full of other people.
_BROWSER_SESSION_ROLE = "viewer"

#: The role for the tool call being served RIGHT NOW. A ContextVar, not a module
#: global, because this process serves calls from every transport concurrently —
#: a global would let one request's identity leak into another's.
_REQUEST_ROLE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "talk_request_role", default="viewer"
)

#: Voice tools that only READ. They stay available to anyone in the channel,
#: including when speaker attribution could not be resolved at all — losing
#: attribution should cost a refusal on mutation, not silence the assistant.
READ_ONLY_TALK_TOOLS = frozenset({"memory_search", "calendar_events", "check_work"})

#: Voice tools that spend, mutate, or reach outside this box. On Discord these
#: require a speaker the sidecar could bind to one utterance; the role gate
#: downstream then still has to accept that speaker.
MUTATING_TALK_TOOLS = frozenset(
    {
        "homie_command",
        "delegate_task",
        "run_skill",
        "run_archon",
        "computer",
        "browse",
        "manage_run",
        "run_python",
        "run_shell",
    }
)

#: hermes-talk's denial shape — say what happened AND what still works, rather
#: than a bare permission error that reads like a bug. Reworded for this split:
#: the sidecar resolves the speaker and the allowlist lives here, so the honest
#: failure is "couldn't verify which speaker", not "not a configured operator".
MUTATION_DENIAL = (
    "I couldn't verify which speaker this request came from, so the {tool} tool "
    "was not run. Read-only requests are still available."
)

#: The other half of a mutating refusal: we DO know who spoke, and they are not
#: authorized. Kept distinct from MUTATION_DENIAL so the operator can tell an
#: attribution failure (retry in a clean turn) from an authority one (you are
#: not on the allowlist) — and deliberately carries no user ids.
ROLE_DENIAL = (
    "You're not authorized to run the {tool} tool on this voice channel, so it "
    "was not run. Read-only requests are still available."
)

#: Mirrors `chat/extension_manager.py::ROLE_LEVEL`, which is the canonical
#: ladder. Duplicated rather than imported because that module lives in the
#: chat slice and is only reachable here through the lazy sys.path join in
#: `_command_manager()` — an import-time dependency on it would make this gate
#: fail open whenever the chat slice is not importable, which is exactly
#: backwards for an authorization check.
_ROLE_LEVEL: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}


def set_browser_session_role(role: str | None) -> str:
    """Record the authenticated operator's role for the BROWSER Talk session.

    Unknown or empty values fail closed to `viewer`; returns the value actually
    stored so callers can log it.
    """
    global _BROWSER_SESSION_ROLE
    normalized = str(role or "").strip().lower()
    _BROWSER_SESSION_ROLE = normalized if normalized in _VALID_SESSION_ROLES else "viewer"
    return _BROWSER_SESSION_ROLE


def end_talk_session() -> None:
    """Drop the browser session's authority (teardown)."""
    global _BROWSER_SESSION_ROLE
    _BROWSER_SESSION_ROLE = "viewer"


def _binding_is_trusted(binding: object) -> bool:
    """Did the sidecar prove which utterance produced this call?

    Absent binding fails CLOSED on the voice transport. The sidecar always
    sends one, so a missing verdict means an unknown or tampered client — the
    exact case that must not inherit the old "trust the id" behavior.
    """
    if binding is None:
        return False
    if isinstance(binding, dict):
        return bool(binding.get("trusted"))
    return bool(getattr(binding, "trusted", False))


def _binding_reason(binding: object) -> str:
    if isinstance(binding, dict):
        return str(binding.get("reason") or "no speaker binding on the request")
    return str(getattr(binding, "reason", "") or "no speaker binding on the request")


def resolve_request_role(
    *,
    transport: str | None,
    speaker_id: str | None,
    binding: object = None,
) -> str:
    """THE single resolution authority for one incoming tool call.

    Resolves from the REQUEST, never from what some other process believes.
    The Discord sidecar sends the raw speaker id rather than a role on purpose:
    a sidecar can then only assert WHO spoke, and this process independently
    checks that id against the same `DISCORD_ALLOWED_USERS` seam the Discord
    chat adapter stamps from. A buggy or tampered sidecar cannot claim `admin`
    — it can only name a user, and naming one still resolves to whatever the
    allowlist says about them.

    `binding` is the sidecar's interval-bound verdict (`speaker_auth`): whether
    the speaker it names was resolved from the audio the model actually
    answered, rather than from whoever happened to be talking when the call
    came back. Naming a speaker is not enough — an untrusted or missing verdict
    resolves `viewer` even when an id is present, so a sidecar bug can never
    upgrade an ambiguous room into one person's authority.

    - `discord_voice` -> the bound speaker's own role; unbound -> viewer.
    - `browser`       -> the authenticated browser session's role.
    - anything else   -> viewer (fail closed on an unrecognized transport).
    """
    if transport == TRANSPORT_BROWSER:
        return _BROWSER_SESSION_ROLE
    if transport != TRANSPORT_DISCORD_VOICE:
        return "viewer"
    if not _binding_is_trusted(binding):
        _log.warning(
            "discord voice call has no trusted speaker binding (%s); refusing as viewer",
            _binding_reason(binding),
        )
        return "viewer"
    speaker = str(speaker_id or "").strip()
    if not speaker:
        return "viewer"
    try:
        import config  # noqa: PLC0415 — call-time read (Rule 1)
        from models import resolve_ingress_role  # noqa: PLC0415

        return resolve_ingress_role(speaker, config.DISCORD_ALLOWED_USERS)
    except Exception:  # noqa: BLE001 — an unresolvable identity is not an admin
        _log.warning("speaker role resolution failed; refusing as viewer")
        return "viewer"


_TOOL_MEMORY_SEARCH: dict = {
    "type": "function",
    "name": "memory_search",
    "description": (
        "Search owner's thehomie memory vault (hybrid keyword + semantic) "
        "and return the top matching notes with snippets. Use for anything "
        "about his projects, decisions, people, or past work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search query."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "Max notes to return (default 5).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

_TOOL_CALENDAR_EVENTS: dict = {
    "type": "function",
    "name": "calendar_events",
    "description": (
        "List owner's Google Calendar events for today, or for the upcoming "
        "N days. Use for schedule, meeting, and 'what's on today' questions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "minimum": 0,
                "maximum": 14,
                "description": "0 = today only; N = upcoming N days (default 0).",
            },
        },
        "additionalProperties": False,
    },
}

_TOOL_HOMIE_COMMAND: dict = {
    "type": "function",
    "name": "homie_command",
    "description": (
        "Run one of the Homie's own router commands and return its text. "
        "Covers the read surface: 'gsc' (Search Console), 'analytics', "
        "'status', 'diagnostics', 'provider', 'model', 'goals'. Use for "
        "business stats or system state instead of guessing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command name without the slash, e.g. 'gsc' or 'analytics'.",
            },
            "args": {
                "type": "string",
                "description": "Optional trailing arguments for the command.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}

_TOOL_DELEGATE_TASK: dict = {
    "type": "function",
    "name": "delegate_task",
    "description": (
        "Hand a task to a background Homie agent. Default scope 'quick' runs "
        "in-process and starts working immediately — research, drafts, "
        "analysis, file work, anything under about thirty minutes. Scope "
        "'substantial' deploys the task through Archon instead: a repo clone "
        "and a worktree, which costs about a minute of setup before anything "
        "happens, so it is for real multi-step builds and never for a lookup. "
        "Either way a work-queue row is kept and the result is spoken to owner "
        "when it lands. Quick agents are STEERABLE while running: manage_run "
        "'say' with the receipt number queues a course-correction for the "
        "agent's next turn boundary, and 'cancel' stops it. Say what you're "
        "delegating before you fire it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The full task: what to do, context, acceptance criteria. "
                    "On scope 'substantial' this is all the Archon worker ever "
                    "sees — it starts in a fresh checkout with no access to "
                    "this conversation, so write it out standalone."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional short label for status reports.",
            },
            "lane": {
                "type": "string",
                "enum": ["codex", "claude", "gemini", "kimi"],
                "description": "Optional engine lane override (default codex, quick scope only).",
            },
            "scope": {
                "type": "string",
                "enum": ["quick", "substantial"],
                "description": (
                    "'quick' (default) = fast in-process agent. 'substantial' "
                    "= deploy through Archon with its own worktree; only worth "
                    "the setup cost for real builds."
                ),
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    },
}

_TOOL_RUN_ARCHON: dict = {
    "type": "function",
    "name": "run_archon",
    "description": (
        "Deploy a heavy real-work workflow through Archon — detached, can take "
        "thirty minutes to two hours. Returns a started receipt now; the "
        "outcome is spoken when it lands. Workflows: 'archon-clutch' (full "
        "CLUTCH multi-agent team build — use when owner says CLUTCH or team "
        "build), 'archon-ralph-dag' (autonomous feature implementation), "
        "'image-node-factory' or 'codex-image-asset-factory' (brand images), "
        "'video-production', 'client-site-factory', or any other workflow in "
        "the repo (fuzzy-matched). An Archon run means a repo clone and a "
        "worktree — even a trivial one costs about a minute of setup, so short "
        "lookups belong on memory_search, homie_command, or a quick "
        "delegate_task instead. Say in one sentence what you're about to run, "
        "then fire it: the work lands in an isolated worktree and costs only "
        "tokens, so it does not need approval. If the workflow reaches "
        "something that spends real money, it pauses itself and asks first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": "Workflow name, e.g. 'archon-clutch' or 'archon-ralph-dag'.",
            },
            "brief": {
                "type": "string",
                "description": (
                    "The complete, self-contained brief. This is ALL the "
                    "worker ever sees: it starts in a fresh worktree with no "
                    "access to this conversation, so never pass 'yeah do "
                    "that', 'what we discussed', or any pointer back to what "
                    "was said. Write out the whole task — what to build, where "
                    "it lives, and what done looks like — as if to someone who "
                    "never heard a word of it. A brief that only points back "
                    "is refused."
                ),
            },
        },
        "required": ["workflow", "brief"],
        "additionalProperties": False,
    },
}

_TOOL_COMPUTER: dict = {
    "type": "function",
    "name": "computer",
    "description": (
        "Control owner's Windows desktop. Actions: open_terminal (new terminal "
        "window, optional command), open_url (default browser), open_file, "
        "run_command (launches a shell command in a new terminal and returns "
        "NOTHING — fire-and-forget only; if you need to read the output, use "
        "run_shell instead, which hands you the text), notify (desktop "
        "toast), type_into_window (find a window by part of its title — for "
        "example one of his Claude Code terminals — focus it, type text, press "
        "Enter, then give focus back), press_keys (like 'enter' or 'ctrl+c'), "
        "click (screen coordinates or the middle of a named window), and "
        "look_at_screen (screenshot plus a description; takes about thirty "
        "seconds and is spoken when ready). For anything that types, clicks, "
        "or runs a command: say what you're about to do first and get a "
        "spoken yes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "open_terminal",
                    "open_url",
                    "open_file",
                    "run_command",
                    "notify",
                    "type_into_window",
                    "press_keys",
                    "click",
                    "look_at_screen",
                ],
                "description": "Which desktop action to perform.",
            },
            "text": {
                "type": "string",
                "description": "Text to type, command to run, or notification message.",
            },
            "window_title": {
                "type": "string",
                "description": "Part of a window title, e.g. 'claude' or 'thehomie'.",
            },
            "url": {"type": "string", "description": "Absolute URL for open_url."},
            "path": {"type": "string", "description": "File path for open_file."},
            "keys": {
                "type": "string",
                "description": "Key or combo for press_keys, e.g. 'enter' or 'ctrl+c'.",
            },
            "x": {"type": "integer", "description": "Screen X coordinate for click."},
            "y": {"type": "integer", "description": "Screen Y coordinate for click."},
            "press_enter": {
                "type": "boolean",
                "description": "type_into_window: press Enter after typing (default true).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

_TOOL_BROWSE: dict = {
    "type": "function",
    "name": "browse",
    "description": (
        "Drive the Homie's visible Chrome — the real logged-in browser. "
        "Actions: status (is it ready), tabs (what's open), open (navigate to "
        "an absolute http or https URL), snapshot (read the current page as "
        "text — use this to answer 'what's on this page'). Reading and "
        "navigating only; posting or editing on websites is not available "
        "from voice."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "tabs", "open", "snapshot"],
                "description": "Which browser action to perform.",
            },
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL — required for 'open'.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

_TOOL_CHECK_WORK: dict = {
    "type": "function",
    "name": "check_work",
    "description": (
        "Status of deployed work, and what it's actually doing right now. With "
        "no arguments: everything active and recent — voice runs (skills, "
        "background agents, screen looks), Archon workflow runs, anything "
        "paused and waiting on owner, and open work-queue tasks. With a run "
        "id: that run's detail, the node it's on, and the last few tool calls. "
        "Use whenever owner asks how it's going, what it's doing, whether "
        "something finished, or what happened to a task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "integer",
                "description": "A receipt number from a WORK_STARTED message.",
            },
        },
        "additionalProperties": False,
    },
}

_TOOL_MANAGE_RUN: dict = {
    "type": "function",
    "name": "manage_run",
    "description": (
        "Steer an Archon run that's already going. Actions: 'list' (runs, "
        "paused ones first), 'get' (one run's node and recent tool calls), "
        "'say' (send owner's own words to the run — on a paused run that IS "
        "the approval, so 'looks good, ship it' resumes it), 'approve' / "
        "'reject' (answer a gate), 'resume', 'cancel', 'abandon', 'help'. "
        "Leave run_id out and it uses the paused run, or the only run going. "
        "Reject, cancel and abandon destroy work in flight: call them once "
        "WITHOUT confirm to hear what would happen, say it to owner, and only "
        "call again with confirm true after he says yes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "help",
                    "list",
                    "get",
                    "say",
                    "approve",
                    "reject",
                    "resume",
                    "cancel",
                    "abandon",
                ],
                "description": "Which steering action to take.",
            },
            "run_id": {
                "type": "string",
                "description": (
                    "An Archon run id, or a WORK_STARTED receipt number like "
                    "'3'. Omit to use the paused run or the only one running."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "owner's words. Required for 'say'. On approve/reject it "
                    "rides along as his comment; the gate's own required "
                    "phrase is added automatically, so never type one."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Only for reject/cancel/abandon, and only after owner has "
                    "heard the preview and said yes."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

_TOOL_RUN_SKILL: dict = {
    "type": "function",
    "name": "run_skill",
    "description": (
        "Run one of owner's workflow skill packs (vault-ops, keyword-research, "
        "seo-coach, client-site-factory, video skills, and dozens more) through "
        "the Homie engine. Use when owner names a workflow ('run vault ops', "
        "'do keyword research on X', 'run the SEO audit'). For simple lookups "
        "use memory_search or homie_command instead. Runs are async: this "
        "returns a started receipt immediately — tell owner it's running — and "
        "the result is spoken to him when it lands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name, e.g. 'vault-ops' or 'keyword-research'. Fuzzy-resolved.",
            },
            "input": {
                "type": "string",
                "description": "owner's request for the skill, e.g. 'YourProduct.com context'.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}

_TOOL_RUN_PYTHON: dict = {
    "type": "function",
    "name": "run_python",
    "description": (
        "Execute a short Python snippet in the Homie scripts environment and "
        "return stdout. Use for calculations, quick data checks, or probing "
        "local files/APIs. Operator-gated capability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source to execute; stdout is returned.",
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    },
}

_TOOL_RUN_SHELL: dict = {
    "type": "function",
    "name": "run_shell",
    "description": (
        "Run a shell command and get its OUTPUT BACK AS TEXT. Use this "
        "whenever you need to READ the result — gh, git, ls, cat, curl, uv, "
        "anything. Runs in the repo root. This is the right tool for "
        "questions like 'what repos do I have' or 'what changed': you get the "
        "text immediately and can just read it out. Do NOT use the computer "
        "tool for those — it opens a terminal on screen and gives you nothing "
        "back, so you would have to take a screenshot and describe it, which "
        "is slow and lossy. Operator-gated capability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The shell command to run, e.g. "
                    "'gh repo list your-github-user --limit 200'. stdout is returned."
                ),
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}


class TalkToolError(Exception):
    """Unknown tool name or malformed tool call."""


def code_exec_enabled() -> bool:
    """Fail-closed opt-in for voice-controllable code execution."""

    return os.environ.get(_CODE_EXEC_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def default_talk_tools() -> list[dict]:
    """The tool set advertised to new Talk sessions (fresh copies per call)."""

    import copy

    tools = [
        _TOOL_MEMORY_SEARCH,
        _TOOL_CALENDAR_EVENTS,
        _TOOL_HOMIE_COMMAND,
        _TOOL_DELEGATE_TASK,
        _TOOL_RUN_SKILL,
        _TOOL_RUN_ARCHON,
        _TOOL_COMPUTER,
        _TOOL_BROWSE,
        _TOOL_CHECK_WORK,
        _TOOL_MANAGE_RUN,
    ]
    if code_exec_enabled():
        # run_shell is listed FIRST so it is the nearer of the two when the
        # model is choosing how to run something.
        tools.append(_TOOL_RUN_SHELL)
        tools.append(_TOOL_RUN_PYTHON)
    return copy.deepcopy(tools)


def talk_tool_denial(
    name: str,
    *,
    transport: str | None,
    binding: object = None,
    resolved_role: str = "viewer",
) -> str | None:
    """The ONE gate every voice tool passes. `None` = proceed to the handler.

    Codex R8 BLOCKER: resolving the speaker's role was not the same thing as
    ENFORCING it. Only `_handle_homie_command` ever read `_REQUEST_ROLE` (it
    hands it to the command registry's own min_role gate); the seven direct
    mutating handlers — run_python, run_shell, delegate_task, run_skill,
    run_archon, computer, manage_run — executed unconditionally. A stranger
    with a perfectly TRUSTED binding resolved to `viewer` and still ran them:
    the identity chain was intact and simply never consulted.

    So the check lives here, at the dispatch chokepoint, rather than in seven
    copies that the eighth handler would forget. Four rules, all fail-closed:

    - read-only tools always run, even with no attribution at all — losing
      attribution costs mutation, not the assistant;
    - a tool in NEITHER set is unclassified and is treated AS MUTATING, so a
      new capability inherits the strict bucket instead of a free pass
      (`test_every_talk_tool_is_classified_read_only_or_mutating` then makes
      the missing classification loud in CI rather than silent at runtime);
    - mutating needs a trusted binding — proof of WHICH utterance asked;
    - mutating needs the resolved role to clear `viewer` — proof that the
      speaker it names is allowed to. Both, not either.

    Scoped to the Discord transport by name, deliberately. That is the surface
    where ONE authorized session serves MANY identities, which is the whole
    reason the role is resolved per call there. The browser surface has a
    single participant and authorizes at session mint — both
    `POST /api/talk/session` and `POST /api/talk/tool` are admin-classified in
    `orchestration/route_policy.py`, so a browser call has already cleared an
    admin route gate before it reaches this function.
    """
    if transport != TRANSPORT_DISCORD_VOICE:
        return None
    if name in READ_ONLY_TALK_TOOLS:
        return None
    if name not in MUTATING_TALK_TOOLS:
        _log.error(
            "unclassified Discord voice tool %s — enforcing it as mutating", name
        )
    if not _binding_is_trusted(binding):
        _log.warning(
            "denied mutating Discord voice tool %s: %s", name, _binding_reason(binding)
        )
        return MUTATION_DENIAL.format(tool=name)
    if _ROLE_LEVEL.get(resolved_role, 0) < _ROLE_LEVEL["operator"]:
        # The speaker is known and provable — they are simply not allowed. Say
        # that, rather than reusing the attribution message, which would claim
        # we could not identify someone we identified fine. No ids in the text:
        # a denial must not become an allowlist oracle.
        _log.warning(
            "denied mutating Discord voice tool %s: speaker resolved as %s",
            name,
            resolved_role,
        )
        return ROLE_DENIAL.format(tool=name)
    return None


def execute_talk_tool(
    name: str,
    arguments: dict | None,
    *,
    transport: str | None = TRANSPORT_BROWSER,
    speaker_id: str | None = None,
    binding: object = None,
) -> str:
    """Dispatch one tool call and return plain text for the model to speak.

    `transport` + `speaker_id` + `binding` are THIS CALL's identity, resolved
    here and bound for the duration of the handler. Every tool that reaches a
    role gate reads that bound value, so authority always comes from the
    request rather than from whatever a previous call — or another process —
    left behind.

    Known tools never raise for execution failures — the error text goes to
    the model so it can say what broke. Unknown names raise TalkToolError.
    """

    handler = _HANDLERS.get(name)
    if handler is None:
        raise TalkToolError(f"unknown talk tool: {name!r}")
    # Resolve BEFORE the gate, not just before the handler: the gate's whole
    # job is to enforce the resolved role, so it has to be able to see it.
    resolved_role = resolve_request_role(
        transport=transport, speaker_id=speaker_id, binding=binding
    )
    denial = talk_tool_denial(
        name, transport=transport, binding=binding, resolved_role=resolved_role
    )
    if denial is not None:
        # Refused BEFORE the handler, so an unauthorized call spends nothing
        # and mutates nothing — not even the tool's own side effects.
        return denial
    role_token = _REQUEST_ROLE.set(resolved_role)
    try:
        output = handler(arguments or {})
    except kill_switches.KillSwitchDisabled:
        # A kill switch is an operator DECISION, not a tool failure. It keeps
        # its own shape all the way out of the gate (house contract), so map
        # it here into something speakable instead of letting it surface as
        # "run_archon failed: KillSwitchDisabled" or reach the route as an
        # unhandled 500 (codex R4 major). The route re-derives 503 + switch
        # name from its own guard.
        raise
    except Exception as exc:  # noqa: BLE001 — the model speaks the failure
        _log.warning("talk tool %s failed: %s: %s", name, type(exc).__name__, exc)
        return f"{name} failed: {type(exc).__name__}: {exc}"
    finally:
        # This call's identity dies with this call — never left bound for the
        # next one, whatever transport that one arrives on.
        _REQUEST_ROLE.reset(role_token)
    return output or "(no output)"


# -- handlers -----------------------------------------------------------------


def _handle_memory_search(arguments: dict) -> str:
    import memory_search  # noqa: PLC0415 — lazy: keeps session-mint import light

    query = str(arguments.get("query") or "").strip()
    if not query:
        return "memory_search needs a non-empty query."
    try:
        limit = int(arguments.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 8))
    results = memory_search.search(query, mode="hybrid", limit=limit)
    if not results:
        return f"No memory notes matched '{query}'."
    lines = []
    for result in results:
        title = result.section_title or result.path
        snippet = " ".join(result.text.split())[:_MAX_MEMORY_SNIPPET_CHARS]
        lines.append(f"- {title} ({result.path}): {snippet}")
    return f"Top {len(lines)} memory notes for '{query}':\n" + "\n".join(lines)


def _handle_calendar_events(arguments: dict) -> str:
    from integrations import calendar_api  # noqa: PLC0415 — lazy import

    try:
        days = int(arguments.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    days = max(0, min(days, 14))
    if days == 0:
        events = calendar_api.get_today_events()
        if not events:
            return "Nothing on the calendar today."
        return calendar_api.format_events_for_context(events)
    events = calendar_api.get_upcoming_events(days=days)
    if not events:
        return f"Nothing on the calendar in the next {days} days."
    return calendar_api.format_events_for_context(events)


def _handle_run_python(arguments: dict) -> str:
    if not code_exec_enabled():
        return (
            "run_python is disabled by the operator "
            f"(set {_CODE_EXEC_ENV}=1 in the Homie environment to enable it)."
        )
    code = str(arguments.get("code") or "")
    if not code.strip():
        return "run_python needs non-empty code."
    completed = subprocess.run(  # noqa: S603 — operator-gated voice capability
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=_CODE_EXEC_TIMEOUT_S,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        err = (completed.stderr or "").strip()[-500:]
        return f"run_python exited {completed.returncode}: {err}"
    return output[:_MAX_OUTPUT_CHARS] or "(no stdout)"


def _handle_run_shell(arguments: dict) -> str:
    """Run a shell command and RETURN ITS TEXT.

    The gap this closes: every action on the ``computer`` tool drives the
    desktop and captures nothing, so asking the voice surface for a repo list
    meant opening a terminal, typing into it, and then reading the pixels back
    with a ~30s screenshot-plus-vision call — four of them, in the session that
    prompted this. The output was already there; nothing was carrying it.

    Gated by the SAME switch as :func:`_handle_run_python` rather than its own.
    ``run_python`` can already spawn any subprocess it likes, so a second flag
    would imply a boundary that does not exist. One switch, honestly named.

    Runs in the REPO ROOT, not the scripts directory — ``gh``, ``git`` and
    ``uv`` are the point of this tool, and they are all repo-relative.
    """

    if not code_exec_enabled():
        return (
            "run_shell is disabled by the operator "
            f"(set {_CODE_EXEC_ENV}=1 in the Homie environment to enable it)."
        )
    command = str(arguments.get("command") or "").strip()
    if not command:
        return "run_shell needs a command."
    try:
        completed = subprocess.run(  # noqa: S602 — operator-gated voice capability
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_CODE_EXEC_TIMEOUT_S,
            cwd=str(_repo_root()),
        )
    except subprocess.TimeoutExpired:
        return (
            f"run_shell timed out after {_CODE_EXEC_TIMEOUT_S}s. "
            "Nothing was returned; the command may still be running."
        )
    except Exception as exc:  # noqa: BLE001 — a spoken answer beats a traceback
        return f"run_shell could not start that: {type(exc).__name__}: {exc}"

    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        err = (completed.stderr or "").strip()[-500:]
        # stdout often carries the useful part even on a non-zero exit (git and
        # gh both do this), so it is reported alongside rather than discarded.
        detail = f": {err}" if err else ""
        if output:
            return f"run_shell exited {completed.returncode}{detail}\n{output[:_MAX_OUTPUT_CHARS]}"
        return f"run_shell exited {completed.returncode}{detail}"
    # "(no output)" rather than "" — an empty string reads to the model as a
    # failure it should narrate, when a silent success is often the right
    # answer (mkdir, git add).
    return output[:_MAX_OUTPUT_CHARS] or "(no output)"


def _handle_homie_command(arguments: dict) -> str:
    """Run a router command through ExtensionManager.dispatch (collect_only).

    Mirrors what `thehomie chat -q "/cmd" -Q` does; commands whose handlers
    need engine context degrade to their error text, which the model speaks.
    """

    command = str(arguments.get("command") or "").strip().lstrip("/").split(" ")[0].lower()
    args = str(arguments.get("args") or "").strip()
    if not command:
        return "homie_command needs a command name."
    manager = _command_manager()
    if manager is None:
        return "router commands are unavailable in this process."
    import asyncio  # noqa: PLC0415 — lazy

    from models import IncomingMessage, User, Channel, Platform  # noqa: PLC0415

    incoming = IncomingMessage(
        text=f"/{command} {args}".strip(),
        user=User(platform=Platform.CLI, platform_id="voice"),
        channel=Channel(platform=Platform.CLI, platform_id="voice", is_dm=True),
        platform=Platform.CLI,
        # THIS request's identity, bound by `execute_talk_tool` from the wire:
        # on Discord voice that is the speaker who actually talked (resolved
        # here, in the process that owns the allowlist), so the operator who
        # opened the session cannot lend their admin to anyone else in the
        # channel. On the browser surface it is that session's own role.
        user_role=_REQUEST_ROLE.get(),
    )
    reply = asyncio.run(
        manager.dispatch(command, adapter=None, incoming=incoming, args=args, collect_only=True)
    )
    if reply is None:
        return f"'/{command}' is not a router command."
    return " ".join(str(reply).split())[:_MAX_OUTPUT_CHARS]


_COMMAND_MANAGER = None
_COMMAND_MANAGER_FAILED = False


def _command_manager():
    """Lazily build the router registry (chat dir joins sys.path on demand)."""

    global _COMMAND_MANAGER, _COMMAND_MANAGER_FAILED
    if _COMMAND_MANAGER is not None or _COMMAND_MANAGER_FAILED:
        return _COMMAND_MANAGER
    try:
        chat_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "chat")
        chat_dir = os.path.abspath(chat_dir)
        if chat_dir not in sys.path:
            sys.path.insert(0, chat_dir)
        from commands import COMMANDS, CATEGORIES, CORE_INTENTS  # noqa: PLC0415
        from core_handlers import CORE_HANDLERS, set_context  # noqa: PLC0415
        from extension_manager import ExtensionManager, set_manager  # noqa: PLC0415

        manager = ExtensionManager()
        manager.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)
        manager.register_core_intents(CORE_INTENTS)
        set_manager(manager)
        set_context(adapters={})
        _COMMAND_MANAGER = manager
    except Exception as exc:  # noqa: BLE001 — degrade, don't break the session
        _log.warning("homie_command registry unavailable: %s: %s", type(exc).__name__, exc)
        _COMMAND_MANAGER_FAILED = True
    return _COMMAND_MANAGER


_AGENT_LANE_ENV = "TALK_AGENT_LANE"
_AGENT_TIMEOUT_ENV = "TALK_AGENT_TIMEOUT_S"
_DEFAULT_AGENT_TIMEOUT_S = 1800
_VALID_LANES = ("codex", "claude", "gemini", "kimi")


def _convoy_service():
    """Open the orchestration DB the dashboard Work Queue also reads."""

    import config  # noqa: PLC0415 — lazy: dynamic config resolution (Rule 2)
    from orchestration.convoy_service import ConvoyService  # noqa: PLC0415
    from orchestration.db import OrchestrationDB  # noqa: PLC0415

    return ConvoyService(OrchestrationDB(config.ORCHESTRATION_DB_PATH))


def _create_voice_convoy(title: str, task: str) -> tuple[int | str, int | str]:
    """Create the ledger row for a voice-delegated task. (convoy_id, subtask_id)."""

    from orchestration.models import CreateConvoyInput, CreateSubtaskInput  # noqa: PLC0415

    created = _convoy_service().create_convoy(
        CreateConvoyInput(
            title=title[:180],
            description=task,
            created_by="voice",
            subtasks=[
                CreateSubtaskInput(
                    title=title[:180],
                    description=task,
                    metadata='{"source": "talk"}',
                )
            ],
        )
    )
    convoy = getattr(created, "convoy", None)
    subtask = created.subtasks[0] if getattr(created, "subtasks", None) else None
    return getattr(convoy, "id", "?"), getattr(subtask, "id", "?")


def _try_convoy_service():
    """Open the ledger, or ``None`` when it cannot be opened.

    A deploy that already cleared every gate must not die because the
    work-queue DB is locked or missing — the caller degrades to running
    without a row rather than refusing work it was told to do.
    """

    try:
        return _convoy_service()
    except Exception as exc:  # noqa: BLE001 — the work matters, the row is a record
        _log.warning("talk ledger unavailable: %s: %s", type(exc).__name__, exc)
        return None


def _create_voice_convoy_safely(title: str, task: str) -> tuple[int | str, int | str]:
    """:func:`_create_voice_convoy` that degrades to ``("?", "?")`` on failure."""

    try:
        return _create_voice_convoy(title, task)
    except Exception as exc:  # noqa: BLE001 — same reason as _try_convoy_service
        _log.warning("talk ledger row not created: %s: %s", type(exc).__name__, exc)
        return "?", "?"


def _agent_lane(requested: str | None = None) -> str:
    lane = (requested or "").strip().lower()
    if lane in _VALID_LANES:
        return lane
    env_lane = (os.environ.get(_AGENT_LANE_ENV) or os.environ.get(_SKILL_LANE_ENV) or "").strip()
    return env_lane or _DEFAULT_SKILL_LANE


def _agent_timeout_s() -> int:
    try:
        return int(os.environ.get(_AGENT_TIMEOUT_ENV) or _DEFAULT_AGENT_TIMEOUT_S)
    except ValueError:
        return _DEFAULT_AGENT_TIMEOUT_S


def _run_engine_lane(prompt: str, lane: str, timeout_s: int) -> str:
    """One engine-lane subprocess run; returns the reply text.

    Raises ``subprocess.TimeoutExpired`` so callers can decide what a timeout
    means for their run kind.
    """

    completed = subprocess.run(  # noqa: S603 — fixed argv, operator's own CLI
        ["uv", "run", "thehomie", "chat", "-m", lane, "-q", prompt, "-Q"],
        capture_output=True,
        text=True,
        # Lane replies are UTF-8; the Windows default codec would raise
        # mid-decode and lose an otherwise successful run.
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        cwd=Path(__file__).resolve().parent,
    )
    return _parse_quiet_envelope(completed, lane)


def _parse_quiet_envelope(completed: subprocess.CompletedProcess, lane: str) -> str:
    """Read the quiet-mode JSON envelope: the LAST stdout line starting with '{'."""

    envelope = None
    for line in reversed((completed.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                envelope = json.loads(line)
                break
            except ValueError:
                continue
    if envelope is None:
        err = (completed.stderr or completed.stdout or "").strip()[-400:]
        return f"the engine produced no usable reply (exit {completed.returncode}): {err}"
    if not envelope.get("success"):
        return f"engine lane '{lane}' failed: {envelope.get('error') or 'unknown engine error'}"
    response = str(envelope.get("response") or "").strip()
    return response[:_MAX_OUTPUT_CHARS] or "(the agent produced no text reply)"


def _ledger(action: str, call) -> None:
    """Best-effort work-queue bookkeeping — never fails the run it describes."""

    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the work matters, the row is a record
        _log.warning("talk ledger %s failed: %s: %s", action, type(exc).__name__, exc)


# ─── steerable agent turns ────────────────────────────────────────────────

#: 1 initial turn + this many steer follow-ups, then the run finishes with
#: whatever is queued named as undelivered.
_MAX_STEER_TURNS = 3
#: Below this much remaining budget a new turn is not worth spawning.
_SPAWN_FLOOR_S = 10
#: Cap for the context-reassembly fallback prompt (mirrors the skill body
#: cap; also keeps argv far from the Windows 32K CreateProcess limit).
_REASSEMBLY_MAX_CHARS = 12_000
#: The cli_adapter resume-miss warning, always emitted on stderr — a
#: belt-and-suspenders miss signal (envelope session_id equality cannot
#: distinguish a legitimate SDK id rotation from a miss).
_RESUME_MISS_MARKER = "not found, starting new session"
#: Strict-resume refusal in the quiet error envelope: the primary miss
#: signal. With --resume-strict the CLI ABORTS before the engine runs, so a
#: missed resume never executes a context-less turn with real side effects.
_STRICT_MISS_MARKER = "resume session not found"


def _kill_pid_tree(pid: int) -> None:
    """Kill a process tree by pid — the cancel path has only the annotated
    pid, never a Popen handle.

    On Windows a plain kill reaps only the direct child (``uv.exe``) and the
    engine grandchild survives — ``taskkill /T`` takes the tree. Third
    per-adapter copy of this shape (engine_archon.py:427 is the template);
    centralizing is a noted follow-up, duplication is the standing doctrine.
    """

    try:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 — fixed argv
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(pid, 9)
    except Exception as exc:  # noqa: BLE001 — best-effort; the wait decides
        _log.warning("talk agent tree-kill failed for pid %s: %s", pid, exc)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Popen-handle variant used by the timeout path inside a turn."""

    if sys.platform == "win32":
        _kill_pid_tree(proc.pid)
    else:
        try:
            proc.kill()
        except Exception as exc:  # noqa: BLE001
            _log.warning("talk agent kill failed: %s", exc)


def _run_agent_turn(
    prompt: str, lane: str, timeout_s: int, run_id: int, *, resume_sid: str = ""
) -> dict:
    """One agent conversation turn. Returns the parsed envelope as a dict.

    Popen + ``communicate`` (never ``wait`` — a chatty child fills the 64KB
    stdout pipe and ``wait`` deadlocks until the budget expires). The pid is
    annotated for the cancel path and CLEARED the moment the turn ends —
    Windows reuses pids, and a stale annotation would let a later cancel
    shoot an innocent process. On timeout the whole tree dies (the old
    ``subprocess.run`` kill reaped only ``uv.exe`` and orphaned the engine).
    """

    argv = ["uv", "run", "thehomie", "chat", "-m", lane]
    if resume_sid:
        # Strict: a missed resume ABORTS before the engine — a context-less
        # steer turn must never execute (it could act on real targets with
        # zero conversation context).
        argv += ["--resume", resume_sid, "--resume-strict"]
    argv += ["-q", prompt, "-Q"]
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, operator's own CLI
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=Path(__file__).resolve().parent,
    )
    if not talk_runs.attach_pid(run_id, proc.pid):
        # Cancel won the race between the worker's status check and this
        # spawn: the run is terminal, so this process must not run at all.
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return {
            "success": False,
            "response": "",
            "session_id": "",
            "error": "cancelled",
            "stderr": "",
        }
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            # Bounded post-kill drain (engine_archon.py:480-490 pattern) so
            # the pipes close and the process object is reaped.
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        talk_runs.annotate_run(run_id, pid=None)
    return _parse_envelope_fields(stdout, stderr, proc.returncode, lane)


def _parse_envelope_fields(
    stdout: str, stderr: str, returncode: int | None, lane: str
) -> dict:
    """Quiet envelope as a dict — the steer loop needs ``session_id`` and the
    captured stderr; the string-returning ``_parse_quiet_envelope`` stays as
    the skill path's contract."""

    envelope = None
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                envelope = json.loads(line)
                break
            except ValueError:
                continue
    if envelope is None:
        err = (stderr or stdout or "").strip()[-400:]
        return {
            "success": False,
            "response": "",
            "session_id": "",
            "error": f"the engine produced no usable reply (exit {returncode}): {err}",
            "stderr": stderr or "",
        }
    return {
        "success": bool(envelope.get("success")),
        "response": str(envelope.get("response") or "").strip(),
        "session_id": str(envelope.get("session_id") or "").strip(),
        "error": str(envelope.get("error") or "unknown engine error"),
        "stderr": stderr or "",
    }


def _reassemble_prompt(task: str, last_output: str, steer_text: str) -> str:
    """Fallback when the conversation cannot be resumed: carry the context
    by hand, budgeted per component with the STEERING reserved first — a
    tail-truncation once silently dropped the very course-correction the
    operator was promised (a chained skill task alone can be 12k chars)."""

    steer = steer_text[: _REASSEMBLY_MAX_CHARS // 3]
    remaining = _REASSEMBLY_MAX_CHARS - len(steer) - 300  # headers
    task_part = task[: max(0, remaining * 2 // 3)]
    output_part = last_output[: max(0, remaining - len(task_part))]
    return (
        "You are continuing a background task; the earlier conversation "
        "context could not be resumed, so it is restated here.\n\n"
        f"Original task:\n{task_part}\n\n"
        f"Your previous reply:\n{output_part}\n\n"
        f"Operator steering — apply this now:\n{steer}"
    )


def start_agent_run(task: str, title: str, lane: str) -> tuple[int, int | str]:
    """Create the ledger row and spawn the STEERABLE agent worker.

    Returns ``(run_id, subtask_id)``. The worker is a bounded turn loop:
    turn 1 runs the task; at every turn boundary it atomically drains
    queued operator steers or finishes (``drain_steers_or_finish`` — no
    window where a steer can vanish). Steer follow-ups resume the SAME
    conversation via the chained quiet-envelope ``session_id``; a resume
    miss (stderr marker) falls back to context re-assembly. Terminal
    status doubles as the cancel flag: manage_run's cancel finishes the
    run first, and the worker exits at its next check without touching
    the ledger again.
    """

    convoy_id, subtask_id = _create_voice_convoy(title, task)

    def worker(run_id: int) -> str:
        svc = _try_convoy_service()

        def ledger(action: str, call) -> None:
            if svc is not None and isinstance(subtask_id, int):
                _ledger(action, call)

        def cancelled() -> bool:
            run = talk_runs.get_run(run_id)
            return run is None or run["status"] in talk_runs.TERMINAL_STATUSES

        ledger(
            "dispatch",
            lambda: svc.dispatch_subtask(subtask_id, paperclip_issue_id=f"talk:{run_id}"),
        )
        ledger("running", lambda: svc.transition_subtask(subtask_id, "running"))

        # ONE deadline for the whole run — env re-reads mid-run must not
        # stretch or shrink the budget.
        deadline = time.monotonic() + _agent_timeout_s()
        resume_sid = ""
        last_output = ""
        steer_text = ""
        retry_reassembly = False
        turns_done = 0

        while True:
            if cancelled():
                return last_output
            remaining = deadline - time.monotonic()
            if remaining < _SPAWN_FLOOR_S:
                if turns_done == 0:
                    message = (
                        f"the agent run passed {_agent_timeout_s()} seconds "
                        "and was stopped"
                    )
                    ledger(
                        "failure",
                        lambda: svc.handle_subtask_failure(
                            subtask_id, error_message=message
                        ),
                    )
                    talk_runs.finish_run(run_id, "failed", message)
                    return message
                if steer_text:
                    last_output = (
                        f"{last_output} (ran out of budget before delivering "
                        "the queued steering)"
                    )
                talk_runs.finish_run(run_id, "done", last_output)
                ledger("completion", lambda: svc.handle_subtask_completion(subtask_id))
                return last_output

            if turns_done == 0:
                sid, turn_prompt = "", task
            elif retry_reassembly or not resume_sid:
                retry_reassembly = False
                sid, turn_prompt = "", _reassemble_prompt(task, last_output, steer_text)
            else:
                sid, turn_prompt = resume_sid, steer_text

            try:
                envelope = _run_agent_turn(
                    turn_prompt, lane, int(remaining), run_id, resume_sid=sid
                )
            except subprocess.TimeoutExpired:
                if turns_done == 0:
                    message = (
                        f"the agent run passed {_agent_timeout_s()} seconds "
                        "and was stopped"
                    )
                    ledger(
                        "failure",
                        lambda: svc.handle_subtask_failure(
                            subtask_id, error_message=message
                        ),
                    )
                    talk_runs.finish_run(run_id, "failed", message)
                    return message
                # A follow-up timing out must not discard turn 1's good reply.
                message = (
                    f"{last_output} (a steer follow-up timed out; the reply "
                    "above is the last completed turn)"
                )
                talk_runs.finish_run(run_id, "done", message)
                ledger("completion", lambda: svc.handle_subtask_completion(subtask_id))
                return message

            if cancelled():
                return last_output

            if sid and (
                _STRICT_MISS_MARKER in envelope["error"]
                or _RESUME_MISS_MARKER in envelope["stderr"]
            ):
                resume_sid = ""
                retry_reassembly = True
                if _STRICT_MISS_MARKER in envelope["error"]:
                    # STRICT miss: the CLI refused BEFORE the engine ran —
                    # nothing executed, so the retry is FREE (it must not
                    # charge the cap, or a final-slot miss silently drops
                    # the drained steer). It is structurally bounded: the
                    # re-assembly retry passes no --resume, so it cannot
                    # miss again.
                    continue
                # NON-strict miss: a context-less turn DID execute (older
                # CLI without strict support) — that cost charges the cap
                # like any turn, and at the cap the drained steer's loss is
                # named rather than silent.
                turns_done += 1
                if turns_done > _MAX_STEER_TURNS:
                    message = (
                        f"{last_output} (the last steer could not be "
                        "delivered — its resume missed and the retry budget "
                        "is spent)"
                    )
                    talk_runs.finish_run(run_id, "done", message)
                    ledger(
                        "completion",
                        lambda: svc.handle_subtask_completion(subtask_id),
                    )
                    return message
                continue

            if envelope["success"] and envelope["response"]:
                last_output = envelope["response"][:_MAX_OUTPUT_CHARS]
            elif turns_done == 0:
                # In-band lane failure: the agent finished its attempt, so
                # the run lands 'done' with the text and the row completes
                # (existing semantics) — and the boundary below still lets
                # a queued steer drive a retry turn.
                last_output = f"engine lane '{lane}' failed: {envelope['error']}"
            else:
                last_output = (
                    f"{last_output} (a steer follow-up failed: "
                    f"{envelope['error'][:200]})"
                )

            # Chain, don't verify: the runtime id legitimately rotates on
            # resume (claude lane), so the returned id is always the next
            # target. An empty id degrades future steers to re-assembly.
            resume_sid = envelope["session_id"]
            turns_done += 1

            if turns_done > _MAX_STEER_TURNS:
                # Cap reached: finish REGARDLESS — finish_run names any
                # still-queued steers as undelivered.
                talk_runs.finish_run(run_id, "done", last_output)
                ledger("completion", lambda: svc.handle_subtask_completion(subtask_id))
                return last_output

            steers = talk_runs.drain_steers_or_finish(run_id, last_output)
            if not steers:
                # The run is now done (or was cancelled underneath us).
                run = talk_runs.get_run(run_id)
                if run is not None and run["status"] == "done":
                    ledger(
                        "completion",
                        lambda: svc.handle_subtask_completion(subtask_id),
                    )
                return last_output
            steer_text = "\n".join(steers)

    run_id = talk_runs.start_run(
        "agent",
        title[:60],
        worker,
        meta={"subtask_id": subtask_id, "convoy_id": convoy_id, "lane": lane},
    )
    return run_id, subtask_id


def _handle_delegate_task(arguments: dict) -> str:
    """Run a task on a background agent; keep a work-queue row as the record.

    F5 hybrid boundary: ``scope="quick"`` (default) keeps the fast in-process
    lane; ``scope="substantial"`` deploys through Archon, which buys isolation
    and full telemetry at the cost of a clone plus a worktree (~53s before
    anything happens — Spike A receipt). The model picks, guided by the tool
    description; an unrecognised scope degrades to the cheap lane rather than
    silently paying for a worktree.
    """

    task = str(arguments.get("task") or "").strip()
    if not task:
        return "delegate_task needs the task itself."
    title = str(arguments.get("title") or "").strip() or " ".join(task.split())[:80]
    scope = str(arguments.get("scope") or "").strip().lower()
    if scope == "substantial":
        return _delegate_through_archon(task, title)
    lane = _agent_lane(arguments.get("lane"))
    run_id, subtask_id = start_agent_run(task, title, lane)
    return (
        f"{talk_runs.started_sentinel(run_id, 'agent', title[:60])} "
        f"Task #{subtask_id} is on the work queue and running now."
    )


def _delegate_through_archon(task: str, title: str) -> str:
    """The substantial branch: route a delegated task onto the Archon spine."""

    import talk_archon  # noqa: PLC0415 — lazy: keeps session-mint import light

    try:
        run_id, resolved, subtask_id = start_archon_run(
            talk_archon.default_workflow(),
            task,
            title=title,
            caller="talk.delegate_task",
        )
    except (TalkToolError, talk_archon.ArchonDispatchError) as exc:
        return str(exc)
    return (
        f"{talk_runs.started_sentinel(run_id, 'archon', resolved)} "
        f"That one is substantial, so it's deploying through Archon on the "
        f"{resolved} workflow; task #{subtask_id} is on the work queue."
    )


_SKILL_LANE_ENV = "TALK_SKILL_LANE"
_SKILL_TIMEOUT_ENV = "TALK_SKILL_TIMEOUT_S"
_DEFAULT_SKILL_LANE = "codex"  # quota-safe default per AGENTS.md guidance
_DEFAULT_SKILL_TIMEOUT_S = 300  # async runs — voice never blocks on this
_SKILL_BODY_MAX_CHARS = 12_000  # mirrors engine.SKILL_PROMPT_BLOCK_MAX_CHARS


def _skill_roots() -> list[Path]:
    """Skill search roots in precedence order (project before user)."""

    repo = Path(__file__).resolve().parents[2]
    home = Path.home()
    return [
        repo / ".agents" / "skills",
        repo / ".kimi-code" / "skills",
        home / ".agents" / "skills",
        home / ".kimi-code" / "skills",
    ]


def resolve_skill(name: str) -> Path:
    """Resolve a skill name to its SKILL.md across the known roots.

    Exact -> case-insensitive -> substring -> TalkToolError with close-match
    suggestions so the model can self-correct on the next call.
    """

    wanted = name.strip().lower().replace(" ", "-").replace("_", "-")
    if not wanted:
        raise TalkToolError("run_skill needs a skill name")
    candidates: list[Path] = []
    for root in _skill_roots():
        if root.is_dir():
            candidates.extend(sorted(root.rglob("SKILL.md")))
    names: dict[str, Path] = {}
    for candidate in candidates:
        names.setdefault(candidate.parent.name.lower(), candidate)
    if wanted in names:
        return names[wanted]
    for skill_name, path in names.items():
        if wanted in skill_name:
            return path
    import difflib

    close = difflib.get_close_matches(wanted, names.keys(), n=5, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise TalkToolError(f"no skill named {name!r}.{hint}")


def _skill_timeout_s() -> int:
    try:
        return int(os.environ.get(_SKILL_TIMEOUT_ENV) or _DEFAULT_SKILL_TIMEOUT_S)
    except ValueError:
        return _DEFAULT_SKILL_TIMEOUT_S


def _skill_prompt(skill_md: Path, body: str, user_input: str) -> str:
    return (
        f"Run the '{skill_md.parent.name}' skill with this operator input: "
        f"\"{user_input or 'no specific input'}\"\n\n"
        f"## Skill Body ({skill_md.name})\n\n{body}"
    )


def start_skill_run(name: str, user_input: str) -> tuple[int, str]:
    """Resolve + spawn an async skill run; returns (run_id, skill_name).

    Raises TalkToolError for unknown skills (with close-match hints) and for
    unreadable SKILL.md files — the tool handler renders those as speech.
    """

    skill_md = resolve_skill(name)
    try:
        body = skill_md.read_text(encoding="utf-8")[:_SKILL_BODY_MAX_CHARS]
    except OSError as exc:
        raise TalkToolError(f"could not read skill {name}: {exc}") from exc
    skill_name = skill_md.parent.name
    prompt = _skill_prompt(skill_md, body, user_input)
    lane = _agent_lane()

    def worker(run_id: int) -> str:
        try:
            return _run_engine_lane(prompt, lane, _skill_timeout_s())
        except subprocess.TimeoutExpired:
            # A long skill is not a failed skill: hand the SAME prompt to a
            # background agent (longer budget, work-queue row) and point the
            # poller at the new run instead of claiming a false success.
            title = f"skill {skill_name} continued"
            chained_id, subtask_id = start_agent_run(prompt, title, lane)
            message = (
                f"The {skill_name} skill passed {_skill_timeout_s()} seconds, so it moved to "
                f"a background agent. {talk_runs.started_sentinel(chained_id, 'agent', title)} "
                f"Task #{subtask_id} on the work queue."
            )
            talk_runs.finish_run(run_id, "failed", message)
            return message

    run_id = talk_runs.start_run(
        "skill", skill_name, worker, meta={"skill": skill_name, "input": user_input}
    )
    return run_id, skill_name


def get_skill_run(run_id: int) -> dict | None:
    """Back-compat alias for the original skill-run poll route."""

    return talk_runs.get_run(run_id)


def _handle_run_skill(arguments: dict) -> str:
    """Async: start the run and return the started marker immediately.

    The Realtime model speaks the receipt; the Talk page polls
    ``/api/talk/runs/<id>`` and injects the result when it lands.
    """

    name = str(arguments.get("name") or "").strip()
    user_input = str(arguments.get("input") or "").strip()
    if not name:
        return "run_skill needs a skill name."
    try:
        run_id, skill_name = start_skill_run(name, user_input)
    except TalkToolError as exc:
        return str(exc)
    return (
        f"{talk_runs.started_sentinel(run_id, 'skill', skill_name)} "
        "I'll report back when it lands."
    )


# -- archon workflows ---------------------------------------------------------

_ARCHON_POLL_ENV = "TALK_ARCHON_POLL_S"
_ARCHON_BUDGET_ENV = "TALK_ARCHON_BUDGET_S"
_ARCHON_DB_ENV = "TALK_ARCHON_DB"
_DEFAULT_ARCHON_POLL_S = 15
_DEFAULT_ARCHON_BUDGET_S = 3 * 60 * 60
_ARCHON_TERMINAL = ("completed", "failed", "cancelled", "error", "done")
_ARCHON_OK_TERMINAL = ("completed", "done")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _archon_settings() -> dict:
    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name) or default)
        except ValueError:
            return default

    db_override = (os.environ.get(_ARCHON_DB_ENV) or "").strip()
    return {
        "poll_s": _int_env(_ARCHON_POLL_ENV, _DEFAULT_ARCHON_POLL_S),
        "budget_s": _int_env(_ARCHON_BUDGET_ENV, _DEFAULT_ARCHON_BUDGET_S),
        "db": Path(db_override) if db_override else Path.home() / ".archon" / "archon.db",
    }


def resolve_workflow(name: str) -> str:
    """Resolve a spoken workflow name against the repo's workflow YAMLs."""

    wanted = name.strip().lower().replace(" ", "-").replace("_", "-")
    if not wanted:
        raise TalkToolError("run_archon needs a workflow name")
    workflow_dir = _repo_root() / ".archon" / "workflows"
    names = sorted(p.stem for p in workflow_dir.glob("*.yaml")) if workflow_dir.is_dir() else []
    if not names:
        raise TalkToolError("no Archon workflows are installed in this repo")
    lowered = {n.lower(): n for n in names}
    if wanted in lowered:
        return lowered[wanted]
    for candidate_lower, candidate in lowered.items():
        if wanted in candidate_lower:
            return candidate
    import difflib  # noqa: PLC0415

    close = difflib.get_close_matches(wanted, list(lowered), n=5, cutoff=0.4)
    hint = f" Did you mean: {', '.join(lowered[c] for c in close)}?" if close else ""
    raise TalkToolError(f"no Archon workflow named {name!r}.{hint}")


def _archon_run_row(db_path: Path, run_id: str) -> dict | None:
    """Read one Archon run row through a write-refusing URI (physical truth)."""

    import sqlite3  # noqa: PLC0415

    if not db_path.exists():
        return None
    columns = (
        "id",
        "workflow_name",
        "status",
        "working_path",
        "started_at",
        "completed_at",
        # The steering bound: a run this Homie may target has to belong to this
        # Homie's Archon project, and that is decided by the ledger row.
        "codebase_id",
    )
    try:
        connection = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            row = connection.execute(
                f"SELECT {', '.join(columns)} FROM remote_agent_workflow_runs WHERE id = ?",
                (str(run_id),),
            ).fetchone()
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 — a locked/absent DB is "unknown", never fatal
        return None
    return dict(zip(columns, row)) if row else None


def recent_archon_runs(limit: int = 5) -> list[dict]:
    """Most recent Archon runs from the ledger, newest first."""

    import sqlite3  # noqa: PLC0415

    db_path = _archon_settings()["db"]
    if not db_path.exists():
        return []
    columns = ("id", "workflow_name", "status", "started_at", "completed_at")
    try:
        connection = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM remote_agent_workflow_runs "
                "ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            connection.close()
    except Exception:  # noqa: BLE001
        return []
    return [dict(zip(columns, row)) for row in rows]


def start_archon_run(
    workflow: str,
    brief: str,
    *,
    title: str | None = None,
    caller: str = "talk.run_archon",
) -> tuple[int, str, int | str]:
    """Deploy a workflow through Archon. ``(run_id, workflow, subtask_id)``.

    Everything that can refuse runs SYNCHRONOUSLY here — workflow resolution,
    the kill switch, the capability policy, the F2 brief contract, the
    codebase binding, and the work-queue row — so a refusal is spoken on this
    turn instead of landing minutes later as a run result. What is left is
    detached into a ``talk_runs`` worker thread: the HTTP dispatch, the
    ledger writes and the status poll. No Archon call ever touches a caller's
    event loop (the 2026-07-13 wedge class), and the caller gets a bounded
    immediate reply.

    There is no confirmation step: a dispatch is bounded to a worktree and
    tokens, which is the operator's free tier. Spend and outward mutations
    are approved where they happen, not here.

    Raises:
        TalkToolError: unknown workflow name (carries close-match hints).
        talk_archon.ArchonDispatchRefusedError: kill switch, policy, vague
            brief, no resolvable codebase, or no work-queue row.
    """

    import talk_archon  # noqa: PLC0415 — lazy: keeps session-mint import light

    brief = str(brief or "").strip()
    try:
        resolved = resolve_workflow(workflow)
    except TalkToolError as exc:
        talk_archon.audit_attempt(
            workflow=str(workflow or "").strip(),
            outcome="refused_unknown_workflow",
            caller=caller,
            brief_preview=brief,
            error=str(exc),
        )
        raise

    grant = talk_archon.require_dispatch_allowed(
        resolved, brief, caller=caller, repo_root=_repo_root()
    )
    settings = _archon_settings()
    label = (title or resolved)[:60]
    # The convoy row is the SPECIFIED home of the correlation key (#257/#258/
    # #259 join on it), so it is a precondition of dispatch, not bookkeeping
    # that may fail quietly (codex R2 major). Created BEFORE the external call
    # so a ledger that cannot record the work refuses it instead of leaving a
    # running Archon worker nothing can join to. talk_runs is in-memory and
    # cannot stand in for it across an API restart.
    try:
        convoy_id, subtask_id = _create_voice_convoy(label, brief)
    except Exception as exc:  # noqa: BLE001 — no join, no dispatch
        talk_archon.audit_attempt(
            workflow=resolved,
            outcome="refused_no_ledger_row",
            caller=caller,
            brief_preview=brief,
            codebase_id=grant.codebase_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise talk_archon.ArchonDispatchRefusedError(
            "I could not create the work-queue row that tracks this deploy, so "
            f"I am not starting it: {exc}"
        ) from exc

    def worker(run_id: int) -> str:
        svc = _try_convoy_service()

        def ledger(action: str, call) -> None:
            """Ledger bookkeeping, skipped entirely when there is no row to keep.

            The work is the point; the row is a record. A ledger that could not
            be opened must not stop a deploy that already cleared every gate.
            """

            if svc is None or not isinstance(subtask_id, int):
                return
            _ledger(action, call)

        try:
            dispatch = talk_archon.dispatch_now(grant, run_id=run_id)
        except talk_archon.ArchonDispatchError as exc:
            message = f"I could not deploy {resolved} through Archon: {exc}"
            ledger(
                "failure",
                lambda: svc.handle_subtask_failure(subtask_id, error_message=str(exc)),
            )
            talk_runs.finish_run(run_id, "failed", message)
            return message

        try:
            correlation_ref = talk_archon.build_correlation_ref(
                run_id, dispatch.conversation_db_id, dispatch.conversation_id
            )
        except ValueError as exc:
            # Archon's ids are remote input, and a shape that cannot be encoded
            # into the ref means the LOAD-BEARING join (#257/#258/#259) is
            # gone. The work is already running so there is nothing to roll
            # back, but silently completing with a legacy `talk:<run_id>` ref
            # would report success for a run nothing can steer (codex R3
            # major). Preserve both raw ids in the durable audit trail and end
            # the run as failed so the loss is visible.
            _log.warning("talk archon correlation ref unencodable: %s", exc)
            talk_archon.audit_attempt(
                workflow=resolved,
                outcome="correlation_unpersisted",
                caller=caller,
                run_id=run_id,
                conversation_id=dispatch.conversation_id,
                conversation_db_id=dispatch.conversation_db_id,
                codebase_id=grant.codebase_id,
                error=f"correlation ref unencodable: {exc}",
            )
            message = (
                f"I deployed {resolved} through Archon (conversation "
                f"{dispatch.conversation_id}), but Archon returned an id shape "
                "I cannot key on, so live tracking cannot join this run. The "
                "run itself is going; both ids are in the dispatch audit log."
            )
            talk_runs.annotate_run(
                run_id,
                workflow=resolved,
                archon_conversation_id=dispatch.conversation_id,
                archon_conversation_db_id=dispatch.conversation_db_id,
                archon_dispatch_status=dispatch.status,
            )
            talk_runs.finish_run(run_id, "failed", message)
            return message
        talk_runs.annotate_run(
            run_id,
            workflow=resolved,
            archon_conversation_id=dispatch.conversation_id,
            archon_conversation_db_id=dispatch.conversation_db_id,
            archon_dispatch_status=dispatch.status,
            correlation_ref=correlation_ref,
        )
        # The correlation key MUST reach the convoy row: it is the join
        # #257/#258/#259 use, and the dispatch has already happened so there
        # is nothing to roll back. Write it directly (not through the
        # best-effort `ledger` helper) and, if it will not land, say so in the
        # run's own outcome instead of reporting a clean success — plus a
        # durable audit row carrying the full join so a re-key is mechanical.
        correlation_persisted = False
        if svc is not None and isinstance(subtask_id, int):
            for attempt in (1, 2):
                try:
                    svc.dispatch_subtask(subtask_id, paperclip_issue_id=correlation_ref)
                    correlation_persisted = True
                    break
                except Exception as exc:  # noqa: BLE001 — retried, then surfaced
                    _log.warning(
                        "talk correlation write attempt %s failed: %s: %s",
                        attempt,
                        type(exc).__name__,
                        exc,
                    )
        if not correlation_persisted:
            talk_archon.audit_attempt(
                workflow=resolved,
                outcome="correlation_unpersisted",
                caller=caller,
                run_id=run_id,
                conversation_id=dispatch.conversation_id,
                conversation_db_id=dispatch.conversation_db_id,
                codebase_id=grant.codebase_id,
                error="convoy row did not receive the correlation key",
            )
            message = (
                f"I deployed {resolved} through Archon (conversation "
                f"{dispatch.conversation_id}), but the work-queue row never "
                "took the correlation key, so live tracking cannot join this "
                "run. The run itself is going; the join is in the dispatch "
                "audit log."
            )
            talk_runs.finish_run(run_id, "failed", message)
            return message
        # (The dispatch write carrying the correlation key already ran above,
        # mandatorily — paperclip_issue_id is not patchable afterwards.)
        ledger("running", lambda: svc.transition_subtask(subtask_id, "running"))

        started = time.time()
        archon_run_id: str | None = None
        while time.time() - started < settings["budget_s"]:
            time.sleep(settings["poll_s"])
            if archon_run_id is None:
                archon_run_id = talk_archon.run_id_for_conversation(
                    dispatch.conversation_db_id, db_path=settings["db"]
                )
                if archon_run_id is None:
                    continue
                talk_runs.annotate_run(run_id, archon_run_id=archon_run_id)
            row = _archon_run_row(settings["db"], archon_run_id)
            if row is None:
                continue
            status = str(row.get("status") or "").lower()
            talk_runs.annotate_run(run_id, archon_status=status)
            if status in _ARCHON_TERMINAL:
                minutes = int((time.time() - started) / 60)
                where = row.get("working_path") or "no worktree recorded"
                if status in _ARCHON_OK_TERMINAL:
                    ledger("completion", lambda: svc.handle_subtask_completion(subtask_id))
                else:
                    ledger(
                        "failure",
                        lambda: svc.handle_subtask_failure(
                            subtask_id, error_message=f"archon run {status}"
                        ),
                    )
                return (
                    f"Archon run {archon_run_id} ({resolved}) finished with status {status} "
                    f"after about {minutes} minutes. Worktree: {where}."
                )
        hours = round(settings["budget_s"] / 3600, 1)
        watched = archon_run_id or f"on conversation {dispatch.conversation_id}"
        return (
            f"Archon run {watched} ({resolved}) is still going after {hours} hours, so I "
            "stopped watching it. It keeps running — ask me to check work for the latest."
        )

    run_id = talk_runs.start_run(
        "archon",
        resolved,
        worker,
        meta={
            "workflow": resolved,
            "subtask_id": subtask_id,
            "convoy_id": convoy_id,
            "codebase_id": grant.codebase_id,
        },
    )
    return run_id, resolved, subtask_id


def _handle_run_archon(arguments: dict) -> str:
    import talk_archon  # noqa: PLC0415 — lazy: keeps session-mint import light

    workflow = str(arguments.get("workflow") or "").strip()
    brief = str(arguments.get("brief") or "").strip()
    if not workflow:
        return "run_archon needs a workflow name."
    if not brief:
        return "run_archon needs a brief describing the work."
    try:
        run_id, resolved, subtask_id = start_archon_run(workflow, brief)
    except (TalkToolError, talk_archon.ArchonDispatchError) as exc:
        return str(exc)
    return (
        f"{talk_runs.started_sentinel(run_id, 'archon', resolved)} "
        f"The {resolved} workflow is deploying through Archon into its own worktree; "
        f"task #{subtask_id} is on the work queue. I'll tell you when it lands."
    )


# -- computer use -------------------------------------------------------------


def _require_computer_use() -> str | None:
    """Operator kill-switch for the whole physical-action surface."""

    from security import kill_switches  # noqa: PLC0415 — Rule 3: module attribute

    try:
        kill_switches.requireEnabled("computer_use", caller="talk_tools.computer")
    except kill_switches.KillSwitchDisabled:
        return "computer use is switched off by the operator right now."
    return None


def _handle_computer(arguments: dict) -> str:
    blocked = _require_computer_use()
    if blocked:
        return blocked

    import talk_computer  # noqa: PLC0415 — lazy: keeps GUI deps off the import path

    action = str(arguments.get("action") or "").strip().lower()
    text = str(arguments.get("text") or "").strip()
    window_title = str(arguments.get("window_title") or "").strip()
    settings = talk_computer.get_computer_settings()
    queue_path = settings["queue_path"]

    try:
        if action in ("open_terminal", "run_command", "open_url", "open_file", "notify"):
            talk_computer.ensure_desktop_agent(queue_path)

        if action == "open_terminal":
            command = text or f'cd /d "{_repo_root()}"'
            talk_computer.queue_desktop_command(
                queue_path,
                {"action": "run", "command": command, "title": window_title or "Homie Terminal"},
            )
            return f"Opened a terminal running: {command}"

        if action == "run_command":
            if not text:
                return "run_command needs the command to run."
            talk_computer.queue_desktop_command(
                queue_path,
                {"action": "run", "command": text, "title": window_title or "Homie Command"},
            )
            return f"Running '{text}' in a new terminal."

        if action == "open_url":
            url = str(arguments.get("url") or "").strip()
            if not url:
                return "open_url needs a URL."
            talk_computer.queue_desktop_command(queue_path, {"action": "open-url", "url": url})
            return f"Opened {url}."

        if action == "open_file":
            path = str(arguments.get("path") or "").strip()
            if not path:
                return "open_file needs a file path."
            talk_computer.queue_desktop_command(queue_path, {"action": "open-file", "path": path})
            return f"Opened {path}."

        if action == "notify":
            if not text:
                return "notify needs a message."
            talk_computer.queue_desktop_command(
                queue_path,
                {"action": "notify", "title": window_title or "The Homie", "message": text},
            )
            return "Sent the notification."

        if action == "type_into_window":
            if not window_title:
                return "type_into_window needs part of the window's title."
            if not text:
                return "type_into_window needs the text to type."
            press_enter = arguments.get("press_enter")
            return talk_computer.type_into_window(
                window_title, text, press_enter=True if press_enter is None else bool(press_enter)
            )

        if action == "press_keys":
            keys = str(arguments.get("keys") or "").strip()
            if not keys:
                return "press_keys needs a key or combo."
            return talk_computer.press_keys(keys, window_title or None)

        if action == "click":
            x = arguments.get("x")
            y = arguments.get("y")
            return talk_computer.click(
                int(x) if x is not None else None,
                int(y) if y is not None else None,
                window_title or None,
            )

        if action == "look_at_screen":
            png = talk_computer.capture_screen()

            def worker(_run_id: int) -> str:
                return talk_computer.describe_screen(png)

            run_id = talk_runs.start_run("look", "screen", worker, meta={"png": str(png)})
            return (
                f"{talk_runs.started_sentinel(run_id, 'look', 'screen')} "
                "Give me about thirty seconds to look at it."
            )

        return f"'{action}' isn't a computer action I have."
    except talk_computer.ComputerError as exc:
        return str(exc)


def _handle_browse(arguments: dict) -> str:
    """Read/navigate the visible browser through the router's gated commands."""

    blocked = _require_computer_use()
    if blocked:
        return blocked

    action = str(arguments.get("action") or "").strip().lower()
    if action not in ("status", "tabs", "open", "snapshot"):
        return f"'{action}' isn't a browser action I have."
    args = action
    if action == "open":
        url = str(arguments.get("url") or "").strip()
        if not url:
            return "browse open needs an absolute URL."
        args = f"open {url}"
    return _handle_homie_command({"command": "browser", "args": args})


# -- steering (#259) ----------------------------------------------------------

_MANAGE_RUN_ACTIONS = (
    "help",
    "list",
    "get",
    "say",
    "approve",
    "reject",
    "resume",
    "cancel",
    "abandon",
)

#: Statuses that mean "this run can still be steered". A terminal run has
#: nothing to approve and nothing to cancel, so it is never the implicit target.
_ARCHON_ACTIVE = ("paused", "running", "pending")

_MANAGE_RUN_HELP = (
    "manage_run steers work that is already executing. For Archon runs: "
    "'list' shows what is going and what is paused; 'get' says which node a "
    "run is on and what it just did; 'say' sends your words to the run, which "
    "on a paused run is the approval; 'approve' and 'reject' answer a gate; "
    "'resume' restarts a stopped run; 'cancel' and 'abandon' stop one. "
    "Archon steering only reaches a run whose workflow has an authored pause "
    "point — anything else can be cancelled but not redirected. For "
    "background agents (delegate_task receipt numbers): 'say' QUEUES the "
    "words and they land at the agent's next turn boundary — not instantly — "
    "and 'cancel' stops the agent now; agents have no gates, so the other "
    "actions don't apply. Reject, cancel and abandon preview first and need "
    "a confirm."
)


def archon_runs_by_status(
    statuses: tuple[str, ...],
    limit: int = 10,
    *,
    codebase_id: str | None = None,
) -> list[dict]:
    """Runs in any of ``statuses``, newest first, from the ro ledger.

    Rule 2: pausedness is read from the ledger row at call time, not from a
    cached list or from what the Homie remembers dispatching. Archon is the one
    that knows a run paused, and it may have paused a run this process never
    saw. Degrades to ``[]`` on a missing/locked ledger — never raises.

    ``codebase_id`` bounds the result to ONE Archon project. Archon's own
    mirrored tool is project-scoped (``manage-run-tool.ts:211-223``) and this
    was not: the query filtered on status alone, so a blank "approve it" could
    resolve to — and mutate — a paused run belonging to an entirely different
    codebase that happened to be the only one waiting. Callers that STEER pass
    it; a caller that only wants to look may leave it ``None`` and see
    everything, which is the read-only dashboard's job.
    """

    import sqlite3  # noqa: PLC0415

    if not statuses:
        return []
    db_path = _archon_settings()["db"]
    if not db_path.exists():
        return []
    columns = ("id", "workflow_name", "status", "started_at", "working_path")
    placeholders = ",".join("?" for _ in statuses)
    where = f"status IN ({placeholders})"
    params: list = [*statuses]
    if codebase_id:
        where += " AND codebase_id = ?"
        params.append(str(codebase_id))
    params.append(int(limit))
    try:
        connection = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM remote_agent_workflow_runs "
                f"WHERE {where} ORDER BY started_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger is "unknown"
        _log.warning("archon run status read failed: %s: %s", type(exc).__name__, exc)
        return []
    return [dict(zip(columns, row)) for row in rows]


def _steerable_codebase_id() -> str:
    """This Homie's Archon codebase id, or ``""`` when it cannot be resolved.

    The bound for every run this tool may TARGET. Resolution reads physical
    state (the registered codebase rows matched against the real repo root), so
    a stale env value cannot widen the blast radius. Failure returns ``""``,
    and callers treat that as "cannot bound" rather than "no bound" — the
    difference between refusing to guess and steering someone else's run.
    """

    try:
        import talk_archon  # noqa: PLC0415

        return str(talk_archon.resolve_codebase_id() or "")
    except Exception as exc:  # noqa: BLE001 — unresolvable is a bound, not a crash
        _log.warning("codebase resolution failed: %s: %s", type(exc).__name__, exc)
        return ""


def _tool_call_lines(archon_run_id: str, limit: int = 3) -> list[str]:
    """The run's most recent tool calls, newest last. Never raises.

    This is the "what is it actually doing" half of narration. The event rows
    are read through the DISPLAY reader on purpose — ``tool_input`` is
    LLM-authored and this text is spoken back, so the capped, secret-redacted
    copy is exactly right here (the uncapped reader is control-plane only).
    """

    try:
        from integrations import archon_events  # noqa: PLC0415 — Rule 3 module attr

        events, status = archon_events.read_recent_events(
            run_id=archon_run_id,
            limit=max(1, limit) * 8,
            db_path=_archon_settings()["db"],
        )
    except Exception as exc:  # noqa: BLE001 — narration never breaks a status reply
        _log.warning("archon tool-call read failed: %s: %s", type(exc).__name__, exc)
        return []
    if status != "ok":
        return []
    lines: list[str] = []
    for event in reversed(events):
        if event.get("type") != "tool_called":
            continue
        data = event.get("data") or {}
        name = str(data.get("tool_name") or "").strip() or "a tool"
        step = str(event.get("stepName") or "").strip()
        lines.append(f"{name} in {step}" if step else name)
        if len(lines) >= max(1, limit):
            break
    return list(reversed(lines))


def narrate_archon_run(archon_run_id: str, *, tool_calls: int = 3) -> str:
    """Speakable answer to "how's it going?" for one Archon run.

    Three physical reads, each degrading independently: the run row (status,
    worktree), the newest node event (where it is), and the newest tool calls
    (what it is doing). A read that fails is simply absent from the sentence —
    an operator asking for a status update must never get an exception instead.
    """

    row = _archon_run_row(_archon_settings()["db"], archon_run_id)
    if row is None:
        return f"Archon has no run {archon_run_id} in its ledger."
    status = str(row.get("status") or "unknown")
    workflow = row.get("workflow_name") or "a workflow"
    parts = [f"Run {archon_run_id} ({workflow}): {status}"]

    try:
        from integrations import archon_events  # noqa: PLC0415 — Rule 3 module attr

        node, node_status = archon_events.read_current_node(
            archon_run_id, db_path=_archon_settings()["db"]
        )
    except Exception as exc:  # noqa: BLE001 — the status line still stands alone
        _log.warning("archon node read failed: %s: %s", type(exc).__name__, exc)
        node, node_status = None, "unreadable"
    if node and node_status == "ok":
        parts.append(f"on node '{node['currentNode']}' ({node['nodeStatus']})")
    parts.append(f"worktree {row.get('working_path') or 'not recorded'}")
    summary = ", ".join(parts) + "."

    calls = _tool_call_lines(archon_run_id, limit=tool_calls)
    if calls:
        summary += f" Recent tool calls: {', '.join(calls)}."
    if status == "paused":
        summary += (
            " It is PAUSED waiting on you — approve it, reject it, or say "
            "something to it."
        )
    return summary


def resolve_archon_run(reference: str | None) -> tuple[str, str]:
    """Turn what the operator said into an Archon run id. ``(run_id, error)``.

    Exactly one of the two is non-empty. Three inputs are accepted because a
    run id is a 32-character hex string nobody says out loud:

    * blank — the single paused run, else the single active run. Two candidates
      is an ambiguity, not a coin flip, so it names them and asks.
    * a WORK_STARTED receipt number (``3`` / ``#3``) — resolved through the
      in-session registry, which is where the Archon id was recorded.
    * an Archon run id, which is validated rather than trusted.
    """

    reference = str(reference or "").strip().lstrip("#")

    # Every run this function hands back is a MUTATION target, so it is bounded
    # to this Homie's Archon project whenever the project can be resolved. This
    # box has ten codebases registered, and before the bound a blank "approve
    # it" resolved against ALL of them — one unrelated paused run was enough to
    # make the single-candidate path pick someone else's work and steer it.
    #
    # An unresolvable codebase degrades to unbounded rather than refusing: the
    # ledger may legitimately have no codebase table yet, and bricking the whole
    # steering surface is a worse failure than the residual risk. That risk is
    # already narrow — the multi-candidate branch below refuses to guess, so the
    # only exposed case is a SINGLE paused run that is not ours.
    scope = _steerable_codebase_id()
    if not scope:
        _log.warning(
            "manage_run: codebase unresolved — run targeting is unbounded this call"
        )

    if not reference:
        for statuses, label in ((("paused",), "paused"), (_ARCHON_ACTIVE, "going")):
            rows = archon_runs_by_status(statuses, codebase_id=scope)
            if len(rows) == 1:
                return (str(rows[0]["id"]), "")
            if len(rows) > 1:
                named = "; ".join(
                    f"{row['workflow_name']} ({row['id']})" for row in rows[:5]
                )
                return ("", f"There are {len(rows)} runs {label}: {named}. Which one?")
        # Archon has nothing — but a background AGENT may be mid-run, and
        # answering "nothing is running" while one burns is a lie.
        agents = [
            r
            for r in talk_runs.list_runs(20)
            if r.get("kind") == "agent" and r.get("status") == "running"
        ]
        if agents:
            named = "; ".join(f"#{r['runId']} {r['label']}" for r in agents[:5])
            return (
                "",
                "Nothing is running or paused in Archon, but background "
                f"agent(s) are going: {named}. Say the receipt number to "
                "steer or cancel one.",
            )
        return ("", "Nothing is running or paused in Archon right now.")

    if reference.isdigit() and len(reference) <= 6:
        # A short all-digit token is a receipt number, never an Archon id
        # (those are 32 hex chars). Anything longer falls through to the id
        # branch so a real numeric-looking id is not swallowed here.
        run = talk_runs.get_run(int(reference))
        if run is None:
            return ("", f"I don't have a run #{reference} in this session.")
        archon_run_id = (run.get("meta") or {}).get("archon_run_id")
        if not archon_run_id:
            kind = str(run.get("kind") or "")
            if kind == "agent":
                # manage_run routes agent receipts before this resolver; this
                # arm keeps any OTHER caller honest.
                return (
                    "",
                    f"Run #{reference} is a background agent — manage_run can "
                    "say/cancel it directly; it never gets an Archon id.",
                )
            if kind in ("skill", "look"):
                return (
                    "",
                    f"Run #{reference} is a {kind} run — check_work reads it; "
                    "manage_run steers Archon runs and background agents.",
                )
            return (
                "",
                f"Run #{reference} ({run.get('label')}) has not been matched to an "
                "Archon run yet — Archon may still be starting it.",
            )
        return (str(archon_run_id), "")

    import talk_archon  # noqa: PLC0415 — lazy: keeps session-mint import light

    if not talk_archon._RUN_ID_RE.match(reference):
        return ("", f"'{reference}' is not an Archon run id or a receipt number.")

    # A well-formed id was previously trusted on shape alone, so a run id from
    # another codebase — pasted, misheard, or hallucinated — reached steer_now
    # and mutated someone else's work. The ledger row decides whether it is
    # ours. An id with no row is left alone: Archon may simply not have written
    # it yet, and refusing a real run is worse than the caller's own 404.
    row = _archon_run_row(_archon_settings()["db"], reference)
    if scope and row and str(row.get("codebase_id") or "") not in ("", scope):
        return (
            "",
            f"Run {reference} belongs to a different Archon project, so I am "
            "not going to touch it from here.",
        )
    return (reference, "")


def _manage_run_list() -> str:
    """Paused runs first — that is the set actually waiting on the operator."""

    paused = archon_runs_by_status(("paused",))
    active = [
        row
        for row in archon_runs_by_status(("running", "pending"))
        if row["id"] not in {p["id"] for p in paused}
    ]
    agents = [
        r
        for r in talk_runs.list_runs(20)
        if r.get("kind") == "agent" and r.get("status") == "running"
    ]
    if not paused and not active and not agents:
        return "No Archon runs or background agents are going or paused."
    sections: list[str] = []
    if paused:
        sections.append(
            "Paused, waiting on you:\n"
            + "\n".join(
                f"- {row['workflow_name']} ({row['id']})" for row in paused
            )
        )
    if active:
        sections.append(
            "Running:\n"
            + "\n".join(
                f"- {row['workflow_name']} ({row['id']}) [{row['status']}]"
                for row in active
            )
        )
    if agents:
        sections.append(
            "Background agents:\n"
            + "\n".join(
                f"- #{r['runId']} {r['label']}"
                + (
                    f" ({len(r['steers'])} steer(s) queued)"
                    if r.get("steers")
                    else ""
                )
                for r in agents
            )
        )
    return "\n\n".join(sections)[:_MAX_OUTPUT_CHARS]


def _destructive_preview(action: str, archon_run_id: str) -> str:
    """The first half of announce-then-act, built from the run's real state."""

    consequence = {
        "reject": (
            "refuse the gate it is sitting at. On a spend gate that CANCELS the "
            "run; on a steer gate it buys one corrective round"
        ),
        "cancel": "stop it where it stands; work already written stays on disk",
        "abandon": "drop it entirely",
    }[action]
    return (
        f"{narrate_archon_run(archon_run_id)} Saying {action} would {consequence}. "
        f"Tell owner that and, if he says yes, call manage_run again with "
        f"action '{action}', run_id '{archon_run_id}', and confirm true."
    )


def _agent_cancel_preview(run_id: int, run: dict) -> str:
    """Announce-then-act, built from the agent run's REAL state — the Archon
    preview narrates the Archon ledger and cannot be reused here."""

    lane = (run.get("meta") or {}).get("lane") or "?"
    minutes = max(0, int(time.time() - (run.get("ts") or time.time())) // 60)
    pending = len(run.get("steers") or [])
    pending_txt = f", {pending} steer(s) queued" if pending else ""
    return (
        f"Run #{run_id} is '{run.get('label')}' on the {lane} lane, running "
        f"for {minutes} minute(s){pending_txt}. Cancelling stops it where it "
        "stands; anything it already produced stays in the transcript. Tell "
        "owner that and, if he says yes, call manage_run again with action "
        f"'cancel', run_id '{run_id}', and confirm true."
    )


def _manage_agent_run(action: str, run_id: int, arguments: dict) -> str:
    """manage_run's background-agent branch: say and cancel, honestly.

    Agent runs have no gates — a steer is QUEUED and lands at the worker's
    next turn boundary; cancel finishes the run first (atomically blocking
    new steers and new turns) and only then kills whatever pid is live, so
    every interleaving with the worker ends with the process dead or the
    honest answer "already finished".
    """

    note = str(arguments.get("note") or "").strip()
    run = talk_runs.get_run(run_id)
    if run is None:
        return f"I don't have a run #{run_id} in this session."

    if action == "get":
        lane = (run.get("meta") or {}).get("lane") or "?"
        bits = [
            f"Run #{run_id} '{run.get('label')}' on the {lane} lane is "
            f"{run.get('status')}."
        ]
        pending = len(run.get("steers") or [])
        if pending:
            bits.append(f"{pending} steer(s) queued for the next turn boundary.")
        undelivered = (run.get("meta") or {}).get("undelivered_steers") or []
        if undelivered:
            # finish_run's durable output promises "manage_run get has the
            # text" — honor it: the verbatim text lives only in this
            # ephemeral meta, so THIS is where it gets spoken.
            named = "; ".join(str(s)[:120] for s in undelivered[:5])
            bits.append(
                f"{len(undelivered)} steer(s) arrived too late to be "
                f"delivered: {named}"
            )
        if run.get("status") in talk_runs.TERMINAL_STATUSES and run.get("output"):
            bits.append(f"Result: {str(run.get('output'))[:400]}")
        return " ".join(bits)

    if action == "say":
        if not note:
            return "say needs the words to send to the run."
        return talk_runs.queue_steer(run_id, note)

    if action == "cancel":
        if run.get("status") in talk_runs.TERMINAL_STATUSES:
            return f"Run #{run_id} already finished — nothing to cancel."
        if arguments.get("confirm") is not True:
            # Same literal-True gate as the Archon branch — `bool("false")`
            # is True, and that once cancelled a run without a preview.
            return _agent_cancel_preview(run_id, run)
        # Finish FIRST: this atomically blocks new steers and new turns and
        # tells the worker (terminal status IS the cancel flag). A lost race
        # against a naturally-finishing worker means nothing to kill.
        if not talk_runs.finish_run(run_id, "failed", "cancelled by operator"):
            return f"Run #{run_id} already finished — nothing to cancel."
        fresh = talk_runs.get_run(run_id) or {}
        pid = (fresh.get("meta") or {}).get("pid")
        if pid:
            _kill_pid_tree(int(pid))
        subtask_id = (run.get("meta") or {}).get("subtask_id")
        if isinstance(subtask_id, int):
            svc = _try_convoy_service()
            if svc is not None:
                _ledger(
                    "failure",
                    lambda: svc.handle_subtask_failure(
                        subtask_id, error_message="cancelled by operator"
                    ),
                )
        return (
            f"Cancelled run #{run_id}. Anything it already produced stays in "
            "the transcript."
        )

    return (
        "Background agents support say (queued to the next turn boundary) and "
        f"cancel. '{action}' answers Archon gates, which agent runs don't have."
    )


def _handle_manage_run(arguments: dict) -> str:
    import talk_archon  # noqa: PLC0415 — lazy: keeps session-mint import light

    action = str(arguments.get("action") or "").strip().lower()
    if action not in _MANAGE_RUN_ACTIONS:
        return f"'{action}' isn't a manage_run action. {_MANAGE_RUN_HELP}"
    if action == "help":
        return _MANAGE_RUN_HELP
    if action == "list":
        return _manage_run_list()

    # A receipt number naming a background AGENT run diverts before Archon
    # resolution — resolve_archon_run's miss text ("Archon may still be
    # starting it") is a lie for a run that will never have an Archon id.
    # NO length gate here: the registry seeds epoch-sized ids when its
    # history file is unreadable, and those ten-digit receipts must still
    # steer. The live-registry lookup is the authority, not the shape.
    reference = str(arguments.get("run_id") or "").strip().lstrip("#")
    if reference.isdigit():
        receipt_run = talk_runs.get_run(int(reference))
        if receipt_run is not None and receipt_run.get("kind") == "agent":
            return _manage_agent_run(action, int(reference), arguments)

    archon_run_id, problem = resolve_archon_run(arguments.get("run_id"))
    if problem:
        return problem

    note = str(arguments.get("note") or "").strip()

    if action == "get":
        return narrate_archon_run(archon_run_id)

    if action == "say":
        if not note:
            return "say needs the words to send to the run."
        # Archon routes a plain-text message to the WORKFLOW only when the run
        # is paused (orchestrator-agent.ts:1162-1176 enters NL approval solely
        # when getPausedWorkflowRun returns a pause). On a running run the
        # message is accepted as ordinary parent-conversation chatter, the
        # worker never hears it, and Archon still answers 200 — so reporting
        # "sent it to the run" was true of the HTTP call and false of the
        # world. The ticket asks for exactly this honesty: a run with no pause
        # point is cancellable, not redirectable. Rule 2 — the ledger row
        # decides, not what the Homie remembers dispatching.
        row = _archon_run_row(_archon_settings()["db"], archon_run_id)
        if row is None:
            # UNKNOWN is not PAUSED. _archon_run_row returns None on a locked or
            # missing ledger by design, and `if row and ...` used to fall through
            # to the send — so a 2s timeout meant the words went out as ordinary
            # conversation chatter, Archon answered accepted=true because it IS a
            # valid message, and the Homie announced "if it was sitting at a gate,
            # that message is the approval." A control the operator believes fired
            # and did not is the costly failure here, so an unreadable ledger
            # refuses instead of guessing.
            return (
                "I can't read Archon's ledger right now, so I can't tell whether "
                f"run {archon_run_id} is actually paused. I'm not sending words "
                "that might land as chatter and then telling you they were an "
                "approval. Try again in a moment, or approve it explicitly."
            )
        status = str(row.get("status") or "").lower()
        if status != "paused":
            return (
                f"Run {archon_run_id} is {status}, not paused, so there is no "
                "gate for those words to answer — Archon would file them as "
                "conversation chatter and the worker would never see them. "
                "While it is running I can cancel it, not redirect it."
            )
        conversation_id = _conversation_for_run(archon_run_id)
        if not conversation_id:
            return (
                f"I can't find the conversation behind run {archon_run_id}, so I "
                "have nowhere to send that. Approve, reject or cancel it instead."
            )
        # run_id is what lets say_now attach the gate's mandatory phrase. It is
        # already resolved above; not passing it is what made "looks good, ship
        # it" resume the DAG and then fail the very check node it was meant to
        # satisfy.
        return talk_archon.say_now(
            conversation_id, note, run_id=archon_run_id
        ).message

    # `is True`, not truthiness. The arguments dict is untyped all the way from
    # the model/client (talk_api.py:33-37), and `bool("false")` is True — so a
    # confirm of the STRING "false" cancelled a run without ever showing the
    # preview. Every non-boolean confirm now falls to the preview, which is the
    # safe direction: the cost of being wrong is one extra sentence, versus
    # destroying in-flight work the operator never agreed to destroy.
    if action in talk_archon.DESTRUCTIVE_STEER_ACTIONS and (
        arguments.get("confirm") is not True
    ):
        return _destructive_preview(action, archon_run_id)

    return talk_archon.steer_now(archon_run_id, action, note=note or None).message


def _conversation_for_run(archon_run_id: str) -> str:
    """The PLATFORM conversation id to talk to a run on. ``""`` when unknown.

    ``send_message`` only accepts the platform id (``web-…``), and the run row
    carries database ids. The in-session registry is where the two were joined
    at dispatch, so it is read first; the correlation key on the work-queue row
    is the durable fallback that survives an API restart.
    """

    for run in talk_runs.list_runs(50):
        meta = run.get("meta") or {}
        if meta.get("archon_run_id") == archon_run_id and meta.get(
            "archon_conversation_id"
        ):
            return str(meta["archon_conversation_id"])

    import talk_archon  # noqa: PLC0415

    try:
        svc = _convoy_service()
        for convoy in svc.list_convoys():
            if getattr(convoy, "created_by", "") != "voice":
                continue
            for subtask in getattr(svc.get_convoy(convoy.id), "subtasks", []) or []:
                parsed = talk_archon.parse_correlation_ref(
                    getattr(subtask, "paperclip_issue_id", None)
                )
                if not parsed or not parsed.get("conversation_db_id"):
                    continue
                if (
                    talk_archon.run_id_for_conversation(parsed["conversation_db_id"])
                    == archon_run_id
                ):
                    return str(parsed.get("conversation_id") or "")
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger is "unknown"
        _log.warning(
            "archon conversation lookup failed: %s: %s", type(exc).__name__, exc
        )
    return ""


# -- work status --------------------------------------------------------------


def _voice_subtask_lines(limit: int = 5) -> list[str]:
    """Open work-queue rows this voice surface created."""

    try:
        svc = _convoy_service()
        lines: list[str] = []
        for convoy in svc.list_convoys():
            if getattr(convoy, "created_by", "") != "voice":
                continue
            detail = svc.get_convoy(convoy.id)
            for subtask in getattr(detail, "subtasks", []) or []:
                status = getattr(subtask, "status", "?")
                if status in ("completed", "cancelled"):
                    continue
                lines.append(f"- task #{subtask.id} [{status}]: {getattr(subtask, 'title', '')}")
                if len(lines) >= limit:
                    return lines
        return lines
    except Exception as exc:  # noqa: BLE001 — status must never raise at the operator
        _log.warning("check_work ledger read failed: %s: %s", type(exc).__name__, exc)
        return []


def _handle_check_work(arguments: dict) -> str:
    run_id = arguments.get("run_id")
    if run_id is not None:
        try:
            wanted = int(run_id)
        except (TypeError, ValueError):
            return "check_work needs a numeric run id."
        run = talk_runs.get_run(wanted)
        if run is None:
            return f"I don't have a run #{wanted} in this session."
        meta = run.get("meta") or {}
        if run["kind"] == "archon" and meta.get("archon_run_id"):
            # Narration, not a status code (#259): the operator asking "how's
            # it going" wants the node it is on and what it just did. Rule 2 —
            # every field comes from the ledger, never from the in-memory
            # registry's last-known annotation.
            row = _archon_run_row(_archon_settings()["db"], meta["archon_run_id"])
            if row:
                return f"Run #{wanted} — {narrate_archon_run(meta['archon_run_id'])}"[
                    :_MAX_OUTPUT_CHARS
                ]
        body = run.get("output") or "still working"
        return f"Run #{wanted} ({run['kind']}, {run['label']}): {run['status']} — {body}"[
            :_MAX_OUTPUT_CHARS
        ]

    sections: list[str] = []

    # "How's it going?" is the DEFAULT question — it arrives as check_work with
    # no run id at all, because run_id is optional and the operator asked about
    # nothing in particular. Listing workflow names and statuses answers "what
    # exists", not "what is happening", so the ticket's first
    # what-done-looks-like bullet went unmet on the one path most likely to be
    # used. Narrate the run that is actually live: the current node and the
    # recent tool calls, the same answer a named run already gives.
    #
    # ONE narration, not one per run — each costs three ledger reads. Running
    # outranks paused (a paused run's own line already says what to do about
    # it), newest first. Everything else still gets listed below.
    active = archon_runs_by_status(("running",), limit=1) or archon_runs_by_status(
        ("paused",), limit=1
    )
    if active:
        live = active[0]
        narration = narrate_archon_run(str(live.get("id") or ""))
        if narration:
            sections.append(f"{live.get('workflow_name') or 'run'} — {narration}")

    runs = talk_runs.list_runs(10)
    if runs:
        lines = []
        for run in runs:
            summary = run["status"]
            if run["status"] == "running" and (run.get("meta") or {}).get("archon_status"):
                summary = f"running ({run['meta']['archon_status']})"
            lines.append(f"- #{run['runId']} {run['kind']} '{run['label']}': {summary}")
        sections.append("Voice runs this session:\n" + "\n".join(lines))

    # Paused runs lead: they are the only ones the operator has to DO something
    # about, and they are read from the ledger so a run this process never
    # dispatched still surfaces (Rule 2).
    paused = archon_runs_by_status(("paused",))
    if paused:
        sections.append(
            "Paused, waiting on you:\n"
            + "\n".join(
                f"- {row['workflow_name']} ({row['id']}) — say approve, reject, "
                "or talk to it"
                for row in paused
            )
        )

    archon_rows = recent_archon_runs(5)
    if archon_rows:
        lines = [
            f"- {row.get('workflow_name')} ({row.get('status')}) started {row.get('started_at')}"
            for row in archon_rows
        ]
        sections.append("Recent Archon runs:\n" + "\n".join(lines))

    queue_lines = _voice_subtask_lines()
    if queue_lines:
        sections.append("Open work-queue tasks:\n" + "\n".join(queue_lines))

    if not sections:
        return "Nothing is running and nothing recent is on the queue."
    return "\n\n".join(sections)[:_MAX_OUTPUT_CHARS]


_HANDLERS = {
    "memory_search": _handle_memory_search,
    "calendar_events": _handle_calendar_events,
    "homie_command": _handle_homie_command,
    "delegate_task": _handle_delegate_task,
    "run_skill": _handle_run_skill,
    "run_archon": _handle_run_archon,
    "computer": _handle_computer,
    "browse": _handle_browse,
    "check_work": _handle_check_work,
    "manage_run": _handle_manage_run,
    "run_python": _handle_run_python,
    "run_shell": _handle_run_shell,
}


__all__ = [
    "MUTATING_TALK_TOOLS",
    "MUTATION_DENIAL",
    "ROLE_DENIAL",
    "READ_ONLY_TALK_TOOLS",
    "TalkToolError",
    "archon_runs_by_status",
    "code_exec_enabled",
    "default_talk_tools",
    "execute_talk_tool",
    "TRANSPORT_BROWSER",
    "TRANSPORT_DISCORD_VOICE",
    "end_talk_session",
    "get_skill_run",
    "narrate_archon_run",
    "recent_archon_runs",
    "resolve_archon_run",
    "resolve_skill",
    "resolve_workflow",
    "resolve_request_role",
    "talk_tool_denial",
    "set_browser_session_role",
    "start_agent_run",
    "start_archon_run",
    "start_skill_run",
]
