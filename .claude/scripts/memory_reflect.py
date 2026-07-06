"""
Daily Reflection Script for The Homie

Reviews yesterday's daily log (and optionally last N days) and uses Claude
Agent SDK to promote important items to MEMORY.md. Runs daily at 8 AM via
OS scheduler.

Usage:
    uv run python memory_reflect.py              # Run reflection
    uv run python memory_reflect.py --test       # Dry run (no file edits)
    uv run python memory_reflect.py --days 3     # Review last 3 days
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override

apply_persona_override()

# M4 import-order pattern (PRD-8 Phase 2 WS3): inject .claude/chat onto sys.path
# AFTER apply_persona_override() boot-shim and BEFORE importing the new shim.
# Lifts the inline pattern previously living at the recall-import site below to
# module-level so the bare-script invocation (no conftest) resolves the import.
_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.amendments import (  # noqa: E402
    ProposalLedger,
    build_amendment_gate_section,
    ledger_file_lock,
    process_amendment_output,
)
from cognition.proactive_brief import build_proactive_brief_section  # noqa: E402
from cognition.scheduled_payload import (  # noqa: E402
    build_scheduled_cognition_payload,
)

from config import (  # noqa: E402
    AMENDMENT_APPLY_LIMIT,
    AMENDMENT_LEDGER_FILE,
    AMENDMENT_SECTION_CAP,
    DAILY_DIR,
    GOALS_FILE,
    MEMORY_DIR,
    MEMORY_FILE,
    OWNER_NAME,
    PROJECT_ROOT,
    REFLECTION_STATE_FILE,
    SELF_FILE,
    SOUL_FILE,
    USER_FILE,
    ensure_directories,
    get_background_models,
    get_today_log_path,
    now_local,
)
from runtime.base import RuntimeRequest  # noqa: E402
from runtime.capabilities import TOOL_REASONING  # noqa: E402
from runtime.lane_router import run_with_runtime_lanes  # noqa: E402
from repository_memory import read_text_safe  # noqa: E402
from shared import (  # noqa: E402
    append_to_daily_log,
    file_lock,
    load_state,
    save_state,
    validate_bash_command,
)

# =============================================================================
# LOG HELPERS
# =============================================================================

MAX_LOG_CHARS = 20_000


def get_recent_logs(days: int = 1) -> list[tuple[str, str]]:
    """Read the last N days of daily logs.

    Returns list of (date_str, content) tuples, most recent first.
    """
    logs: list[tuple[str, str]] = []
    today = now_local().date()

    for i in range(1, days + 1):
        target_date = today - timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        log_path = DAILY_DIR / f"{date_str}.md"

        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            # Truncate to limit token usage — keep the end (freshest entries)
            if len(content) > MAX_LOG_CHARS:
                content = "... (truncated)\n\n" + content[-MAX_LOG_CHARS:]
            logs.append((date_str, content))

    return logs


def load_current_memory() -> str:
    """Read current MEMORY.md content."""
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text(encoding="utf-8")
    return ""


def load_user_file() -> str:
    """Read current USER.md content."""
    if USER_FILE.exists():
        return USER_FILE.read_text(encoding="utf-8")
    return ""


def load_soul_file() -> str:
    """Read current SOUL.md content."""
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text(encoding="utf-8")
    return ""


def load_goals_file() -> str:
    """Read current GOALS.md content."""
    if GOALS_FILE.exists():
        return GOALS_FILE.read_text(encoding="utf-8")
    return ""


def load_self_file() -> str:
    """Read current SELF.md content."""
    if SELF_FILE.exists():
        return SELF_FILE.read_text(encoding="utf-8")
    return ""


# =============================================================================
# IDENTITY SECTION ASSEMBLY (PRD-8 Phase 2 WS3 — F2 post-build fix)
# =============================================================================


def _assemble_reflect_identity_section(memory_dir: Path) -> str:
    """Assemble the daily-reflection identity section using the shim.

    Single source of truth for the prompt's identity prologue — production
    code (``_run_reflection_inner``) and parity tests both consume this
    helper, so any drift in headers or ordering breaks both at once.

    Order MEMORY/USER/SOUL/SELF/GOALS and ``## Current X.md`` headers are
    contract-locked by ``tests/test_memory_reflect.py``.
    """
    payload = build_scheduled_cognition_payload(memory_dir).identity
    current_memory = payload.get("MEMORY", "")
    current_user = payload.get("USER", "")
    current_soul = payload.get("SOUL", "")
    current_self = payload.get("SELF", "")
    current_goals = payload.get("GOALS", "")
    current_repositories = read_text_safe(memory_dir / "REPOSITORIES.md")

    return f"""## Current MEMORY.md

{current_memory}

## Current USER.md

{current_user}

## Current SOUL.md

{current_soul}

## Current SELF.md

{current_self}

## Current GOALS.md (read-only context — do NOT edit this file during reflection)

{current_goals}

## Current REPOSITORIES.md (private repo routing context)

{current_repositories}"""


def _assemble_reflect_cognition_section(
    memory_dir: Path,
    inference_state_file: Path | None = None,
) -> str:
    """Assemble the unified proactive brief for daily reflection."""

    return build_proactive_brief_section(
        memory_dir,
        inference_state_file=inference_state_file,
        include_identity=False,
        header="## Scheduled Proactive Brief",
    )


def _assemble_reflect_repo_routing_section() -> str:
    """Assemble the repository-pages routing rules for the reflection prompt.

    Single source of truth for the ``### 5. Repository pages`` prompt block —
    production (``_run_reflection_inner``) and the routing tests consume this
    helper, so a dropped bullet breaks both at once. US-019 adds the
    co-founder routing bullet: project activity from the vault's
    ``cofounder/`` folder routes to the owning repo page's Dispatch History
    exactly like Archon dispatches already do.
    """

    cofounder_bullet = (
        f"- Route co-founder project activity ({MEMORY_DIR / 'cofounder'} builds, "
        "dispatches, status flips) to the owning repo page's `## Dispatch History` "
        "the same way, resolving the repo from the project file's `repo:` frontmatter."
    )
    return f"""### 5. Repository pages ({MEMORY_DIR / "repositories"})
When the daily logs contain repository/codebase activity:
- Resolve the repo slug from REPOSITORIES.md first.
- Append Archon/Codex dispatches, workflow names, branches, worktrees, outcomes, and blockers to that repo page's `## Dispatch History`.
{cofounder_bullet}
- Append commits, pull requests, local proof, and validation results to `## Recent Activity`.
- Append new repo-specific operating rules to `## Workflow Preferences`.
- Do not auto-create a new repo page unless the repo appears in at least three daily logs or the user explicitly asked for the page.
- Keep private local paths and dispatch history in the private memory vault only."""


def _assemble_reflect_amendment_section(
    ledger_file: Path | None = None,
) -> str:
    """Assemble the human-gated amendment proposal instructions.

    ``ledger_file`` is a ``None`` sentinel resolved to
    ``AMENDMENT_LEDGER_FILE`` at call time (Rule 1 — never bind tunable
    config in default args).
    """

    if ledger_file is None:
        ledger_file = AMENDMENT_LEDGER_FILE
    return build_amendment_gate_section(
        ledger_file,
        source="memory_reflect",
        ledger=ProposalLedger(ledger_file),
    )


# =============================================================================
# MAIN REFLECTION FUNCTION
# =============================================================================


async def _run_self_model_pass(days: int, test_mode: bool) -> None:
    """Run the log-independent self-model blocks: Act-1 belief extraction,
    Act-2 contradiction pass, and inference decay.

    These read the chat.db corpus and the belief store — never the daily
    logs — so they must also run on a persona's no-logs first pass (a
    brand-new persona has attributed turns but no daily logs yet). Called
    from `_run_reflection_inner` in both the normal flow and the no-logs
    persona branch; each block keeps its own non-blocking try/except.
    """
    # --- Living Self Act 1 (B2): operator-belief extraction from VERBATIM
    # chat.db user turns ---
    # The real LLM claim-extractor over the operator's OWN words (NOT the
    # daily-log paraphrase in log_context, NOT staging). Amortized once per
    # reflection, provider-agnostic via reasoning_step. Whole-block try/except
    # mirrors the promotion/decay non-blocking style; the count may legitimately
    # be 0 on a quiet day or when no interactive user turns fall in the window.
    #
    # Persona-corpus semantics (US-007): under a named profile, reads THIS
    # persona's attributed turns from the install DB, gates them through
    # is_injection_attempt rejection, and forces source='reflection' on every
    # claim (no persona-sourced claim can ever mint a sacrosanct 'explicit').
    try:
        from cognition.operator_beliefs import (
            apply_operator_beliefs,
            extract_operator_beliefs,
        )
        from session import get_session_store, read_operator_user_turns

        from config import INFERENCE_STATE_FILE
        from personas import activity as _personas_activity
        from personas.core import get_default_paths

        active_profile = _personas_activity.get_active_profile_name()
        is_persona_run = active_profile not in ("default", "custom")
        corpus_persona_id = active_profile if is_persona_run else None

        window_start = now_local() - timedelta(days=days)
        if is_persona_run:
            # Persona corpora ALWAYS live in the install DB (the R1 keystone):
            # a named profile reads its own attributed turns from there.
            install_store = get_session_store(
                chat_db_path=get_default_paths()["data"] / "chat.db"
            )
        else:
            # Main/custom-profile runs must read their OWN store via active-
            # profile resolution (a custom profile reads the store it writes to).
            install_store = get_session_store()
        user_turns = read_operator_user_turns(
            window_start, store=install_store, persona_id=corpus_persona_id
        )

        if is_persona_run and user_turns:
            from cognition.injection import is_injection_attempt

            pre_filter = len(user_turns)
            user_turns = [t for t in user_turns if not is_injection_attempt(t)]
            dropped = pre_filter - len(user_turns)
            if dropped:
                print(
                    f"[{now_local()}] Persona injection filter: "
                    f"dropped {dropped}/{pre_filter} turns",
                    flush=True,
                )

        claims = await extract_operator_beliefs(user_turns, cwd=PROJECT_ROOT)

        if is_persona_run:
            for c in claims:
                c["kind"] = "inferred"

        belief_count = 0
        write_time_applied = 0
        if not test_mode:
            belief_count, write_time_applied = await apply_operator_beliefs(
                claims, INFERENCE_STATE_FILE, cwd=PROJECT_ROOT
            )
            if write_time_applied:
                # WS3 #84 — operator-visible write-time resolution count (M3).
                print(
                    f"[{now_local()}] write-time contradictions applied: "
                    f"{write_time_applied}",
                    flush=True,
                )
        label = f"Persona '{active_profile}'" if is_persona_run else "Operator"
        print(
            f"[{now_local()}] {label}-belief extraction: "
            f"{len(user_turns)} turns -> {len(claims)} claims -> {belief_count} written"
        )
        append_to_daily_log(
            f"{label}-belief extraction: {len(claims)} claims from "
            f"{len(user_turns)} verbatim turns, {belief_count} written to self-model",
            "Self-Model",
        )
    except ImportError:
        pass  # Cognition/session module not available — skip extraction
    except Exception as e:
        print(f"[{now_local()}] Operator-belief extraction error (non-blocking): {e}")

    # --- Living Self Act 2 (the keystone): belief-contradiction pass ---
    # Wires the disconfirmation primitive contradict() into a real caller. Runs
    # AFTER the Act-1 extraction (so a belief written THIS cycle is judged against
    # the corpus) and BEFORE decay (so decay sees post-contradiction confidences).
    # Embedding PRE-FILTER -> LLM JUDGE (provider-agnostic) -> EXPLICIT-protective
    # resolution policy -> audited contradict(). Whole-block try/except mirrors the
    # extraction/decay non-blocking style; K may legitimately be 0 (no candidates,
    # or the judge found no real conflict) — success is "completes + logs a count,"
    # not ">=1 contradiction." test_mode runs the judge but skips the live apply.
    try:
        from cognition import belief_conflicts
        from cognition.self_model import InferenceTracker

        from config import INFERENCE_STATE_FILE

        records = InferenceTracker(INFERENCE_STATE_FILE).load()
        pairs = belief_conflicts.find_candidate_pairs(records)
        conflicts = await belief_conflicts.judge_contradictions(
            pairs, cwd=PROJECT_ROOT
        )
        applied = 0
        if not test_mode:
            applied = belief_conflicts.apply_contradictions(
                conflicts, INFERENCE_STATE_FILE
            )
        print(
            f"[{now_local()}] Contradiction pass: {len(pairs)} pairs -> "
            f"{len(conflicts)} conflicts -> {applied} applied"
        )
        append_to_daily_log(
            f"Contradiction pass: {len(pairs)} candidate pairs, "
            f"{len(conflicts)} judged conflicts, {applied} applied",
            "Self-Model",
        )
    except ImportError:
        pass  # Cognition module not available — skip contradiction pass
    except Exception as e:
        print(f"[{now_local()}] Contradiction pass error (non-blocking): {e}")

    # --- Move 5a: Inference decay + state sync ---
    try:
        from cognition.self_model import InferenceTracker

        from config import INFERENCE_STATE_FILE

        tracker = InferenceTracker(INFERENCE_STATE_FILE)
        decayed = tracker.decay_old_inferences()
        if decayed > 0:
            print(f"[{now_local()}] Decayed {decayed} old inferences")
            append_to_daily_log(
                f"Decayed {decayed} old inferences (confidence lowered)", "Self-Model"
            )
    except ImportError:
        pass
    except Exception as e:
        print(f"[{now_local()}] Inference decay error (non-blocking): {e}")


async def run_reflection(test_mode: bool = False, days: int = 1) -> str | None:
    """Run daily reflection with concurrency guard.

    Wraps the inner reflection with a file lock to prevent simultaneous runs.
    """
    try:
        with file_lock(REFLECTION_STATE_FILE, timeout=5.0):
            return await _run_reflection_inner(test_mode, days)
    except TimeoutError:
        print(f"[{now_local()}] Another reflection is already running, skipping")
        return None


async def _run_reflection_inner(test_mode: bool = False, days: int = 1) -> str | None:
    """Run daily reflection using Agent SDK.

    Reviews recent daily logs and promotes important items to MEMORY.md.

    Args:
        test_mode: If True, run in dry-run mode (no file edits).
        days: Number of days of logs to review (default: 1 = yesterday only).

    Returns:
        Response summary, or None if REFLECTION_OK.
    """
    from claude_agent_sdk import HookMatcher

    print(f"[{now_local()}] Running daily reflection (days={days}, test={test_mode})...")

    # Load recent logs
    logs = get_recent_logs(days=days)
    if not logs:
        # Persona runs read their belief corpus from chat.db, not daily logs —
        # a brand-new persona has attributed turns but no daily logs yet, so
        # the self-model pass must still run (first beliefs). Fail-open: if
        # profile detection errors, fall through to the main-run skip.
        try:
            from personas import activity as _personas_activity

            _active_profile = _personas_activity.get_active_profile_name()
        except Exception:
            _active_profile = "default"
        if _active_profile not in ("default", "custom"):
            msg = (
                f"No daily logs found for the last {days} day(s) — "
                "running persona corpus pass only"
            )
            print(f"[{now_local()}] {msg}")
            append_to_daily_log(f"REFLECTION_LOGS_EMPTY - {msg}", "Reflection")
            await _run_self_model_pass(days, test_mode)
            return None
        msg = f"No daily logs found for the last {days} day(s), skipping reflection"
        print(f"[{now_local()}] {msg}")
        append_to_daily_log(f"REFLECTION_SKIPPED - {msg}", "Reflection")
        return None

    # Build log context
    log_sections: list[str] = []
    for date_str, content in logs:
        log_sections.append(f"### Daily Log: {date_str}\n\n{content}")
    log_context = "\n\n---\n\n".join(log_sections)

    # Proactive recall — search memory for context related to today's logs
    recalled_section = ""
    try:
        _chat_dir = Path(__file__).resolve().parent.parent / "chat"
        if str(_chat_dir) not in sys.path:
            sys.path.insert(0, str(_chat_dir))
        from recall_service import recall as recall_fn

        from config import RECALL_BACKGROUND_MAX_CHARS, RECALL_BACKGROUND_MAX_RESULTS

        log_summary = log_context[:300] if log_context else ""
        if log_summary:
            recall_resp = await recall_fn(
                query=log_summary,
                memory_dir=MEMORY_DIR,
                caller="reflection",
                max_results=RECALL_BACKGROUND_MAX_RESULTS,
            )
            if recall_resp.formatted_text:
                recalled_section = (
                    "\n\n## Recalled Context (from memory search)\n\n"
                    "The following related content was found in memory. "
                    "Check for duplicates before promoting.\n\n"
                    + recall_resp.formatted_text[:RECALL_BACKGROUND_MAX_CHARS]
                )
                print(f"[{now_local()}] Recalled {len(recalled_section)} chars for reflection")
    except Exception as e:
        print(f"[{now_local()}] Recall for reflection failed (non-blocking): {e}")

    # PRD-8 Phase 2 WS3: assemble identity section via the extracted helper.
    # Order MEMORY/USER/SOUL/SELF/GOALS + headers locked by parity tests in
    # tests/test_memory_reflect.py — production helper is the test target.
    identity_section = _assemble_reflect_identity_section(MEMORY_DIR)
    cognition_section = _assemble_reflect_cognition_section(MEMORY_DIR)
    amendment_section = _assemble_reflect_amendment_section()

    dry_run_note = (
        "\n\nDRY RUN: Do NOT edit any files. Just describe what you would change.\n"
        if test_mode
        else ""
    )

    reflection_prompt = f"""Daily memory reflection. Review recent daily logs and update \
long-term memory files.
{dry_run_note}
{identity_section}
{cognition_section}
{amendment_section}

## Recent Daily Logs

{log_context}
{recalled_section}
## Instructions

Review the daily logs carefully and propose durable memory updates as needed:

### 1. MEMORY.md ({MEMORY_FILE})
Propose important items:
- Key decisions and their rationale
- Lessons learned or mistakes
- Important facts or configurations
- Project status updates
- Upcoming events needing preparation

### 2. USER.md ({USER_FILE})
Propose an update when you notice patterns about {OWNER_NAME or "the user"}:
- Communication preferences (how they like to interact)
- Schedule patterns (when they work, meeting patterns, creative time)
- Content preferences (what topics, formats, or styles they gravitate toward)
- Tool/workflow preferences (what they use, how they like things done)
- Team updates (new collaborators, role changes)
- New integrations or account info

### 3. SOUL.md ({SOUL_FILE})
Propose an update ONLY if you see clear evidence of communication style adaptations:
- Tone preferences confirmed through repeated interactions
- Behavioral patterns that should be codified
- Changes to how the assistant should operate

### 4. SELF.md ({SELF_FILE})
Propose an update ONLY when you see clear evidence in the logs — require 2+ instances or an explicit lesson.
Do NOT propose for one-off mentions.

- **Capabilities** — A new tool or approach confirmed to work
- **Patterns** — A recurring successful behavior observed this week
- **Failure Modes** — A mistake that recurred in the logs
- **Confidence Notes** — An assumption corrected, or a known uncertain area

1-2 sentences per entry. If nothing meets the bar, skip the proposal.

{_assemble_reflect_repo_routing_section()}

**Rules:**
- Do not edit MEMORY.md, USER.md, SOUL.md, or SELF.md directly
- Use the amendment proposal ledger for any change to those files
- You may edit existing repository pages under {MEMORY_DIR / "repositories"} for repo-scoped routing/activity updates
- You may append a concise run summary to today's daily log ({get_today_log_path()})
- Do NOT duplicate items already present in a file
- Keep entries concise
- Only update USER.md/SOUL.md when there is clear, repeated evidence (not one-off mentions)
- Log only what you proposed to today's daily log ({get_today_log_path()})

If nothing is worth updating in any file, respond with exactly: REFLECTION_OK
"""

    try:
        result = await run_with_runtime_lanes(
            RuntimeRequest(
                prompt=reflection_prompt,
                cwd=PROJECT_ROOT,
                task_name="memory_reflect",
                capability=TOOL_REASONING,
                # QUALITY background tier (sonnet) — deep synthesis that rewrites
                # durable memory. Cheap vs Opus; never the interactive flagship.
                model=get_background_models()["quality"],
                setting_sources=["user", "project"],
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=[
                    "Read",
                    "Edit",
                    "Glob",
                    "Grep",
                    "Bash",
                ],
                permission_mode="acceptEdits",
                max_turns=20,
                hooks={
                    "PreToolUse": [
                        HookMatcher(
                            matcher="Bash",
                            hooks=[validate_bash_command],
                        )
                    ]
                },
            )
        )
        response_text = result.text
        print(
            f"[{now_local()}] Reflection completed via {result.provider}:{result.model}"
            + (f" cost=${result.cost_usd:.4f}" if result.cost_usd else "")
        )
        if not test_mode:
            # Reentrant ledger lock — shared.file_lock here would deadlock
            # against the ledger mutations inside (per-handle OS locks).
            with ledger_file_lock(AMENDMENT_LEDGER_FILE):
                apply_results = process_amendment_output(
                    response_text,
                    ProposalLedger(AMENDMENT_LEDGER_FILE),
                    MEMORY_DIR,
                    default_source="memory_reflect",
                    apply_limit=AMENDMENT_APPLY_LIMIT,
                    section_cap=AMENDMENT_SECTION_CAP,
                )
            applied = [item for item in apply_results if item.status == "applied"]
            if applied:
                print(
                    f"[{now_local()}] Auto-applied {len(applied)} reflection amendment(s)"
                )

    except Exception as e:
        # PRD-8 Phase 7a WS4 R2 NM2 — detect kill-switch and exit cleanly
        # (NOT failed-with-traceback). Late-bind import (defensive).
        try:
            from security.kill_switches import KillSwitchDisabled
        except ImportError:
            KillSwitchDisabled = ()  # type: ignore[assignment,misc]
        if isinstance(e, KillSwitchDisabled):  # type: ignore[arg-type]
            switch_name = getattr(e, "switch_name", "unknown")
            print(f"[{now_local()}] Reflection skipped: kill-switch '{switch_name}' disabled")
            append_to_daily_log(
                f"**SKIPPED**: Reflection skipped (kill-switch '{switch_name}' disabled)",
                "Reflection",
            )
            return None  # exit 0, NOT an error
        print(f"[{now_local()}] Reflection error: {e}")
        append_to_daily_log(f"**ERROR**: Reflection failed - {e}", "Reflection")
        return None

    # --- Promotion Pipeline (Move 2) ---
    try:
        from cognition.promotion import run_promotion_pipeline
        from cognition.staging import StagingStore

        from config import STAGING_STORE_PATH

        store = StagingStore(STAGING_STORE_PATH)
        promotion_results = await run_promotion_pipeline(
            staging_store=store,
            memory_dir=MEMORY_DIR,
            cwd=PROJECT_ROOT,
            dry_run=test_mode,
        )

        promoted = [r for r in promotion_results if r.action == "promoted"]
        rejected = [r for r in promotion_results if r.action == "rejected"]

        if promoted:
            targets: dict[str, int] = {}
            for r in promoted:
                targets[r.target_file] = targets.get(r.target_file, 0) + 1
            target_summary = ", ".join(f"{v} to {k}" for k, v in targets.items())
            append_to_daily_log(
                f"Promoted {len(promoted)} candidates from staging: {target_summary}",
                "Promotion",
            )
        if rejected:
            reasons: dict[str, int] = {}
            for r in rejected:
                key = r.reason.split(" (")[0]
                reasons[key] = reasons.get(key, 0) + 1
            reason_summary = ", ".join(f"{v}x {k}" for k, v in reasons.items())
            append_to_daily_log(
                f"Rejected {len(rejected)} staging candidates: {reason_summary}",
                "Promotion",
            )

        expired = store.cleanup_expired()
        if expired:
            append_to_daily_log(
                f"Cleaned up {expired} expired staging candidates", "Promotion"
            )

    except ImportError:
        pass  # Cognition module not available — skip promotion
    except Exception as e:
        print(f"[{now_local()}] Promotion pipeline error (non-blocking): {e}")
        append_to_daily_log(f"**WARNING**: Promotion pipeline failed - {e}", "Promotion")

    # --- Self-model pass: Act-1 belief extraction, Act-2 contradiction pass,
    # inference decay --- (extracted to _run_self_model_pass so the no-logs
    # persona branch above can run it too; behavior here is unchanged)
    await _run_self_model_pass(days, test_mode)

    # --- Move 5a (state sync half): sync state files to vault ---
    try:
        from state_sync import sync_state_to_vault

        sync_results = sync_state_to_vault()
        synced = [k for k, v in sync_results.items() if v]
        if synced:
            print(f"[{now_local()}] Synced state to vault: {synced}")
    except ImportError:
        pass
    except Exception as e:
        print(f"[{now_local()}] State sync error (non-blocking): {e}")

    # Update state
    state = load_state(REFLECTION_STATE_FILE)
    state["last_run"] = now_local().isoformat()
    state["days_reviewed"] = days
    state["logs_found"] = len(logs)
    state["result"] = "REFLECTION_OK" if "REFLECTION_OK" in response_text else "promoted"
    save_state(state, REFLECTION_STATE_FILE)

    response_text = response_text.strip()

    if "REFLECTION_OK" in response_text:
        append_to_daily_log("REFLECTION_OK - Nothing to promote from recent logs", "Reflection")
        print(f"[{now_local()}] Reflection OK - nothing to promote")
    else:
        append_to_daily_log(f"Promoted items from last {days} day(s) to MEMORY.md", "Reflection")

        if test_mode:
            print(f"[{now_local()}] DRY RUN - would have promoted:\n{response_text[:500]}")
        else:
            print(f"[{now_local()}] Reflection promoted items to MEMORY.md")

    # Reindex AFTER all daily log appends + state saves — catches everything
    try:
        _chat_dir_ri = Path(__file__).resolve().parent.parent / "chat"
        if str(_chat_dir_ri) not in sys.path:
            sys.path.insert(0, str(_chat_dir_ri))
        from recall_service import reindex_changed

        stats = reindex_changed(MEMORY_DIR)
        if stats["files_indexed"] > 0:
            print(f"[{now_local()}] Reindexed {stats['files_indexed']} memory files after reflection")
    except Exception as e:
        print(f"[{now_local()}] Reindex after reflection failed (non-blocking): {e}")

    # Entity compilation: compile concepts from the daily log(s) reviewed
    if not test_mode and "REFLECTION_OK" not in response_text:
        try:
            from entity_extractor import compile_single_log

            for date_str, _content in get_recent_logs(days):
                log_path = DAILY_DIR / f"{date_str}.md"
                report = compile_single_log(log_path, MEMORY_DIR)
                if report and (report.pages_created or report.pages_updated):
                    print(
                        f"[{now_local()}] Compiled entities from {date_str}: "
                        f"+{len(report.pages_created)} created, ~{len(report.pages_updated)} updated"
                    )
        except Exception as e:
            print(f"[{now_local()}] Entity compilation after reflection failed (non-blocking): {e}")

    # --- Sweep + Lint post-step ---
    if not test_mode:
        try:
            from entity_extractor import sweep_uncompiled

            totals = sweep_uncompiled(MEMORY_DIR)
            if totals["files_compiled"] > 0:
                print(
                    f"[{now_local()}] Sweep: {totals['files_compiled']} notes compiled, "
                    f"+{totals['pages_created']} concepts"
                )
        except Exception as e:
            print(f"[{now_local()}] Sweep after reflection failed (non-blocking): {e}")

        try:
            from entity_extractor import load_schema
            from vault_lint import run_lint

            schema = load_schema(MEMORY_DIR)
            issues = run_lint(MEMORY_DIR, schema=schema)
            errors = [i for i in issues if i.severity == "error"]
            warnings = [i for i in issues if i.severity == "warning"]
            if errors or warnings:
                print(
                    f"[{now_local()}] Vault lint: {len(errors)} errors, {len(warnings)} warnings"
                )
                # Log top 5 errors to daily log for visibility
                top = errors[:5] if errors else warnings[:5]
                lint_summary = "; ".join(f"[{i.check}] {i.file}" for i in top)
                append_to_daily_log(f"Vault lint: {len(errors)}E/{len(warnings)}W — {lint_summary}", "Lint")
            else:
                print(f"[{now_local()}] Vault lint: clean")
        except Exception as e:
            print(f"[{now_local()}] Vault lint after reflection failed (non-blocking): {e}")

    # --- Dream consolidation post-step ---
    if not test_mode:
        try:
            from memory_dream import run_dream

            dream_result = await run_dream(test_mode=False, force=False, days=days)
            if dream_result and dream_result != "DREAM_SILENT":
                print(f"[{now_local()}] Dream consolidation completed post-reflection")
                append_to_daily_log("Dream consolidation ran as reflection post-step", "Reflection")
            elif dream_result == "DREAM_SILENT":
                print(f"[{now_local()}] Dream post-reflection: no signal (SILENT)")
        except Exception as e:
            print(f"[{now_local()}] Dream post-reflection failed (non-blocking): {e}")

    # --- Hermes Scout post-step (daily upstream intelligence) ---
    if not test_mode:
        try:
            from example_scout import run_example_scout

            scout_result = await run_example_scout(test_mode=False, days=1)
            if scout_result and scout_result != "HERMES_SILENT":
                print(f"[{now_local()}] Hermes Scout completed post-reflection")
                append_to_daily_log("Hermes Scout ran as daily post-step", "Reflection")
            elif scout_result == "HERMES_SILENT":
                print(f"[{now_local()}] Hermes Scout: no upstream activity (SILENT)")
        except Exception as exc:
            print(f"[{now_local()}] Hermes Scout post-reflection failed (non-blocking): {exc}")

    # --- Signal engine post-step (daily business intelligence) ---
    if not test_mode:
        try:
            from business_signal.signal_engine import run_signal_engine

            signal_result = await run_signal_engine(test_mode=False)
            if signal_result and signal_result != "SIGNAL_SILENT":
                print(f"[{now_local()}] Signal engine completed post-reflection: {signal_result}")
            elif signal_result == "SIGNAL_SILENT":
                print(f"[{now_local()}] Signal engine: no relevant signal (SILENT)")
        except Exception as exc:
            print(f"[{now_local()}] Signal engine post-reflection failed (non-blocking): {exc}")

    # --- Vault log append (chronological wiki timeline) ---
    if not test_mode and "REFLECTION_OK" not in response_text:
        try:
            from entity_extractor import append_vault_log

            append_vault_log(
                MEMORY_DIR,
                "reflect",
                f"Daily reflection for last {days} day(s)",
                bullets=[
                    f"days reviewed: {days}",
                    f"logs reviewed: {len(logs)}",
                ],
            )
        except Exception as exc:
            print(f"[{now_local()}] Vault log append failed (non-blocking): {exc}")

    if "REFLECTION_OK" in response_text:
        return None
    return response_text


# =============================================================================
# ENTRY POINT
# =============================================================================


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Daily memory reflection")
    parser.add_argument("--test", action="store_true", help="Dry run mode")
    parser.add_argument("--json", action="store_true", help="Emit validation probe JSON")
    parser.add_argument("--vault", type=Path, default=None, help="Override vault root for validation probe")
    parser.add_argument("--days", type=int, default=1, help="Days of logs to review (default: 1)")
    args = parser.parse_args()

    if args.json:
        from cognitive_loop_test_harness import build_scheduled_entrypoint_report

        report = build_scheduled_entrypoint_report(
            "memory_reflect",
            args.vault or MEMORY_DIR,
            test_mode=args.test,
        )
        print(json.dumps(report, indent=2))
        return

    ensure_directories()

    if args.test:
        print("Running in TEST MODE (dry run, no file edits)")
        print(f"Project root: {PROJECT_ROOT}")
        print(f"Reviewing last {args.days} day(s) of logs")

    result = asyncio.run(run_reflection(test_mode=args.test, days=args.days))

    if result:
        try:
            print(f"\nReflection result:\n{result[:500]}")
        except UnicodeEncodeError:
            print(f"\nReflection result:\n{result[:500].encode('ascii', 'replace').decode()}")
    else:
        print("\nReflection complete: OK or skipped")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        from datetime import datetime
        err_log = PROJECT_ROOT / ".claude" / "scripts" / "reflection_errors.log"
        try:
            with open(err_log, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now().isoformat()} ===\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
