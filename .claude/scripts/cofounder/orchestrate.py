"""Co-founder LLM orchestration step (US-012) — decide-only, strict JSON.

The LLM side of the ``run_pass`` decide seam: :func:`decide` matches the
``decide(project, context) -> decision-dict`` contract from US-011 and is
called ONLY when pure-code classification says a real decision remains
(new project, job finished, human replied). The model DECIDES; CODE executes
— the decision flows back into ``run_pass._execute_decision``, which owns
every dispatch guard, so the model never runs shell and can never mint
``done`` (the action set has no such move).

Inputs are assembled in code: the read-only Spec, the current Plan, the last
~10 Activity Log lines, job status, new steering lines, and the available
workflows (a cached ``archon workflow list --json`` plus the per-repo page's
``## Workflow Preferences`` section). The runtime call goes through
``run_with_fallback`` on the background QUALITY tier (Rule 1 call-time via
``config.get_background_models()`` — never the interactive flagship), with
no tools and one turn; the prompt is plain text so it survives a
Claude -> Codex -> Gemini fallback unchanged.

Output contract (strict): ONE JSON object
``{action: reuse|author|test|park, workflow, message, status, plan,
log_line}``. The parser rejects invented statuses, unknown keys (a ``spec``
key is how a rewrite would smuggle in), plans carrying H2 headings (the only
way a plan could shadow the Spec section), and anything but exactly one
action. Invalid or unparseable output is a no-op with ONE ``[warn]``
Activity Log line (skipped on dry runs — --test writes nothing), never a
crash or a partial write (Invariant 6).

Kept import-light at module level (status.py is the pure enum module);
config / runtime / project_model resolve inside function bodies so the
heartbeat's lazy import chain stays cheap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from cofounder import status as status_mod

logger = logging.getLogger(__name__)

TASK_NAME = "cofounder_orchestrate"

DECISION_KEYS = ("action", "workflow", "message", "status", "plan", "log_line")
DECISION_ACTIONS = frozenset({"reuse", "author", "test", "park"})

# Prompt assembly caps — the decision needs orientation, not the whole vault.
SPEC_PROMPT_CAP = 4000
PLAN_PROMPT_CAP = 2000
PREFS_PROMPT_CAP = 800
LOG_TAIL_LINES = 10
MAX_WORKFLOW_NAMES = 40
MAX_TURNS = 1

# Fixed literals with call-arg overrides (the engine_archon grace/poll shape).
WORKFLOW_LIST_TIMEOUT_S = 30.0
WORKFLOW_CACHE_TTL_S = 600.0

_WORKFLOW_PREFS_HEADING = "Workflow Preferences"

# {repo-path key: (monotonic stamp, names)} — a per-process convenience cache
# so one pass never shells out per project; NEVER a truth source (Rule 2: the
# dispatch itself is confirmed against archon.db, not this list).
_workflow_cache: dict[str, tuple[float, tuple[str, ...]]] = {}

_H2_RE = re.compile(r"^##\s", re.MULTILINE)
_STEER_MARKER = "[steer]"


class DecisionParseError(ValueError):
    """The model's output is not one valid decision object."""


# =============================================================================
# The decide seam (run_pass calls this; everything below feeds it).
# =============================================================================


def decide(project, context: dict[str, Any]) -> dict[str, Any] | None:
    """One validated JSON decision for one project, or ``None`` (no-op).

    Fail-open at every seam: a runtime failure, invalid JSON, or an invented
    status returns ``None`` after ONE ``[warn]`` Activity Log line (skipped
    when ``context['dry_run']`` — --test writes nothing), never a crash or a
    partial write. ``run_pass`` treats a non-dict decision as a logged no-op.
    """
    try:
        return _decide_inner(project, context)
    except DecisionParseError as exc:
        logger.warning(
            "cofounder: %s decision rejected (%s); no-op", project.slug, exc
        )
        _append_warning(
            project, context, f"orchestration decision invalid ({exc}); no-op"
        )
        return None
    except Exception as exc:
        logger.exception("cofounder: orchestration step failed for %s", project.slug)
        _append_warning(
            project,
            context,
            f"orchestration step failed ({type(exc).__name__}); no-op",
        )
        return None


def _decide_inner(project, context: dict[str, Any]) -> dict[str, Any]:
    import config  # function-local: Rule 1 call-time + cheap heartbeat import

    fm = project.frontmatter
    workflows = available_workflows(_repo_local_path(fm.repo))
    prefs = repo_workflow_preferences(fm.repo)
    prompt = build_prompt(project, context, workflows, prefs)

    from runtime import registry  # module-attribute call site (patchable)
    from runtime.base import RuntimeRequest
    from runtime.capabilities import TEXT_REASONING

    request = RuntimeRequest(
        prompt=prompt,
        cwd=config.PROJECT_ROOT,
        task_name=TASK_NAME,
        capability=TEXT_REASONING,
        # Background QUALITY tier resolved at call time (Rule 1) — a decision
        # over pre-assembled context never burns the interactive flagship.
        model=config.get_background_models()["quality"],
        max_turns=MAX_TURNS,
        allowed_tools=[],  # decide-only: the model never runs shell
    )
    result = asyncio.run(registry.run_with_fallback(request))
    decision = parse_decision(getattr(result, "text", "") or "")
    logger.info(
        "cofounder: %s decision action=%s workflow=%s",
        project.slug,
        decision["action"],
        decision["workflow"] or "none",
    )
    return decision


def _append_warning(project, context: dict[str, Any], message: str) -> None:
    """One fail-open ``[warn]`` Activity Log line — skipped on dry runs."""
    if context.get("dry_run"):
        return
    line = f"[warn] {message}".replace(_STEER_MARKER, "[steer-ref]")
    try:
        from cofounder import project_model

        project_model.append_activity_log(project.path, line)
    except Exception as exc:
        logger.warning(
            "cofounder: warn line append failed for %s (%s)", project.slug, exc
        )


# =============================================================================
# Prompt assembly (all in code; the model receives orientation, not tools).
# =============================================================================


def build_prompt(
    project,
    context: dict[str, Any],
    workflows: list[str],
    repo_prefs: str,
) -> str:
    """The lane-agnostic decision prompt (Prompt Contract, original text)."""
    fm = project.frontmatter
    steering = [str(item) for item in (context.get("new_steering") or [])]
    log_tail = _tail_lines(project.activity_log, LOG_TAIL_LINES)
    names = ", ".join(workflows[:MAX_WORKFLOW_NAMES]) if workflows else "(none listed)"
    statuses = ", ".join(status_mod.STATUSES)

    lines = [
        "You are the co-founder build orchestrator for one project. Choose the",
        "single next move and reply with ONE JSON object only. No prose, no",
        "code fences, nothing before or after the object.",
        "",
        'Shape: {"action": "reuse|author|test|park", "workflow": string or null,',
        '"message": string or null, "status": string or null, "plan": string or',
        'null, "log_line": string or null}',
        "",
        "Hard rules:",
        "- Exactly one move per reply.",
        '- "reuse": dispatch one of the available workflows; set workflow and',
        "  message (the build instruction). Builds in existing repos must open",
        "  a PR for review, never merge.",
        '- "author": no available workflow fits; describe the workflow you need',
        "  in message.",
        '- "test": the build looks finished; code will run the completion check.',
        "  Only that executable check proves completion. Never claim done.",
        '- "park": nothing useful to do without the human; say why in log_line.',
        f"- status, when set, must be one of: {statuses}. Never invent a status.",
        "- plan, when set, REPLACES the Plan / Working Memory section body. Keep",
        "  it a short markdown checklist with no '## ' headings.",
        "- Never rewrite or restate the Spec.",
        "- log_line is one short single-line Activity Log note.",
        "",
        f"Project: {project.slug}",
        f"Status: {fm.status}",
        f"Iteration: {fm.iterations} of {fm.max_iterations}",
        f"Why you are asked: {context.get('reason')}",
        f"Job status: {context.get('job_status') or 'no job'}",
        (
            f"Builds in flight: {context.get('in_flight')} of "
            f"{context.get('max_concurrent')}"
        ),
        f"Available workflows: {names}",
        "",
        "New operator steering:",
        *([f"  {line}" for line in steering] or ["  (none)"]),
    ]
    if repo_prefs:
        lines += [
            "",
            "Repo workflow preferences:",
            _cap(repo_prefs, PREFS_PROMPT_CAP),
        ]
    lines += [
        "",
        f"Completion check (the only done signal): {fm.completion_check or '(not set)'}",
        "",
        "Spec (read-only):",
        _cap(project.spec.strip() or "(empty)", SPEC_PROMPT_CAP),
        "",
        "Current plan:",
        _cap(project.plan.strip() or "(empty)", PLAN_PROMPT_CAP),
        "",
        f"Activity log (last {LOG_TAIL_LINES} non-empty lines):",
        *([f"  {line}" for line in log_tail] or ["  (empty)"]),
    ]
    return "\n".join(lines)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _tail_lines(text: str, count: int) -> list[str]:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-count:]


# =============================================================================
# Decision parsing — strict validation over a liberally EXTRACTED object
# (providers wrap JSON in fences/prose; the contract is strict once found).
# =============================================================================


def parse_decision(raw: str) -> dict[str, Any]:
    """Validate the model's output into one normalized decision dict.

    Raises :class:`DecisionParseError` on anything but exactly one move with
    enum-only status and no Spec write-back vector (unknown keys such as
    ``spec`` and H2 headings inside ``plan`` are both rejected).
    """
    data = _load_json_object(raw)
    if not isinstance(data, dict):
        raise DecisionParseError("decision is not a JSON object")

    unknown = set(data) - set(DECISION_KEYS)
    if unknown:
        # A `spec` key is the write-back attempt this guard exists for.
        raise DecisionParseError(f"unknown keys: {', '.join(sorted(unknown))}")

    action = data.get("action")
    if not isinstance(action, str) or action.strip().lower() not in DECISION_ACTIONS:
        raise DecisionParseError(
            f"action must be exactly one of: {', '.join(sorted(DECISION_ACTIONS))}"
        )
    action = action.strip().lower()

    status = data.get("status")
    if status is not None and (
        not isinstance(status, str) or not status_mod.is_enum(status)
    ):
        raise DecisionParseError(f"invented status {status!r}")

    for key in ("workflow", "message", "log_line", "plan"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise DecisionParseError(f"{key} must be a string or null")

    plan = data.get("plan")
    if plan is not None and _H2_RE.search(plan):
        raise DecisionParseError(
            "plan may not contain H2 headings (the Spec stays untouched)"
        )

    log_line = data.get("log_line")
    if log_line is not None and ("\n" in log_line or "\r" in log_line):
        raise DecisionParseError("log_line must be a single line")

    normalized = {key: data.get(key) for key in DECISION_KEYS}
    normalized["action"] = action
    return normalized


def _load_json_object(raw: str) -> Any:
    """The first JSON object in ``raw`` — tolerant of fences and prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        fenced = text.splitlines()[1:]
        while fenced and fenced[-1].strip().startswith("```"):
            fenced.pop()
        text = "\n".join(fenced).strip()
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise DecisionParseError("output is not valid JSON")


# =============================================================================
# Available-workflow inputs (cached CLI list + per-repo page preferences).
# =============================================================================


def available_workflows(
    repo_path: Path | str | None = None,
    *,
    archon_bin: Path | str | None = None,
    ttl_seconds: float | None = None,
    now: float | None = None,
) -> list[str]:
    """Workflow names from a cached ``archon workflow list --json``.

    Cached per repo path for ``WORKFLOW_CACHE_TTL_S`` so one pass shells out
    at most once per repo; a failed listing caches ``[]`` too (do not hammer
    a broken CLI once per project). Fail-open: never raises.
    """
    if ttl_seconds is None:
        ttl_seconds = WORKFLOW_CACHE_TTL_S
    if now is None:
        now = time.monotonic()
    key = str(repo_path) if repo_path is not None else ""
    cached = _workflow_cache.get(key)
    if cached is not None and now - cached[0] < ttl_seconds:
        return list(cached[1])
    names = _fetch_workflow_list(repo_path, archon_bin)
    _workflow_cache[key] = (now, tuple(names))
    return list(names)


def _fetch_workflow_list(
    repo_path: Path | str | None,
    archon_bin: Path | str | None = None,
) -> list[str]:
    """One bounded ``archon workflow list --json`` run; fail-open to []."""
    import config
    from cofounder import engine_archon

    argv = [
        engine_archon._resolve_archon_bin(archon_bin),
        "workflow",
        "list",
        "--json",
    ]
    cwd = str(repo_path) if repo_path else str(config.PROJECT_ROOT)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            # CLAUDECODE* scrubbed — archon hangs under a Claude Code env.
            env=engine_archon.build_child_env(archon_bin),
            capture_output=True,
            text=True,
            timeout=WORKFLOW_LIST_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("cofounder: archon workflow list failed (%s)", exc)
        return []
    if proc.returncode != 0:
        logger.warning(
            "cofounder: archon workflow list exited %d", proc.returncode
        )
        return []
    return _workflow_names(proc.stdout)


def _workflow_names(stdout: str) -> list[str]:
    """Names out of the CLI's JSON — list of strings/objects or a wrapper."""
    try:
        data = json.loads(stdout or "")
    except (TypeError, json.JSONDecodeError):
        logger.warning("cofounder: archon workflow list output is not JSON")
        return []
    if isinstance(data, dict):
        data = data.get("workflows", [])
    names: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    names.append(name)
    return names[:MAX_WORKFLOW_NAMES]


def repo_workflow_preferences(
    repo_slug: str | None,
    memory_dir: Path | str | None = None,
) -> str:
    """The per-repo page's ``## Workflow Preferences`` section text (or '')."""
    slug = (repo_slug or "").strip()
    if not slug:
        return ""
    from cofounder import repos

    if slug.lower() == repos.GREENFIELD_SLUG:
        return ""
    try:
        import config
        import repository_memory

        if memory_dir is None:
            memory_dir = config.MEMORY_DIR
        page = (
            Path(memory_dir) / repository_memory.REPOSITORY_PAGES_DIR / f"{slug}.md"
        )
        content = repository_memory.read_text_safe(page)
        if not content.strip():
            return ""
        return repository_memory.extract_h2_section(
            content, _WORKFLOW_PREFS_HEADING
        ).strip()
    except Exception as exc:
        logger.warning("cofounder: repo page read failed for %s (%s)", slug, exc)
        return ""


def _repo_local_path(repo_slug: str | None) -> Path | None:
    """The slug's local repo path for workflow listing; fail-open to None."""
    try:
        from cofounder import repos

        return repos.resolve_repo(repo_slug or "").local_path
    except Exception:
        return None
