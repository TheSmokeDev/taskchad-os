"""Gated cognitive pass — the mind thinks before it speaks (Living Self Act 3).

Three connected pieces, all gated so DEFAULT/short turns add ZERO extra LLM
calls and a substantive turn adds EXACTLY ONE monologue call:

- ``should_run_cognitive_pass`` — a PURE gate (Rule-1 knobs): the turn fires
  only when the pass is enabled, the ALREADY-detected ``active_process`` is in
  the configured ``fire_processes`` set (default ``{"planning"}``), and the
  message clears a length floor. The gate consumes the live process value the
  engine already detected (engine.py:953) — it NEVER re-runs detection (G1).
- ``run_cognitive_monologue`` — one monologue LLM call via the refactored
  ``*_process`` function (which calls ``internal_monologue`` -> ``wm.transform``
  -> ``run_with_runtime_lanes``, the Claude->Codex->Gemini fallback). It appends
  the thought as a ``role="system", region="internal"`` memory so the region
  renderer (``prompt_regions_from_working_memory``, role=="system" only) SEES it
  — a role="assistant" memory would be invisible. It SURFACES failure via an
  explicit ``ok`` flag (M4): a raising process_fn returns ``(wm, "", [], False)``
  with a visible print — it does NOT double-swallow the raise into a
  benign-looking empty-but-ok result; the engine owns the receipt.
- ``maybe_queue_actions`` — the LIVE policy seam (B2). For each proposed action
  up to ``max_actions_per_turn`` it queues ONLY ``operator_notification`` actions
  that ``evaluate_action_policy`` allows. Integration actions are default-denied
  INSIDE ``evaluate_action_policy`` (the unchanged integration-action gate) and
  are NOT queued by this pass. Queuing != dispatch — no adapter is called in the
  hot path (Act 4 owns the drain).

History purity (Living Mind Act 4 invariant): the monologue lives ONLY on the
prompt-assembly ``turn_wm`` (region="internal"). It never enters ``message.text``,
``response_text``, or the persisted transcript — the engine persists
``current_wm`` (not the enriched ``turn_wm``).
"""

from __future__ import annotations

import inspect


def _accepts_processor_cwd(fn) -> bool:
    """True iff ``fn`` accepts the ``processor`` keyword (or ``**kwargs``).

    Lets ``run_cognitive_monologue`` thread the F2 model tier + F4 cwd to the
    refactored ``*_process`` functions while staying compatible with injected
    test/legacy ``process_fn``s that take only ``(wm)``. Fail-open to ``False``
    (positional-only) when the signature cannot be read — the bare call always
    works; only the real process functions need the kwargs and they inspect
    cleanly.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return "processor" in params


def _bounded_monologue_wm(wm, *, max_chars: int):
    """Return a WM whose system context is BUDGETED + win32-capped (F1).

    The monologue must think over a BOUNDED context — never the full uncapped
    identity payload. Left unbounded, ``process_fn -> internal_monologue ->
    wm.transform -> render_runtime_request -> wm.to_system_prompt()`` ships a
    ~90K-char ``system_prompt["append"]`` which WinError-206s on the native
    Claude lane (argv limit 32767) — so the monologue reliably FAILS on the
    operator's primary platform+lane (a systematic outage masquerading as an
    occasional ``monologue_failed``).

    The fix reuses the EXACT reply-path mechanism (the same budgeting +
    truncation the engine applies at the region-render seam), so there is ONE
    bound, not a second band-aid:

    1. ``prompt_regions_from_working_memory(wm, REGION_BUDGETS)`` +
       ``assemble_regions`` — the per-region token budgets (the reply path's
       budgeted view of the SAME turn context).
    2. ``truncate_for_win32_argv`` — the canonical 27000-char head cap (the SAME
       helper the reply path's ``_truncate_win32_append`` delegates to).

    The bounded text becomes a SINGLE ``role="system"`` memory; the original
    NON-system memories (the recent-conversation trace) are preserved so the
    monologue still sees the live turn. ``to_system_prompt()`` over this WM now
    renders <= ``max_chars`` (plus the small recent-conversation tail
    ``render_runtime_request`` appends), so the monologue's own RuntimeRequest
    append can no longer overflow argv. Pure WM transform — no LLM, no I/O.
    """
    from cognition.regions import (
        assemble_regions,
        prompt_regions_from_working_memory,
        truncate_for_win32_argv,
    )
    from cognition.working_memory import Memory, WorkingMemory

    try:
        from config import REGION_BUDGETS
    except Exception:
        REGION_BUDGETS = {}

    regions = prompt_regions_from_working_memory(wm, REGION_BUDGETS)
    assembled = assemble_regions(regions)
    # ``assemble_regions`` already emits the per-region ``# Header`` blocks; strip
    # the single outer header ``to_system_prompt`` would add (the injected memory
    # gets ONE wrapper) so the bounded block is not double-headered. The win32
    # head cap is the LAST step — it bounds the total argv length regardless of
    # how the regions compose (the reply path's exact ordering).
    bounded_text = truncate_for_win32_argv(assembled, max_chars)

    # Preserve the live conversation trace (the non-system memories — what the
    # monologue actually needs to think about THIS turn) and drop the raw
    # uncapped system memories; inject the bounded context as ONE system memory.
    # ``order_regions`` sorts unknown regions LAST, so the bounded block is named
    # ``identity`` (first in region_order) to lead the rendered prompt — its
    # ``# Identity`` wrapper is the only outer header and reads honestly (the
    # block opens with the SOUL identity content).
    preserved = tuple(m for m in wm.memories if m.role != "system")
    bounded = WorkingMemory(
        soul_name=wm.soul_name,
        memories=preserved,
        region_order=wm.region_order,
    )
    if bounded_text.strip():
        bounded = bounded.with_memory(Memory(
            role="system",
            content=bounded_text,
            region="identity",
            source="cognition",
        ))
    return bounded


def should_run_cognitive_pass(
    message_text: str,
    active_process,
    *,
    settings=None,
) -> tuple[bool, str]:
    """Pure gate: should the cognitive pass fire for this turn? Returns (fire, reason).

    Reasons are the GATE verdict only (``disabled`` / ``not_substantive`` /
    ``too_short`` / ``fired``); the engine separates the run OUTCOME
    (empty_monologue / monologue_failed / timeout / fired_content) from the gate
    verdict (M3). Settings resolve at call time (Rule 1) — no module-level bind.
    The ``active_process`` is consumed as-is (G1: never re-runs process detection).
    """
    if settings is None:
        from config import get_cognitive_pass_settings
        settings = get_cognitive_pass_settings()
    if not settings.enabled:
        return False, "disabled"
    value = str(getattr(active_process, "value", active_process)).lower()
    if value not in settings.fire_processes:
        return False, "not_substantive"
    if len((message_text or "").strip()) < settings.min_chars:
        return False, "too_short"
    return True, "fired"


async def run_cognitive_monologue(
    wm,
    active_process,
    cwd,
    *,
    process_fn=None,
    settings=None,
):
    """Run ONE monologue via the refactored *_process; enrich WM; surface failure.

    Returns ``(WorkingMemory, monologue_text, list[ProactiveAction], ok)``:
    - ``process_fn`` defaults to ``execute_process(active_process)`` — the
      refactored ``*_process`` that returns the 3-tuple
      ``(WorkingMemory, monologue_text, list[ProactiveAction])``.
    - F1: the monologue thinks over a BUDGETED + win32-capped view of ``wm``
      (``_bounded_monologue_wm``) — NOT the full uncapped identity payload. Left
      unbounded the monologue's own RuntimeRequest append is ~90K chars and
      WinError-206s on the native Claude lane (argv limit 32767). The bound
      reuses the reply path's exact budgeting + 27000-char truncation, so the
      monologue is correct AND cheap (a budgeted context is the right input for
      a "think before replying" step).
    - F2: the monologue runs on the CHEAP model tier (``settings.model``, default
      ``"fast"`` = haiku). A "think before replying" pass is a classic cheap-model
      job; the default expensive reply profile would ~2x the input cost.
    - F4: ``cwd`` is threaded through to the monologue's RuntimeRequest so it runs
      in the project root (matching the reply path), not ``Path.cwd()``.
    - the thought is appended as a ``role="system", region="internal"`` memory to
      the ORIGINAL ``wm`` (NOT the bounded thinking-scratch WM, and NOT the
      monologue's own conversation-trace WM) so the REPLY sees the full context
      plus the thought, and the renderer (role=="system" only) renders it (a
      role="assistant" internal memory would be invisible).
    - the ``actions`` list is THREADED OUT (B2) so the engine can queue it.
    - FAIL-OPEN + SURFACE (M4): a raising ``process_fn`` returns the ORIGINAL
      ``wm`` + ``""`` + ``[]`` + ``ok=False`` WITH a visible print. ``ok=False``
      lets the ENGINE record ``reason="monologue_failed"`` — this does NOT bury
      the failure as a benign empty-but-ok result.
    """
    from cognition.regions import WIN32_APPEND_MAX_CHARS
    from cognition.working_memory import Memory

    if settings is None:
        from config import get_cognitive_pass_settings
        settings = get_cognitive_pass_settings()

    if process_fn is None:
        from cognition.processes import execute_process
        process_fn = execute_process(active_process)

    # F1: budget + win32-cap the context the monologue thinks over. The thinking
    # WM is SEPARATE from the enrichment WM the engine renders for the reply.
    thinking_wm = _bounded_monologue_wm(wm, max_chars=WIN32_APPEND_MAX_CHARS)

    # Decide the call shape ONCE from the signature (deterministic — never a
    # call-it-and-catch-TypeError dance that would double-invoke on a real error
    # or mis-read a TypeError raised inside the monologue body). The refactored
    # *_process functions accept processor/cwd; an injected test/legacy process_fn
    # may take only (wm).
    pass_kwargs = _accepts_processor_cwd(process_fn)

    try:
        # F2 (model tier) + F4 (cwd) thread straight through to the *_process ->
        # internal_monologue -> wm.transform -> render_runtime_request. The WM
        # process_fn returns (its own thinking-scratch trace) is DISCARDED — only
        # the thought + actions are kept.
        if pass_kwargs:
            _scratch, thought, actions = await process_fn(
                thinking_wm, processor=settings.model, cwd=cwd,
            )
        else:
            _scratch, thought, actions = await process_fn(thinking_wm)
    except Exception as exc:
        # Visible (silent-failure guard); ok=False -> engine sets monologue_failed.
        print(f"[cognitive_pass] monologue failed (non-fatal): {exc!r}", flush=True)
        return wm, "", [], False

    thought = (thought or "").strip()
    enriched = wm
    if thought:
        enriched = wm.with_memory(Memory(
            role="system",
            content=thought,
            region="internal",
            source="cognition",
        ))
    return enriched, thought, list(actions or []), True


def maybe_queue_actions(
    actions,
    *,
    settings=None,
    queue=None,
) -> int:
    """Queue operator_notification proposals through the default-deny policy seam (B2).

    The LIVE production caller is ``engine._maybe_cognitive_pass``. For each
    action up to ``max_actions_per_turn``, queue ONLY when
    ``channel == "operator_notification"`` AND ``evaluate_action_policy(action)``
    allows it. Integration actions are default-denied INSIDE
    ``evaluate_action_policy`` (the unchanged integration-action gate) and are
    NOT queued by this pass. Returns the count queued. Whole-body fail-open: a
    queue-write failure -> the count so far, never raises (queuing is best-effort
    agency, not a turn dependency). The queue is a physical append-only JSONL
    file (Rule 2) whose own ``dedupe_key`` rejects active duplicates.
    """
    if not actions:
        return 0
    if settings is None:
        from config import get_cognitive_pass_settings
        settings = get_cognitive_pass_settings()
    try:
        from cognition.proactive_actions import (
            ProactiveActionQueue,
            evaluate_action_policy,
        )
        if queue is None:
            from config import PROACTIVE_ACTION_QUEUE_FILE
            queue = ProactiveActionQueue(PROACTIVE_ACTION_QUEUE_FILE)

        queued = 0
        for action in actions:
            if queued >= settings.max_actions_per_turn:
                break
            # Act-3 scope: operator_notification ONLY (the default-allowed
            # surface). Integration actions are default-denied at the policy
            # seam (the integration-action gate inside evaluate_action_policy).
            if getattr(action, "channel", "") != "operator_notification":
                continue
            allowed, _decision = evaluate_action_policy(action)
            if not allowed:
                continue
            if queue.append(action):
                queued += 1
        return queued
    except Exception as exc:
        # Visible (silent-failure guard); best-effort agency never breaks a turn.
        print(f"[cognitive_pass] queue failed (non-fatal): {exc!r}", flush=True)
        return 0


__all__ = (
    "should_run_cognitive_pass",
    "run_cognitive_monologue",
    "maybe_queue_actions",
)
