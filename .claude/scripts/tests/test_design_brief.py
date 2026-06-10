"""Tests for the native /design capability (Open Design port, no daemon).

Pure-logic coverage — no LLM, no runtime. Exercises direction selection, brief
assembly (incl. the lane-agnostic + no-blue invariants), artifact paths, and
brand-system loading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from design import (
    DesignSystemPackage,
    artifact_dir,
    build_design_brief,
    design_root,
    list_systems,
    load_system,
    pick_direction,
    render_system_block,
    slugify,
    summarize_components_manifest,
)
from design.brief import LLM_TELL_WORDS
from design.directions import find_direction, render_direction_spec


def _demo_pkg(slug: str = "brandx", accent: str = "#533afd") -> DesignSystemPackage:
    return DesignSystemPackage(
        slug=slug,
        design_md=f"# {slug}\nUse the brand palette.\n",
        tokens_css=":root {\n  --bg:#ffffff;\n  --accent:" + accent + ";\n}",
        usage_md="Read DESIGN.md, paste tokens.css verbatim.",
        components_manifest=(
            '{"groups":[{"id":"buttons","label":"Buttons","present":true,'
            '"classes":["btn","btn-primary"]}]}'
        ),
    )


# --- direction selection -----------------------------------------------------

def test_pick_direction_by_tone():
    assert pick_direction("editorial / magazine").id == "editorial-monocle"
    assert pick_direction("dashboard tool UI").id == "tech-utility"
    assert pick_direction("brutalist agency manifesto").id == "brutalist-experimental"
    assert pick_direction("friendly consumer marketplace").id == "human-approachable"


def test_pick_direction_default_is_modern_minimal():
    # Empty / unmatched tone falls back to the safe software-native default.
    assert pick_direction("").id == "modern-minimal"
    assert pick_direction("something with no tone keyword xyz").id == "modern-minimal"


def test_find_direction_by_id_and_label():
    assert find_direction("tech-utility").id == "tech-utility"
    assert find_direction("Editorial — Monocle / FT magazine").id == "editorial-monocle"
    assert find_direction("nope") is None


def test_render_direction_spec_has_root_and_posture():
    spec = render_direction_spec(find_direction("modern-minimal"))
    assert ":root {" in spec
    assert "--accent:" in spec
    assert "Posture" in spec


# --- brief assembly: the lane-agnostic + anti-slop invariants ----------------

def _direction_brief(**kw):
    return build_design_brief(
        kind="html",
        brief_text="fintech dashboard for SMB owners, dark, dense KPIs",
        finalized_path="/abs/out/finalized.html",
        out_dir="/abs/out",
        direction=find_direction("tech-utility"),
        **kw,
    )


def test_brief_is_a_plain_string_with_the_exact_output_path():
    """Lane-agnostic invariant: the whole brief is one string (goes in prompt),
    and the exact write path is embedded."""
    brief = _direction_brief()
    assert isinstance(brief, str)
    assert "/abs/out/finalized.html" in brief
    assert "Output contract" in brief


def test_brief_carries_charter_brief_text_and_critique():
    brief = _direction_brief()
    assert "HTML is your tool" in brief
    assert "fintech dashboard for SMB owners" in brief
    assert "5-dimensional" in brief
    assert "Philosophy" in brief and "Restraint" in brief


def test_brief_embeds_anti_slop_rules():
    brief = _direction_brief()
    assert "NO blue as the accent" in brief
    assert "NO em-dashes" in brief
    # Every banned word is present in the LLM-tell ban line.
    for word in LLM_TELL_WORDS:
        assert word in brief


def test_no_blue_override_note_only_when_autopicked():
    # modern-minimal defaults to cobalt (hue 255). Auto-picked (not brand_locked)
    # => the no-blue substitution note appears.
    auto = build_design_brief(
        kind="html", brief_text="b", finalized_path="/o/f.html", out_dir="/o",
        direction=find_direction("modern-minimal"), brand_locked=False,
    )
    assert "House no-blue override" in auto
    # Operator explicitly chose it => brand contract wins, no override note.
    locked = build_design_brief(
        kind="html", brief_text="b", finalized_path="/o/f.html", out_dir="/o",
        direction=find_direction("modern-minimal"), brand_locked=True,
    )
    assert "House no-blue override" not in locked


def test_brief_with_system_uses_full_package_block():
    pkg = _demo_pkg("stripe", "#533afd")
    brief = build_design_brief(
        kind="html", brief_text="pricing page", finalized_path="/o/f.html", out_dir="/o",
        system=pkg, brand_locked=True,
    )
    assert "Active design system — stripe" in brief
    assert "VERBATIM" in brief                    # tokens paste instruction
    assert "--accent:#533afd" in brief            # tokens.css pasted verbatim (not paraphrased)
    assert "How to use this design system" in brief   # USAGE injected first
    assert ".btn-primary" in brief                # components summary present
    # An explicit brand keeps its (blue/purple) accent and suppresses the no-blue note.
    assert "House no-blue override" not in brief


def test_render_system_block_precedence_and_verbatim_tokens():
    block = render_system_block(_demo_pkg("acme", "#ff3300"))
    i_usage = block.index("How to use this design system")
    i_design = block.index("Active design system — acme")
    i_tokens = block.index("Active design system tokens")
    i_comp = block.index("Reference component inventory")
    # OD precedence: USAGE -> DESIGN.md -> tokens.css -> components
    assert i_usage < i_design < i_tokens < i_comp
    assert "--accent:#ff3300" in block


def test_summarize_components_manifest():
    raw = (
        '{"groups":[{"id":"buttons","label":"Buttons","present":true,'
        '"classes":["btn","btn-primary"]},'
        '{"id":"hidden","present":false,"classes":["z"]}]}'
    )
    s = summarize_components_manifest(raw)
    assert "Buttons" in s and ".btn-primary" in s
    assert ".z" not in s  # not present → skipped
    assert summarize_components_manifest(None) == ""
    assert summarize_components_manifest("not json") == ""


# --- artifacts + systems ------------------------------------------------------

def test_slugify():
    assert slugify("Fintech Dashboard for SMB Owners!") == "fintech-dashboard-for-smb-owners"
    assert slugify("") == "design"
    assert len(slugify("x" * 200)) <= 48


def test_artifact_dir_shape(tmp_path: Path):
    d = artifact_dir("my-slug", "html", date_str="20260608", memory_dir=tmp_path)
    assert d == tmp_path / "design" / "my-slug" / "html-20260608"


def test_design_root(tmp_path: Path):
    assert design_root(tmp_path) == tmp_path / "design"


def test_load_and_list_systems(tmp_path: Path):
    # tmp_path acts as the systems-library root (systems_dir override).
    sys_dir = tmp_path / "demo"
    sys_dir.mkdir(parents=True)
    (sys_dir / "DESIGN.md").write_text("# Demo system\n", encoding="utf-8")
    (sys_dir / "tokens.css").write_text(":root { --accent:#0a0; }", encoding="utf-8")
    assert list_systems(tmp_path) == ["demo"]
    pkg = load_system("demo", tmp_path)
    assert pkg is not None
    assert "Demo system" in pkg.design_md
    assert "--accent:#0a0" in pkg.tokens_css
    # DESIGN.md present but tokens.css missing → unusable contract → None.
    noco = tmp_path / "noco"
    noco.mkdir()
    (noco / "DESIGN.md").write_text("# x\n", encoding="utf-8")
    assert load_system("noco", tmp_path) is None
    assert load_system("missing", tmp_path) is None
    assert load_system("", tmp_path) is None


def test_system_diversity_distinct_tokens(tmp_path: Path):
    # The diversity engine: different systems → mutually distinct injected blocks,
    # each binding its own tokens.css verbatim.
    for slug, accent in [("s1", "#533afd"), ("s2", "#ff3300"), ("s3", "#00aa88")]:
        d = tmp_path / slug
        d.mkdir(parents=True)
        (d / "DESIGN.md").write_text(f"# {slug}\n", encoding="utf-8")
        (d / "tokens.css").write_text(f":root {{ --accent:{accent}; }}", encoding="utf-8")
    blocks = [render_system_block(load_system(s, tmp_path)) for s in ("s1", "s2", "s3")]
    assert "--accent:#533afd" in blocks[0]
    assert "--accent:#ff3300" in blocks[1]
    assert "--accent:#00aa88" in blocks[2]
    assert blocks[0] != blocks[1] and blocks[1] != blocks[2]


# --- Codex bucket-A regression coverage -------------------------------------

def test_pick_direction_tech_is_tech_utility():
    # Codex HIGH: `--tone tech` must resolve to tech-utility, not modern-minimal.
    assert pick_direction("tech").id == "tech-utility"


def test_load_system_rejects_traversal(tmp_path: Path):
    # Codex HIGH: slug must not escape _systems via .. or path separators.
    assert load_system("../../etc/passwd", tmp_path) is None
    assert load_system("..", tmp_path) is None
    assert load_system("a/b", tmp_path) is None
    assert load_system("a\\b", tmp_path) is None


def _import_core_handlers():
    import sys as _sys

    chat = Path(__file__).resolve().parent.parent.parent / "chat"
    if str(chat) not in _sys.path:
        _sys.path.insert(0, str(chat))
    import core_handlers

    return core_handlers


def test_parse_design_flags_windows_safe_and_flag_value_guard():
    ch = _import_core_handlers()
    # Codex MEDIUM: a Windows path in the brief must survive (no shlex mangling).
    brief, opts = ch._parse_design_flags(r"page using C:\Users\YourUser\foo --tone tech")
    assert r"C:\Users\YourUser\foo" in brief
    assert opts["tone"] == "tech"
    # Codex MEDIUM: a flag whose value is another flag is dropped, not consumed.
    _b2, opts2 = ch._parse_design_flags("--accent --tone editorial")
    assert "accent" not in opts2
    assert opts2["tone"] == "editorial"
    # quoted multi-word accent value
    _b3, opts3 = ch._parse_design_flags('landing --accent "moss green"')
    assert opts3["accent"] == "moss green"


@pytest.mark.asyncio
async def test_handle_design_lane_agnostic_and_contained(tmp_path: Path, monkeypatch):
    ch = _import_core_handlers()
    import config
    import runtime.lane_router as lr
    from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult

    # Redirect the vault to tmp so the test never writes the real vault.
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path, raising=False)

    captured: dict = {}

    async def fake_run(req):
        captured["req"] = req
        import re as _re

        m = _re.search(r"absolute path[^`]*`([^`]+)`", req.prompt)
        p = Path(m.group(1))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<!doctype html><html></html>", encoding="utf-8")
        return RuntimeResult(
            text="ok 8/10", runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude", model="opus", cost_usd=0.01,
        )

    monkeypatch.setattr(lr, "run_with_runtime_lanes", fake_run)

    out = await ch.handle_design(None, None, 'html "fintech dashboard, dark"')
    req = captured["req"]
    # Lane-agnostic invariant: full brief in prompt, system_prompt stays None.
    assert req.system_prompt is None
    assert req.capability == "tool_reasoning"
    assert "fintech dashboard, dark" in req.prompt
    # Containment: no shell, cwd is the artifact dir under the (tmp) vault.
    assert "Bash" not in req.allowed_tools
    assert "Write" in req.allowed_tools
    assert Path(req.cwd).is_relative_to(tmp_path / "design")
    assert "Design ready" in out


@pytest.mark.asyncio
async def test_handle_design_collect_only_does_not_generate(monkeypatch):
    ch = _import_core_handlers()
    import runtime.lane_router as lr

    async def boom(req):
        raise AssertionError("collect_only must not invoke the runtime")

    monkeypatch.setattr(lr, "run_with_runtime_lanes", boom)
    out = await ch.handle_design(None, None, 'html "x"', collect_only=True)
    assert "usage" in out.lower()


@pytest.mark.asyncio
async def test_handle_design_system_path_threads_real_package(tmp_path: Path, monkeypatch):
    ch = _import_core_handlers()
    import config
    import design.systems as dsys
    import runtime.lane_router as lr
    from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult

    # Artifacts → tmp vault; systems library → a separate tmp root (override).
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path, raising=False)
    sysroot = tmp_path / "_systems_lib"
    acme = sysroot / "acme"
    acme.mkdir(parents=True)
    (acme / "DESIGN.md").write_text("# Acme\nbrand prose\n", encoding="utf-8")
    (acme / "tokens.css").write_text(":root { --accent:#abc123; }", encoding="utf-8")
    monkeypatch.setattr(dsys, "systems_root", lambda systems_dir=None: sysroot)

    captured: dict = {}

    async def fake_run(req):
        captured["req"] = req
        import re as _re

        m = _re.search(r"absolute path[^`]*`([^`]+)`", req.prompt)
        p = Path(m.group(1))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<!doctype html><html></html>", encoding="utf-8")
        return RuntimeResult(
            text="ok", runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude", model="opus", cost_usd=0.01,
        )

    monkeypatch.setattr(lr, "run_with_runtime_lanes", fake_run)

    # `system <slug>` subcommand path
    out = await ch.handle_design(None, None, 'system acme "a pricing page"')
    req = captured["req"]
    assert req.system_prompt is None                    # lane-agnostic preserved
    assert "Active design system — acme" in req.prompt  # package block injected
    assert "--accent:#abc123" in req.prompt             # real tokens bound verbatim
    assert "a pricing page" in req.prompt
    assert "system `acme`" in out                       # used line via system.slug

    # `--system` flag path resolves the same package
    captured.clear()
    out2 = await ch.handle_design(None, None, 'html "x" --system acme')
    assert "Active design system — acme" in captured["req"].prompt
    assert "system `acme`" in out2

    # unknown system → friendly error, no generation
    captured.clear()
    err = await ch.handle_design(None, None, 'system nope "x"')
    assert "No brand system named" in err
    assert "req" not in captured
