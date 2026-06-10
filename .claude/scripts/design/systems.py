"""Bundled DESIGN.md brand-system loader (B1: full-package binding).

Brand systems are a TRACKED framework asset at ``.claude/scripts/design/_systems/<slug>/``
(beside this module, so they ship with the repo — a fresh clone can run
``/design system <slug>`` with no setup). Each is a full Open Design package —
``DESIGN.md`` (prose) + ``tokens.css`` (compiled CSS custom properties, pasted
verbatim) + ``components.html`` / ``components.manifest.json`` (real component
shapes) + ``USAGE.md`` + ``manifest.json``. The DESIGN.md systems
are MIT (upstream ``VoltAgent/awesome-design-md``, redistributed by
nexu-io/open-design); the prompt method is Apache-2.0. See THIRD-PARTY-NOTICES.md.

A chosen system is the brand contract. ``render_system_block`` composes it into
the prompt in Open Design's exact precedence (verified against
``apps/daemon/src/prompts/system.ts::composeSystemPrompt``):

    USAGE.md  ->  DESIGN.md (prose)  ->  tokens.css (paste-verbatim, fenced)
              ->  components summary (from components.manifest.json)

The brand's own palette overrides the generic direction defaults, including
de-ai-slop's "no blue" rule (no-blue governs the pick-a-direction fallback,
never an explicit brand).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .artifacts import systems_root

# Files that make up a system package. DESIGN.md + tokens.css are the contract
# (a system missing either is unusable). The rest are optional / fail-open.
_REQUIRED = ("DESIGN.md", "tokens.css")


@dataclass(frozen=True, slots=True)
class DesignSystemPackage:
    """A loaded full system package. Optional files are None when absent."""

    slug: str
    design_md: str
    tokens_css: str
    usage_md: str | None = None
    components_manifest: str | None = None  # raw components.manifest.json text
    components_html: str | None = None
    manifest: dict | None = None  # parsed manifest.json
    category: str | None = None


def list_systems(systems_dir: Path | None = None) -> list[str]:
    """Return available brand-system slugs (sorted), or [] if none bundled."""
    root = systems_root(systems_dir)
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / "DESIGN.md").is_file()
    )


def _read_within(base_resolved: Path, path: Path) -> str | None:
    """Read a file ONLY if it resolves under ``base_resolved`` (Codex HIGH:
    symlink-escape safe — an individual file symlink inside the package dir
    cannot point outside it and leak arbitrary file content into the prompt)."""
    try:
        rp = path.resolve()
        rp.relative_to(base_resolved)
    except (OSError, ValueError):
        return None
    try:
        return rp.read_text(encoding="utf-8")
    except OSError:
        return None


def load_system(name: str, systems_dir: Path | None = None) -> DesignSystemPackage | None:
    """Load a full system package by slug. Returns None if absent/unusable.

    Path-traversal safe (Codex HIGH): the slug must be a bare directory name
    (no separators, no ``..``) AND resolve under ``systems_root``. A missing
    ``DESIGN.md`` OR ``tokens.css`` (the binding contract) returns None; other
    files fail-open to None fields.
    """
    slug = (name or "").strip().lower()
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        return None
    root = systems_root(systems_dir)
    sys_dir = root / slug
    try:
        resolved = sys_dir.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None

    # Each file must resolve under the package dir (symlink-escape safe).
    design_md = _read_within(resolved, resolved / "DESIGN.md")
    tokens_css = _read_within(resolved, resolved / "tokens.css")
    if not design_md or not tokens_css:
        return None  # contract files absent → fall back to a direction

    manifest_raw = _read_within(resolved, resolved / "manifest.json")
    manifest: dict | None = None
    category: str | None = None
    if manifest_raw:
        try:
            manifest = json.loads(manifest_raw)
            category = manifest.get("category") if isinstance(manifest, dict) else None
        except (ValueError, AttributeError):
            manifest = None

    return DesignSystemPackage(
        slug=slug,
        design_md=design_md,
        tokens_css=tokens_css,
        usage_md=_read_within(resolved, resolved / "USAGE.md"),
        components_manifest=_read_within(resolved, resolved / "components.manifest.json"),
        components_html=_read_within(resolved, resolved / "components.html"),
        manifest=manifest,
        category=category,
    )


def summarize_components_manifest(raw: str | None, *, max_groups: int = 12) -> str:
    """Compact text summary of a components.manifest.json (groups → classes).

    Cheaper than injecting the full components.html. Fail-open to "" so a
    missing/malformed manifest never breaks brief assembly.
    """
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            return ""
    except (ValueError, AttributeError):
        return ""

    lines: list[str] = []
    for group in groups:
        if len(lines) >= max_groups:  # cap AFTER filtering (Codex MEDIUM)
            break
        if not isinstance(group, dict) or not group.get("present", True):
            continue
        label = group.get("label") or group.get("id") or "component"
        classes = [c for c in (group.get("classes") or []) if isinstance(c, str)][:8]
        selectors = [s for s in (group.get("selectors") or []) if isinstance(s, str)][:6]
        elements = [e for e in (group.get("elements") or []) if isinstance(e, str)][:6]
        bits: list[str] = []
        if classes:
            bits.append("classes " + ", ".join(f".{c}" for c in classes))
        elif selectors:  # element-only / selector-only groups still surface
            bits.append("selectors " + ", ".join(selectors))
        if elements:
            bits.append("elements " + ", ".join(elements))
        if not bits:
            continue
        lines.append(f"- {label}: " + " · ".join(bits))
    return "\n".join(lines)


def render_system_block(pkg: DesignSystemPackage) -> str:
    """Compose the active-design-system prompt block in OD's exact precedence.

    USAGE -> DESIGN.md -> tokens.css (paste-verbatim, fenced, binding) ->
    components summary (manifest) or a trimmed components.html fallback.
    """
    title = pkg.slug
    parts: list[str] = []

    if pkg.usage_md and pkg.usage_md.strip():
        parts.append(f"## How to use this design system — {title}\n\n{pkg.usage_md.strip()}")

    parts.append(
        f"## Active design system — {title} (THE brand contract)\n\n"
        "Treat the following DESIGN.md as authoritative for color, typography, "
        "spacing, and component rules. Follow it exactly, INCLUDING its accent "
        "color even if it is blue/purple (an explicit brand overrides the house "
        "no-blue rule). Do not invent tokens outside this system.\n\n"
        + pkg.design_md.strip()
    )

    # tokens.css is the binding contract — inject byte-for-byte (no .strip();
    # Codex HIGH). Only add a fence-closing newline if the content lacks one.
    tokens = pkg.tokens_css
    tokens_fenced = "```css\n" + tokens + ("" if tokens.endswith("\n") else "\n") + "```"
    parts.append(
        f"## Active design system tokens — {title}\n\n"
        "Paste the `:root { ... }` block below VERBATIM into the artifact's first "
        "`<style>`. Do not invent new tokens. Do not redefine these values. Do not "
        "write raw hex outside this `:root` block. The DESIGN.md above is prose; "
        "THIS is the binding contract — every color/size references `var(--*)`.\n\n"
        + tokens_fenced
    )

    summary = summarize_components_manifest(pkg.components_manifest)
    if summary:
        parts.append(
            f"## Reference component inventory — {title}\n\n"
            "Compose from these component shapes (class names already wired to the "
            "tokens above); match their structure, do not redraw from scratch:\n\n"
            + summary
        )
    elif pkg.components_html and pkg.components_html.strip():
        trimmed = pkg.components_html.strip()[:8000]
        parts.append(
            f"## Reference component fixture — {title}\n\n"
            "Copy component fragments from this fixture; keep every `var(--*)` "
            "reference intact:\n\n```html\n" + trimmed + "\n```"
        )

    return "\n\n".join(parts)
