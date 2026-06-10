"""Design brief assembly — the lane-agnostic prompt.

This is the heart of the native port. It assembles a single self-contained
prompt that makes ANY capable runtime lane (claude_native, codex, gemini)
produce brand-grade, non-AI-slop HTML and write it to an exact path.

CRITICAL (lane-agnostic invariant): the entire brief goes into
``RuntimeRequest.prompt`` — NOT ``system_prompt``. ``runtime.prompt_builder``
now forwards both a string ``system_prompt`` and the ``{"append": ...}`` dict to
the generic CLI lanes, but the brief still belongs in ``prompt`` because it IS
the task content: it stays independent of system_prompt semantics and is the
most robust form across every lane (claude -> codex -> gemini). (Same pattern as
the heartbeat runtime call.)

Method adapted from nexu-io/open-design (Apache-2.0)
``apps/daemon/src/prompts/discovery.ts`` (charter, anti-slop checklist, RULE 3
plan + 5-dimensional critique). Anti-slop hard rules fused from The Homie's own
``de-ai-slop-frontend`` skill (no blue, no em-dashes, single type pairing, one
accent, LLM-tell word ban). Modified from originals; see THIRD-PARTY-NOTICES.md.
"""

from __future__ import annotations

from .directions import DesignDirection, render_direction_spec
from .systems import DesignSystemPackage, render_system_block

# --- LLM-tell vocabulary banned in customer-facing copy (de-ai-slop) ---------
LLM_TELL_WORDS = (
    "leverage", "seamless", "unlock", "innovative", "cutting-edge",
    "revolutionary", "powerful", "synergies", "transform", "paradigm",
    "elevate", "robust", "in today's fast-paced world",
)

_DESIGN_CHARTER = """\
You are an expert designer working with the operator as your manager. You
produce a design ARTIFACT in HTML. HTML is your tool, not your medium: when the
brief is a landing page be a brand designer; a dashboard, be a systems designer;
a deck, be a slide designer; a mobile screen, be an interaction designer. Embody
the specialist before you write any CSS.

Your output is a single, complete, standalone HTML document: inline all CSS in a
`<style>` block, use real system/web-safe font stacks, no external JS unless
pinned with SRI, no build step. It must open correctly by double-clicking the
file."""

_ANTI_SLOP_RULES = """\
## Anti-AI-slop hard rules (non-negotiable — audit before you finish)

These are what separate "designed" from "obviously AI-generated". Violating any
is a failure:

- NO blue as the accent unless the chosen brand system explicitly specifies it.
  Every generic AI SaaS reaches for cobalt/indigo; do not. (A brand system's
  own palette overrides this — follow the brand.)
- NO em-dashes in any customer-facing copy. Use commas, periods, or colons.
- NO LLM-tell words: %s. Write plain, specific, operator-grade copy instead.
- ONE typography pairing for the whole artifact (a display face + a quieter body
  face). Never let display and body be the same family (the tech/utility
  direction is the only exception — it is deliberately one family).
- ONE hot accent, used at most twice per screen (hero CTA, key figure). Not a
  gradient on every section.
- NO generic emoji feature icons (no rocket/sparkle/target), NO hand-drawn SVG
  humans/faces, NO icon next to every heading, NO rounded card with a coloured
  left-border accent, NO purple/violet gradient hero blob.
- NO invented metrics ("10x faster", "99.9%%") without a real source. When you
  lack a real value, leave an honest placeholder (a dash, a labelled grey block)
  — an honest placeholder beats a fake stat.
- NO filler copy ("Feature One / Feature Two", lorem ipsum). Every word, number,
  and section must be specific to THIS brief.
- ONE decisive flourish (one orchestrated entrance, one striking pull-quote, one
  real piece of photography). Three competing flourishes turn it back into noise.""" % (
    ", ".join(LLM_TELL_WORDS),
)

_CRITIQUE_RUBRIC = """\
## Self-critique before you finish (5-dimensional radar — score each 1-5)

After the artifact is written, silently score yourself. Any dimension under 3/5
is a regression: fix the weakest, re-score, then finish. Two passes is normal.

1. Philosophy — does the visual posture match the brief (editorial vs minimal vs
   brutalist vs utility), or did it drift back to a generic AI default?
2. Hierarchy — does the eye land in ONE obvious place per screen, or is
   everything competing?
3. Execution — typography, spacing, alignment, contrast: right, or just close?
4. Specificity — is every word/number/image specific to THIS brief? No filler,
   no generic stat-slop.
5. Restraint — one accent used at most twice, one decisive flourish — not three."""


def _brand_contract_block(
    *,
    direction: DesignDirection | None,
    system: DesignSystemPackage | None,
    accent_override: str | None,
    brand_locked: bool,
) -> str:
    """Render the active visual direction — a chosen full system package, or a
    picked direction. The brand contract wins over the no-blue default."""
    if system is not None:
        # Full-package binding (B1): USAGE -> DESIGN.md -> tokens.css verbatim ->
        # components summary, in Open Design's exact precedence.
        return render_system_block(system)

    if direction is None:
        return (
            "## Visual direction: pick one yourself\n\n"
            "No brand system was supplied. Choose a coherent non-blue palette that "
            "fits the brief and bind it into `:root`."
        )

    spec = render_direction_spec(direction, accent_override=accent_override)
    note = ""
    if not brand_locked and direction.is_blue_accent:
        # blue-accent default vs the house no-blue rule: when the direction was
        # auto-picked (not operator-chosen), substitute non-blue.
        note = (
            "\n\n**House no-blue override:** this direction defaults to a cobalt "
            "accent, but it was auto-selected (no brand supplied). Substitute a "
            "non-blue accent that suits the brief (keep the rest of the palette)."
        )
    return "## Visual direction (auto-selected — bind verbatim)\n\n" + spec + note


def build_design_brief(
    *,
    kind: str,
    brief_text: str,
    finalized_path: str,
    out_dir: str,
    direction: DesignDirection | None = None,
    system: DesignSystemPackage | None = None,
    accent_override: str | None = None,
    brand_locked: bool = False,
) -> str:
    """Assemble the full, self-contained, lane-agnostic design prompt.

    Args:
        kind: artifact kind (e.g. "html", "landing", "dashboard", "deck").
        brief_text: the operator's design brief.
        finalized_path: absolute path the agent MUST write the final HTML to.
        out_dir: the artifact working directory (absolute).
        direction: chosen/auto-picked DesignDirection (when no system).
        system: a loaded full DesignSystemPackage (the brand contract) — when set,
            its USAGE/DESIGN.md/tokens.css/components are injected in OD precedence.
        accent_override: free-text accent override request, passed to the agent.
        brand_locked: True when the operator explicitly chose the direction/system
            (suppresses the auto-pick no-blue substitution note).

    Returns:
        A single prompt string for ``RuntimeRequest.prompt``.
    """
    contract = _brand_contract_block(
        direction=direction,
        system=system,
        accent_override=accent_override,
        brand_locked=brand_locked,
    )

    return f"""# Design task: build a {kind} artifact

{_DESIGN_CHARTER}

## The brief

{brief_text.strip()}

{contract}

{_ANTI_SLOP_RULES}

## Work order

1. Plan the sections/screens/slides for this brief (state the list before writing).
2. Bind the palette + font stacks above into a `:root` block.
3. Write the COMPLETE standalone HTML document. Replace every placeholder with
   real, specific copy from the brief; leave honest placeholders where you lack a
   real value.
4. Run the anti-AI-slop audit and the 5-dimensional self-critique below. Fix any
   failing dimension, then finish.

{_CRITIQUE_RUBRIC}

## Output contract (exact)

- Working directory: `{out_dir}`
- Write the final artifact to EXACTLY this absolute path (create parent dirs if
  needed): `{finalized_path}`
- The file must be a complete `<!doctype html> ... </html>` document with all CSS
  inlined. Do not write any other canonical HTML file.

## When done, report (plain text, no markdown artifact wrapper)

- The absolute path you wrote.
- Typography pairing chosen + the accent color (as an OKLch or hex value).
- The brand system or direction used.
- AI-slop patterns you actively avoided.
- Self-grade: X/10, and the one thing another pass would improve.
- Any TODOs for the operator (real photos, real numbers, copy that needs a human).
"""
