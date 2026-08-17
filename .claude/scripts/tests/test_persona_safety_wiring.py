"""Persona SAFETY.md wiring (#484) — loader, ordering, stub gate, seeder.

Covers the PRP-persona-safety-file-wiring proof obligations:

- The regression test IS the incident: an authored ``SAFETY.md`` reaches the
  assembled prompt on an off-topic query with recall UNAVAILABLE — the exact
  shape that failed live on 2026-08-16.
- Ordering under REAL pressure: with the assembled append forced past the
  27K win32 head-keep cap, ``safety`` survives while ``recalled_memory`` and
  ``procedural_memory`` are sheared. (A tuple-position assertion alone would
  pass while the region still tail-truncates.)
- Byte-identical prompt parity for (a) no ``SAFETY.md`` at all and (b) an
  unedited seeded stub.
- The authored-vs-stub predicate is SEMANTIC (frontmatter + H1 + HTML
  comments stripped), never a seed-comment string match.
- Overflow is LOUD: an over-budget ``SAFETY.md`` logs persona + byte size +
  cap, and the in-prompt trim is marked, never silent.
- The win32 clamp holds with the largest existing ``SAFETY.md`` (2473 bytes,
  mirrored as a fixture — tests never read the real vault) fully intact.
- The lifecycle seeder no longer creates ``SAFETY.md`` for new profiles, and
  the template any residual creator gets states its own interactive-only
  scope (#485).
"""

from __future__ import annotations

from pathlib import Path

import engine as engine_module
import pytest
from cognition.identity_payload import (
    DEFAULT_INCLUDE,
    build_identity_payload,
    has_authored_content,
)
from cognition.processes import PROCESS_WEIGHTS, MentalProcess, get_process_weights
from cognition.regions import (
    CHARS_PER_TOKEN,
    DEFAULT_REGION_BUDGETS,
    apply_process_weights,
    assemble_regions,
    prompt_regions_from_working_memory,
    truncate_for_win32_argv,
)
from cognition.working_memory import Memory, WorkingMemory
from engine import ConversationEngine
from models import Channel, IncomingMessage, Platform, Thread, User
from session import SQLiteSessionStore

import config as config_module
from personas.lifecycle import _REQUIRED_IDENTITY_FILES, _seed_identity_body
from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult

WIN32_CAP = 27000

# The exact template the 29 pre-#484 profiles carry on disk (generic seed).
LEGACY_STUB = (
    "---\n"
    "profile: sales\n"
    "identity_file: SAFETY.md\n"
    "---\n"
    "\n"
    "# SAFETY\n"
    "\n"
    "<!-- Seeded by `thehomie profile create sales`. "
    "Author this file with profile-specific content as appropriate. -->\n"
)

AUTHORED_SAFETY = (
    "---\n"
    "profile: seo_geo\n"
    "identity_file: SAFETY.md\n"
    "---\n"
    "\n"
    "# SAFETY\n"
    "\n"
    "## Non-negotiable boundaries\n"
    "\n"
    "- Hard spend ceiling: $25/month across paid research tools, "
    "$0.50 per-run cap.\n"
    "- Publishing, deploys, CMS edits, and browser writes are default-deny "
    "without operator approval.\n"
    "- Never expose, copy, or reconfigure secrets or OAuth tokens.\n"
)


def _make_message(text: str) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(
            platform=Platform.TELEGRAM, platform_id="user-1", display_name="YourUser"
        ),
        channel=Channel(
            platform=Platform.TELEGRAM, platform_id="chat-1", is_dm=True
        ),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id="thread-1"),
    )


def _seed_vault(memory_dir: Path, *, safety: str | None = None) -> None:
    """Seed a minimal persona-like vault fixture (never the real vault)."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "SOUL.md").write_text(
        "# SOUL\nFixture: identity, behavioral rules.\n", encoding="utf-8"
    )
    (memory_dir / "MEMORY.md").write_text(
        "# MEMORY\nFixture: durable decisions.\n", encoding="utf-8"
    )
    if safety is not None:
        (memory_dir / "SAFETY.md").write_text(safety, encoding="utf-8")


def _isolate_engine_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, memory_dir: Path
) -> None:
    """Point the engine at fixture state so builds are deterministic."""
    monkeypatch.setattr(config_module, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(
        config_module,
        "INFERENCE_STATE_FILE",
        tmp_path / "absent-inferences.json",
        raising=False,
    )


def _patch_brief_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the session-opening-brief machinery off live STATE_DIR files."""
    import cognition.proactive_brief as _pb

    monkeypatch.setattr(_pb, "read_brief_owed", lambda **kwargs: None)
    monkeypatch.setattr(
        engine_module,
        "resolve_last_operator_activity",
        lambda store, **kwargs: None,
    )


def _make_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_dir: Path,
) -> ConversationEngine:
    _isolate_engine_seams(monkeypatch, tmp_path, memory_dir)
    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(
        parents=True, exist_ok=True
    )
    (project_root / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    return ConversationEngine(store, project_root)


# ===========================================================================
# 1. Authored-vs-stub predicate (semantic, not string-matched)
# ===========================================================================


def test_stub_predicate_current_lifecycle_template() -> None:
    """The lifecycle SAFETY.md template (with its scope note) is a stub."""
    body = _seed_identity_body("SAFETY.md", "sales")
    assert has_authored_content(body, scaffold_title="SAFETY") is False


def test_stub_predicate_legacy_seeded_template() -> None:
    """The pre-#484 generic seed the 29 existing profiles carry is a stub."""
    assert has_authored_content(LEGACY_STUB, scaffold_title="SAFETY") is False


def test_stub_predicate_near_stub_one_authored_line() -> None:
    """One line of real authored text flips the file to authored."""
    near_stub = LEGACY_STUB + "\n- Never spend money without approval.\n"
    assert has_authored_content(near_stub, scaffold_title="SAFETY") is True


def test_stub_predicate_html_comment_only_body() -> None:
    """A file whose only body is an HTML comment is a stub."""
    comment_only = "<!-- nothing authored here,\nspanning lines -->\n"
    assert has_authored_content(comment_only, scaffold_title="SAFETY") is False
    with_frontmatter = (
        "---\nprofile: x\nidentity_file: SAFETY.md\n---\n\n"
        "<!-- still nothing authored -->\n"
    )
    assert has_authored_content(with_frontmatter, scaffold_title="SAFETY") is False


def test_stub_predicate_not_a_string_match_of_seed_comment() -> None:
    """A reworded seed comment is STILL a stub (semantic, not string match)."""
    reworded = (
        "---\nprofile: x\nidentity_file: SAFETY.md\n---\n\n"
        "# SAFETY\n\n"
        "<!-- Completely different template wording after a future reword. -->\n"
    )
    assert has_authored_content(reworded, scaffold_title="SAFETY") is False


def test_stub_predicate_h2_heading_counts_as_authored() -> None:
    """Only the scaffold H1 title is scaffold — an H2 carries authored content."""
    body = (
        "---\nprofile: x\nidentity_file: SAFETY.md\n---\n\n"
        "# SAFETY\n\n## Spend ceiling: $25/month\n"
    )
    assert has_authored_content(body, scaffold_title="SAFETY") is True


def test_stub_predicate_h1_only_policy_counts_as_authored() -> None:
    """r2 FIX 1: constraints written AS H1 headings are authored content.

    The r1 predicate stripped ALL H1 lines, so a policy file written as bare
    H1 rules classified as a stub and silently vanished from the prompt —
    the exact disappearance #484 exists to prevent."""
    h1_rules_only = (
        "---\nprofile: x\nidentity_file: SAFETY.md\n---\n\n"
        "# NEVER SPEND WITHOUT APPROVAL\n"
        "# ALWAYS ASK BEFORE DEPLOYS\n"
    )
    assert has_authored_content(h1_rules_only, scaffold_title="SAFETY") is True

    # Scaffold title present, but a SECOND H1 carries a rule — authored.
    scaffold_plus_second_h1 = (
        "---\nprofile: x\nidentity_file: SAFETY.md\n---\n\n"
        "# SAFETY\n\n"
        "# HARD CEILING: $25/MONTH\n"
    )
    assert (
        has_authored_content(scaffold_plus_second_h1, scaffold_title="SAFETY")
        is True
    )


def test_stub_predicate_frontmatter_policy_counts_as_authored() -> None:
    """r2 FIX 1: policy written as frontmatter keys is authored content.

    Only the seeder's own keys (profile / identity_file) are scaffold; ANY
    additional frontmatter line counts as authored even when the body is
    pure scaffold."""
    frontmatter_policy = (
        "---\n"
        "profile: seo_geo\n"
        "identity_file: SAFETY.md\n"
        "spend_ceiling_usd_month: 25\n"
        "---\n"
        "\n"
        "# SAFETY\n"
        "\n"
        "<!-- body is untouched scaffold -->\n"
    )
    assert has_authored_content(frontmatter_policy, scaffold_title="SAFETY") is True


# ===========================================================================
# 2. Payload gate — stub yields NO key; authored passes through verbatim
# ===========================================================================


def test_default_include_gains_safety() -> None:
    assert "SAFETY" in DEFAULT_INCLUDE


def test_payload_stub_yields_no_key(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    _seed_vault(memory_dir, safety=LEGACY_STUB)

    payload = build_identity_payload(memory_dir)

    assert "SAFETY" not in payload
    assert payload.get("SAFETY", "<missing>") == "<missing>"


def test_payload_authored_yields_verbatim_content(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    _seed_vault(memory_dir, safety=AUTHORED_SAFETY)

    payload = build_identity_payload(memory_dir)

    # Verbatim — the stub predicate gates inclusion, it never strips content.
    assert payload["SAFETY"] == AUTHORED_SAFETY


# ===========================================================================
# 3. Byte-identical prompt parity — no file / unedited stub
# ===========================================================================


def test_prompt_byte_identical_without_safety_and_with_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) no SAFETY.md and (b) an unedited seeded stub render byte-identical
    region assemblies; an authored file changes them."""
    memory_dir = tmp_path / "TheHomie" / "Memory"
    _seed_vault(memory_dir, safety=None)
    convo = _make_engine(tmp_path, monkeypatch, memory_dir)

    baseline = assemble_regions(convo._build_frozen_regions())

    # (b) unedited seeded stub (both template generations) → byte-identical.
    for stub in (LEGACY_STUB, _seed_identity_body("SAFETY.md", "sales")):
        (memory_dir / "SAFETY.md").write_text(stub, encoding="utf-8")
        assert assemble_regions(convo._build_frozen_regions()) == baseline

    # Authored → the prompt DOES change and carries the rule.
    (memory_dir / "SAFETY.md").write_text(AUTHORED_SAFETY, encoding="utf-8")
    authored = assemble_regions(convo._build_frozen_regions())
    assert authored != baseline
    assert "$25/month" in authored
    assert "# Safety" in authored


# ===========================================================================
# 4. The regression test IS the incident (2026-08-16 shape)
# ===========================================================================


@pytest.mark.asyncio
async def test_authored_safety_reaches_prompt_on_off_topic_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authored SAFETY.md is in the system append on a query that does NOT
    mention its rules, with recall UNAVAILABLE — deterministic wiring, not a
    lucky recall match."""
    memory_dir = tmp_path / "TheHomie" / "Memory"
    _seed_vault(memory_dir, safety=AUTHORED_SAFETY)
    convo = _make_engine(tmp_path, monkeypatch, memory_dir)
    _patch_brief_seams(monkeypatch)

    # The incident shape: recall CANNOT be the carrier.
    monkeypatch.setattr(engine_module, "_RECALL_SERVICE_AVAILABLE", False)

    async def passthrough_pass(
        turn_wm, message, active_process, *, trace_decisions=None
    ):
        return turn_wm

    monkeypatch.setattr(convo, "_maybe_cognitive_pass", passthrough_pass)

    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["request"] = request
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude",
            model="test-model",
            profile_key="primary-claude",
            session_id="runtime-session-safety",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    off_topic = "hey homie, how's your day going so far?"
    assert "$25" not in off_topic and "deploy" not in off_topic
    outputs = [out async for out in convo.handle_message(_make_message(off_topic))]
    assert outputs[-1].text == "ok"

    request = captured["request"]
    append = request.system_prompt["append"]
    assert "$25/month" in append
    assert "default-deny" in append
    assert "# Safety" in append
    # Position: safety renders before the durable-memory region, so the win32
    # head-keep cap can never shear it ahead of lower-priority context.
    assert append.index("# Safety") < append.index("# Durable Memory")


# ===========================================================================
# 5. Ordering under REAL truncation pressure (not tuple position)
# ===========================================================================


def _filled_wm(budgets: dict[str, int], safety_content: str) -> WorkingMemory:
    """A WM whose regions are filled to their caps so the assembled append
    clears the 27K win32 cap and head-keep truncation actually fires.

    Every system region ordered BEFORE ``recalled_memory`` is filled to the
    same per-region cap production applies (``budgets`` with the
    ``DEFAULT_REGION_BUDGETS`` fallback — the exact lookup
    ``prompt_regions_from_working_memory`` uses), so the sentinel regions land
    past the 27K boundary the way a real heavy-context turn pushes them."""
    wm = WorkingMemory(soul_name="pressure")

    def cap(region: str) -> int:
        budget = budgets.get(region, DEFAULT_REGION_BUDGETS.get(region, 1000))
        return budget * CHARS_PER_TOKEN

    fills = {
        "identity": "I" * cap("identity"),
        "safety": safety_content,
        "current_speaker": "K" * cap("current_speaker"),
        "self_model": "M" * cap("self_model"),
        "user_model": "U" * cap("user_model"),
        "user_inferences": "F" * cap("user_inferences"),
        "durable_memory": "D" * cap("durable_memory"),
        "working_memory": "W" * cap("working_memory"),
        "continuity": "C" * cap("continuity"),
        "recalled_memory": "RECALL-SENTINEL " + "R" * 1000,
        "procedural_memory": "PROCEDURAL-SENTINEL " + "P" * 500,
    }
    for region, content in fills.items():
        wm = wm.with_memory(
            Memory(role="system", content=content, region=region, source="vault")
        )
    return wm


def test_safety_survives_win32_truncation_while_tail_regions_drop() -> None:
    budgets = config_module.REGION_BUDGETS
    safety_sentinel = (
        "SAFETY-SENTINEL $25/month hard ceiling. "
        + "S" * (budgets["safety"] * CHARS_PER_TOKEN - 200)
    )
    wm = _filled_wm(budgets, safety_sentinel)

    assembled = assemble_regions(prompt_regions_from_working_memory(wm, budgets))

    # Precondition: truncation pressure is REAL, and the tail sentinels were
    # present before the clamp (so their absence after is a clamp effect).
    assert len(assembled) > WIN32_CAP
    assert "RECALL-SENTINEL" in assembled
    assert "PROCEDURAL-SENTINEL" in assembled

    clamped = truncate_for_win32_argv(assembled, WIN32_CAP)

    assert "SAFETY-SENTINEL $25/month hard ceiling." in clamped
    assert "RECALL-SENTINEL" not in clamped
    assert "PROCEDURAL-SENTINEL" not in clamped
    # Ordering authority proof: safety renders immediately after identity.
    assert clamped.index("# Identity") < clamped.index("# Safety")
    assert clamped.index("# Safety") < clamped.index("# Self Model")


def test_safety_region_order_position() -> None:
    """Cheap structural guard on the ordering authority itself."""
    order = WorkingMemory.region_order
    assert order.index("safety") == order.index("identity") + 1
    for late in ("recalled_memory", "continuity", "procedural_memory"):
        assert order.index("safety") < order.index(late)


def test_apply_process_weights_never_scales_safety() -> None:
    """r2 FIX 2: `safety` is EXEMPT from process weighting, both directions.

    A hard-constraint region must never be down-weighted by mental process —
    spend ceilings don't matter less because the homie is planning — and
    up-weighting a fixed-size rules file only bloats the pre-clamp append.
    Enforced structurally in apply_process_weights, so a FUTURE
    PROCESS_WEIGHTS entry cannot reintroduce the starvation."""
    budgets = {"safety": 700, "durable_memory": 1300}
    down = apply_process_weights(budgets, {"safety": 0.5, "durable_memory": 1.5})
    assert down["safety"] == 700  # pinned — the 0.5 weight is ignored
    assert down["durable_memory"] == 1950  # non-exempt regions still scale
    up = apply_process_weights(budgets, {"safety": 2.0})
    assert up["safety"] == 700  # pinned in the up direction too
    # No live weights table carries a safety entry today (the exemption is
    # belt-and-suspenders, not load-bearing against current tables).
    for process, weights in PROCESS_WEIGHTS.items():
        assert "safety" not in weights, process


# Byte sizes of the live main-vault identity files (measured 2026-08-16).
# Mirrored as fixtures so the render test is deterministic and reproducible
# on any machine — committed tests never read the real sanitizer-denied
# vault, but these sizes reproduce its clamp pressure faithfully.
_REAL_SIZE_MIRRORS = {
    "SOUL.md": 13072,
    "SELF.md": 28187,
    "USER.md": 20151,
    "MEMORY.md": 55332,
    "WORKING.md": 30916,
}

# The engine's real append prefix is GROUNDING_RULES + a ~1.9K method-local
# chat_rules block (engine.py:1536) that is not importable; a same-size
# filler reproduces its clamp-window pressure.
_CHAT_RULES_SIZED_FILLER = "# Chat Interface Rules\n" + "R" * 1900 + "\n\n"


def _sized_file(title: str, size: int) -> str:
    line = f"- {title} filler line for realistic sizing.\n"
    body = f"# {title}\n" + line * (size // len(line) + 2)
    return body[:size]


def _largest_safety_fixture() -> str:
    """A 2473-char authored SAFETY.md mirroring the live main-vault file."""
    tail_marker = "\n- END-OF-SAFETY-RULES marker line.\n"
    body = AUTHORED_SAFETY + "- padding rule line for realistic size.\n" * 60
    assert len(body) >= 2473 - len(tail_marker)
    return body[: 2473 - len(tail_marker)] + tail_marker


@pytest.mark.parametrize(
    "process",
    [MentalProcess.DEFAULT, MentalProcess.PLANNING, MentalProcess.LEARNING],
    ids=lambda p: p.value,
)
def test_safety_render_physical_across_process_modes(
    process: MentalProcess,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2 FIX 2: the REAL invariant, proven physically per process mode.

    Replaces the r1 arithmetic net-zero test, which asserted a dict-literal
    sum and never rendered a prompt — it stayed green while PLANNING mode
    (durable x1.5, safety unweighted) broke the claimed property. The true
    invariant is NOT net-zero-across-modes; it is: with main-vault-sized
    identity files and the real append shape, in EVERY mental process mode
    the safety region renders whole, un-truncated by its budget, inside the
    win32 head-keep window — while the clamp genuinely fires on the tail."""
    memory_dir = tmp_path / "TheHomie" / "Memory"
    memory_dir.mkdir(parents=True)
    for name, size in _REAL_SIZE_MIRRORS.items():
        (memory_dir / name).write_text(
            _sized_file(name[:-3], size), encoding="utf-8"
        )
    safety = _largest_safety_fixture()
    (memory_dir / "SAFETY.md").write_text(safety, encoding="utf-8")

    convo = _make_engine(tmp_path, monkeypatch, memory_dir)
    wm = convo._build_base_working_memory()
    # Real turns carry a skill index and active inferences the fixture engine
    # cannot produce; add cap-sized stand-ins so the mode's clamp pressure
    # matches the live shape.
    wm = wm.with_memory(Memory(
        role="system", content="PROC " + "P" * 2100,
        region="procedural_memory", source="skills/",
    ))
    wm = wm.with_memory(Memory(
        role="system", content="BELIEF " + "F" * 2100,
        region="user_inferences", source="inference-tracker",
    ))

    budgets = apply_process_weights(
        config_module.REGION_BUDGETS, get_process_weights(process)
    )
    assert budgets["safety"] == config_module.REGION_BUDGETS["safety"]

    append = (
        engine_module.GROUNDING_RULES
        + _CHAT_RULES_SIZED_FILLER
        + assemble_regions(prompt_regions_from_working_memory(wm, budgets))
    )
    clamped = truncate_for_win32_argv(append, WIN32_CAP)
    print(f"[safety-render] mode={process.value} pre_clamp={len(append)} chars")

    # The clamp pressure is REAL in this mode (precondition, not the claim).
    assert len(append) > WIN32_CAP
    assert clamped != append
    # (a) safety never trimmed by its budget in any mode...
    assert "tokens over budget for safety]" not in append
    # ...and survives the head-keep clamp WHOLE, down to its last line.
    assert "END-OF-SAFETY-RULES" in clamped
    # (b) the final append respects the win32 clamp.
    assert len(clamped) <= WIN32_CAP + len("\n[TRUNCATED]")


# ===========================================================================
# 6. Overflow is LOUD — warning names persona, byte size, cap; trim is marked
# ===========================================================================


def test_overflow_warns_with_persona_bytes_and_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cap_chars = config_module.REGION_BUDGETS["safety"] * CHARS_PER_TOKEN
    oversized = AUTHORED_SAFETY + "- extra rule line.\n" * (
        (cap_chars - len(AUTHORED_SAFETY)) // 19 + 20
    )
    assert len(oversized) > cap_chars

    memory_dir = tmp_path / "TheHomie" / "Memory"
    _seed_vault(memory_dir, safety=oversized)

    import personas

    monkeypatch.setattr(personas, "get_active_profile_name", lambda: "seo_geo")

    convo = _make_engine(tmp_path, monkeypatch, memory_dir)
    capsys.readouterr()  # drain construction output
    wm = convo._build_base_working_memory()
    out = capsys.readouterr().out

    assert "[Safety]" in out
    assert "seo_geo" in out
    assert str(len(oversized.encode("utf-8"))) in out
    assert str(cap_chars) in out

    # The trim itself is marked in-prompt — never silent.
    rendered = assemble_regions(
        prompt_regions_from_working_memory(wm, config_module.REGION_BUDGETS)
    )
    assert "tokens over budget for safety]" in rendered


def test_no_overflow_warning_when_under_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    _seed_vault(memory_dir, safety=AUTHORED_SAFETY)
    convo = _make_engine(tmp_path, monkeypatch, memory_dir)
    capsys.readouterr()
    convo._build_base_working_memory()
    assert "[Safety]" not in capsys.readouterr().out


# ===========================================================================
# 7. Clamp holds with the largest existing SAFETY.md (2473 bytes, mirrored)
# ===========================================================================


def test_largest_existing_safety_file_fits_budget_and_clamp() -> None:
    """A fixture mirroring the largest on-disk SAFETY.md (the main vault's,
    2473 bytes as of 2026-08-16) fits its budget uncut, and survives WHOLE
    through the win32 clamp even with every other region at its cap."""
    budgets = config_module.REGION_BUDGETS
    cap_chars = budgets["safety"] * CHARS_PER_TOKEN

    largest = _largest_safety_fixture()
    assert len(largest) == 2473
    assert len(largest) <= cap_chars, "budget must fit the largest real file"

    wm = _filled_wm(budgets, largest)
    assembled = assemble_regions(prompt_regions_from_working_memory(wm, budgets))
    assert len(assembled) > WIN32_CAP  # pressure is real
    # No budget trim of the safety region itself.
    assert "tokens over budget for safety]" not in assembled

    clamped = truncate_for_win32_argv(assembled, WIN32_CAP)
    assert len(clamped) <= WIN32_CAP + len("\n[TRUNCATED]")
    # The ENTIRE safety file survives the head-keep clamp.
    assert "END-OF-SAFETY-RULES" in clamped


# ===========================================================================
# 8. Seeder — no SAFETY.md for new profiles; residual template states scope
# ===========================================================================


def test_required_identity_files_no_longer_include_safety() -> None:
    assert "SAFETY.md" not in _REQUIRED_IDENTITY_FILES


def test_create_profile_does_not_seed_safety(empty_homie_root: Path) -> None:
    from personas.lifecycle import create_profile

    create_profile("sales")

    memory = empty_homie_root / "profiles" / "sales" / "memory"
    assert (memory / "SOUL.md").exists()  # creation actually ran
    assert not (memory / "SAFETY.md").exists()


def test_safety_seed_template_states_interactive_scope() -> None:
    body = _seed_identity_body("SAFETY.md", "sales")

    assert "profile: sales" in body
    assert "identity_file: SAFETY.md" in body
    assert "#485" in body
    assert "INTERACTIVE" in body
    assert "scheduled cognition" in body
    # The scope note lives in the HTML comment: an unedited scaffold is a stub.
    assert has_authored_content(body, scaffold_title="SAFETY") is False


def test_generic_seed_template_unchanged_for_other_files() -> None:
    body = _seed_identity_body("SOUL.md", "sales")
    assert "Seeded by `thehomie profile create sales`" in body
    assert "#485" not in body
