"""Built-in design direction library.

Adapted from nexu-io/open-design (Apache-2.0),
``apps/daemon/src/prompts/directions.ts`` — the 5-school direction library.
Adapted to Python data (palettes in OKLch + font stacks are source-equivalent;
posture/label text is lightly edited and ``tones``/``is_blue_accent`` are Homie
additions). Dropped the TS form-rendering helpers; kept the prompt-spec renderer.

When no brand system is chosen, ``pick_direction`` selects one of these five
deterministic directions by tone and ``render_direction_spec`` emits the
CSS ``:root`` block + posture cues for the agent to bind verbatim — one choice,
a deterministic palette + type stack, zero model improvisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DesignDirection:
    """A single visual direction: palette (OKLch), font stacks, layout posture."""

    id: str
    label: str
    mood: str
    references: tuple[str, ...]
    display_font: str
    body_font: str
    # OKLch palette — bind directly into the seed `:root`.
    bg: str
    surface: str
    fg: str
    muted: str
    border: str
    accent: str
    posture: tuple[str, ...]
    mono_font: str | None = None
    # Tone keywords this direction satisfies (matched by pick_direction).
    tones: tuple[str, ...] = field(default_factory=tuple)
    # True when the default accent is blue/cobalt — gates the de-ai-slop
    # no-blue substitution note when a direction is auto-picked (not operator-chosen).
    is_blue_accent: bool = False


DESIGN_DIRECTIONS: tuple[DesignDirection, ...] = (
    DesignDirection(
        id="editorial-monocle",
        label="Editorial — Monocle / FT magazine",
        mood=(
            "Print-magazine feel for explicitly editorial or publishing briefs. "
            "Generous whitespace, large serif headlines, restrained palette of "
            "neutral paper + ink + a single brand-justified accent. Not for "
            "commerce, SaaS, dashboards, or product utilities."
        ),
        references=("Monocle", "The Financial Times Weekend", "NYT Magazine", "It's Nice That"),
        display_font="'Iowan Old Style', 'Charter', Georgia, serif",
        body_font="-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
        bg="oklch(98% 0.004 95)",
        surface="oklch(100% 0.002 95)",
        fg="oklch(20% 0.018 70)",
        muted="oklch(48% 0.012 70)",
        border="oklch(90% 0.006 95)",
        accent="oklch(52% 0.10 28)",
        posture=(
            "serif display, sans body, mono for metadata only",
            "no shadows, no rounded cards — borders + whitespace do the work",
            "one decisive image, cropped only at the bottom",
            "kicker / eyebrow in mono uppercase, one accent color used at most twice; "
            "never create peach/pink/orange-beige page washes unless the brand requires them",
        ),
        tones=("editorial", "magazine", "editorial / magazine", "publishing", "luxury", "refined"),
    ),
    DesignDirection(
        id="modern-minimal",
        label="Modern minimal — Linear / Vercel",
        mood=(
            "Quiet, precise, software-native. System fonts, crisp neutral "
            "foundations, and a small but visible product palette so the interface "
            "feels shipped rather than greyscale. Chrome stays restrained while "
            "interaction states, illustrations, and charts carry color."
        ),
        references=("Linear", "Vercel", "Notion 2024", "Stripe docs"),
        display_font="-apple-system, BlinkMacSystemFont, 'SF Pro Display', system-ui, sans-serif",
        body_font="-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif",
        bg="oklch(99% 0.002 240)",
        surface="oklch(100% 0 0)",
        fg="oklch(18% 0.012 250)",
        muted="oklch(54% 0.012 250)",
        border="oklch(92% 0.005 250)",
        accent="oklch(58% 0.18 255)",
        posture=(
            "tight letter-spacing on display sizes (-0.02em)",
            "hairline borders only, no shadows except dropdowns/modals",
            "mono numerics with `font-variant-numeric: tabular-nums`",
            "sticky frosted nav, content-led layouts with one product illustration or data viz",
            "controlled color system: primary action + one secondary signal + status colors; "
            "never flood every card with gradients",
        ),
        # NOTE: "tech" intentionally omitted — it belongs to tech-utility. Leaving
        # it here made `--tone tech` tie and resolve to modern-minimal (Codex HIGH).
        tones=("modern minimal", "minimal", "modern", "saas", "software", "product"),
        is_blue_accent=True,  # cobalt oklch(58% 0.18 255)
    ),
    DesignDirection(
        id="human-approachable",
        label="Human / approachable — Airbnb / Duolingo",
        mood=(
            "Friendly and tactile without the generic cozy canvas. Clean neutral "
            "background, product-led color system, generous radii, clear hierarchy. "
            "Good for consumer tools, marketplaces, wellness, education, AI "
            "assistants, and indie SaaS when the brand has not supplied a palette."
        ),
        references=("Airbnb", "Duolingo product surfaces", "Miro", "Mercury"),
        display_font="'Söhne', 'Avenir Next', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",
        body_font="-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif",
        bg="oklch(98% 0.004 240)",
        surface="oklch(100% 0 0)",
        fg="oklch(20% 0.02 240)",
        muted="oklch(50% 0.018 240)",
        border="oklch(90% 0.006 240)",
        accent="oklch(56% 0.12 170)",
        posture=(
            "sans display with strong weight contrast, system body for readability",
            "comfortable radii (12-18px) paired with crisp grid alignment",
            "primary action color plus a secondary/domain accent and clear status colors",
            "subtle elevation only on interactive cards; tasteful product-moment glows, "
            "never a full-page beige/pastel wash",
            "use real product screenshots, data, or labelled placeholders — not pastel gradients",
        ),
        tones=("human", "approachable", "human / approachable", "friendly", "consumer", "marketplace", "playful"),
    ),
    DesignDirection(
        id="tech-utility",
        label="Tech / utility — Datadog / GitHub",
        mood=(
            "Data-dense, monospace-friendly, dark or light + grid. Made for "
            "engineers and operators who want information per square inch, not vibes."
        ),
        references=("Datadog", "GitHub", "Cloudflare dashboard", "Sentry"),
        display_font="-apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', system-ui, sans-serif",
        body_font="-apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', system-ui, sans-serif",
        mono_font="'JetBrains Mono', 'IBM Plex Mono', ui-monospace, Menlo, monospace",
        bg="oklch(98% 0.005 250)",
        surface="oklch(100% 0 0)",
        fg="oklch(22% 0.02 240)",
        muted="oklch(50% 0.018 240)",
        border="oklch(90% 0.008 240)",
        accent="oklch(58% 0.16 145)",
        posture=(
            "sans display + sans body (one family) is OK here — utility trumps editorial",
            "tabular numerics everywhere, mono for code / IDs / hashes",
            "dense tables with hairline borders, no row striping",
            "inline status pills (success / warn / danger) with restrained tinted backgrounds",
            "avoid: hero images, oversized headlines, marketing copy — show the product instead",
        ),
        tones=("tech", "utility", "tech / utility", "dashboard", "tool", "operator", "engineering", "data"),
    ),
    DesignDirection(
        id="brutalist-experimental",
        label="Brutalist / experimental — Are.na / Yale",
        mood=(
            "Loud type. Visible grid. System sans + a single oversized serif. "
            "Deliberate ugliness as confidence. Great for art, indie, agency, "
            "manifesto pages."
        ),
        references=("Are.na", "Yale Center for British Art", "mschf", "Read.cv"),
        display_font="'Times New Roman', 'Iowan Old Style', Georgia, serif",
        body_font="ui-monospace, 'IBM Plex Mono', 'JetBrains Mono', Menlo, monospace",
        bg="oklch(98% 0.004 240)",
        surface="oklch(100% 0 0)",
        fg="oklch(15% 0.02 100)",
        muted="oklch(40% 0.02 100)",
        border="oklch(15% 0.02 100)",
        accent="oklch(60% 0.22 25)",
        posture=(
            "display = serif at extreme sizes (clamp(80px, 12vw, 200px))",
            "body = monospace — yes, monospace as body, deliberately",
            "borders are full-strength fg (1.5-2px), not muted greys",
            "asymmetric layouts: one column 70%, the other 30%",
            "almost no border-radius (0-2px). No shadows. No gradients.",
            "underline links, no hover decoration — let the typography carry it",
        ),
        tones=("brutalist", "experimental", "brutalist / experimental", "art", "indie", "agency", "manifesto"),
    ),
)

_DEFAULT_DIRECTION = "modern-minimal"


def find_direction(token: str) -> DesignDirection | None:
    """Look up a direction by id or label (case-insensitive)."""
    needle = (token or "").strip().lower()
    if not needle:
        return None
    for d in DESIGN_DIRECTIONS:
        if d.id == needle or d.label.lower() == needle:
            return d
    return None


def pick_direction(tone: str | None) -> DesignDirection:
    """Pick the best-matching direction for a tone string.

    Matches against each direction's ``tones`` keywords; falls back to
    ``modern-minimal`` (the safe software-native default). De-ai-slop's
    "no blue" rule governs this default path — callers that want cobalt must
    say so explicitly or supply a brand system.
    """
    text = (tone or "").strip().lower()
    if text:
        # Exact direction id/label first.
        direct = find_direction(text)
        if direct is not None:
            return direct
        # Then tone-keyword scan (longest keyword wins to avoid 'tech' eating
        # 'editorial' on substring overlap).
        best: tuple[int, DesignDirection] | None = None
        for d in DESIGN_DIRECTIONS:
            for kw in d.tones:
                if kw in text:
                    score = len(kw)
                    if best is None or score > best[0]:
                        best = (score, d)
        if best is not None:
            return best[1]
    return next(d for d in DESIGN_DIRECTIONS if d.id == _DEFAULT_DIRECTION)


def render_direction_spec(direction: DesignDirection, *, accent_override: str | None = None) -> str:
    """Render a direction as a prompt block: CSS `:root` tokens + posture.

    The agent binds these tokens verbatim into the seed `:root`. ``accent_override``
    (free text like "moss green instead of cobalt") is passed through as a note,
    not resolved here — the agent translates it to an OKLch value.
    """
    accent_line = f"  --accent:  {direction.accent};"
    lines = [
        f"### Direction: {direction.label}  (id: {direction.id})",
        "",
        f"**Mood:** {direction.mood}",
        "",
        f"**References:** {', '.join(direction.references)}.",
        "",
        "**Palette + fonts — drop into `:root` VERBATIM (do not improvise colors):**",
        "",
        "```css",
        ":root {",
        f"  --bg:      {direction.bg};",
        f"  --surface: {direction.surface};",
        f"  --fg:      {direction.fg};",
        f"  --muted:   {direction.muted};",
        f"  --border:  {direction.border};",
        accent_line,
        "",
        f"  --font-display: {direction.display_font};",
        f"  --font-body:    {direction.body_font};",
    ]
    if direction.mono_font:
        lines.append(f"  --font-mono:    {direction.mono_font};")
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append("**Posture (honour these in layout, border weight, accent budget):**")
    lines.extend(f"- {p}" for p in direction.posture)
    if accent_override and accent_override.strip():
        lines.append("")
        lines.append(
            f"**Accent override requested:** {accent_override.strip()} — "
            "translate this to an OKLch value and use it in place of `--accent` above."
        )
    return "\n".join(lines)
