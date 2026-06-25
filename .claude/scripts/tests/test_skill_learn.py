"""Tests for cognition.skill_learn — source-driven /learn authoring.

Mirrors tests/test_cognition_skills.py style (tmp_path, flat-sys.path imports).
The only LLM seam (cognition.steps.reasoning_step) is monkeypatched — no network
and no provider dependency, which also asserts the model-agnostic contract:
distillation goes through reasoning_step and nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognition import skill_learn
from cognition.skills import SkillSpec, write_skill


# --------------------------------------------------------------------------- #
# parse_source
# --------------------------------------------------------------------------- #


def test_parse_source_url():
    src = skill_learn.parse_source("https://docs.example.com/api focus on auth")
    assert src.kind == "url"
    assert src.raw == "https://docs.example.com/api"
    assert "auth" in src.focus


def test_parse_source_conversation():
    for text in ("", "this conversation", "what we just did", "this"):
        assert skill_learn.parse_source(text).kind == "conversation"


def test_parse_source_notes():
    src = skill_learn.parse_source("filing an expense: open portal, attach receipt")
    assert src.kind == "notes"
    assert "expense" in src.raw


def test_parse_source_path(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("hello", encoding="utf-8")
    src = skill_learn.parse_source(f"{f} --focus parsing")
    assert src.kind == "path"
    assert src.focus == "parsing"


# --------------------------------------------------------------------------- #
# gather_source
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gather_notes_and_conversation():
    notes = skill_learn.LearnSource(kind="notes", raw="do X then Y")
    assert await skill_learn.gather_source(notes) == "do X then Y"

    conv = skill_learn.LearnSource(kind="conversation")
    got = await skill_learn.gather_source(conv, transcript="user: hi\nassistant: yo")
    assert "assistant: yo" in got


@pytest.mark.asyncio
async def test_gather_path_file_and_dir(tmp_path):
    (tmp_path / "a.md").write_text("alpha doc", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('beta')", encoding="utf-8")
    (tmp_path / "ignore.bin").write_text("xxxx", encoding="utf-8")

    file_src = skill_learn.LearnSource(kind="path", raw=str(tmp_path / "a.md"))
    assert "alpha doc" in await skill_learn.gather_source(file_src)

    dir_src = skill_learn.LearnSource(kind="path", raw=str(tmp_path))
    dir_text = await skill_learn.gather_source(dir_src)
    assert "alpha doc" in dir_text and "beta" in dir_text


@pytest.mark.asyncio
async def test_gather_size_cap(monkeypatch):
    monkeypatch.setattr(skill_learn, "MAX_SOURCE_CHARS", 10)
    notes = skill_learn.LearnSource(kind="notes", raw="x" * 100)
    assert len(await skill_learn.gather_source(notes)) == 10


# --------------------------------------------------------------------------- #
# distill_to_spec (model-agnostic LLM seam mocked)
# --------------------------------------------------------------------------- #


def _mock_reasoning(parsed):
    async def _fn(context, instruction, output_schema=None, cwd=None):
        return SimpleNamespace(parsed=parsed, output_text="")
    return _fn


@pytest.mark.asyncio
async def test_distill_builds_spec(monkeypatch):
    import cognition.steps as steps

    monkeypatch.setattr(steps, "reasoning_step", _mock_reasoning({
        "name": "acme-auth",
        "description": "x" * 80,  # over-long; must be clamped
        "category": "api",
        "tools_used": ["curl"],
        "trigger_patterns": ["authenticate to acme"],
        "body": "# acme-auth\n\n## Overview\n\nAuth flow.\n",
    }))

    spec = await skill_learn.distill_to_spec("source text", focus="auth")
    assert spec.name == "acme-auth"
    assert spec.category == "api"
    assert len(spec.description) <= 60
    assert spec.body.startswith("# acme-auth")


@pytest.mark.asyncio
async def test_distill_fail_soft(monkeypatch):
    import cognition.steps as steps

    async def _boom(*a, **k):
        raise RuntimeError("lane down")

    monkeypatch.setattr(steps, "reasoning_step", _boom)
    spec = await skill_learn.distill_to_spec("src", focus="deploy staging")
    assert isinstance(spec, SkillSpec)
    assert spec.name  # always yields an inspectable draft
    assert spec.body


# --------------------------------------------------------------------------- #
# learn_skill end-to-end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_learn_skill_writes_draft_and_scans(tmp_path, monkeypatch):
    import config
    import cognition.steps as steps

    monkeypatch.setattr(config, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(steps, "reasoning_step", _mock_reasoning({
        "name": "expense-filing",
        "description": "File an expense in the portal",
        "category": "ops",
        "tools_used": [],
        "trigger_patterns": ["file an expense"],
        "body": "# expense-filing\n\n## Overview\n\nFile it.\n## Steps\n\n1. Open portal.\n",
    }))

    skills_dir = tmp_path / "skills"
    result = await skill_learn.learn_skill(
        "filing an expense: open portal, attach receipt, submit",
        skills_dir=skills_dir,
    )

    assert result.ok
    draft = skills_dir / "generated" / "ops" / "expense-filing" / "SKILL.md"
    assert draft.exists()
    text = draft.read_text(encoding="utf-8")
    assert "generated: true" in text
    assert "## Steps" in text  # authored body rendered, not the stub
    assert result.verdict in ("safe", "caution", "dangerous")

    # Seeded reuse counter makes the draft promotion-eligible.
    from cognition import skill_usage

    usage = skill_usage.get_usage("expense-filing")
    assert usage is not None and usage.state == "eligible"


@pytest.mark.asyncio
async def test_learn_skill_empty_source_is_friendly(tmp_path):
    result = await skill_learn.learn_skill(
        "this conversation", transcript="", skills_dir=tmp_path / "skills",
    )
    assert not result.ok
    assert "conversation" in result.message.lower()


# --------------------------------------------------------------------------- #
# write_skill body back-compat + traversal guard
# --------------------------------------------------------------------------- #


def test_write_skill_renders_authored_body(tmp_path):
    spec = SkillSpec(
        name="with-body", description="d", category="cat",
        body="# with-body\n\n## Overview\n\nbody here\n",
    )
    path = write_skill(spec, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "## Overview" in text and "body here" in text
    assert "## Workflow Steps" not in text  # stub suppressed when body present


def test_write_skill_stub_when_no_body(tmp_path):
    spec = SkillSpec(
        name="no-body", description="d", category="cat",
        workflow_steps=["step one"], tools_used=["toolx"],
    )
    text = write_skill(spec, tmp_path).read_text(encoding="utf-8")
    assert "## Workflow Steps" in text and "## Tools Required" in text


def test_write_skill_rejects_path_traversal(tmp_path):
    spec = SkillSpec(name="ok", description="d", category="../escape")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)
