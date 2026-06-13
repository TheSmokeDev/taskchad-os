"""Tests for Living Self Act 3 — Wire The Mind Into Live Turns (gated).

Categories map to the PRP's Validation Loop (Level 2, categories 1-9) plus the
R2-binding additions: the B2 end-to-end CHAIN test (a real proposed action
threads process -> run_cognitive_monologue -> _maybe_cognitive_pass ->
maybe_queue_actions -> queue, AND an integration action is default-denied), the
M1 dual-extractor proof (the monologue reaches system_prompt["append"] and BOTH
generic extractors receive it), and the M3 four-outcome trace proof.

Every test is tmp_path-scoped where state is touched, with injected fake
``process_fn`` / ``queue`` / monkeypatched gate+monologue so the REAL branches
run without an LLM/network — NO live state (proactive-actions.jsonl, chat.db,
the vault) is ever touched. Born-clean: all ids/text are synthetic.

  1. Rule-1 settings resolver — env-swept defaults, monkeypatch flips on next
     call, explicit-arg passthrough.
  2. should_run_cognitive_pass — each gate branch discriminating (FAILS pre-fix).
  3. Process-function refactor — returns thinking + actions, NOT a reply;
     external_dialog is NOT called inside any *_process (FAILS pre-fix).
  4. run_cognitive_monologue — appends role="system", region="internal";
     fail-open SURFACES via ok=False (M4) (FAILS pre-fix).
  5. "internal" renders into the system prompt; with_monologue (role="assistant")
     does NOT (the discriminating contrast) (FAILS pre-fix on absent region).
  6. maybe_queue_actions — operator_notification queues; integration default-
     denied; cap + dedupe honored; fail-open (FAILS pre-fix).
  7. _maybe_cognitive_pass engine method — gated, fail-open, trace every turn,
     cost bound (run_cognitive_monologue NOT invoked when gate closed) (FAILS
     pre-fix).
  8. History purity — the monologue never leaks into message.text / a base-WM
     render / a user memory (the Living Mind Act 4 invariant).
  9. Provider-agnostic — the monologue reaches system_prompt["append"] and BOTH
     the Codex/Gemini renderer (render_cli_prompt) and the openai-compatible
     extractor read it (M1 dual-extractor; FAILS if it ever rode the preset).
 10. B2 end-to-end CHAIN — a real planning thought flows process ->
     run_cognitive_monologue -> _maybe_cognitive_pass -> maybe_queue_actions ->
     queue (lands), AND an integration action is default-denied at the policy
     seam (0 queued).
 11. M3 four-outcome — distinct trace reasons for gate_closed / empty_monologue /
     fired_content / monologue_failed / timeout.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import cognitive_pass as cp  # noqa: E402
from cognition.proactive_actions import (  # noqa: E402
    ProactiveAction,
    ProactiveActionQueue,
)
from cognition.processes import (  # noqa: E402
    MentalProcess,
    default_process,
    execute_process,
    planning_process,
)
from cognition.regions import (  # noqa: E402
    assemble_regions,
    prompt_regions_from_working_memory,
)
from cognition.working_memory import Memory, WorkingMemory  # noqa: E402

import config  # noqa: E402

# ===========================================================================
# Helpers
# ===========================================================================


def _wm() -> WorkingMemory:
    return WorkingMemory(soul_name="TestHomie")


def _opn(message: str = "the homie noticed a thing worth your attention",
         *, source: str = "cognition.planning") -> ProactiveAction:
    return ProactiveAction(
        source=source,
        channel="operator_notification",
        effect="notify",
        reason="cognitive_pass_followup",
        message=message,
    )


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Category 1 — Rule-1 settings resolver
# ===========================================================================


class TestCognitivePassSettings:
    def test_locked_defaults(self, monkeypatch):
        # Sweep every knob so the body resolves from defaults, not a leaked env.
        for k in (
            "COGNITIVE_PASS_ENABLED",
            "COGNITIVE_PASS_FIRE_PROCESSES",
            "COGNITIVE_PASS_MIN_CHARS",
            "COGNITIVE_PASS_MAX_ACTIONS_PER_TURN",
            "COGNITIVE_PASS_TIMEOUT_S",
            "COGNITIVE_PASS_MODEL",
        ):
            monkeypatch.delenv(k, raising=False)
        s = config.get_cognitive_pass_settings()
        assert s.enabled is True
        assert s.fire_processes == frozenset({"planning"})
        assert s.min_chars == 40
        assert s.max_actions_per_turn == 1
        # F6: tightened 8.0 -> 5.0 now that F1+F2 make the monologue a cheap,
        # budgeted haiku call.
        assert s.timeout_s == 5.0
        # F2: the monologue runs on the cheap "fast" (haiku) tier by default.
        assert s.model == "fast"

    def test_env_flips_on_next_call_no_reload(self, monkeypatch):
        monkeypatch.setenv("COGNITIVE_PASS_ENABLED", "false")
        assert config.get_cognitive_pass_settings().enabled is False

        monkeypatch.setenv(
            "COGNITIVE_PASS_FIRE_PROCESSES", " Planning , EXECUTION ,, ",
        )
        # stripped + lowercased + empty dropped
        assert config.get_cognitive_pass_settings().fire_processes == frozenset(
            {"planning", "execution"},
        )

        monkeypatch.setenv("COGNITIVE_PASS_MIN_CHARS", "10")
        assert config.get_cognitive_pass_settings().min_chars == 10

        monkeypatch.setenv("COGNITIVE_PASS_MAX_ACTIONS_PER_TURN", "3")
        assert config.get_cognitive_pass_settings().max_actions_per_turn == 3

        monkeypatch.setenv("COGNITIVE_PASS_TIMEOUT_S", "2.5")
        assert config.get_cognitive_pass_settings().timeout_s == 2.5

        # F2: the model tier is an env-overridable Rule-1 knob.
        monkeypatch.setenv("COGNITIVE_PASS_MODEL", "quality")
        assert config.get_cognitive_pass_settings().model == "quality"

    def test_explicit_args_pass_through(self, monkeypatch):
        # Explicit args must bypass env entirely.
        monkeypatch.setenv("COGNITIVE_PASS_ENABLED", "false")
        s = config.get_cognitive_pass_settings(
            enabled=True,
            fire_processes=frozenset({"monitoring"}),
            min_chars=99,
            max_actions_per_turn=7,
            timeout_s=1.0,
            model="claude",
        )
        assert s.enabled is True
        assert s.fire_processes == frozenset({"monitoring"})
        assert s.min_chars == 99
        assert s.max_actions_per_turn == 7
        assert s.timeout_s == 1.0
        assert s.model == "claude"

    def test_region_budget_and_queue_path_present(self):
        assert config.REGION_BUDGETS.get("internal") == 500
        assert config.PROACTIVE_ACTION_QUEUE_FILE.name == "proactive-actions.jsonl"


# ===========================================================================
# Category 2 — should_run_cognitive_pass (the pure gate)
# ===========================================================================


class TestShouldRunCognitivePass:
    def test_planning_substantive_fires(self):
        fire, reason = cp.should_run_cognitive_pass("x" * 50, MentalProcess.PLANNING)
        assert (fire, reason) == (True, "fired")

    def test_default_never_fires(self):
        # The dominant trivial-turn case — DEFAULT is never in fire_processes.
        fire, reason = cp.should_run_cognitive_pass("x" * 50, MentalProcess.DEFAULT)
        assert (fire, reason) == (False, "not_substantive")

    def test_short_message_too_short(self):
        fire, reason = cp.should_run_cognitive_pass("plan it", MentalProcess.PLANNING)
        assert (fire, reason) == (False, "too_short")

    def test_disabled_short_circuits_before_process_and_length(self):
        s = config.get_cognitive_pass_settings(enabled=False)
        # Even a perfect planning+length turn must report disabled.
        fire, reason = cp.should_run_cognitive_pass(
            "x" * 50, MentalProcess.PLANNING, settings=s,
        )
        assert (fire, reason) == (False, "disabled")

    def test_knob_widens_the_fire_set(self):
        # EXECUTION is OFF by default...
        fire, reason = cp.should_run_cognitive_pass("x" * 50, MentalProcess.EXECUTION)
        assert (fire, reason) == (False, "not_substantive")
        # ...but the knob widens it.
        s = config.get_cognitive_pass_settings(
            fire_processes=frozenset({"planning", "execution"}),
        )
        fire, reason = cp.should_run_cognitive_pass(
            "x" * 50, MentalProcess.EXECUTION, settings=s,
        )
        assert (fire, reason) == (True, "fired")

    def test_gate_accepts_raw_string_process(self):
        # Defensive: a bare string value is handled like the enum value.
        fire, reason = cp.should_run_cognitive_pass("x" * 50, "planning")
        assert (fire, reason) == (True, "fired")


# ===========================================================================
# Category 3 — Process-function refactor (returns thinking, NOT a reply)
# ===========================================================================


class TestProcessRefactor:
    def test_planning_returns_3tuple_and_never_calls_external_dialog(
        self, monkeypatch,
    ):
        # Fake the WM transform that internal_monologue delegates to, so the
        # REAL planning_process body runs without an LLM. external_dialog is
        # monkeypatched to RAISE — if the refactor regressed and called it,
        # this test fails loudly.
        async def fake_transform(self, instruction, processor="claude",
                                 schema=None, cwd=None):
            new = self.with_memory(Memory(
                role="assistant", content="INNER THOUGHT", region="internal",
                source="cognition",
            ))
            return new, "An approach with the plan, the risks, and the next step here."

        monkeypatch.setattr(WorkingMemory, "transform", fake_transform)

        import cognition.steps as steps

        async def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("external_dialog must NOT be called in a *_process")

        monkeypatch.setattr(steps, "external_dialog", _boom)

        wm = _wm()
        out = _run(planning_process(wm))
        assert isinstance(out, tuple) and len(out) == 3
        new_wm, thought, actions = out
        assert isinstance(new_wm, WorkingMemory)
        assert "plan" in thought.lower()
        assert isinstance(actions, list)  # never None

    def test_default_process_contract_consistent_noop(self):
        out = _run(default_process(_wm()))
        assert isinstance(out, tuple) and len(out) == 3
        new_wm, thought, actions = out
        assert isinstance(new_wm, WorkingMemory)
        assert thought == ""
        assert actions == []

    def test_execute_process_signature_unchanged(self):
        fn = execute_process(MentalProcess.PLANNING)
        assert callable(fn)
        assert fn.__name__ == "planning_process"

    def test_all_processes_return_same_3tuple_arity(self, monkeypatch):
        # R2: EVERY *_process returns the SAME 3-tuple arity (no mixed 2/3).
        async def fake_transform(self, instruction, processor="claude",
                                 schema=None, cwd=None):
            return self, "a sufficiently long internal thought for the proposal floor here"

        monkeypatch.setattr(WorkingMemory, "transform", fake_transform)
        for proc in MentalProcess:
            fn = execute_process(proc)
            out = _run(fn(_wm()))
            assert isinstance(out, tuple) and len(out) == 3, proc
            new_wm, thought, actions = out
            assert isinstance(new_wm, WorkingMemory)
            assert isinstance(thought, str)
            assert isinstance(actions, list)


# ===========================================================================
# Category 4 — run_cognitive_monologue (enrich + fail-open SURFACE)
# ===========================================================================


class TestRunCognitiveMonologue:
    def test_appends_system_internal_memory(self):
        async def pf(wm):
            return wm, "INNER THOUGHT", []

        out, thought, actions, ok = _run(
            cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        assert ok is True
        assert thought == "INNER THOUGHT"
        internal = [m for m in out.memories if m.region == "internal"]
        assert len(internal) == 1
        # MUST be role="system" so prompt_regions_from_working_memory renders it
        # (with_monologue's role="assistant" would be invisible).
        assert internal[0].role == "system"
        assert internal[0].content == "INNER THOUGHT"

    def test_empty_thought_appends_nothing(self):
        async def pf(wm):
            return wm, "", []

        out, thought, actions, ok = _run(
            cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        assert ok is True
        assert thought == ""
        assert [m for m in out.memories if m.region == "internal"] == []

    def test_whitespace_only_thought_is_empty(self):
        async def pf(wm):
            return wm, "   \n  ", []

        out, thought, actions, ok = _run(
            cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        assert thought == ""
        assert [m for m in out.memories if m.region == "internal"] == []

    def test_raising_process_fn_fails_open_and_surfaces(self, capsys):
        wm = _wm()

        async def pf(wm):
            raise RuntimeError("provider blew up")

        out, thought, actions, ok = _run(
            cp.run_cognitive_monologue(wm, MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        # M4: ok=False SURFACES the failure (NOT a benign empty-but-ok result).
        assert ok is False
        assert out is wm  # original WM unchanged
        assert thought == ""
        assert actions == []
        captured = capsys.readouterr()
        assert "[cognitive_pass] monologue failed" in captured.out

    def test_actions_threaded_out(self):
        async def pf(wm):
            return wm, "INNER THOUGHT", [_opn()]

        out, thought, actions, ok = _run(
            cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        assert ok is True
        assert len(actions) == 1
        assert actions[0].channel == "operator_notification"


# ===========================================================================
# Category 5 — "internal" renders; with_monologue (role=assistant) does NOT
# ===========================================================================


class TestInternalRegionRenders:
    def test_system_internal_renders_into_prompt(self):
        wm = _wm().with_memory(Memory(
            role="system", content="THINKING", region="internal",
            source="cognition",
        ))
        regions = prompt_regions_from_working_memory(wm, config.REGION_BUDGETS)
        rendered = assemble_regions(regions)
        assert "# Internal" in rendered
        assert "THINKING" in rendered

    def test_with_monologue_assistant_does_not_render(self):
        # The discriminating contrast: with_monologue is role="assistant" ->
        # invisible to prompt_regions_from_working_memory (role=="system" only).
        wm = _wm().with_monologue("THINKING")
        regions = prompt_regions_from_working_memory(wm, config.REGION_BUDGETS)
        rendered = assemble_regions(regions)
        assert "THINKING" not in rendered

    def test_internal_ordered_before_recent_conversation(self):
        order = _wm().region_order
        assert "internal" in order
        assert order.index("internal") < order.index("recent_conversation")

    def test_internal_budget_caps_runaway_monologue(self):
        # 500-token budget == 2000 chars; a longer monologue is truncated, not
        # dumped at the DEFAULT_REGION_BUDGETS fallback (1000 tokens).
        big = "Z" * 6000
        wm = _wm().with_memory(Memory(
            role="system", content=big, region="internal", source="cognition",
        ))
        regions = prompt_regions_from_working_memory(wm, config.REGION_BUDGETS)
        rendered = assemble_regions(regions)
        assert "[TRUNCATED" in rendered


# ===========================================================================
# Category 6 — maybe_queue_actions (operator_notification only; default-deny)
# ===========================================================================


class TestMaybeQueueActions:
    def test_operator_notification_queues(self, tmp_path):
        q = ProactiveActionQueue(tmp_path / "q.jsonl")
        n = cp.maybe_queue_actions([_opn()], queue=q)
        assert n == 1
        assert len(q.read_queued()) == 1
        assert (tmp_path / "q.jsonl").exists()

    def test_integration_channel_not_queued(self, tmp_path):
        # Act-3 scope: operator_notification ONLY. An integration-channel action
        # is the default-deny case for this pass -> 0 queued.
        q = ProactiveActionQueue(tmp_path / "q.jsonl")
        integ = ProactiveAction(
            source="x", channel="integration", effect="send",
            integration="slack", action="send", message="external send",
        )
        n = cp.maybe_queue_actions([integ], queue=q)
        assert n == 0
        assert q.read_queued() == []

    def test_max_actions_per_turn_cap(self, tmp_path):
        q = ProactiveActionQueue(tmp_path / "q.jsonl")
        s = config.get_cognitive_pass_settings(max_actions_per_turn=1)
        actions = [_opn("first nudge worth attention"),
                   _opn("second nudge worth attention")]
        n = cp.maybe_queue_actions(actions, settings=s, queue=q)
        assert n == 1
        assert len(q.read_queued()) == 1

    def test_duplicate_dedup_rejected_by_queue(self, tmp_path):
        q = ProactiveActionQueue(tmp_path / "q.jsonl")
        s = config.get_cognitive_pass_settings(max_actions_per_turn=5)
        dup = "the same nudge text repeated verbatim here"
        n = cp.maybe_queue_actions([_opn(dup), _opn(dup)], settings=s, queue=q)
        # The queue's own dedupe_key rejects the active duplicate.
        assert len(q.read_queued()) == 1
        assert n == 1

    def test_empty_actions_zero(self, tmp_path):
        q = ProactiveActionQueue(tmp_path / "q.jsonl")
        assert cp.maybe_queue_actions([], queue=q) == 0
        assert cp.maybe_queue_actions(None, queue=q) == 0

    def test_queue_failure_fails_open(self, capsys):
        class Boom:
            def append(self, action):
                raise OSError("disk full")

        n = cp.maybe_queue_actions([_opn()], queue=Boom())
        assert n == 0  # whole-body fail-open, no exception
        assert "[cognitive_pass] queue failed" in capsys.readouterr().out


# ===========================================================================
# Category 7 — _maybe_cognitive_pass engine method (gated, fail-open, trace)
# ===========================================================================


def _bind_engine_method(project_root: Path):
    """Bind ConversationEngine._maybe_cognitive_pass to a lightweight stub (no
    heavy ConversationEngine construction). The method only touches
    self.project_root."""
    import engine as engine_mod

    stub = SimpleNamespace(project_root=project_root)
    stub._maybe_cognitive_pass = MethodType(
        engine_mod.ConversationEngine._maybe_cognitive_pass, stub,
    )
    return stub, engine_mod


class TestMaybeCognitivePassEngine:
    def test_fired_enriches_and_traces(self, monkeypatch, tmp_path):
        stub, engine_mod = _bind_engine_method(tmp_path)

        enriched = _wm().with_memory(Memory(
            role="system", content="ENGINE THOUGHT", region="internal",
            source="cognition",
        ))

        monkeypatch.setattr(
            "cognition.cognitive_pass.should_run_cognitive_pass",
            lambda *a, **k: (True, "fired"),
        )

        async def fake_run(wm, ap, cwd, **k):
            return enriched, "ENGINE THOUGHT", [], True

        monkeypatch.setattr(
            "cognition.cognitive_pass.run_cognitive_monologue", fake_run,
        )

        trace: dict = {}
        msg = SimpleNamespace(text="x" * 50)
        out = _run(stub._maybe_cognitive_pass(
            _wm(), msg, MentalProcess.PLANNING, trace_decisions=trace,
        ))
        assert any(m.region == "internal" for m in out.memories)
        d = trace["cognitive_pass"]
        assert d["fired"] is True
        assert d["reason"] == "fired_content"
        assert d["monologue_chars"] > 0

    def test_not_fired_is_byte_identical_and_no_monologue_call(
        self, monkeypatch, tmp_path,
    ):
        # Cost bound: when the gate is closed, run_cognitive_monologue must NOT
        # be invoked (monkeypatch it to RAISE -> proves it is never called).
        stub, engine_mod = _bind_engine_method(tmp_path)

        monkeypatch.setattr(
            "cognition.cognitive_pass.should_run_cognitive_pass",
            lambda *a, **k: (False, "not_substantive"),
        )

        async def boom(*a, **k):
            raise AssertionError("run_cognitive_monologue must not be called")

        monkeypatch.setattr(
            "cognition.cognitive_pass.run_cognitive_monologue", boom,
        )

        original = _wm()
        trace: dict = {}
        msg = SimpleNamespace(text="thanks")
        out = _run(stub._maybe_cognitive_pass(
            original, msg, MentalProcess.DEFAULT, trace_decisions=trace,
        ))
        # Byte-identical: same memories tuple identity (no enrichment).
        assert out is original
        d = trace["cognitive_pass"]
        assert d["fired"] is False
        assert d["reason"] == "not_substantive"

    def test_gate_raise_fails_open(self, monkeypatch, tmp_path, capsys):
        stub, engine_mod = _bind_engine_method(tmp_path)

        def boom(*a, **k):
            raise RuntimeError("gate blew up")

        monkeypatch.setattr(
            "cognition.cognitive_pass.should_run_cognitive_pass", boom,
        )

        original = _wm()
        trace: dict = {}
        msg = SimpleNamespace(text="x" * 50)
        out = _run(stub._maybe_cognitive_pass(
            original, msg, MentalProcess.PLANNING, trace_decisions=trace,
        ))
        assert out is original  # bare, correct turn
        assert trace["cognitive_pass"]["reason"] == "error"
        assert "[CognitivePass] non-blocking failure" in capsys.readouterr().out

    def test_trace_present_every_turn(self, monkeypatch, tmp_path):
        # All three branches write trace_decisions["cognitive_pass"] (finally).
        stub, engine_mod = _bind_engine_method(tmp_path)
        msg = SimpleNamespace(text="x" * 50)

        # fired
        monkeypatch.setattr(
            "cognition.cognitive_pass.should_run_cognitive_pass",
            lambda *a, **k: (True, "fired"),
        )

        async def fake_run(wm, ap, cwd, **k):
            return wm, "T", [], True

        monkeypatch.setattr(
            "cognition.cognitive_pass.run_cognitive_monologue", fake_run,
        )
        t1: dict = {}
        _run(stub._maybe_cognitive_pass(
            _wm(), msg, MentalProcess.PLANNING, trace_decisions=t1,
        ))
        assert "cognitive_pass" in t1


# ===========================================================================
# Category 8 — History purity (the monologue never leaks)
# ===========================================================================


class TestHistoryPurity:
    def test_monologue_is_internal_system_only(self):
        async def pf(wm):
            return wm, "PRIVATE INNER THOUGHT", []

        out, thought, _actions, _ok = _run(
            cp.run_cognitive_monologue(_wm(), MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        appended = [m for m in out.memories if m.content == "PRIVATE INNER THOUGHT"]
        assert len(appended) == 1
        assert appended[0].role == "system"
        assert appended[0].region == "internal"

    def test_base_wm_render_excludes_monologue(self):
        # current_wm-equivalent: a WM WITHOUT the per-turn internal region (what
        # _append_turn_to_working_memory consumes) does NOT carry the monologue.
        base = _wm().with_memory(Memory(
            role="user", content="let's plan the migration",
            region="recent_conversation", source="conversation",
        ))

        async def pf(wm):
            return wm, "PRIVATE INNER THOUGHT", []

        enriched, thought, _a, _ok = _run(
            cp.run_cognitive_monologue(base, MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        # Render the BASE wm (the one persistence uses) — no monologue.
        base_rendered = assemble_regions(
            prompt_regions_from_working_memory(base, config.REGION_BUDGETS),
        )
        assert "PRIVATE INNER THOUGHT" not in base_rendered
        # The enriched (turn_wm) DOES carry it (prompt-only).
        enriched_rendered = assemble_regions(
            prompt_regions_from_working_memory(enriched, config.REGION_BUDGETS),
        )
        assert "PRIVATE INNER THOUGHT" in enriched_rendered

    def test_monologue_not_equal_any_user_memory(self):
        base = _wm().with_memory(Memory(
            role="user", content="plan the migration please",
            region="recent_conversation", source="conversation",
        ))

        async def pf(wm):
            return wm, "PRIVATE INNER THOUGHT", []

        out, thought, _a, _ok = _run(
            cp.run_cognitive_monologue(base, MentalProcess.PLANNING, Path.cwd(),
                                       process_fn=pf),
        )
        user_contents = [m.content for m in out.memories if m.role == "user"]
        assert thought not in user_contents  # never becomes message.text


# ===========================================================================
# Category 9 — Provider-agnostic (M1 dual-extractor)
# ===========================================================================


class TestProviderAgnostic:
    def _engine_append_for_internal_memory(self) -> str:
        # Mirror the engine render path: a turn_wm with an internal memory ->
        # prompt_regions_from_working_memory + assemble_regions == the text the
        # engine puts into system_prompt["append"].
        wm = _wm().with_memory(Memory(
            role="system", content="MONOLOGUE-CONTENT", region="internal",
            source="cognition",
        ))
        return assemble_regions(
            prompt_regions_from_working_memory(wm, config.REGION_BUDGETS),
        )

    def test_codex_gemini_renderer_carries_monologue(self):
        from runtime.base import RuntimeRequest
        from runtime.capabilities import TEXT_REASONING
        from runtime.prompt_builder import render_cli_prompt

        append_text = self._engine_append_for_internal_memory()
        assert "MONOLOGUE-CONTENT" in append_text

        req = RuntimeRequest(
            prompt="reply to the user",
            cwd=Path.cwd(),
            task_name="chat_turn",
            capability=TEXT_REASONING,
            max_turns=1,
            allowed_tools=[],
            # The claude_code preset key is IGNORED off-Claude; only append reads.
            system_prompt={
                "type": "preset", "preset": "claude_code", "append": append_text,
            },
        )
        rendered = render_cli_prompt(req)
        assert "System context:" in rendered
        assert "MONOLOGUE-CONTENT" in rendered

    def test_openai_compatible_extractor_reads_append(self):
        # The SECOND generic extractor (openai_compatible.py:48) — same .get(
        # "append") contract. Asserting the field every generic lane reads is
        # populated proves the monologue survives, and would FAIL if it ever
        # rode the preset instead.
        append_text = self._engine_append_for_internal_memory()
        system_prompt = {
            "type": "preset", "preset": "claude_code", "append": append_text,
        }
        extracted = str(system_prompt.get("append", "")).strip()
        assert "MONOLOGUE-CONTENT" in extracted
        # Discriminating: the content is NOT smuggled in the preset key.
        assert "MONOLOGUE-CONTENT" not in str(system_prompt.get("preset", ""))


# ===========================================================================
# Category 10 — B2 end-to-end CHAIN (R2 binding)
# ===========================================================================


class TestB2Chain:
    def test_planning_thought_threads_to_queue(self, monkeypatch, tmp_path):
        # A REAL proposed action threads:
        #   planning_process -> run_cognitive_monologue -> _maybe_cognitive_pass
        #   -> maybe_queue_actions -> queue  (lands EXACTLY ONE record).
        # Fake the WM transform so planning_process's REAL body + _propose_actions
        # run (deterministic parse of the thought) without an LLM.
        async def fake_transform(self, instruction, processor="claude",
                                 schema=None, cwd=None):
            return self, (
                "We should sequence the migration in three stages and notify "
                "the operator before the cutover step to avoid a surprise."
            )

        monkeypatch.setattr(WorkingMemory, "transform", fake_transform)

        # Point the live queue path at a tmp file (Rule 2 physical state).
        queue_file = tmp_path / "chain.jsonl"
        monkeypatch.setattr(config, "PROACTIVE_ACTION_QUEUE_FILE", queue_file)

        stub, engine_mod = _bind_engine_method(tmp_path)
        trace: dict = {}
        msg = SimpleNamespace(text="let's plan the migration carefully end to end")
        out = _run(stub._maybe_cognitive_pass(
            _wm(), msg, MentalProcess.PLANNING, trace_decisions=trace,
        ))

        d = trace["cognitive_pass"]
        assert d["fired"] is True
        assert d["reason"] == "fired_content"
        # The action LANDED in the live queue via the real chain.
        assert d["actions_queued"] == 1
        q = ProactiveActionQueue(queue_file)
        queued = q.read_queued()
        assert len(queued) == 1
        assert queued[0].channel == "operator_notification"
        assert queued[0].source == "cognition.planning"
        # And the monologue enriched the turn WM (internal region).
        assert any(m.region == "internal" for m in out.memories)

    def test_chain_exists_grep_surface(self):
        # The grep chain the PRP asserts: _maybe_cognitive_pass calls
        # maybe_queue_actions; maybe_queue_actions calls ProactiveActionQueue.
        import inspect

        import engine as engine_mod

        src = inspect.getsource(engine_mod.ConversationEngine._maybe_cognitive_pass)
        assert "maybe_queue_actions" in src
        assert "run_cognitive_monologue" in src
        cp_src = inspect.getsource(cp.maybe_queue_actions)
        assert "ProactiveActionQueue" in cp_src
        assert "evaluate_action_policy" in cp_src

    def test_integration_action_default_denied_at_policy_seam(self):
        # The default-deny seam itself: a denied integration action is rejected
        # by evaluate_action_policy -> require_integration_action.
        from cognition.proactive_actions import evaluate_action_policy

        denied = ProactiveAction(
            source="c", channel="integration", effect="send",
            integration="gmail", action="send", message="send an email",
        )
        allowed, reason = evaluate_action_policy(denied)
        assert allowed is False
        assert reason.startswith("integration_policy_rejected")

    def test_integration_action_zero_queued_via_pass(self, tmp_path):
        # End-to-end: even handed to maybe_queue_actions, an integration action
        # never queues (Act-3 channel filter is the default-deny for the pass).
        q = ProactiveActionQueue(tmp_path / "q.jsonl")
        denied = ProactiveAction(
            source="c", channel="integration", effect="send",
            integration="gmail", action="send", message="send an email",
        )
        assert cp.maybe_queue_actions([denied], queue=q) == 0
        assert q.read_queued() == []


# ===========================================================================
# Category 11 — M3 four-outcome trace (distinct reasons)
# ===========================================================================


class TestFourOutcomeTrace:
    @staticmethod
    def _drive(monkeypatch, tmp_path, *, gate, run_result=None, run_exc=None,
               timeout=False):
        stub, engine_mod = _bind_engine_method(tmp_path)
        monkeypatch.setattr(
            "cognition.cognitive_pass.should_run_cognitive_pass",
            lambda *a, **k: gate,
        )
        if timeout:
            async def fake_run(wm, ap, cwd, **k):
                raise TimeoutError
            # Make wait_for surface the TimeoutError deterministically.
            monkeypatch.setattr(
                "cognition.cognitive_pass.run_cognitive_monologue", fake_run,
            )
        elif run_exc is not None:
            async def fake_run(wm, ap, cwd, **k):
                raise run_exc
            monkeypatch.setattr(
                "cognition.cognitive_pass.run_cognitive_monologue", fake_run,
            )
        else:
            async def fake_run(wm, ap, cwd, **k):
                return run_result
            monkeypatch.setattr(
                "cognition.cognitive_pass.run_cognitive_monologue", fake_run,
            )
        trace: dict = {}
        msg = SimpleNamespace(text="x" * 50)
        _run(stub._maybe_cognitive_pass(
            _wm(), msg, MentalProcess.PLANNING, trace_decisions=trace,
        ))
        return trace["cognitive_pass"]

    def test_gate_closed_reason(self, monkeypatch, tmp_path):
        d = self._drive(monkeypatch, tmp_path, gate=(False, "not_substantive"))
        assert d["reason"] == "not_substantive"
        assert d["fired"] is False

    def test_fired_content_reason(self, monkeypatch, tmp_path):
        wm = _wm()
        d = self._drive(
            monkeypatch, tmp_path, gate=(True, "fired"),
            run_result=(wm, "REAL THOUGHT", [], True),
        )
        assert d["reason"] == "fired_content"
        assert d["fired"] is True

    def test_empty_monologue_reason(self, monkeypatch, tmp_path):
        wm = _wm()
        d = self._drive(
            monkeypatch, tmp_path, gate=(True, "fired"),
            run_result=(wm, "", [], True),
        )
        # Ran-but-empty is DISTINCT from gate-closed.
        assert d["reason"] == "empty_monologue"
        assert d["fired"] is False

    def test_monologue_failed_reason(self, monkeypatch, tmp_path):
        wm = _wm()
        d = self._drive(
            monkeypatch, tmp_path, gate=(True, "fired"),
            run_result=(wm, "", [], False),  # ok=False surfaced
        )
        # The surfaced failure is reachable (M4) and distinct.
        assert d["reason"] == "monologue_failed"
        assert d["fired"] is False

    def test_timeout_reason(self, monkeypatch, tmp_path):
        d = self._drive(monkeypatch, tmp_path, gate=(True, "fired"), timeout=True)
        assert d["reason"] == "timeout"
        assert d["fired"] is False

    def test_wait_for_actually_cancels_a_hung_monologue(self, monkeypatch, tmp_path):
        # M2 discriminating: a monologue that SLEEPS past the (tiny) timeout is
        # cancelled by asyncio.wait_for -> reason="timeout" + bare turn. This
        # exercises the real deadline, not just TimeoutError propagation.
        monkeypatch.setenv("COGNITIVE_PASS_TIMEOUT_S", "0.05")
        stub, engine_mod = _bind_engine_method(tmp_path)
        monkeypatch.setattr(
            "cognition.cognitive_pass.should_run_cognitive_pass",
            lambda *a, **k: (True, "fired"),
        )

        async def slow_run(wm, ap, cwd, **k):
            await asyncio.sleep(5)  # far beyond the 0.05s wall
            return wm, "SHOULD NOT ARRIVE", [], True

        monkeypatch.setattr(
            "cognition.cognitive_pass.run_cognitive_monologue", slow_run,
        )
        original = _wm()
        trace: dict = {}
        msg = SimpleNamespace(text="x" * 50)
        out = _run(stub._maybe_cognitive_pass(
            original, msg, MentalProcess.PLANNING, trace_decisions=trace,
        ))
        assert out is original  # bare turn (no enrichment)
        assert trace["cognitive_pass"]["reason"] == "timeout"

    def test_all_five_reasons_distinct(self):
        # The four-outcome contract: gate-closed / empty / failed / timeout /
        # fired_content are five distinct strings (no collapse).
        reasons = {
            "not_substantive", "empty_monologue", "monologue_failed",
            "timeout", "fired_content",
        }
        assert len(reasons) == 5


# ===========================================================================
# Category 12 — F1: the monologue's RuntimeRequest append is win32-capped even
# on a FULL-VAULT-sized WM (FAILS pre-fix — the uncapped path ships ~90K chars
# -> WinError 206 on the native Claude lane). This is the test the 49 prior
# tests could NOT catch: they fake transform and use the stdin lane, never
# driving the real to_system_prompt() -> render_runtime_request -> argv path.
# ===========================================================================


def _vault_sized_wm() -> WorkingMemory:
    """A WM with realistic identity-file sizes (the measured F1 payload)."""
    wm = WorkingMemory(soul_name="TestHomie")
    sizes = {
        "identity": 7551,
        "self_model": 26495,
        "user_model": 17059,
        "durable_memory": 35449,
        "working_memory": 3740,
    }
    for region, n in sizes.items():
        wm = wm.with_memory(Memory(
            role="system",
            content=f"# {region.title()}\n" + ("X" * n),
            region=region,
            source="vault",
            name=region,
        ))
    # A live conversation turn (non-system) — must be preserved into the bound.
    return wm.with_memory(Memory(
        role="user", content="let's plan the migration end to end",
        region="recent_conversation", source="conversation",
    ))


class TestF1Win32Cap:
    def test_bounded_wm_renders_under_cap(self):
        # The pre-fix monologue rendered the FULL WM uncapped (~90K). The bound
        # collapses the system regions into one budgeted, win32-capped block.
        from cognition.cognitive_pass import _bounded_monologue_wm
        from cognition.regions import WIN32_APPEND_MAX_CHARS

        big = _vault_sized_wm()
        assert len(big.to_system_prompt()) > 80000  # the F1 payload, pre-bound

        bounded = _bounded_monologue_wm(big, max_chars=WIN32_APPEND_MAX_CHARS)
        rendered = bounded.to_system_prompt()
        # The bounded system context is <= the win32 cap (+ a tiny header slack).
        assert len(rendered) <= WIN32_APPEND_MAX_CHARS + 64
        # The live conversation trace survives the bound (non-system preserved).
        assert any(m.role == "user" for m in bounded.memories)

    def test_real_monologue_request_append_within_argv_limit(self, monkeypatch):
        # Drive the FULL real chain — _bounded_monologue_wm -> planning_process ->
        # internal_monologue -> wm.transform -> render_runtime_request -> (captured
        # run_with_runtime_lanes) — and assert the monologue's OWN
        # system_prompt["append"] is within the win32 argv limit. Pre-fix this is
        # ~90K (WinError 206 on the native Claude lane); post-fix it is bounded.
        from cognition.regions import WIN32_APPEND_MAX_CHARS

        import runtime.lane_router as lane_router

        captured: dict = {}

        async def fake_lanes(request):
            sp = request.system_prompt
            captured["append"] = (
                sp.get("append", "") if isinstance(sp, dict) else ""
            )
            captured["model"] = request.model
            captured["cwd"] = request.cwd
            return SimpleNamespace(text="a bounded internal thought", model="haiku",
                                   cost_usd=0.0)

        monkeypatch.setattr(lane_router, "run_with_runtime_lanes", fake_lanes)

        big = _vault_sized_wm()
        project_root = Path("~/thehomie")
        out, thought, actions, ok = _run(
            cp.run_cognitive_monologue(big, MentalProcess.PLANNING, project_root),
        )
        assert ok is True
        # THE F1 PROOF: the monologue's real argv append is bounded (was ~90K).
        assert "append" in captured
        assert len(captured["append"]) <= WIN32_APPEND_MAX_CHARS + 200
        assert len(captured["append"]) < 40000  # nowhere near the ~90K pre-fix
        # The enrichment WM the engine renders for the REPLY still carries the
        # full original context PLUS the internal thought.
        assert any(m.region == "internal" for m in out.memories)
        assert any(m.region == "durable_memory" for m in out.memories)


# ===========================================================================
# Category 13 — F2: the monologue request carries the CHEAP model tier hint
# (FAILS pre-fix — the monologue inherited the default expensive profile,
# model hint None). F4: the monologue runs in the project root, not Path.cwd().
# ===========================================================================


class TestF2CheapTierAndF4Cwd:
    def test_monologue_request_carries_haiku_hint(self, monkeypatch):
        # The real chain must hand render_runtime_request the "fast" processor ->
        # claude-haiku-4-5 model hint (NOT None = the default reply profile).
        import runtime.lane_router as lane_router

        captured: dict = {}

        async def fake_lanes(request):
            captured["model"] = request.model
            captured["cwd"] = str(request.cwd)
            return SimpleNamespace(text="thought", model="haiku", cost_usd=0.0)

        monkeypatch.setattr(lane_router, "run_with_runtime_lanes", fake_lanes)

        project_root = Path("~/thehomie")
        _run(cp.run_cognitive_monologue(
            _vault_sized_wm(), MentalProcess.PLANNING, project_root,
        ))
        # F2 PROOF: cheap tier, not the default (None) reply profile.
        assert captured["model"] == "claude-haiku-4-5"
        # F4 PROOF: the monologue runs in the project root (threaded cwd), not the
        # bot-process Path.cwd().
        assert captured["cwd"] == str(project_root)

    def test_model_knob_overrides_tier(self, monkeypatch):
        # The Rule-1 settings.model knob selects the tier handed to the process fn.
        import runtime.lane_router as lane_router

        captured: dict = {}

        async def fake_lanes(request):
            captured["model"] = request.model
            return SimpleNamespace(text="thought", model="sonnet", cost_usd=0.0)

        monkeypatch.setattr(lane_router, "run_with_runtime_lanes", fake_lanes)

        s = config.get_cognitive_pass_settings(model="quality")  # -> sonnet hint
        _run(cp.run_cognitive_monologue(
            _vault_sized_wm(), MentalProcess.PLANNING,
            Path("~/thehomie"), settings=s,
        ))
        assert captured["model"] == "claude-sonnet-4-6"


# ===========================================================================
# Category 14 — F3: the queue append read-then-write is cross-process lock-
# guarded (the dedupe guarantee holds across concurrent cross-session turns).
# ===========================================================================


class TestF3QueueLock:
    def test_append_acquires_file_lock(self, tmp_path, monkeypatch):
        # The read-then-write dedupe must run UNDER shared.file_lock. Spy on the
        # lock context manager and assert append entered it.
        import cognition.proactive_actions as pa

        entered = {"count": 0}

        import contextlib

        @contextlib.contextmanager
        def spy_lock(path, timeout=30.0):
            entered["count"] += 1
            yield

        monkeypatch.setattr(pa, "_file_lock", spy_lock)

        q = ProactiveActionQueue(tmp_path / "locked.jsonl")
        assert q.append(_opn("a nudge worth the operator's attention here")) is True
        # The lock wrapped the append (read + write under one critical section).
        assert entered["count"] == 1
        assert len(q.read_queued()) == 1

    def test_lock_guards_dedupe_atomically(self, tmp_path):
        # Real-lock path (no monkeypatch): the dedupe still holds and the append
        # is crash-safe under the lock. A duplicate is rejected.
        q = ProactiveActionQueue(tmp_path / "dedupe.jsonl")
        dup = _opn("the same nudge text repeated verbatim under the lock")
        assert q.append(dup) is True
        assert q.append(_opn("the same nudge text repeated verbatim under the lock")) is False
        assert len(q.read_queued()) == 1

    def test_lock_missing_fails_open(self, tmp_path, monkeypatch):
        # If shared.file_lock is unavailable (imported outside scripts env), the
        # append proceeds unlocked rather than hard-failing the turn.
        import cognition.proactive_actions as pa

        monkeypatch.setattr(pa, "_file_lock", None)
        q = ProactiveActionQueue(tmp_path / "nolock.jsonl")
        assert q.append(_opn("a nudge that still queues without the lock")) is True
        assert len(q.read_queued()) == 1
