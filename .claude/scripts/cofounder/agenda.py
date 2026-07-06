"""Co-founder v2 WS2 — the morning portfolio scan (propose-don't-act).

Run manually (testable without a heartbeat):

    cd .claude/scripts && uv run python -m cofounder.agenda [--test] [--force]

Once per day (first heartbeat pass on/after ``COFOUNDER_AGENDA_HOUR`` local),
the cofounder reads the portfolio — ``REPOSITORIES.md`` + the per-repo pages'
``## Dispatch History`` / ``## Recent Activity`` tails, ``GOALS.md``, the open
cofounder projects, and the registered persona roster — and PROPOSES a daily
agenda (which persona should work which repo on what) as a vault artifact
(``<projects_dir>/agendas/AGENDA-YYYY-MM-DD.md``) plus a gated Telegram card.

Nothing here executes anything. No convoy, no mailbox, no Archon dispatch, no
project-file write — the artifact and the card are the entire output surface
(the PRD's propose-don't-act contract; delegation is WS3+ behind its own flag
and operator approval).

Gate order (mirrors ``run_pass`` — quiet no-op exits, never heartbeat errors):

1. Kill switch ``cofounder`` (shared with v1 — the operator's one emergency
   stop covers the whole cofounder surface; refusals counted).
2. ``COFOUNDER_AGENDA_ENABLED`` (default false — dormant by default, gated
   SEPARATELY from ``COFOUNDER_ENABLED`` so v2.0 can bake while the v1
   project pipeline stays off, and vice versa).
3. Due check (skipped by ``--force``): today's agenda already exists in state,
   or the local hour is before ``COFOUNDER_AGENDA_HOUR``.
4. Attempt cap: ``COFOUNDER_AGENDA_MAX_ATTEMPTS`` failed proposals per day,
   then quiet until tomorrow (a broken provider must not burn quality-tier
   tokens every 30 minutes all day).

The scan is pure Python and fail-open at every seam (a missing index, page,
or GOALS.md just narrows the scan; an EMPTY scan — no repos AND no personas —
skips the LLM entirely). The LLM runs on the background QUALITY tier through
``run_with_fallback`` (Rule 1 call-time model resolution, no tools, one turn,
plain-text prompt that survives a Claude -> Codex -> Gemini fallback). Its
output is ONE strict JSON object; validation is fail-closed per line — an
unknown persona or repo slug drops THAT line with a warning, garbage output
is a counted failed attempt with no artifact and no card.

The Telegram card rides the existing gated ``cofounder.notify`` sender
(kill switch + ``require_integration_action`` + audit row per attempt),
WITHOUT the pause/approve buttons (there is no project to steer). An
operator-emptied ``COFOUNDER_NOTIFY_LEVELS`` ("disable all cofounder
notifications") mutes the agenda card too; ``COFOUNDER_AGENDA_NOTIFY=false``
mutes only the card while the artifact still lands.

State (``cofounder-state.json``, top-level ``"agenda"`` key — a sibling of
``"projects"`` and ``"last_pass_at"``): ``last_date``, ``attempts`` per date,
``last_artifact``. Rule 2: derived bookkeeping only — losing it costs at most
one duplicate agenda, never truth.

No exception escapes :func:`run_agenda_pass`; every outcome is an
:class:`AgendaResult` (exit code 1 only for ``error``). Import-light at
module level so the heartbeat seam stays cheap.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

# Boot-shim (PRP-7a): persona env overrides must apply BEFORE any
# config-touching import resolves paths. Idempotent — the heartbeat entry
# point already ran it; a direct ``python -m cofounder.agenda`` needs it.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

logger = logging.getLogger(__name__)

TASK_NAME = "cofounder_agenda"

OUTCOME_COMPLETED = "completed"
OUTCOME_DISABLED = "disabled"
OUTCOME_REFUSED = "refused"
OUTCOME_NOT_DUE = "not-due"
OUTCOME_ATTEMPTS_CAPPED = "attempts-capped"
OUTCOME_SCAN_EMPTY = "scan-empty"
OUTCOME_PROPOSAL_FAILED = "proposal-failed"
OUTCOME_WRITE_FAILED = "write-failed"
OUTCOME_HAS_DELEGATED = "delegated-lines-exist"
OUTCOME_ERROR = "error"

# Artifacts live OUTSIDE project discovery: discover_projects() globs
# ``<projects_dir>/*.md`` non-recursively, so a subfolder keeps every agenda
# out of the v1 pipeline (a top-level AGENDA-*.md would warn-skip every pass).
AGENDAS_SUBDIR = "agendas"

# The notify level the agenda card sends under. Not part of the v1 terminal
# default set; the pass extends the resolved settings' levels for its ONE
# send call (never the env), so v1 terminal-flip filtering is untouched.
AGENDA_LEVEL = "agenda"

STATE_KEY = "agenda"
_STATE_LOCK_TIMEOUT_S = 5.0

# Prompt assembly caps — orientation, not the whole vault (orchestrate shape).
GOALS_PROMPT_CAP = 3000
REPO_SECTION_TAIL_LINES = 8
REPO_SECTION_CAP = 900
IDENTITY_CAP = 300
MAX_TURNS = 1
TASK_TEXT_CAP = 300
WHY_TEXT_CAP = 200
SUMMARY_CAP = 600

_REPO_PAGE_SECTIONS = ("Identity", "Recent Activity", "Dispatch History")

AGENDA_KEYS = ("summary", "items")
ITEM_KEYS = ("persona", "repo", "task", "why", "priority", "mode")

# Execution modes the operator approves per line (WS4): draft = direct
# no-tools runtime run producing a vault deliverable; code = detached Archon
# worktree dispatch (repo required).
ITEM_MODES = frozenset({"draft", "code"})

PROPOSE_ONLY_BANNER = (
    "_PROPOSE-ONLY: nothing below executes without operator approval. "
    "Reply in chat (or `/cofounder`) to act on a line._"
)


class AgendaParseError(ValueError):
    """The model's output is not one valid agenda object."""


@dataclass
class AgendaResult:
    """What one agenda pass did. ``error`` is the only non-zero exit code."""

    outcome: str
    dry_run: bool = False
    artifact_path: Path | None = None
    items: int = 0
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == OUTCOME_ERROR else 0


def run_agenda_pass(
    *,
    dry_run: bool = False,
    force: bool = False,
    settings=None,
    agenda_settings=None,
    state_file: Path | str | None = None,
    now: datetime | None = None,
    propose: Callable | None = None,
    notify: Callable | None = None,
) -> AgendaResult:
    """Run one morning-agenda pass. Never raises.

    ``settings`` / ``agenda_settings`` / ``state_file`` are None-sentinels
    resolved at call time (Rule 1). ``propose`` is the LLM seam
    (``prompt -> raw text``; ``None`` resolves the background-quality runtime
    call). ``notify`` is the card sender (``None`` resolves the gated
    ``cofounder.notify.notify`` — Rule 3 module-attribute lookup, fail-open
    to a logging stub). ``force`` skips ONLY the due check (hour /
    already-ran-today / attempt cap) — the kill switch and the enabled flag
    always apply. ``dry_run`` scans and proposes but writes NOTHING (no
    artifact, no state, no card) — v1 ``--test`` semantics.
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled("cofounder", caller="cofounder.agenda")
        except kill_switches.KillSwitchDisabled:
            logger.info("cofounder.agenda: refused by kill switch; quiet exit")
            return AgendaResult(outcome=OUTCOME_REFUSED, dry_run=dry_run)

        import config

        if agenda_settings is None:
            agenda_settings = config.get_cofounder_agenda_settings()
        if not agenda_settings.enabled:
            logger.debug("cofounder.agenda: COFOUNDER_AGENDA_ENABLED is false; no-op")
            return AgendaResult(outcome=OUTCOME_DISABLED, dry_run=dry_run)
        if settings is None:
            settings = config.get_cofounder_settings()

        from cofounder import state as state_mod

        state_path = state_mod._resolve_state_file(state_file)
        if now is None:
            # The canonical operator-local clock (HEARTBEAT_TIMEZONE) — the
            # SAME clock the delegation transport keys its day on, so the
            # agenda filename and the cap ledger can never disagree about
            # what "today" means.
            now = config.now_local()
        today = now.date().isoformat()

        # A regenerated agenda must never orphan delegation stamps: once any
        # line of today's JSON is delegated, a rewrite would renumber lines
        # and reset statuses (double-delegation bait — adversarial-review
        # finding). Refuse regardless of --force; tomorrow starts fresh.
        if _has_delegated_lines(settings.projects_dir, today):
            logger.warning(
                "cofounder.agenda: %s has delegated lines; regeneration refused",
                today,
            )
            return AgendaResult(outcome=OUTCOME_HAS_DELEGATED, dry_run=dry_run)

        if not force:
            agenda_state = _agenda_state(state_mod.load_state(state_path))
            if agenda_state.get("last_date") == today:
                logger.debug("cofounder.agenda: %s already produced; not due", today)
                return AgendaResult(outcome=OUTCOME_NOT_DUE, dry_run=dry_run)
            if now.hour < agenda_settings.agenda_hour:
                logger.debug(
                    "cofounder.agenda: local hour %d < agenda hour %d; not due",
                    now.hour,
                    agenda_settings.agenda_hour,
                )
                return AgendaResult(outcome=OUTCOME_NOT_DUE, dry_run=dry_run)
            attempts = agenda_state.get("attempts")
            if (
                isinstance(attempts, dict)
                and int(attempts.get(today) or 0) >= agenda_settings.max_attempts
            ):
                logger.warning(
                    "cofounder.agenda: %d failed attempts today; quiet until tomorrow",
                    attempts.get(today),
                )
                return AgendaResult(outcome=OUTCOME_ATTEMPTS_CAPPED, dry_run=dry_run)

        scan = build_portfolio_scan(settings)
        if not scan["repos"] and not scan["personas"]:
            # Nothing to propose over — zero-cost skip (no LLM, no attempt
            # burned; a mid-day index fix makes the next pass actionable).
            logger.info("cofounder.agenda: empty portfolio scan; nothing to propose")
            return AgendaResult(outcome=OUTCOME_SCAN_EMPTY, dry_run=dry_run)

        prompt = build_agenda_prompt(scan, now, agenda_settings.max_items)
        if propose is None:
            propose = _llm_propose
        try:
            raw = propose(prompt)
            summary, items = parse_agenda(
                raw,
                persona_ids=frozenset(p["id"] for p in scan["personas"]),
                repo_slugs=frozenset(scan["repos"]),
                max_items=agenda_settings.max_items,
            )
        except AgendaParseError as exc:
            logger.warning("cofounder.agenda: proposal rejected (%s)", exc)
            if not dry_run:
                _record_attempt(state_mod, state_path, today)
            return AgendaResult(
                outcome=OUTCOME_PROPOSAL_FAILED, dry_run=dry_run, error=str(exc)
            )
        except Exception as exc:
            logger.exception("cofounder.agenda: proposal step failed")
            if not dry_run:
                _record_attempt(state_mod, state_path, today)
            return AgendaResult(
                outcome=OUTCOME_PROPOSAL_FAILED,
                dry_run=dry_run,
                error=f"{type(exc).__name__}: {exc}",
            )

        if dry_run:
            logger.info(
                "cofounder.agenda: [dry-run] %d proposed items — not written:\n%s",
                len(items),
                render_agenda_markdown(summary, items, scan, today),
            )
            return AgendaResult(
                outcome=OUTCOME_COMPLETED, dry_run=True, items=len(items)
            )

        try:
            artifact = _write_artifact(
                settings.projects_dir, today, summary, items, scan
            )
        except Exception as exc:
            # A write failure past a SUCCESSFUL (billed) proposal must count
            # toward the daily attempt cap too — a locked vault folder or a
            # full disk would otherwise re-burn a quality-tier call every
            # heartbeat tick all day (the exact runaway the cap exists for).
            logger.exception("cofounder.agenda: artifact write failed")
            _record_attempt(state_mod, state_path, today)
            return AgendaResult(
                outcome=OUTCOME_WRITE_FAILED,
                dry_run=dry_run,
                items=len(items),
                error=f"{type(exc).__name__}: {exc}",
            )
        _send_card(settings, agenda_settings, today, summary, items, notify)
        _stamp_success(state_mod, state_path, today, artifact)
        logger.info(
            "cofounder.agenda: %s written (%d items)", artifact, len(items)
        )
        return AgendaResult(
            outcome=OUTCOME_COMPLETED, artifact_path=artifact, items=len(items)
        )
    except Exception as exc:  # the whole-pass wrap: nothing escapes the caller
        logger.exception("cofounder.agenda: pass failed")
        return AgendaResult(
            outcome=OUTCOME_ERROR,
            dry_run=dry_run,
            error=f"{type(exc).__name__}: {exc}",
        )


# =============================================================================
# The portfolio scan — pure Python, fail-open at every seam.
# =============================================================================


def build_portfolio_scan(settings) -> dict[str, Any]:
    """Assemble the read-only portfolio picture for the proposal prompt.

    Every input degrades independently: a missing index yields no repos, an
    unreadable page yields an empty section, a broken persona config skips
    that persona. Nothing here raises.
    """
    return {
        "repos": _tracked_repos(),
        "repo_pages": _repo_page_tails(),
        "goals": _goals_text(),
        "projects": _open_projects(settings),
        "personas": _available_personas(),
    }


def _tracked_repos() -> list[str]:
    try:
        from cofounder import repos

        return repos.list_tracked_repos()
    except Exception:
        logger.warning("cofounder.agenda: repo index read failed", exc_info=True)
        return []


def _repo_page_tails() -> dict[str, dict[str, str]]:
    """Per-repo page section tails keyed by slug (fail-open per page)."""
    pages: dict[str, dict[str, str]] = {}
    try:
        import config
        import repository_memory

        pages_dir = Path(config.MEMORY_DIR) / repository_memory.REPOSITORY_PAGES_DIR
        for slug in _tracked_repos():
            try:
                content = repository_memory.read_text_safe(pages_dir / f"{slug}.md")
                if not content.strip():
                    continue
                sections: dict[str, str] = {}
                for heading in _REPO_PAGE_SECTIONS:
                    body = repository_memory.extract_h2_section(content, heading)
                    if not body.strip():
                        continue
                    cap = IDENTITY_CAP if heading == "Identity" else REPO_SECTION_CAP
                    sections[heading] = _tail(body, REPO_SECTION_TAIL_LINES, cap)
                if sections:
                    pages[slug] = sections
            except Exception:
                logger.warning(
                    "cofounder.agenda: repo page read failed for %s",
                    slug,
                    exc_info=True,
                )
    except Exception:
        logger.warning("cofounder.agenda: repo pages scan failed", exc_info=True)
    return pages


def _goals_text() -> str:
    try:
        import config
        import repository_memory

        content = repository_memory.read_text_safe(
            Path(config.MEMORY_DIR) / "GOALS.md"
        )
        return _cap(content.strip(), GOALS_PROMPT_CAP)
    except Exception:
        logger.warning("cofounder.agenda: GOALS.md read failed", exc_info=True)
        return ""


def _open_projects(settings) -> list[dict[str, Any]]:
    try:
        from cofounder import project_model

        return [
            {
                "slug": p.slug,
                "status": p.frontmatter.status,
                "repo": p.frontmatter.repo,
                "iterations": p.frontmatter.iterations,
            }
            for p in project_model.discover_projects(Path(settings.projects_dir))
        ]
    except Exception:
        logger.warning("cofounder.agenda: project discovery failed", exc_info=True)
        return []


def _available_personas() -> list[dict[str, str]]:
    """Registered persona profiles: ``{id, name, role}`` (fail-open per one).

    The operator's resolution: the cofounder delegates to ANY registered
    persona — the roster IS the registry, never a hardcoded list. A profile
    whose config is unreadable or carries no ``persona:`` section is skipped
    (it cannot be addressed as a delegation target anyway).
    """
    found: list[dict[str, str]] = []
    try:
        from personas import core as personas_core
        from personas import services as personas_services

        profiles_root = personas_core.get_default_homie_root() / "profiles"
        if not profiles_root.is_dir():
            return []
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                cfg = personas_services.load_persona_config(entry.name)
            except Exception:
                logger.debug(
                    "cofounder.agenda: persona config unreadable for %s; skipped",
                    entry.name,
                )
                continue
            persona = cfg.get("persona")
            if not isinstance(persona, dict):
                continue
            found.append(
                {
                    "id": entry.name,
                    "name": str(
                        persona.get("display_name")
                        or persona.get("name")
                        or entry.name
                    ),
                    "role": str(persona.get("role") or ""),
                }
            )
    except Exception:
        logger.warning("cofounder.agenda: persona scan failed", exc_info=True)
    return found


def _tail(text: str, lines: int, cap: int) -> str:
    kept = [line for line in (text or "").splitlines() if line.strip()]
    return _cap("\n".join(kept[-lines:]), cap)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


# =============================================================================
# Proposal prompt + the LLM seam (background QUALITY tier, decide-only).
# =============================================================================


def build_agenda_prompt(scan: dict[str, Any], now: datetime, max_items: int) -> str:
    """The lane-agnostic morning-scan prompt (plain text, strict JSON out)."""
    lines = [
        "You are the co-founder of this operator's company doing the morning",
        "portfolio scan. Propose today's agenda: which persona (department",
        "head) should work on which repo, on what task, and why. You only",
        "PROPOSE — the operator approves before anything executes. Reply with",
        "ONE JSON object only. No prose, no code fences, nothing before or",
        "after the object.",
        "",
        'Shape: {"summary": string, "items": [{"persona": string,',
        '"repo": string or null, "task": string, "why": string,',
        '"priority": 1|2|3, "mode": "draft"|"code"}]}',
        "",
        "Hard rules:",
        f"- At most {max_items} items; fewer is better than filler.",
        "- persona must be one of the registered persona ids listed below.",
        "- repo must be one of the tracked repo slugs below, or null for a",
        "  non-repo task (research, outreach, content).",
        "- task is one concrete, checkable assignment. why is one sentence.",
        "- priority: 1 = today-critical, 2 = normal, 3 = nice-to-have.",
        '- mode: "draft" for research/checklists/briefs (the default);',
        '  "code" ONLY for substantive coding work in a tracked repo.',
        "- summary is 2-3 sentences of portfolio state for the operator.",
        "- Do not propose work a repo's recent activity shows is already done",
        "  or in flight.",
        "",
        f"Today: {now.date().isoformat()} ({now.strftime('%A')})",
        "",
        "Registered personas (id — name — role):",
    ]
    for p in scan["personas"]:
        role = f" — {p['role']}" if p["role"] else ""
        lines.append(f"  {p['id']} — {p['name']}{role}")
    if not scan["personas"]:
        lines.append("  (none registered)")

    lines += ["", "Tracked repos:"]
    for slug in scan["repos"]:
        lines.append(f"  {slug}")
        sections = scan["repo_pages"].get(slug) or {}
        for heading in _REPO_PAGE_SECTIONS:
            body = sections.get(heading)
            if body:
                lines.append(f"    {heading}:")
                lines += [f"      {ln}" for ln in body.splitlines()]
    if not scan["repos"]:
        lines.append("  (none tracked)")

    if scan["projects"]:
        lines += ["", "Open co-founder projects (already orchestrated — do not"]
        lines += ["re-propose these; they advance on their own):"]
        for proj in scan["projects"]:
            lines.append(
                f"  {proj['slug']} — status {proj['status']}"
                f" (repo {proj['repo'] or 'none'}, iteration {proj['iterations']})"
            )

    if scan["goals"]:
        lines += ["", "Operator goals:", scan["goals"]]

    return "\n".join(lines)


def _llm_propose(prompt: str) -> str:
    """One background-QUALITY runtime call (the orchestrate.decide shape)."""
    import asyncio

    import config
    from runtime import registry  # module-attribute call site (patchable)
    from runtime.base import RuntimeRequest
    from runtime.capabilities import TEXT_REASONING

    request = RuntimeRequest(
        prompt=prompt,
        cwd=config.PROJECT_ROOT,
        task_name=TASK_NAME,
        capability=TEXT_REASONING,
        # Background QUALITY tier resolved at call time (Rule 1) — a morning
        # scan over pre-assembled context never burns the interactive flagship.
        model=config.get_background_models()["quality"],
        max_turns=MAX_TURNS,
        allowed_tools=[],  # propose-only: the model never runs shell
    )
    result = asyncio.run(registry.run_with_fallback(request))
    return getattr(result, "text", "") or ""


# =============================================================================
# Strict parse + fail-closed per-line validation.
# =============================================================================


def parse_agenda(
    raw: str,
    *,
    persona_ids: frozenset[str],
    repo_slugs: frozenset[str],
    max_items: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Validate the model's output into ``(summary, items)``.

    Raises :class:`AgendaParseError` on anything but one JSON object with the
    agenda keys and at least ONE valid item. Per-line validation is
    fail-closed: an unknown persona id or repo slug drops that line with a
    warning (the model cannot invent delegation targets — Rule 4's grain
    check starts at the proposal).
    """
    from cofounder import orchestrate

    try:
        data = orchestrate._load_json_object(raw)
    except Exception as exc:
        raise AgendaParseError(f"output is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise AgendaParseError("agenda is not a JSON object")

    unknown = set(data) - set(AGENDA_KEYS)
    if unknown:
        raise AgendaParseError(f"unknown keys: {', '.join(sorted(unknown))}")

    summary = data.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise AgendaParseError("summary must be a string or null")
    summary = _cap((summary or "").strip(), SUMMARY_CAP)

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise AgendaParseError("items must be a list")

    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if len(items) >= max_items:
            logger.warning(
                "cofounder.agenda: item cap %d reached; extra items dropped",
                max_items,
            )
            break
        valid = _validate_item(item, index, persona_ids, repo_slugs)
        if valid is not None:
            items.append(valid)

    if not items:
        raise AgendaParseError("no valid agenda items survived validation")
    return summary, items


def _validate_item(
    item: Any,
    index: int,
    persona_ids: frozenset[str],
    repo_slugs: frozenset[str],
) -> dict[str, Any] | None:
    """One agenda line, or None (dropped with a warning). Fail-closed."""
    if not isinstance(item, dict):
        logger.warning("cofounder.agenda: item %d is not an object; dropped", index)
        return None
    unknown = set(item) - set(ITEM_KEYS)
    if unknown:
        logger.warning(
            "cofounder.agenda: item %d carries unknown keys %s; dropped",
            index,
            sorted(unknown),
        )
        return None

    persona = item.get("persona")
    persona = persona.strip() if isinstance(persona, str) else ""
    if persona not in persona_ids:
        logger.warning(
            "cofounder.agenda: item %d persona %r is not registered; dropped",
            index,
            item.get("persona"),
        )
        return None

    repo = item.get("repo")
    if repo is not None:
        repo = repo.strip() if isinstance(repo, str) else ""
        if repo not in repo_slugs:
            logger.warning(
                "cofounder.agenda: item %d repo %r is not tracked; dropped",
                index,
                item.get("repo"),
            )
            return None

    task = item.get("task")
    task = task.strip() if isinstance(task, str) else ""
    if not task:
        logger.warning("cofounder.agenda: item %d has no task text; dropped", index)
        return None

    why = item.get("why")
    why = why.strip() if isinstance(why, str) else ""

    priority = item.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or priority not in (1, 2, 3):
        priority = 2

    mode = item.get("mode")
    mode = mode.strip().lower() if isinstance(mode, str) else "draft"
    if mode not in ITEM_MODES:
        mode = "draft"
    if mode == "code" and repo is None:
        # Fail-safe downgrade: a code dispatch needs a repo worktree.
        logger.warning(
            "cofounder.agenda: item %d proposes mode=code without a repo; "
            "downgraded to draft",
            index,
        )
        mode = "draft"

    return {
        "persona": persona,
        "repo": repo,
        "task": _cap(task, TASK_TEXT_CAP),
        "why": _cap(why, WHY_TEXT_CAP),
        "priority": priority,
        "mode": mode,
    }


# =============================================================================
# Artifact + card + state.
# =============================================================================


def render_agenda_markdown(
    summary: str,
    items: list[dict[str, Any]],
    scan: dict[str, Any],
    today: str,
) -> str:
    """The vault artifact body (frontmatter + propose-only banner + lines)."""
    lines = [
        "---",
        "tags: [system, cofounder, agenda]",
        f"date: {today}",
        "status: proposed",
        f"items: {len(items)}",
        "---",
        f"# Co-Founder Agenda — {today}",
        "",
        PROPOSE_ONLY_BANNER,
        "",
    ]
    if summary:
        lines += [summary, ""]
    lines.append("## Proposed Assignments")
    lines.append("")
    for number, item in enumerate(items, start=1):
        target = f" → `{item['repo']}`" if item["repo"] else ""
        lines.append(f"{number}. **{item['persona']}**{target} — {item['task']}")
        if item["why"]:
            lines.append(f"   - why: {item['why']}")
        lines.append(
            f"   - priority: P{item['priority']} | mode: {item.get('mode', 'draft')}"
        )
    lines += [
        "",
        "## Scan Coverage",
        "",
        f"- repos scanned: {len(scan['repos'])}",
        f"- personas available: {len(scan['personas'])}",
        f"- open co-founder projects: {len(scan['projects'])}",
        "",
    ]
    return "\n".join(lines)


def _has_delegated_lines(projects_dir: Path | str, today: str) -> bool:
    """True when today's JSON artifact carries any delegated line.

    Fail-open to False (a missing/unreadable artifact never blocks the
    morning pass — the delegation transport is what needs the stamps).
    """
    import json as json_mod

    try:
        path = Path(projects_dir) / AGENDAS_SUBDIR / f"AGENDA-{today}.json"
        if not path.is_file():
            return False
        data = json_mod.loads(path.read_text(encoding="utf-8"))
        return any(
            isinstance(item, dict) and item.get("status") == "delegated"
            for item in (data.get("items") or [])
        )
    except Exception:
        logger.warning("cofounder.agenda: delegated-lines check failed", exc_info=True)
        return False


def _write_artifact(
    projects_dir: Path | str,
    today: str,
    summary: str,
    items: list[dict[str, Any]],
    scan: dict[str, Any],
) -> Path:
    """Atomic write of the day's agenda pair (same-day rerun overwrites).

    Two artifacts per day: the ``.md`` (the human propose-only view — never
    mutated after write) and a ``.json`` sibling (the machine-readable line
    list WS3's delegation reads and stamps ``status`` on: proposed ->
    delegated). A JSON write failure degrades to md-only with a warning —
    the operator can still read the agenda; only ``/cofounder run`` loses
    its input for the day.
    """
    import json as json_mod

    from cofounder import project_model
    from shared import file_lock

    agendas_dir = Path(projects_dir) / AGENDAS_SUBDIR
    agendas_dir.mkdir(parents=True, exist_ok=True)
    path = agendas_dir / f"AGENDA-{today}.md"
    content = render_agenda_markdown(summary, items, scan, today)
    with file_lock(path, timeout=_STATE_LOCK_TIMEOUT_S):
        project_model._atomic_write(path, content)
    try:
        json_path = agendas_dir / f"AGENDA-{today}.json"
        payload = {
            "date": today,
            "summary": summary,
            "items": [
                {"n": number, **item, "status": "proposed"}
                for number, item in enumerate(items, start=1)
            ],
        }
        with file_lock(json_path, timeout=_STATE_LOCK_TIMEOUT_S):
            project_model._atomic_write(
                json_path, json_mod.dumps(payload, indent=2)
            )
    except Exception:
        logger.warning("cofounder.agenda: json sibling write failed", exc_info=True)
    return path


def _send_card(
    settings,
    agenda_settings,
    today: str,
    summary: str,
    items: list[dict[str, Any]],
    notify: Callable | None,
) -> None:
    """The gated Telegram card. Fail-open — a card failure never fails a pass.

    An operator-emptied ``COFOUNDER_NOTIFY_LEVELS`` is the global cofounder
    mute and wins over ``COFOUNDER_AGENDA_NOTIFY``; otherwise the resolved
    settings are extended with the ``agenda`` level for this ONE call (the
    env, and therefore v1 terminal-flip filtering, is untouched).
    """
    try:
        if not agenda_settings.notify:
            return
        if not settings.notify_levels:
            logger.info(
                "cofounder.agenda: COFOUNDER_NOTIFY_LEVELS is empty; card muted"
            )
            return
        if notify is None:
            notify = _resolve_notify()
        card_settings = settings._replace(
            notify_levels=(*settings.notify_levels, AGENDA_LEVEL)
        )
        lines = [f"Proposed agenda for {today} — approve lines to act:"]
        if summary:
            lines += ["", summary, ""]
        for number, item in enumerate(items, start=1):
            target = f" -> {item['repo']}" if item["repo"] else ""
            lines.append(
                f"{number}. [P{item['priority']}] {item['persona']}{target}: "
                f"{item['task']}"
            )
        pseudo = SimpleNamespace(slug=f"agenda-{today}", path=None)
        notify(
            pseudo,
            "\n".join(lines),
            AGENDA_LEVEL,
            settings=card_settings,
            with_buttons=False,
        )
    except Exception:
        logger.warning("cofounder.agenda: card send failed", exc_info=True)


def _resolve_notify() -> Callable:
    """The gated Telegram sender, failing open to a logging stub (v1 shape)."""
    try:
        from cofounder import notify as notify_mod

        return notify_mod.notify
    except Exception:
        logger.warning(
            "cofounder.agenda: notify module unavailable; using the logging stub",
            exc_info=True,
        )
        return _stub_notify


def _stub_notify(project, text: str, level: str, **kwargs) -> bool:
    logger.info("cofounder.agenda: [notify:%s] %s", level, text)
    return False


def _agenda_state(state: dict[str, Any]) -> dict[str, Any]:
    entry = state.get(STATE_KEY)
    return entry if isinstance(entry, dict) else {}


def _record_attempt(state_mod, state_path: Path, today: str) -> None:
    """Count one failed proposal attempt (locked read-modify-write)."""

    def mutate(entry: dict[str, Any]) -> None:
        attempts = entry.get("attempts")
        if not isinstance(attempts, dict):
            attempts = {}
        # Only today's counter matters; stale dates are pruned on write.
        attempts = {today: int(attempts.get(today) or 0) + 1}
        entry["attempts"] = attempts

    _update_agenda_state(state_mod, state_path, mutate)


def _stamp_success(
    state_mod, state_path: Path, today: str, artifact: Path
) -> None:
    """Stamp the day's agenda as produced (locked read-modify-write)."""

    def mutate(entry: dict[str, Any]) -> None:
        entry["last_date"] = today
        entry["last_artifact"] = str(artifact)
        entry["attempts"] = {}

    _update_agenda_state(state_mod, state_path, mutate)


def _update_agenda_state(state_mod, state_path: Path, mutate: Callable) -> None:
    """One locked read-modify-write of the top-level ``agenda`` state key.

    Uses the SAME lock file as the v1 pass (``shared.file_lock`` on the state
    path), so an agenda write can never interleave a run_pass read-modify-write
    from another process. Fail-open: a state failure costs bookkeeping (at
    worst one duplicate agenda tomorrow), never the pass.
    """
    try:
        from shared import file_lock

        with file_lock(state_path, timeout=_STATE_LOCK_TIMEOUT_S):
            state = state_mod.load_state(state_path)
            entry = state.get(STATE_KEY)
            if not isinstance(entry, dict):
                entry = {}
            state[STATE_KEY] = entry
            mutate(entry)
            state_mod._write_state(state, state_path)
    except Exception:
        logger.warning("cofounder.agenda: state write failed", exc_info=True)


# =============================================================================
# CLI.
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cofounder.agenda",
        description="Run one co-founder morning-agenda pass (propose-only).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="dry run: full scan + proposal logging, no artifact/state/card writes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the due check (hour / already-ran-today / attempt cap); "
        "the kill switch and COFOUNDER_AGENDA_ENABLED still apply",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_agenda_pass(dry_run=args.test, force=args.force)
    logger.info(
        "cofounder.agenda: outcome=%s items=%d artifact=%s",
        result.outcome,
        result.items,
        result.artifact_path or "none",
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
