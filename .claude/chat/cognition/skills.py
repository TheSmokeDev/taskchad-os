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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Boot-shim: resolve the active persona's paths BEFORE any framework import.
# config imports here are lazy (inside functions), but the shim still runs at
# module top level so a standalone run picks up the right profile.
import sys

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personas import apply_persona_override  # noqa: E402

apply_persona_override()


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
    # Optional pre-authored markdown body (e.g. /learn distillation). When set,
    # write_skill renders it verbatim instead of the auto-capture stub.
    body: str = ""


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


def _log_skill(*, action: str, skill_name: str, category: str, tool_count: int) -> None:
    """Fire-and-forget skill-event log (never raises into the turn)."""
    try:
        from cognition.observability import SkillLog, log_skill_event
    except ImportError:
        return
    try:
        log_skill_event(SkillLog(
            action=action,
            skill_name=skill_name,
            category=category,
            tool_count=tool_count,
        ))
    except (TypeError, ValueError) as exc:
        import logging
        logging.getLogger(__name__).warning(
            "SkillLog shape drift on %s: %s", action, exc,
        )


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


@dataclass
class ConflictMatch:
    """The existing skill a proposal collides with (B2).

    ``name`` is the MATCHED skill's name — recurrence telemetry is keyed on
    THIS, not the new proposal's name. ``is_generated`` is True iff the match
    lives under a ``generated/`` ancestor (path segment is source of truth,
    Rule 2 — NOT the frontmatter ``generated:`` flag).
    """

    name: str
    path: Path
    is_generated: bool


def _path_has_generated_segment(skill_md: Path, skills_dir: Path) -> bool:
    """True iff the SKILL.md path has a ``generated`` segment under skills_dir.

    Mirrors ``build_skill_index`` / ``discover_skills`` (path-segment filter,
    Rule 2). Falls back to scanning the absolute path's parts when the file is
    not relative to ``skills_dir`` (defensive — should not happen in practice).
    """
    try:
        return "generated" in skill_md.relative_to(skills_dir).parts
    except ValueError:
        return "generated" in skill_md.parts


def _find_conflict(spec: SkillSpec, skills_dir: Path) -> ConflictMatch | None:
    """Return the existing skill a proposal collides with, or None (B2).

    Uses token-set subset matching: `{quote}` is a subset of
    `{turborater, quote}` → conflict (proposed would shadow existing).
    `{email, inbox}` is NOT a subset of `{email, check}` → no conflict
    (legit skill family, different jobs). Scans every SKILL.md under
    skills_dir — no rendered-index cap that could hide skill #51.
    Prevents the ITC-style collision where an auto-generated skill
    shadows or duplicates a hand-authored one.

    Returns the FIRST match (name + path + whether it is a generated draft) so
    the caller can record recurrence against the matched draft (B2).
    """
    proposed = _tokenize(spec.name)
    if not proposed:
        return None
    if not skills_dir.exists():
        return None
    for skill_md in skills_dir.rglob("SKILL.md"):
        try:
            fm = _parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError:
            continue
        existing_name = fm.get("name") or skill_md.parent.name
        existing = _tokenize(existing_name)
        if not existing:
            continue
        if (
            proposed == existing
            or proposed.issubset(existing)
            or existing.issubset(proposed)
        ):
            return ConflictMatch(
                name=existing_name,
                path=skill_md,
                is_generated=_path_has_generated_segment(skill_md, skills_dir),
            )
    return None


def _has_conflict(spec: SkillSpec, skills_dir: Path) -> bool:
    """True when a proposed skill's token set overlaps an existing skill.

    Thin back-compat wrapper over ``_find_conflict`` (B2) — the existing 32
    skills tests call this. New code should call ``_find_conflict`` to get the
    matched skill's name/path/is_generated.
    """
    return _find_conflict(spec, skills_dir) is not None


def _normalize_skill_allowlist(allowlist: Iterable[str] | None) -> frozenset[str] | None:
    if allowlist is None:
        return None
    normalized = frozenset(
        item.strip()
        for item in allowlist
        if isinstance(item, str) and item.strip()
    )
    if "*" in normalized:
        return None
    return normalized


def _collect_index_entries(
    skills_dir: Path,
    *,
    allowlist: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []

    if not skills_dir.exists():
        return entries

    for skill_md in skills_dir.rglob("SKILL.md"):
        # Default-deny: skip auto-drafted skills under generated/ — they are
        # unscanned and ungated, so they must not enter the procedural_memory
        # prompt until promoted out of generated/ by the skill rails.
        try:
            if "generated" in skill_md.relative_to(skills_dir).parts:
                continue
        except ValueError:
            pass
        try:
            content = skill_md.read_text(encoding="utf-8")
            fm = _parse_skill_frontmatter(content)
            name = fm.get("name", skill_md.parent.name)
            description = fm.get("description", "")
            if allowlist is not None and name not in allowlist:
                continue
            if name and description:
                entries.append((name, description))
        except Exception:
            continue  # Skip malformed files

    return entries


def build_skill_index(
    skills_dir: Path,
    max_entries: int = 20,
    *,
    allowlist: Iterable[str] | None = None,
    extra_skill_dirs: Iterable[Path] | None = None,
) -> str:
    """Scan skills/ for hand-authored SKILL.md files (procedural_memory region).

    Default-deny: auto-drafted skills under ``generated/`` are EXCLUDED. They are
    unvetted (no security scan, no operator gate) and must not influence behavior
    until the skill-from-experience rails promote them out of ``generated/``.
    Return names + descriptions as formatted text for the procedural_memory region.
    CRITICAL: Names and one-line descriptions ONLY — no full body.
    """
    allowed = _normalize_skill_allowlist(allowlist)
    entries = _collect_index_entries(skills_dir, allowlist=allowed)
    for extra_dir in extra_skill_dirs or []:
        # Profile-local skills are explicitly installed into that persona's
        # brain, so they are surfaced without requiring central allowlist edits.
        entries.extend(_collect_index_entries(extra_dir, allowlist=None))

    if not entries:
        return ""

    # De-duplicate by name, preferring the last entry so profile-local skills
    # can override a central skill description with the same name.
    deduped: dict[str, str] = {}
    for name, description in entries:
        deduped[name] = description
    entries = list(deduped.items())

    # Sort by name, cap at max_entries.
    entries.sort(key=lambda e: e[0])
    entries = entries[:max_entries]

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
            match = _find_conflict(spec, skills_dir)
            if match is not None:
                if match.is_generated:
                    # B2: the proposal re-appeared against an existing GENERATED
                    # draft — count it as a recurrence (the "reuse" signal),
                    # keyed on the MATCHED draft's name, NOT spec.name. Generated
                    # drafts are inert pre-promotion, so this is recurrence, not
                    # invocation. Fire-and-forget — telemetry never breaks a turn.
                    try:
                        from cognition import skill_usage

                        skill_usage.record_recurrence(
                            match.name,
                            source_session=spec.source_session,
                            path=str(match.path),
                        )
                    except Exception as exc:  # noqa: BLE001 - telemetry best-effort
                        import logging
                        logging.getLogger(__name__).warning(
                            "skill recurrence record failed for %s: %s",
                            match.name, exc,
                        )
                    _log_skill(
                        action="reused",
                        skill_name=match.name,
                        category=spec.category,
                        tool_count=len(tool_calls),
                    )
                else:
                    # Hand-authored collision — keep the existing skip (no
                    # recurrence: a hand-authored skill is not a draft to graduate).
                    _log_skill(
                        action="conflict_skipped",
                        skill_name=spec.name,
                        category=spec.category,
                        tool_count=len(tool_calls),
                    )
                return None
            return spec
    return None


def _reject_frontmatter_value(value: str, field_name: str) -> None:
    """Hard-reject newline/control chars in a model-authored frontmatter VALUE.

    F2 (SECURITY — YAML field injection): ``sanitize_skill_path_component`` only
    guards the PATH. The frontmatter VALUES (``name``/``category``/``description``)
    are interpolated straight into the SKILL.md YAML. A newline or other control
    character in one of these could forge extra frontmatter keys (e.g. a
    ``description`` of ``"foo\\ngenerated: false"`` would flip a scan/gate field)
    or otherwise produce malformed/misleading frontmatter BEFORE any scan gate.

    These three fields are all single-line by contract, so the safe, explicit fix
    is to refuse anything carrying ``\\n`` / ``\\r`` or other C0 control chars
    (``\\x00``-``\\x1f``) — NOT to silently strip, which could still smuggle a
    misleading value. NOT fail-open: this raises ``ValueError`` (the engine's
    post-response try/except swallows it for the turn; nothing is written).
    """
    if any(ch in value for ch in "\n\r"):
        raise ValueError(
            f"refusing to write skill: {field_name} contains a newline "
            "(YAML field injection guard)"
        )
    if any(ord(ch) < 0x20 for ch in value):
        raise ValueError(
            f"refusing to write skill: {field_name} contains a control character "
            "(YAML field injection guard)"
        )


def write_skill(spec: SkillSpec, skills_dir: Path) -> Path:
    """Write SkillSpec to skills/generated/{category}/{name}/SKILL.md.

    B4 (SECURITY — path traversal): ``spec.category`` and ``spec.name`` are
    MODEL-authored. They are sanitized via ``sanitize_skill_path_component``
    (HARD-rejects ``..`` / path separators / absolute paths / dotfiles) BEFORE
    they touch the filesystem, and the resolved write dir is asserted to stay
    under ``skills_dir/"generated"``. This is NOT fail-open — a traversal attempt
    raises ``ValueError`` (the engine's post-response try/except swallows it for
    the turn, but the file is never written outside ``generated/``). The
    frontmatter still records the original (display) ``spec.name``/``category``;
    only the PATH components are sanitized.

    F2 (SECURITY — YAML field injection): the model-authored frontmatter VALUES
    (``spec.name``/``spec.category``/``spec.description``) are hard-rejected via
    ``_reject_frontmatter_value`` if they carry a newline or control character,
    so a crafted value cannot forge extra frontmatter keys before the scan gate.

    Returns path to written file.
    """
    from cognition.skill_guard import sanitize_skill_path_component

    # F2: validate the frontmatter VALUES before any path work — these are
    # single-line by contract and must not smuggle YAML structure.
    _reject_frontmatter_value(spec.name, "name")
    _reject_frontmatter_value(spec.category, "category")
    _reject_frontmatter_value(spec.description, "description")

    safe_category = sanitize_skill_path_component(spec.category)
    safe_name = sanitize_skill_path_component(spec.name)
    generated_root = (skills_dir / "generated").resolve()
    skill_dir = generated_root / safe_category / safe_name
    # Defense in depth: even with both components slugged, assert the resolved
    # final dir cannot escape generated/ (Rule 2 — path is the gating source).
    if not skill_dir.resolve().is_relative_to(generated_root):
        raise ValueError(
            f"refusing to write skill outside generated/: {skill_dir!r}"
        )
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"

    steps_text = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(spec.workflow_steps))
    tools_text = "\n".join(f"- {tool}" for tool in spec.tools_used)

    frontmatter = (
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
    )

    # A pre-authored body (e.g. /learn distillation) is rendered verbatim so the
    # SKILL.md follows house section order. Otherwise fall back to the
    # auto-capture stub (Workflow Steps / Tools Required).
    if spec.body.strip():
        body_md = spec.body.strip() + "\n"
    else:
        body_md = (
            f"# {spec.name}\n\n"
            f"{spec.description}\n\n"
            f"## Workflow Steps\n\n"
            f"{steps_text}\n\n"
            f"## Tools Required\n\n"
            f"{tools_text}\n"
        )

    content = frontmatter + body_md

    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def validate_skill(skill_path: Path) -> list[str]:
    """Validate a SKILL.md file for discoverability.

    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []
    if not skill_path.exists():
        errors.append(f"File not found: {skill_path}")
        return errors
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"Cannot read file: {exc}")
        return errors
    size_kb = len(content.encode("utf-8")) / 1024
    if size_kb > 25:
        errors.append(f"File too large: {size_kb:.1f}KB (max 25KB)")
    fm = _parse_skill_frontmatter(content)
    if not fm:
        errors.append("No YAML frontmatter found (expected --- markers)")
    else:
        if not fm.get("name"):
            errors.append("Missing or empty 'name' in frontmatter")
        if not fm.get("description"):
            errors.append("Missing or empty 'description' in frontmatter")
    fm_match = re.match(r"^---\s*\n.*?\n---\s*\n?", content, re.DOTALL)
    body = content[fm_match.end():].strip() if fm_match else content.strip()
    if not body:
        errors.append("Body is empty (no content after frontmatter)")
    return errors


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


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "--validate-skill":
        target = Path(sys.argv[2])
        errs = validate_skill(target)
        if errs:
            print(f"FAIL: {target}")
            for e in errs:
                print(f"  - {e}")
            sys.exit(1)
        else:
            fm = _parse_skill_frontmatter(target.read_text(encoding="utf-8"))
            print(f"OK: {target}")
            print(f"  name: {fm.get('name', '?')}")
            print(f"  description: {fm.get('description', '?')}")
            sys.exit(0)
    else:
        print("Usage: python skills.py --validate-skill <path/to/SKILL.md>")
        sys.exit(2)
