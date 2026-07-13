"""User-initiated, source-driven skill authoring — the ``/learn`` command.

Ports Hermes Agent's ``/learn``: turn a source the operator points at — a URL,
a local dir/file, the current conversation, or pasted notes — into a DRAFT
``SKILL.md`` under ``skills/generated/`` without hand-writing it. The draft is
inert; graduation stays manual via ``/skills review`` -> ``promote`` (the
existing default-deny promotion gate). This module adds NO new storage,
security, or promotion path — it feeds the rails that already exist.

Model-agnostic by construction:
  * The ONLY LLM call is ``cognition.steps.reasoning_step`` — routed through the
    runtime lane system (whatever provider ``/model`` selected). No direct
    Anthropic/OpenAI client, no model-native tool-call format.
  * Source gathering uses the framework's own primitives (httpx + trafilatura,
    stdlib file reads, the session transcript) — never a model tool surface.
  So ``/learn`` behaves identically on every lane (claude/opus/codex/gemini/...).

Flow:  parse_source -> gather_source -> distill_to_spec (reasoning_step)
       -> write_skill (generated/, inert) -> seed reuse counter -> scan_skill.

Reuses: ``cognition.skills.write_skill`` / ``ScanResult`` via
``cognition.skill_guard.scan_skill`` / ``cognition.skill_usage.record_recurrence``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep the distill prompt cheap on every lane — cap gathered source text.
MAX_SOURCE_CHARS = 24_000
# Per-file cap when walking a local directory (mirrors skill_guard sizing).
_MAX_FILE_BYTES = 64 * 1024
# Text-ish extensions worth reading when a path points at a directory.
_TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".go", ".rs",
}
_CONVERSATION_HINTS = (
    "this conversation", "this chat", "what we just did", "what i just did",
    "what you just did", "the workflow above", "the steps above", "what we did",
)

VALID_KINDS = ("url", "path", "conversation", "notes")


@dataclass
class LearnSource:
    """A classified ``/learn`` argument."""

    kind: str  # url | path | conversation | notes
    raw: str = ""  # the source descriptor (URL, path, or notes text)
    focus: str = ""  # optional "focus on ..." hint


@dataclass
class LearnResult:
    """Outcome of a ``/learn`` invocation."""

    ok: bool
    message: str = ""
    skill_name: str = ""
    category: str = ""
    verdict: str = ""  # scan verdict: safe | caution | dangerous
    draft_path: str = ""
    source_kind: str = ""
    findings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 1. Parse
# --------------------------------------------------------------------------- #

_FOCUS_FLAG_RE = re.compile(r"--focus\s+(.+)$", re.IGNORECASE | re.DOTALL)
_FOCUS_PHRASE_RE = re.compile(r"\bfocus(?:ing)?\s+on\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _extract_focus(text: str) -> tuple[str, str]:
    """Split a trailing ``--focus ...`` / ``focus on ...`` hint off ``text``.

    Returns ``(remaining_source, focus)``.
    """
    m = _FOCUS_FLAG_RE.search(text)
    if m:
        return text[: m.start()].strip(), m.group(1).strip()
    m = _FOCUS_PHRASE_RE.search(text)
    if m:
        return text[: m.start()].strip(), m.group(1).strip()
    return text.strip(), ""


def parse_source(args: str) -> LearnSource:
    """Classify a ``/learn`` argument into a :class:`LearnSource`.

    Order: extract any focus hint, then classify the remainder as
    url -> path -> conversation -> notes.
    """
    body, focus = _extract_focus((args or "").strip())
    low = body.lower()

    if not body or any(h in low for h in _CONVERSATION_HINTS) or low in {"this", "."}:
        return LearnSource(kind="conversation", raw="", focus=focus)

    first = body.split()[0] if body.split() else ""
    if first.lower().startswith(("http://", "https://")):
        return LearnSource(kind="url", raw=first, focus=focus or _remaining_as_focus(body, first))

    if first.startswith(("/", "~", "./", "../")) or _looks_like_path(first):
        return LearnSource(kind="path", raw=first, focus=focus or _remaining_as_focus(body, first))

    return LearnSource(kind="notes", raw=body, focus=focus)


def _remaining_as_focus(body: str, first: str) -> str:
    """Treat words after a URL/path token as an implicit focus hint."""
    rest = body[len(first):].strip()
    return rest if rest else ""


def _looks_like_path(token: str) -> bool:
    """Heuristic: a bare token that resolves to an existing file/dir."""
    try:
        return Path(token).expanduser().exists()
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 2. Gather (model-agnostic — framework primitives only)
# --------------------------------------------------------------------------- #


async def gather_source(
    src: LearnSource,
    *,
    transcript: str = "",
    cwd: Path | None = None,
) -> str:
    """Return raw source text for ``src``. Fail-soft: returns ``""`` on error."""
    try:
        if src.kind == "conversation":
            return (transcript or "")[:MAX_SOURCE_CHARS]
        if src.kind == "notes":
            return src.raw[:MAX_SOURCE_CHARS]
        if src.kind == "url":
            return (await _fetch_url(src.raw))[:MAX_SOURCE_CHARS]
        if src.kind == "path":
            return _read_path(src.raw, cwd=cwd)[:MAX_SOURCE_CHARS]
    except Exception as exc:  # noqa: BLE001 - never break the turn on gather
        logger.warning("gather_source(%s) failed: %s", src.kind, exc)
    return ""


async def _fetch_url(url: str) -> str:
    """Fetch ``url`` and extract readable text (httpx + trafilatura)."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "YourProduct-learn/1.0"})
        resp.raise_for_status()
        html = resp.text

    try:
        import trafilatura

        extracted = trafilatura.extract(html, include_links=False, include_comments=False)
        if extracted:
            return extracted
    except Exception:  # noqa: BLE001 - trafilatura optional; fall back to raw
        pass
    # Fallback: crude tag strip so we never return raw HTML to the distiller.
    return re.sub(r"<[^>]+>", " ", html)


def _read_path(raw: str, *, cwd: Path | None = None) -> str:
    """Read a local file, or concatenate text-ish files under a directory."""
    p = Path(raw).expanduser()
    if not p.is_absolute() and cwd is not None:
        p = (cwd / p)
    p = p.resolve()
    if p.is_file():
        return _read_text_capped(p)
    if p.is_dir():
        chunks: list[str] = []
        budget = MAX_SOURCE_CHARS
        for child in sorted(p.rglob("*")):
            if budget <= 0:
                break
            if not child.is_file() or child.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            text = _read_text_capped(child)
            if not text:
                continue
            snippet = f"\n\n===== {child.relative_to(p)} =====\n{text}"
            chunks.append(snippet[:budget])
            budget -= len(snippet)
        return "".join(chunks)
    return ""


def _read_text_capped(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return path.read_text(encoding="utf-8", errors="replace")[: _MAX_FILE_BYTES]
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# 3. Distill (the single model-agnostic LLM call)
# --------------------------------------------------------------------------- #

# House authoring standards, ported from Hermes' /learn (see module docstring).
_HOUSE_STANDARDS = (
    "You are authoring a reusable agent SKILL.md from the SOURCE MATERIAL below. "
    "Follow these house standards exactly:\n"
    "1. description: <= 60 characters, states WHAT it does and WHEN to use it.\n"
    "2. name: short, lowercase, hyphen-or-space separated; no slashes or '..'.\n"
    "3. category: one lowercase word (e.g. 'api', 'devops', 'content').\n"
    "4. body: full markdown with house section order — '## Overview' then "
    "'## Steps' (or '## Usage') then '## Notes'. Use imperative voice.\n"
    "5. Reference ONLY real tools/commands. Do NOT invent commands. If a list of "
    "KNOWN COMMANDS is provided, frame steps around those; otherwise stay tool-agnostic.\n"
    "6. Do NOT fabricate anything not grounded in the SOURCE MATERIAL.\n"
)

_SPEC_SCHEMA = {
    "name": "string",
    "description": "string (<=60 chars)",
    "category": "string",
    "tools_used": ["string"],
    "trigger_patterns": ["string"],
    "body": "string (full SKILL.md markdown body)",
}


async def distill_to_spec(
    source_text: str,
    *,
    focus: str = "",
    source_label: str = "",
    known_commands: list[str] | None = None,
    cwd: Path | None = None,
):
    """Distill ``source_text`` into a :class:`cognition.skills.SkillSpec`.

    One ``reasoning_step`` call (TEXT_REASONING, lane-routed). Fail-soft: on any
    error or empty model output, returns a minimal spec built from the source
    label / focus so ``/learn`` always yields an inspectable draft.
    """
    from cognition.skills import SkillSpec
    from cognition.steps import reasoning_step

    cmds = ", ".join(known_commands or []) or "(none provided — stay tool-agnostic)"
    context_parts = [f"SOURCE MATERIAL:\n{source_text or '(empty)'}", f"KNOWN COMMANDS: {cmds}"]
    if focus:
        context_parts.append(f"FOCUS: {focus}")
    if source_label:
        context_parts.append(f"SOURCE LABEL: {source_label}")
    context = "\n\n".join(context_parts)

    try:
        result = await reasoning_step(
            context=context,
            instruction=_HOUSE_STANDARDS,
            output_schema=_SPEC_SCHEMA,
            cwd=cwd,
        )
        data = result.parsed if isinstance(result.parsed, dict) else None
    except Exception as exc:  # noqa: BLE001 - fall back to a minimal draft
        logger.warning("distill_to_spec reasoning_step failed: %s", exc)
        data = None

    if not data or not str(data.get("name", "")).strip():
        return _fallback_spec(focus=focus, source_label=source_label, source_text=source_text)

    name = _slug_value(str(data.get("name", "learned-skill")), default="learned-skill")
    category = _slug_value(str(data.get("category", "general")), default="general")
    description = _clamp_description(str(data.get("description", "")) or name)
    return SkillSpec(
        name=name,
        description=description,
        category=category,
        tools_used=_str_list(data.get("tools_used")),
        trigger_patterns=_str_list(data.get("trigger_patterns")),
        body=str(data.get("body", "")).strip(),
        created_at=datetime.now(UTC).isoformat(),
    )


def _fallback_spec(*, focus: str, source_label: str, source_text: str):
    """Minimal, always-valid spec when distillation yields nothing usable."""
    from cognition.skills import SkillSpec

    label = focus or source_label or "learned skill"
    name = _slug_value(label, default="learned-skill")
    body = (
        f"# {name}\n\n## Overview\n\n{label}\n\n## Notes\n\n"
        "Auto-distillation produced no structured output; review the source and "
        "edit this draft before promoting.\n"
    )
    if source_text:
        body += f"\n## Source Excerpt\n\n{source_text[:1500]}\n"
    return SkillSpec(
        name=name,
        description=_clamp_description(label),
        category="general",
        body=body,
        created_at=datetime.now(UTC).isoformat(),
    )


def _clamp_description(text: str) -> str:
    text = " ".join(text.split())
    return text[:60] if len(text) > 60 else text


def _slug_value(text: str, *, default: str) -> str:
    """Single-line, traversal-safe display value (write_skill slugs the PATH)."""
    cleaned = " ".join(str(text).replace("/", " ").replace("\\", " ").split())
    cleaned = cleaned.replace("..", "").strip()
    return cleaned or default


def _str_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


# --------------------------------------------------------------------------- #
# 4. Orchestrate
# --------------------------------------------------------------------------- #


async def learn_skill(
    args: str,
    *,
    transcript: str = "",
    cwd: Path | None = None,
    skills_dir: Path | None = None,
    known_commands: list[str] | None = None,
    source_session: str = "",
) -> LearnResult:
    """Author a draft skill from a source and stage it for ``/skills`` review.

    Writes an inert draft under ``skills/generated/`` (via ``write_skill``),
    seeds the reuse counter to the promotion threshold (explicit operator
    request substitutes for recurrence evidence), then runs the security scan.
    Fail-soft: returns ``LearnResult(ok=False, ...)`` rather than raising.
    """
    from cognition.skill_guard import scan_skill
    from cognition.skills import write_skill

    src = parse_source(args)
    source_text = await gather_source(src, transcript=transcript, cwd=cwd)

    if not source_text:
        return LearnResult(
            ok=False,
            source_kind=src.kind,
            message=_empty_source_message(src),
        )

    spec = await distill_to_spec(
        source_text,
        focus=src.focus,
        source_label=src.raw or src.kind,
        known_commands=known_commands,
        cwd=cwd,
    )
    if source_session:
        spec.source_session = source_session

    target_dir = skills_dir if skills_dir is not None else _resolve_skills_dir()

    try:
        path = write_skill(spec, target_dir)
    except ValueError as exc:  # path-traversal / YAML-injection guard fired
        logger.warning("write_skill rejected /learn draft: %s", exc)
        return LearnResult(
            ok=False,
            source_kind=src.kind,
            skill_name=spec.name,
            message=f"Refused to write skill draft (guard): {exc}",
        )

    _seed_reuse_eligibility(spec.name, path=str(path), source_session=spec.source_session)

    scan = scan_skill(path)
    findings = [f"{f.severity}:{f.category} {f.description}" for f in scan.findings]
    return LearnResult(
        ok=True,
        source_kind=src.kind,
        skill_name=spec.name,
        category=spec.category,
        verdict=scan.verdict,
        draft_path=str(path),
        findings=findings,
        message=_success_message(spec, scan.verdict, src.kind),
    )


def _resolve_skills_dir() -> Path:
    """Resolve ``.claude/skills`` at call time (mirrors skill_promotion)."""
    try:
        import config

        return Path(config.CLAUDE_DIR) / "skills"
    except Exception:
        return Path(__file__).resolve().parents[2] / "skills"


def _seed_reuse_eligibility(name: str, *, path: str, source_session: str) -> None:
    """Bump the usage counter to the threshold so ``/skills review`` surfaces it.

    A ``/learn`` draft is authored once on explicit operator request; that
    request stands in for the auto-capture recurrence evidence the promotion
    gate normally requires. ``record_recurrence`` flips state to ``eligible``
    once the count reaches the configured threshold; a small cap bounds the loop.
    """
    try:
        from cognition import skill_usage
    except Exception:  # noqa: BLE001 - optional outside the scripts env
        return
    try:
        for _ in range(10):
            usage = skill_usage.record_recurrence(
                name, source_session=source_session, path=path,
            )
            if getattr(usage, "state", "") == "eligible":
                break
    except Exception as exc:  # noqa: BLE001 - never break the turn on telemetry
        logger.warning("seed reuse eligibility for %r failed: %s", name, exc)


def _empty_source_message(src: LearnSource) -> str:
    if src.kind == "conversation":
        return (
            "*Learn* — no conversation history was available to learn from. "
            "Try `/learn <url>`, `/learn <path>`, or paste the procedure as notes."
        )
    if src.kind == "url":
        return f"*Learn* — could not fetch or extract text from {src.raw!r}."
    if src.kind == "path":
        return f"*Learn* — no readable text found at {src.raw!r}."
    return "*Learn* — nothing to learn from. Provide a URL, a path, or notes."


def _success_message(spec, verdict: str, kind: str) -> str:
    icon = {"safe": "✅", "caution": "⚠️", "dangerous": "⛔"}.get(verdict, "•")
    return (
        f"*Learned a skill draft* — *{spec.name}* (category: {spec.category})\n"
        f"  • source: {kind}\n"
        f"  • security scan: {icon} {verdict}\n"
        f"  • staged in `skills/generated/` (inert)\n\n"
        f"Review and promote with `/skills review` then "
        f"`/skills promote {spec.name}`."
    )
