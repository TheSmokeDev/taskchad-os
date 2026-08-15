"""`skill_view` body resolution + scope fence (#429 codex R5 BLOCKER).

A linked skill installs into the persona's OWN skills tree; before this fix the
tool only ever read the central top-level dir, so an installed skill's body was
unreadable — a catalog card, not a book. Resolution now checks the calling
persona's own tree first, then central, then central promoted/ — the last ONLY
inside the same scope fence the index applies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cognition import skill_usage

import config
from runtime import tool_impl


def _write(path: Path, name: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name} desc\n---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def skill_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    central = tmp_path / "central"
    persona_skills = tmp_path / "persona" / "skills"
    _write(central / "hand-authored" / "SKILL.md", "hand-authored", "# hand body")
    _write(central / "promoted" / "sales-only" / "SKILL.md", "sales-only", "# sales body")
    _write(central / "promoted" / "global-tool" / "SKILL.md", "global-tool", "# global body")
    _write(persona_skills / "linked-tool" / "SKILL.md", "linked-tool", "# linked body")

    monkeypatch.setattr(tool_impl, "_central_skills_dir", lambda: central)
    other = tmp_path / "other" / "skills"  # deliberately nonexistent

    def _paths(pid: str) -> dict[str, Path]:
        # Only sales has the linked install; anyone else's tree is empty.
        return {"skills": persona_skills if pid == "sales" else other}

    monkeypatch.setattr("personas.get_persona_paths", _paths)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)

    skill_usage.record_recurrence("sales-only", path="x")
    skill_usage.record_persona_assignment("sales-only", "sales")
    skill_usage.record_recurrence("global-tool", path="x")
    skill_usage.mark_scope_unrestricted("global-tool")
    return {"central": central, "persona": persona_skills}


def test_skill_view_reads_the_personas_own_installed_skill(skill_tree):
    """The R5 failure itself: a linked skill's body, loadable by the persona it
    was linked for. Pre-fix this returned `no skill named` — the persona's own
    tree was never consulted."""
    out = tool_impl._skill_view("linked-tool", _persona_id="sales")
    assert "# linked body" in out


def test_skill_view_reads_a_hand_authored_central_skill(skill_tree):
    assert "# hand body" in tool_impl._skill_view("hand-authored", _persona_id="sales")


def test_skill_view_fences_a_scoped_promoted_body(skill_tree):
    """The tool must not be a quieter fence-bypass than the index: sales reads
    its own scoped skill; marketing and the default reader do not."""
    assert "# sales body" in tool_impl._skill_view("sales-only", _persona_id="sales")
    assert "no skill named" in tool_impl._skill_view("sales-only", _persona_id="marketing")
    assert "no skill named" in tool_impl._skill_view("sales-only")


def test_skill_view_reads_a_globally_promoted_skill(skill_tree):
    assert "# global body" in tool_impl._skill_view("global-tool", _persona_id="marketing")
    assert "# global body" in tool_impl._skill_view("global-tool")


def test_skill_view_rejects_path_traversal(skill_tree):
    """The name comes from the model — it is never a path."""
    out = tool_impl._skill_view("../../../windows/win.ini", _persona_id="sales")
    assert out.startswith("error:")
    assert "flat identifiers" in out


def test_skill_view_through_the_real_dispatcher_carries_persona_identity(skill_tree):
    """#429 codex R6 BLOCKER: the handler accepted ``_persona_id`` but the
    registry entry was never marked ``persona_scoped``, so the dispatcher never
    injected it — the persona-local branch was dead code and the promoted fence
    read every persona caller as ``default``. This drives the REAL
    payload -> dispatch path a persona turn uses, never the handler directly.

    Non-vacuity: pre-fix, ``linked-tool`` returns "no skill named" here (no
    identity injected), and ``sales-only`` is refused to sales ITSELF (the
    fence reads the caller as default).
    """
    from runtime import persona_tools, tool_impl, tool_registry

    saved = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    tool_impl.register_tools()
    try:
        payload = persona_tools.build_persona_tool_payload(
            "sales", {"toolsets": ["safe_core"]}
        )
        assert payload is not None
        _defs, dispatch = payload

        # sales reads its OWN installed skill through the real dispatch path.
        assert "# linked body" in dispatch("skill_view", {"name": "linked-tool"})
        # sales reads the promoted skill scoped to it.
        assert "# sales body" in dispatch("skill_view", {"name": "sales-only"})

        # marketing gets neither through the same path.
        _defs2, dispatch_m = persona_tools.build_persona_tool_payload(
            "marketing", {"toolsets": ["safe_core"]}
        )
        assert "# linked body" not in dispatch_m("skill_view", {"name": "linked-tool"})
        assert "# sales body" not in dispatch_m("skill_view", {"name": "sales-only"})
    finally:
        tool_registry._REGISTRY.clear()
        tool_registry._REGISTRY.update(saved)


def test_skill_view_opens_a_multi_word_skill_by_its_folded_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """#429 codex R7 MAJOR: the display name, the promotion slug, and the
    assignment slug are three foldings of one skill — ``Daily Spend`` indexes
    by frontmatter name, promotes to ``daily-spend``, installs to
    ``Daily-Spend``. The viewer must open the body by the name the INDEX
    advertised, whichever fold is on disk. Non-vacuity: pre-fix, the raw join
    misses both folded dirs and every assertion here reads "no skill named"."""
    central = tmp_path / "central"
    persona_skills = tmp_path / "persona" / "skills"
    # The assignment fold (Daily-Spend) in the persona's own tree…
    _write(
        persona_skills / "Daily-Spend" / "SKILL.md",
        "Daily Spend",
        "# local folded body",
    )
    # …and the promotion fold (daily-spend) in the central promoted tree.
    _write(
        central / "promoted" / "daily-spend" / "SKILL.md",
        "Daily Spend",
        "# promoted folded body",
    )
    monkeypatch.setattr(tool_impl, "_central_skills_dir", lambda: central)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)

    # The scope row keys on the display name, as intake records it.
    skill_usage.record_recurrence("Daily Spend", path="x")
    skill_usage.record_persona_assignment("Daily Spend", "sales")

    monkeypatch.setattr(
        "personas.get_persona_paths", lambda _pid: {"skills": persona_skills}
    )
    assert "# local folded body" in tool_impl._skill_view(
        "Daily Spend", _persona_id="sales"
    )

    # No local install in reach: the promoted fold reads through the fence —
    # sales (in scope) opens it; marketing and the default reader do not.
    monkeypatch.setattr(
        "personas.get_persona_paths",
        lambda _pid: {"skills": tmp_path / "empty" / "skills"},
    )
    assert "# promoted folded body" in tool_impl._skill_view(
        "Daily Spend", _persona_id="sales"
    )
    assert "# promoted folded body" not in tool_impl._skill_view(
        "Daily Spend", _persona_id="marketing"
    )
    assert "# promoted folded body" not in tool_impl._skill_view("Daily Spend")
