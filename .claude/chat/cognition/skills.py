"""Auto-skill generation, index scanning, and self-patching.

Captures repeating tool-call workflows as reusable SKILL.md files.
Provides a skill index for the procedural_memory prompt region
(names + descriptions only — progressive disclosure).

Pattern: capture.py auto_capture_from_turn() — fire-and-forget post-response.
Pattern: promotion.py _batch_distill() — single LLM call for template generation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class SkillSpec:
    """Auto-generated skill specification."""

    name: str
    description: str
    category: str
    version: str = "1.0.0"
    tools_used: list[str] = field(default_factory=list)
    trigger_patterns: list[str] = field(default_factory=list)
    workflow_steps: list[str] = field(default_factory=list)
    source_session: str = ""
    created_at: str = ""


def _parse_skill_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter fields from a SKILL.md file."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _tokenize(name: str) -> frozenset[str]:
    """Split skill name into a set of lowercase tokens."""
    return frozenset(t for t in name.lower().replace("-", " ").split() if t)


def _iter_existing_skills(skills_dir: Path) -> Iterator[str]:
    """Yield names of every existing SKILL.md under skills_dir.

    Walks rglob directly — no cap, no description requirement, no regex
    re-parsing of rendered markdown. Names come from frontmatter `name`
    field; falls back to parent directory name when frontmatter is missing
    or malformed.
    """
    if not skills_dir.exists():
        return
    for skill_md in skills_dir.rglob("SKILL.md"):
        try:
            fm = _parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = fm.get("name") or skill_md.parent.name
        if name:
            yield name


def _has_conflict(spec: SkillSpec, skills_dir: Path) -> bool:
    """True when a proposed skill's token set overlaps an existing skill.

    Uses token-set subset matching: `{quote}` is a subset of
    `{turborater, quote}` → conflict (proposed would shadow existing).
    `{email, inbox}` is NOT a subset of `{email, check}` → no conflict
    (legit skill family, different jobs). Scans every SKILL.md under
    skills_dir — no rendered-index cap that could hide skill #51.
    Prevents the ITC-style collision where an auto-generated skill
    shadows or duplicates a hand-authored one.
    """
    proposed = _tokenize(spec.name)
    if not proposed:
        return False
    for existing_name in _iter_existing_skills(skills_dir):
        existing = _tokenize(existing_name)
        if not existing:
            continue
        if (
            proposed == existing
            or proposed.issubset(existing)
            or existing.issubset(proposed)
        ):
            return True
    return False


def build_skill_index(skills_dir: Path, max_entries: int = 20) -> str:
    """Scan skills/ + skills/generated/ for SKILL.md files.

    Return names + descriptions as formatted text for procedural_memory region.
    CRITICAL: Names and one-line descriptions ONLY — no full body.
    """
    entries: list[tuple[str, str]] = []

    if not skills_dir.exists():
        return ""

    for skill_md in skills_dir.rglob("SKILL.md"):
        try:
            content = skill_md.read_text(encoding="utf-8")
            fm = _parse_skill_frontmatter(content)
            name = fm.get("name", skill_md.parent.name)
            description = fm.get("description", "")
            if name and description:
                entries.append((name, description))
        except Exception:
            continue  # Skip malformed files

    # Sort by name, cap at max_entries
    entries.sort(key=lambda e: e[0])
    entries = entries[:max_entries]

    if not entries:
        return ""

    return "\n".join(f"- **{name}**: {desc}" for name, desc in entries)


async def propose_skill(
    tool_calls: list[str],
    session_summary: str,
    skills_dir: Path,
    cwd: Path,
) -> SkillSpec | None:
    """After 5+ tool calls, propose skill generation via reasoning_step.

    Returns SkillSpec if proposal makes sense, None if not.
    PATTERN: promotion.py _batch_distill() — single LLM call.
    """
    trigger_threshold = 5
    try:
        from config import SKILL_TRIGGER_TOOL_CALLS

        trigger_threshold = SKILL_TRIGGER_TOOL_CALLS
    except ImportError:
        pass

    if len(tool_calls) < trigger_threshold:
        return None

    from cognition.steps import reasoning_step

    result = await reasoning_step(
        context=f"Tools used: {tool_calls}\nSession: {session_summary}",
        instruction=(
            "Propose a reusable skill from this tool sequence. JSON: "
            '{"name": "...", "description": "...", "category": "...", '
            '"trigger_patterns": [...], "workflow_steps": [...]}'
        ),
        output_schema={"type": "object"},
        cwd=cwd,
    )

    if result.parsed and isinstance(result.parsed, dict):
        valid_fields = {f for f in SkillSpec.__dataclass_fields__}
        filtered = {k: v for k, v in result.parsed.items() if k in valid_fields}
        if "name" in filtered and "description" in filtered and "category" in filtered:
            spec = SkillSpec(**filtered)
            spec.tools_used = tool_calls
            spec.source_session = session_summary[:100]
            spec.created_at = datetime.now(UTC).isoformat()
            if _has_conflict(spec, skills_dir):
                try:
                    from cognition.observability import SkillLog, log_skill_event
                except ImportError:
                    pass
                else:
                    try:
                        log_skill_event(SkillLog(
                            action="conflict_skipped",
                            skill_name=spec.name,
                            category=spec.category,
                            tool_count=len(tool_calls),
                        ))
                    except (TypeError, ValueError) as exc:
                        import logging
                        logging.getLogger(__name__).warning(
                            "SkillLog shape drift on conflict_skipped: %s", exc,
                        )
                return None
            return spec
    return None


def write_skill(spec: SkillSpec, skills_dir: Path) -> Path:
    """Write SkillSpec to skills/generated/{category}/{name}/SKILL.md.

    Returns path to written file.
    """
    skill_dir = skills_dir / "generated" / spec.category / spec.name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"

    steps_text = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(spec.workflow_steps))
    tools_text = "\n".join(f"- {tool}" for tool in spec.tools_used)

    content = (
        f"---\n"
        f"name: {spec.name}\n"
        f"description: {spec.description}\n"
        f"version: {spec.version}\n"
        f"category: {spec.category}\n"
        f"tools_used: {json.dumps(spec.tools_used)}\n"
        f"trigger_patterns: {json.dumps(spec.trigger_patterns)}\n"
        f"generated: true\n"
        f"source_session: {spec.source_session}\n"
        f"created_at: {spec.created_at}\n"
        f"---\n\n"
        f"# {spec.name}\n\n"
        f"{spec.description}\n\n"
        f"## Workflow Steps\n\n"
        f"{steps_text}\n\n"
        f"## Tools Required\n\n"
        f"{tools_text}\n"
    )

    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def patch_skill(skill_path: Path, updates: dict[str, str]) -> bool:
    """Update an existing generated skill's frontmatter fields.

    Only patches generated skills (checks 'generated: true' in frontmatter).
    Returns True if patched, False if not a generated skill.
    """
    if not skill_path.exists():
        return False

    content = skill_path.read_text(encoding="utf-8")
    fm = _parse_skill_frontmatter(content)

    if fm.get("generated") != "true":
        return False

    # Update frontmatter fields
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(f"{key}: {value}", content)
        else:
            # Insert before closing ---
            content = content.replace("\n---\n\n", f"\n{key}: {value}\n---\n\n", 1)

    skill_path.write_text(content, encoding="utf-8")
    return True
