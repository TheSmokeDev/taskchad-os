"""Dedicated keystone tests for ``cognition.cognitive_pass`` (Living Self Act 3).

Fills the gaps NOT already covered by ``test_living_self_act3.py`` (which proves
the should-run gate, history-purity, the win32 cap via _bounded_monologue_wm, the
asyncio timeout path in the engine, and maybe_queue_actions). This file pins the
SIGNATURE-DISPATCH seam that decides the monologue call shape:

  - ``_accepts_processor_cwd`` — True for fns taking ``processor`` or ``**kwargs``,
    False for positional-only / unreadable signatures (fail-open).
  - ``run_cognitive_monologue`` threads ``processor=``/``cwd=`` ONLY when the
    process_fn accepts them, and falls back to the bare ``(wm)`` call for a legacy
    positional-only fn — without a call-it-and-catch-TypeError dance that could
    double-invoke or mis-read a TypeError raised inside the monologue body.
  - the enrichment lands on the ORIGINAL wm (not the bounded thinking-scratch wm),
    so the reply sees full context PLUS the thought.

The LLM boundary is the injected ``process_fn`` (the refactored *_process stand-in).
No network, no engine construction, tmp_path-free (pure WM transforms).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import cognitive_pass as cp  # noqa: E402
from cognition.processes import MentalProcess  # noqa: E402
from cognition.working_memory import Memory, WorkingMemory  # noqa: E402


def _wm():
    return WorkingMemory(soul_name="TestHomie")


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# _accepts_processor_cwd — the signature-dispatch helper
# ===========================================================================


def test_accepts_processor_kw():
    def fn(wm, *, processor="claude", cwd=None):
        return wm
    assert cp._accepts_processor_cwd(fn) is True


def test_accepts_var_keyword():
    def fn(wm, **kwargs):
        return wm
    assert cp._accepts_processor_cwd(fn) is True


def test_positional_only_legacy_fn_rejected():
    def fn(wm):
        return wm
    assert cp._accepts_processor_cwd(fn) is False


def test_unreadable_signature_fails_open_false():
    # A C-builtin has no readable Python signature -> ValueError -> fail-open False.
    assert cp._accepts_processor_cwd(len) is False


# ===========================================================================
# run_cognitive_monologue — dispatch shape follows the signature
# ===========================================================================


def test_monologue_threads_processor_and_cwd_when_accepted():
    """A process_fn that accepts processor/cwd RECEIVES the model tier + cwd."""
    seen = {}

    async def pf(wm, *, processor="claude", cwd=None):
        seen["processor"] = processor
        seen["cwd"] = cwd
        return wm, "INNER THOUGHT", []

    out, thought, actions, ok = _run(
        cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path("/proj"), process_fn=pf)
    )
    assert ok is True
    assert thought == "INNER THOUGHT"
    # default model tier is "fast" (haiku) and the cwd is threaded straight through
    assert seen["processor"] == "fast"
    assert seen["cwd"] == Path("/proj")


def test_monologue_bare_call_for_positional_only_fn():
    """A legacy positional-only process_fn is called with ONLY (wm) — no kwargs."""
    calls = []

    async def pf(wm):
        calls.append(wm)
        return wm, "LEGACY THOUGHT", []

    out, thought, actions, ok = _run(
        cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path("/proj"), process_fn=pf)
    )
    assert ok is True
    assert thought == "LEGACY THOUGHT"
    assert len(calls) == 1  # invoked exactly once (no double-invoke / retry dance)


def test_monologue_invokes_process_fn_exactly_once_on_success():
    """No call-and-catch retry: a successful monologue calls the fn once."""
    count = {"n": 0}

    async def pf(wm, *, processor="claude", cwd=None):
        count["n"] += 1
        return wm, "ONE", []

    _run(cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path("."), process_fn=pf))
    assert count["n"] == 1


def test_enrichment_lands_on_original_wm_not_thinking_scratch():
    """The thought is appended to the ORIGINAL wm (full context), not the bounded scratch.

    The process_fn returns a DIFFERENT scratch WM; the enrichment must ignore that
    scratch and append the thought to the wm the engine passed in, so the reply
    sees the full turn context PLUS the thought.
    """
    original = _wm().with_memory(Memory(
        role="user", content="ORIGINAL TURN CONTEXT", region="recent_conversation",
        source="user",
    ))

    async def pf(wm, *, processor="claude", cwd=None):
        # return a scratch WM that does NOT contain the original turn content
        scratch = WorkingMemory(soul_name="scratch")
        return scratch, "THOUGHT", []

    out, thought, actions, ok = _run(
        cp.run_cognitive_monologue(original, MentalProcess.PLANNING, Path("."), process_fn=pf)
    )
    assert ok is True
    contents = [m.content for m in out.memories]
    # both the original turn context AND the new internal thought are present
    assert "ORIGINAL TURN CONTEXT" in contents
    internal = [m for m in out.memories if m.region == "internal"]
    assert len(internal) == 1
    assert internal[0].role == "system"  # role=system so the region renderer SEES it
    assert internal[0].content == "THOUGHT"


def test_actions_threaded_out_as_list():
    """The actions list is always returned as a list (never None), threaded from the fn."""
    from cognition.proactive_actions import ProactiveAction

    action = ProactiveAction(
        source="cognition.planning",
        channel="operator_notification",
        effect="notify",
        reason="followup",
        message="something worth attention here",
    )

    async def pf(wm, *, processor="claude", cwd=None):
        return wm, "THOUGHT", [action]

    out, thought, actions, ok = _run(
        cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path("."), process_fn=pf)
    )
    assert isinstance(actions, list)
    assert len(actions) == 1
    assert actions[0].channel == "operator_notification"


def test_none_actions_normalized_to_empty_list():
    """A process_fn returning None for actions yields [] (never None) to the caller."""

    async def pf(wm, *, processor="claude", cwd=None):
        return wm, "THOUGHT", None

    out, thought, actions, ok = _run(
        cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path("."), process_fn=pf)
    )
    assert actions == []
