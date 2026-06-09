"""Entity extraction and compilation for vault knowledge graph.

Ported from Karpathy's LLM Wiki pattern: when a source is ingested,
extract key entities/concepts and create or update dedicated concept
pages. This is the "compilation" step that turns a filing system into
a deeply interlinked knowledge graph.

Usage:
    uv run python entity_extractor.py extract "path/to/source.md"
    uv run python entity_extractor.py compile "path/to/source.md" --vault-dir "path/to/vault"
    uv run python entity_extractor.py compile --entities entities.json --vault-dir "path/to/vault"
    uv run python entity_extractor.py contradictions "path/to/concept.md"
    uv run python entity_extractor.py reindex "path/to/file.md" --memory-dir "path/to/memory"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

# Add scripts dir for config, memory_index, etc.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ExtractedEntity:
    """An entity/concept extracted from a source document."""

    name: str
    entity_type: str = "concept"  # concept | person | tool | project | technique
    description: str = ""
    source_claims: list[str] = field(default_factory=list)
    confidence: float = 0.5

    @property
    def slug(self) -> str:
        """UPPER-KEBAB-CASE filename slug."""
        s = re.sub(r"^[\d]+[\.\-\s]+", "", self.name.strip())
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[\s_]+", "-", s)
        return s.upper()


@dataclass
class Contradiction:
    """A potential contradiction between claims on a concept page."""

    concept_page: str
    claim_a: str
    source_a: str
    claim_b: str
    source_b: str
    severity: str = "tension"  # "direct" or "tension"


@dataclass
class ConnectionArticle:
    """A cross-cutting insight linking two concepts."""

    title: str
    connects: list[str]  # concept slugs
    insight: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class CompilationReport:
    """Summary of a compilation run."""

    pages_created: list[str] = field(default_factory=list)
    pages_updated: list[str] = field(default_factory=list)
    connections_created: list[str] = field(default_factory=list)
    contradictions_found: list[Contradiction] = field(default_factory=list)
    files_reindexed: int = 0
    entities_processed: int = 0
    entities_skipped: int = 0


@dataclass(frozen=True)
class VaultFrontmatterIssue:
    """A source note frontmatter finding that blocks compilation writes."""

    file: str
    message: str


class VaultFrontmatterError(ValueError):
    """Raised when vault compilation would write from malformed source notes."""

    def __init__(self, operation: str, issues: list[VaultFrontmatterIssue]):
        self.operation = operation
        self.issues = issues
        preview = "; ".join(f"{issue.file}: {issue.message}" for issue in issues[:5])
        extra = f"; +{len(issues) - 5} more" if len(issues) > 5 else ""
        super().__init__(
            f"{operation} blocked by source frontmatter gate: {preview}{extra}"
        )


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def load_schema(vault_dir: Path) -> dict:
    """Load SCHEMA.md tag taxonomy and scope keywords.

    Returns dict with keys:
        scope_keywords: set[str] — lowercase keywords from the Scope section
        tag_taxonomy: set[str] — all valid tags from the Tag Taxonomy section
        entity_types: set[str] — valid entity types

    Returns empty dict if SCHEMA.md does not exist (best-effort).
    """
    schema_path = vault_dir / "SCHEMA.md"
    if not schema_path.exists():
        return {}

    try:
        content = schema_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    result: dict = {"scope_keywords": set(), "tag_taxonomy": set(), "entity_types": set()}

    # Extract scope keywords from the Scope section
    scope_m = re.search(r"## Scope\n(.*?)(?=\n## )", content, re.DOTALL)
    if scope_m:
        scope_text = scope_m.group(1).lower()
        # Extract significant words (4+ chars, not common words)
        common = {"this", "that", "with", "from", "into", "the", "and", "for", "not", "are", "was", "were"}
        words = re.findall(r"\b[a-z]{4,}\b", scope_text)
        result["scope_keywords"] = {w for w in words if w not in common}

    # Extract tags from Tag Taxonomy tables (pipe-delimited rows)
    tag_m = re.findall(r"\| `([^`]+)` \|", content)
    result["tag_taxonomy"] = {t.strip() for t in tag_m}

    # Extract entity types
    etype_section = re.search(r"### Entity Types.*?\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if etype_section:
        etypes = re.findall(r"\| `([^`]+)` \|", etype_section.group(1))
        result["entity_types"] = {t.strip() for t in etypes}

    return result


# ---------------------------------------------------------------------------
# Heuristic entity extraction (no LLM needed)
# ---------------------------------------------------------------------------

# Patterns for heading-based entity names
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)", re.MULTILINE)
# Bold text: **entity name**
_BOLD_RE = re.compile(r"\*\*([^*]{3,60})\*\*")
# Wiki-links: [[Entity Name]]
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
# Frontmatter related links
_RELATED_RE = re.compile(r'^\s*-\s*"\[\[([^\]]+)\]\]"', re.MULTILINE)
# Frontmatter tags
_TAGS_RE = re.compile(r"^tags:\s*\[([^\]]+)\]", re.MULTILINE)

# Code fence + inline code (stripped during preprocess so bold inside code blocks
# is not extracted as a concept). Triple-backtick fences first, then inline code.
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")

# --- Name-quality filter (Change 2 in fix-it-the-correct-atomic-acorn plan) ---

# Punctuation chars stripped from the start/end of a candidate concept name.
_NAME_PUNCT_STRIP = ".,;:!?\"'`…“”‘’()[]"

# Articles dropped from the FRONT of the name when counting tokens for the
# 6-word cap. Keeps "The Wiki" (1 substantive token) under the cap as a
# single-word concept name.
_LEADING_ARTICLES = ("a", "an", "the")

# Finite verbs that mark a sentence-shaped fragment when preceded by an
# article/pronoun + intervening word. Intentionally narrow — verbs not in
# this list slip through (acceptable per known-limitation in plan).
_FINITE_VERBS = (
    # to-be
    "is", "are", "was", "were", "be", "been", "being", "am",
    # modal
    "can", "could", "should", "would", "will", "may", "might", "must",
    # general
    "has", "have", "had", "do", "does", "did",
    "make", "makes", "made", "need", "needs", "want", "wants",
    "lets", "helps", "means", "shows", "tells", "gives", "takes",
    "becomes", "gets", "keeps", "runs", "works", "seems", "looks",
    "builds", "maintains", "creates", "provides", "uses", "allows",
    "updates", "adds", "removes", "deletes", "enables",
    "contains", "includes", "requires", "returns", "sets",
)
# Anchored at name-start so titles like "How The System Works" (start with
# interrogative "How", not an article) are NOT rejected. Pattern is
# (article|pronoun) WORD finite-verb.
_SENTENCE_FRAGMENT_RE = re.compile(
    r"^\s*(?:a|an|the|it|he|she|they|this|that|these|those|we|i|you)\s+\w+\s+(?:"
    + "|".join(re.escape(v) for v in _FINITE_VERBS)
    + r")\b",
    re.IGNORECASE,
)

# --- Bold-with-definition pattern detection (Change 1 in plan) ---

# Sentence-end punctuation that signals a clause boundary (Karpathy line 23
# packs `time.**Research**:` etc. inline within one paragraph; the period
# before `**` is what makes the bold structurally definitional).
_LEADING_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n\s*$")
_LEADING_LINE_START_RE = re.compile(r"(?:^|\n)\s*(?:[-*>]|\d+\.)?\s*$")
_LEADING_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?][\s\)\]\>\"\']*$")

# Definition-marker peek (after the closing `**`):
#   em-dash:    ` — ` (U+2014)
#   en-dash:    ` – ` (U+2013)
#   double-dash: ` -- `
# Each requires a single trailing space before content, matched against
# at most 8 chars of peek window.
_BOLD_DEF_DASH_RE = re.compile(r"^\s*(?:—|–|--)\s+\S")
# Colon marker requires 4+ chars of content (any chars) within the 32-char
# peek so `**X**:` empty bombs do not qualify. The 4+ chars-of-content rule
# accepts `**Business/team**: an internal...` ("an internal..." is 11 chars
# total) — the previous `\S{4,}` form falsely rejected this because the
# first word ("an") was only 2 chars.
_BOLD_DEF_COLON_RE = re.compile(r"^:\s+\S.{3,}")
# Period-inside-bold: bold content ends with `.` and Title-cased single
# alphabetic word; trailing context is `\s+[A-Z]` (sentence-following).
# Karpathy uses this for `**Ingest.**`, `**Query.**`, `**Lint.**`.
_TITLE_CASE_PERIOD_NAME_RE = re.compile(r"^[A-Z][a-zA-Z]*\.?$")
_BOLD_DEF_PERIOD_TRAILING_RE = re.compile(r"^\s+[A-Z]")

# Noise words to skip
_SKIP_NAMES = {
    "overview", "summary", "introduction", "conclusion", "references",
    "notes", "todo", "table of contents", "appendix", "changelog",
    "getting started", "quick start", "installation", "usage",
    "step 1", "step 2", "step 3", "step 4", "step 5", "step 6",
    # Vault infrastructure (not knowledge concepts)
    "action items", "decisions made", "sessions", "panel", "heartbeats",
    "memory maintenance", "pre-compaction flush", "finance report",
    "daily log", "flush context", "session flush", "compaction recovery",
    # Gap-2 cleanup (2026-04-24): generic doc-section headings that became
    # noise concept pages from heuristic extraction. Each entry blocks one
    # archived noise page from regenerating. NOTE: only blocks exact-match
    # lowercased headings — multi-word entity names that contain these
    # tokens (e.g. "Memory Architecture") are unaffected.
    "file:", "files", "files:", "fix:", "fix",
    "server", "location", "history", "gotchas", "scope",
    "components", "architecture", "tech stack",
    "the math", "the pipeline", "the monorepo (turborepo+pnpm)",
    "key files", "key stats", "key commands",
    "core patterns", "design rules", "build/dev/test commands",
    "build dev test commands", "trace shape",
    "how it works", "why this matters",
    "what done looks like", "what changed for you as the builder",
    "what each city page contains",
    "what makes this different from competitors",
    "what happened (2026-03-24)", "what moved forward",
    "when to use archon", "when to use which workflow",
    "from an idea", "from an existing prd file",
    "active hypotheses", "active projects",
    "archived (cold)", "archived (done)", "archived cold", "archived done",
    "all concepts", "current blockers",
    "concept a", "concept b",
    "correct behavior", "recurring patterns", "source note",
    "custom workflows (this repo)", "custom workflows this repo",
    "relationship to convoy/mailbox",
    "by the numbers", "backlog", "backlogmd",
    "auto-generated tags (set by compilation engine — never add manually)",
    # v3 (2026-04-26): documentation meta-markers. Merged into _SKIP_NAMES
    # rather than a separate _BOLD_META_MARKERS set so they are rejected
    # OUTRIGHT across all extraction paths (heading/bold/wikilink), and so
    # repeated meta-markers cannot be lifted past the threshold by dedup.
    # Some entries (`note`, `tip`) overlap existing entries above — set
    # dedup makes that harmless. Internal punctuation in the candidate is
    # normalized to spaces before lookup so `TL;DR` matches `tl dr`.
    "note", "tip", "pro-tip", "pro-tips", "protip", "protips",
    "warning", "warnings", "caution", "cautions",
    "important", "example", "examples", "todos", "fixme",
    "nb", "n.b.", "aside", "asides", "side note", "note that",
    "see", "see also", "tl dr", "tldr", "caveat", "caveats",
    "rule of thumb", "rules of thumb", "disclaimer", "disclaimers",
}

# Patterns that match operational/temporal names (not domain knowledge)
_SKIP_PATTERNS = [
    re.compile(r"^\d{4}-W\d{1,2}$", re.IGNORECASE),          # weekly refs: 2026-W14
    re.compile(r"^\d{4}-\d{2}-\d{2}"),                         # date refs: 2026-04-05
    re.compile(r"^(pre-compaction|flush|daily-log|session-flush)-", re.IGNORECASE),
    re.compile(r"^finance-report-\d+$", re.IGNORECASE),        # finance snapshots
    re.compile(r"^heartbeat", re.IGNORECASE),                   # heartbeat entries
    # Gap-2 cleanup (2026-04-24): structural patterns from heuristic noise.
    re.compile(r"^[─━—-].*[─━—-]\s*$"),  # dash/em-dash bordered (e.g. "── X ──")
    re.compile(r"^-.*-$"),                # leading + trailing hyphen (broken slugs)
    re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-compile-"),  # compile artifacts
    re.compile(r"^(what|when|how|why|where|who)-(done|changed|moved|happened|each|makes|to|it|this|that)-", re.IGNORECASE),
    re.compile(r"^key-(files|stats|commands|points|takeaways)$", re.IGNORECASE),
    re.compile(r"^the-(math|pipeline|monorepo|stack|flow|system)$", re.IGNORECASE),
    re.compile(r"^from-(an?|the)-", re.IGNORECASE),
]

# Minimum confidence threshold for compilation
CONFIDENCE_THRESHOLD = 0.6

# Stricter threshold for daily logs (high operational noise)
_DAILY_LOG_THRESHOLD = 0.85


def _is_daily_log(source_path: str) -> bool:
    """Check if source is a daily log file (high-noise, needs stricter filtering)."""
    p = Path(source_path)
    return (
        p.parent.name == "daily"
        or bool(re.match(r"^\d{4}-\d{2}-\d{2}$", p.stem))
    )


def _matches_skip_pattern(name: str) -> bool:
    """Check if name matches any operational/temporal skip pattern."""
    return any(pat.search(name) for pat in _SKIP_PATTERNS)


def _clean_concept_name(raw: str) -> str:
    """Strip leading/trailing whitespace and punctuation from a candidate name.

    The CLEANED name is what gets stored on the entity and used for slugging.
    Internal whitespace is collapsed (so multi-line wraps in source markdown
    do not survive into the concept page title).
    """
    cleaned = raw.strip().strip(_NAME_PUNCT_STRIP).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _normalize_for_filter(name: str) -> str:
    """Lowercase + collapse internal punctuation for SKIP_NAMES matching.

    Lets `TL;DR` match the `tl dr` skip-name entry and `Pro-Tip` match
    `pro-tip` — internal punct is replaced with a single space and the
    result is lowercased and collapsed.
    """
    s = re.sub(r"[;,!?]+", " ", name.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_valid_concept_name(name: str) -> bool:
    """Reject sentence-fragment / over-long candidate concept names.

    Applied to BOLD and HEADING extraction paths. Wikilinks and frontmatter
    `related:` are author-strong signals and exempted.

    Three rules (plan v3 Change 2):
      1. After cleaning, length >= 2.
      2. Token count <= 6 after stripping leading articles (a/an/the).
      3. Anchored sentence-fragment regex does NOT match — only finite verbs
         preceded by an article/pronoun + intervening word at name-start
         trigger rejection. "How The System Works" keeps (starts with
         interrogative); "the wiki is persistent" rejects.
    """
    if len(name) < 2:
        return False
    tokens = name.split()
    if not tokens:
        return False
    # Drop leading articles only for the token-count check
    if tokens[0].lower() in _LEADING_ARTICLES:
        substantive = tokens[1:]
    else:
        substantive = tokens
    if len(substantive) > 6:
        return False
    if _SENTENCE_FRAGMENT_RE.match(name):
        return False
    return True


def _is_leading_bold(content: str, match_start: int) -> bool:
    """True if the `**` at *match_start* is structurally positioned for a definition.

    Four leading-position conditions (plan v3 Change 1):
      1. Document start (match_start == 0)
      2. Paragraph break: `\\n\\n` + optional whitespace before `**`
      3. Line start with optional list/quote marker:
         `\\n` + optional whitespace + optional `-`/`*`/`>`/`<digit>.` + whitespace
      4. Sentence boundary: previous non-whitespace char is `.` `!` `?` followed
         by zero or more closers/whitespace. Catches Karpathy line 23 inline
         chain `time.**Research**:` while rejecting mid-sentence `He said
         **really** —`.
    """
    if match_start == 0:
        return True
    prev = content[max(0, match_start - 80):match_start]
    if _LEADING_PARAGRAPH_BREAK_RE.search(prev):
        return True
    if _LEADING_LINE_START_RE.search(prev):
        return True
    if _LEADING_SENTENCE_BOUNDARY_RE.search(prev):
        return True
    return False


def _score_bold_def(content: str, bold_text: str, match_end: int) -> bool:
    """True if a bold-with-definition marker follows the closing `**`.

    Examines up to 32 chars after match_end. Returns True for em-dash,
    en-dash, double-hyphen, or colon-with-substantive-content. Also returns
    True for the period-inside-bold pattern (Karpathy `**Ingest.**`) when
    the bold content is a Title-cased word and a capital-letter sentence
    follows. Plain bold (inline emphasis) returns False.
    """
    peek = content[match_end:match_end + 32]
    if _BOLD_DEF_DASH_RE.match(peek[:8]):
        return True
    if _BOLD_DEF_COLON_RE.match(peek):
        return True
    # Period-inside-bold: e.g. **Ingest.** You drop a new source...
    if bold_text.endswith("."):
        inner = bold_text[:-1]
        if _TITLE_CASE_PERIOD_NAME_RE.match(inner) and _BOLD_DEF_PERIOD_TRAILING_RE.match(peek[:8]):
            return True
    return False


def extract_entities_heuristic(
    content: str,
    source_path: str = "",
    schema: dict | None = None,
) -> list[ExtractedEntity]:
    """Extract entities from markdown using structural heuristics.

    Uses headings, bold text, wikilinks, and frontmatter. No LLM call.
    The vault-ingest skill's LLM can enhance these results.

    If *schema* is provided (from load_schema()), entities whose names
    overlap with scope keywords get a +0.1 confidence boost.
    """
    entities: dict[str, ExtractedEntity] = {}
    source_stem = Path(source_path).stem if source_path else ""

    # Normalize line endings (CRLF/CR -> LF) so the leading-bold algorithm's
    # paragraph-break / sentence-boundary regexes work uniformly.
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    # Strip frontmatter for body analysis
    body = content
    fm_match = re.match(r"^---\n(.*?)\n---\n?", content, re.DOTALL)
    frontmatter_text = ""
    if fm_match:
        frontmatter_text = fm_match.group(1)
        body = content[fm_match.end():]

    # Preprocess: strip code fences and inline code so bold-inside-code is
    # not extracted (e.g. `# **Cache Key** -- comment` in a python block).
    body = _CODE_FENCE_RE.sub("", body)
    body = _INLINE_CODE_RE.sub("", body)

    # 1. Headings (H1-H3) — high confidence
    for m in _HEADING_RE.finditer(body):
        raw = m.group(1).strip().rstrip("#").strip()
        cleaned = _clean_concept_name(raw)
        if len(cleaned) < 3 or len(cleaned) > 80:
            continue
        key = cleaned.lower()
        normalized = _normalize_for_filter(cleaned)
        if (
            key in _SKIP_NAMES
            or normalized in _SKIP_NAMES
            or _matches_skip_pattern(key)
            or key == source_stem.lower().replace("-", " ")
        ):
            continue
        if not _is_valid_concept_name(cleaned):
            continue
        if key not in entities:
            entities[key] = ExtractedEntity(
                name=cleaned, entity_type="concept", confidence=0.7,
            )
        else:
            entities[key].confidence = min(1.0, entities[key].confidence + 0.1)

    # 2. Bold text — base 0.5, boosted to 0.75 for structural definition
    #    contexts (em-dash / colon-content / period-inside-bold).
    for m in _BOLD_RE.finditer(body):
        raw = m.group(1).strip()
        cleaned = _clean_concept_name(raw)
        if len(cleaned) < 3 or len(cleaned) > 60:
            continue
        key = cleaned.lower()
        normalized = _normalize_for_filter(cleaned)
        if key in _SKIP_NAMES or normalized in _SKIP_NAMES or _matches_skip_pattern(key):
            continue
        if not _is_valid_concept_name(cleaned):
            continue
        # Score: 0.75 if structurally-leading AND followed by a definition
        # marker; 0.5 otherwise.
        confidence = 0.5
        if _is_leading_bold(body, m.start()) and _score_bold_def(body, raw, m.end()):
            confidence = 0.75
        if key not in entities:
            entities[key] = ExtractedEntity(
                name=cleaned, entity_type="concept", confidence=confidence,
            )
        else:
            # Existing entry (e.g. heading already created it) — only raise
            # confidence; never lower it.
            entities[key].confidence = min(1.0, max(entities[key].confidence, confidence) + 0.1)

    # 3. Wiki-links — high confidence (author explicitly linked)
    for m in _WIKILINK_RE.finditer(body):
        name = m.group(1).strip()
        if len(name) < 2 or len(name) > 80:
            continue
        key = name.lower()
        if key in _SKIP_NAMES or _matches_skip_pattern(key):
            continue
        if key not in entities:
            entities[key] = ExtractedEntity(
                name=name, entity_type="concept", confidence=0.8,
            )
        else:
            entities[key].confidence = min(1.0, entities[key].confidence + 0.15)

    # 4. Frontmatter related links
    for m in _RELATED_RE.finditer(frontmatter_text):
        name = m.group(1).strip()
        key = name.lower()
        if key not in entities:
            entities[key] = ExtractedEntity(
                name=name, entity_type="concept", confidence=0.6,
            )

    # 5. Extract claims (sentences near entity mentions)
    sentences = re.split(r"[.!?]\s+", body)
    for entity in entities.values():
        pattern = re.compile(re.escape(entity.name), re.IGNORECASE)
        for sentence in sentences:
            if pattern.search(sentence) and 20 < len(sentence) < 300:
                claim = sentence.strip().rstrip(".")
                if claim and claim not in entity.source_claims:
                    entity.source_claims.append(claim)
                    if len(entity.source_claims) >= 3:
                        break

    # Schema-aware boost: entities matching scope keywords get +0.1
    if schema and schema.get("scope_keywords"):
        scope_kw = schema["scope_keywords"]
        for entity in entities.values():
            name_words = set(entity.name.lower().split())
            if name_words & scope_kw:
                entity.confidence = min(1.0, entity.confidence + 0.1)

    # Sort by confidence descending, cap at 15
    result = sorted(entities.values(), key=lambda e: e.confidence, reverse=True)
    return result[:15]


# ---------------------------------------------------------------------------
# Concept page operations
# ---------------------------------------------------------------------------

def find_existing_concept(name: str, vault_dir: Path) -> Path | None:
    """Find an existing concept page by name or alias.

    Searches concepts/ folder first (exact match on filename),
    then falls back to scanning aliases in frontmatter.
    """
    concepts_dir = vault_dir / "concepts"
    if not concepts_dir.exists():
        return None

    slug = ExtractedEntity(name=name).slug
    exact = concepts_dir / f"{slug}.md"
    if exact.exists():
        return exact

    # Fuzzy: scan aliases in all concept pages
    name_lower = name.lower().strip()
    for md_file in concepts_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not fm:
            continue
        # Check aliases
        aliases_m = re.search(r"aliases:\s*\[([^\]]*)\]", fm.group(1))
        if aliases_m:
            aliases = [a.strip().strip('"').strip("'").lower() for a in aliases_m.group(1).split(",")]
            if name_lower in aliases:
                return md_file

    return None


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Vault-level helpers (Karpathy LLM Wiki pattern)
# ---------------------------------------------------------------------------


def preserve_raw(
    source_path: Path,
    vault_dir: Path,
    always_date_prefix: bool = False,
    on_collision: Literal["raise", "skip", "overwrite"] = "raise",
    *,
    subdir: str | None = None,
) -> Path:
    """Copy a source file into {vault}/raw/ as an immutable archive.

    Karpathy "LLM Wiki" raw/ preservation — keeps the original unmodified so
    the wiki can be recompiled from source if extraction logic changes later.
    Uses shutil.copy2 to preserve file metadata.

    Collision semantics:
      - Default (always_date_prefix=False): keep the original filename; on
        collision with an existing raw file, fall back to `{YYYY-MM-DD}-{name}`.
        This is the vault-ingest pattern — sources are ingested rarely and
        usually have unique names.
      - always_date_prefix=True: always prefix with today's date. This is the
        finance_ingest pattern — bank statements cycle daily and share names.

    on_collision (only applies once the chosen destination already exists):
      - "raise"     (default): fail loudly with FileExistsError; raw/ is
                    immutable and unexpected collisions deserve investigation.
      - "skip"      : BYTE-AWARE idempotent — if existing archive bytes match
                    the incoming source (sha256 compare), return the existing
                    path unchanged. If bytes DIFFER, raise FileExistsError to
                    protect provenance (a raw archive must always be the
                    source that produced downstream artifacts).
      - "overwrite" : explicit opt-in to legacy silent-overwrite behavior.

    subdir (keyword-only, optional): if provided, archive is placed under
        ``{vault}/raw/{subdir}/`` instead of the top-level ``{vault}/raw/``.
        Used by URL ingest (gap-4) to land web clips in ``raw/clipped/`` while
        keeping all other raw collision/idempotency semantics identical.
    """
    raw_dir = vault_dir / "raw"
    if subdir:
        raw_dir = raw_dir / subdir
    raw_dir.mkdir(parents=True, exist_ok=True)
    if always_date_prefix:
        dest = raw_dir / f"{_today()}-{source_path.name}"
    else:
        dest = raw_dir / source_path.name
        if dest.exists():
            dest = raw_dir / f"{_today()}-{source_path.name}"

    # Immutable raw/ contract - see .claude/sections/03_memory_pipelines.md:133-137.
    # If the chosen destination (default OR date-prefixed fallback) already
    # exists, on_collision dictates behavior:
    #   - "raise"     (default): fail loudly, FileExistsError. Caller investigates.
    #   - "skip"      : BYTE-AWARE idempotent — if existing archive bytes match
    #                   the incoming source (sha256 compare), return the existing
    #                   path unchanged. If bytes DIFFER, raise FileExistsError.
    #                   This preserves provenance: a raw archive must always be
    #                   the source that produced downstream artifacts. Silent
    #                   skip on differing bytes would break that contract (R3).
    #   - "overwrite" : explicit opt-in to legacy silent-overwrite behavior.
    if dest.exists():
        if on_collision == "skip":
            # Byte-aware skip — only safe if existing archive matches incoming source
            src_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            dst_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
            if src_hash == dst_hash:
                return dest
            raise FileExistsError(
                f"preserve_raw refusing skip-on-divergent-bytes: {dest} exists "
                f"with sha256 {dst_hash[:12]}, but incoming source has sha256 "
                f"{src_hash[:12]}. raw/ archive is the source-of-truth for "
                f"downstream artifacts; silent skip would break provenance. "
                f"Remove the existing target or investigate the source change."
            )
        if on_collision == "raise":
            if always_date_prefix:
                msg = (
                    f"preserve_raw refusing to overwrite existing archive: {dest}. "
                    f"raw/ is immutable; remove the existing date-prefixed target "
                    f"or wait until tomorrow's date prefix changes the destination."
                )
            else:
                msg = (
                    f"preserve_raw refusing to overwrite existing archive: {dest}. "
                    f"raw/ is immutable; rename source or remove the existing target."
                )
            raise FileExistsError(msg)
        # on_collision == "overwrite": fall through to shutil.copy2 below.

    shutil.copy2(source_path, dest)
    return dest


def append_vault_log(
    vault_dir: Path,
    event_type: str,
    title: str,
    bullets: list[str] | None = None,
) -> Path:
    """Append an event entry to {vault}/LOG.md — the vault evolution timeline.

    Karpathy "LLM Wiki" log.md pattern — an append-only, grep-able chronological
    record of vault-level events (ingests, compiles, reflections, weekly
    synthesis, dream cycles, archives). Daily/heartbeat events stay in daily/
    logs; LOG.md is for wiki-evolution events only.

    Format (grep-able via `grep "^## \\[" LOG.md | tail -5`):

        ## [2026-04-11 14:32] ingest | Article Title
        - source: [[SLUG]]
        - pages: +3 created, ~2 updated

    First write creates LOG.md with system frontmatter. Cross-platform locked
    via `shared.file_lock` so concurrent pipelines (reflect + weekly + dream)
    never corrupt the file.
    """
    log_path = vault_dir / "LOG.md"
    vault_dir.mkdir(parents=True, exist_ok=True)

    from datetime import datetime

    from shared import file_lock

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## [{now}] {event_type} | {title}"]
    for bullet in bullets or []:
        lines.append(f"- {bullet}")
    lines.append("")  # trailing blank line separates entries
    entry = "\n".join(lines) + "\n"

    with file_lock(log_path, timeout=5.0):
        if not log_path.exists():
            header = (
                "---\n"
                "tags: [system]\n"
                f"date: {_today()}\n"
                'summary: "Append-only chronological record of vault-level events."\n'
                "---\n\n"
                "# Vault Log\n\n"
                "> Grep pattern: `grep \"^## \\[\" LOG.md | tail -5` surfaces recent activity.\n\n"
            )
            log_path.write_text(header, encoding="utf-8")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)

    return log_path


def _read_summary(md_file: Path) -> str | None:
    """Extract the `summary:` field from a markdown file's YAML frontmatter.

    Returns None if the file has no frontmatter or no summary field.
    Accepts both quoted (`summary: "..."`) and unquoted (`summary: ...`) forms.
    """
    try:
        text = md_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not fm:
        return None
    fm_text = fm.group(1)
    m = re.search(r'summary:\s*"([^"]*)"', fm_text)
    if m:
        return m.group(1)
    m = re.search(r"summary:\s*(.+)$", fm_text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    return None


def _display_source_path(source_path: Path, vault_dir: Path) -> str:
    try:
        resolved_source = source_path.resolve(strict=False)
        resolved_vault = vault_dir.resolve(strict=False)
        return str(resolved_source.relative_to(resolved_vault))
    except ValueError:
        return str(source_path)


def _source_frontmatter_issues(source_path: Path, vault_dir: Path) -> list[VaultFrontmatterIssue]:
    """Return frontmatter gate issues for an existing markdown source note."""
    if source_path.suffix.lower() != ".md" or not source_path.exists():
        return []

    rel = _display_source_path(source_path, vault_dir)
    try:
        content = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [VaultFrontmatterIssue(rel, f"Unreadable source note: {exc}")]

    fm = re.match(r"^---\r?\n(.*?)\r?\n---", content, re.DOTALL)
    if not fm:
        return [VaultFrontmatterIssue(rel, "Missing frontmatter block")]

    fm_text = fm.group(1)
    issues: list[VaultFrontmatterIssue] = []
    if not re.search(r"^tags:\s*\[[^\]]*\]", fm_text, re.MULTILINE):
        issues.append(VaultFrontmatterIssue(rel, "Missing required field: tags"))
    if not re.search(r"^date:\s*\d{4}-\d{2}-\d{2}\s*\r?$", fm_text, re.MULTILINE):
        issues.append(VaultFrontmatterIssue(rel, "Missing required field: date"))
    return issues


def _enforce_source_frontmatter(
    source_path: str | Path,
    vault_dir: Path,
    *,
    operation: str,
) -> None:
    if not source_path:
        return
    issues = _source_frontmatter_issues(Path(source_path), vault_dir)
    if issues:
        raise VaultFrontmatterError(operation, issues)


def _enforce_sources_frontmatter(
    source_paths: list[Path],
    vault_dir: Path,
    *,
    operation: str,
) -> None:
    issues: list[VaultFrontmatterIssue] = []
    for source_path in source_paths:
        issues.extend(_source_frontmatter_issues(source_path, vault_dir))
    if issues:
        raise VaultFrontmatterError(operation, issues)


def create_concept_page(
    entity: ExtractedEntity,
    source_path: str,
    vault_dir: Path,
) -> Path:
    """Create a new concept page in concepts/ folder."""
    concepts_dir = vault_dir / "concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)

    page_path = concepts_dir / f"{entity.slug}.md"

    source_stem = Path(source_path).stem if source_path else "unknown"

    claims_text = ""
    if entity.source_claims:
        claims_text = "\n".join(f"- {c}" for c in entity.source_claims)
    else:
        claims_text = f"- Referenced in [[{source_stem}]]"

    content = f"""---
aliases: ["{entity.name}"]
tags: [concept, auto-compiled, {entity.entity_type}]
status: current
date: {_today()}
related:
  - "[[{source_stem}]]"
compiled_from:
  - "[[{source_stem}]]"
summary: "{entity.description or entity.name}"
---

# {entity.name}

{entity.description}

## From [[{source_stem}]] ({_today()})

{claims_text}
"""
    page_path.write_text(content.strip() + "\n", encoding="utf-8")
    return page_path


def update_concept_page(
    entity: ExtractedEntity,
    source_path: str,
    page_path: Path,
) -> None:
    """Append a new source section to an existing concept page."""
    source_stem = Path(source_path).stem if source_path else "unknown"
    existing = page_path.read_text(encoding="utf-8")

    # Don't double-add from the same source
    if f"From [[{source_stem}]]" in existing:
        return

    claims_text = ""
    if entity.source_claims:
        claims_text = "\n".join(f"- {c}" for c in entity.source_claims)
    else:
        claims_text = f"- Referenced in [[{source_stem}]]"

    new_section = f"""

## From [[{source_stem}]] ({_today()})

{claims_text}
"""
    page_path.write_text(existing.rstrip() + "\n" + new_section.strip() + "\n", encoding="utf-8")

    # Update frontmatter: add source to compiled_from and related
    updated = page_path.read_text(encoding="utf-8")
    if f"[[{source_stem}]]" not in updated.split("---")[1] if "---" in updated else "":
        # Add to compiled_from
        updated = re.sub(
            r"(compiled_from:\n)",
            f'\\1  - "[[{source_stem}]]"\n',
            updated,
        )
        # Add to related
        if f'  - "[[{source_stem}]]"' not in updated:
            updated = re.sub(
                r"(related:\n)",
                f'\\1  - "[[{source_stem}]]"\n',
                updated,
            )
        page_path.write_text(updated, encoding="utf-8")


def update_source_frontmatter(
    source_path: Path,
    concept_names: list[str],
) -> None:
    """Add compiled concept pages to source note's related: frontmatter."""
    if not source_path.exists():
        return

    content = source_path.read_text(encoding="utf-8")
    fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        return

    fm_text = fm_match.group(1)
    for name in concept_names:
        slug = ExtractedEntity(name=name).slug
        link = f'  - "[[{slug}]]"'
        if link not in fm_text:
            fm_text = re.sub(
                r"(related:\n)",
                f"\\1{link}\n",
                fm_text,
            )

    new_content = f"---\n{fm_text}\n---{content[fm_match.end():]}"
    source_path.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

def check_contradictions(page_path: Path) -> list[Contradiction]:
    """Scan a concept page for potentially conflicting claims across sources.

    Heuristic: look for negation patterns and opposing claim structures
    from different source sections.
    """
    content = page_path.read_text(encoding="utf-8")
    page_name = page_path.stem

    # Parse source sections: "## From [[source]] (date)"
    section_re = re.compile(
        r"## From \[\[([^\]]+)\]\] \(([^)]+)\)\n(.*?)(?=\n## |\Z)",
        re.DOTALL,
    )
    sections = section_re.findall(content)
    if len(sections) < 2:
        return []

    contradictions: list[Contradiction] = []

    # Extract claims per source
    source_claims: list[tuple[str, list[str]]] = []
    for source, _date, body in sections:
        claims = [
            line.lstrip("- ").strip()
            for line in body.strip().split("\n")
            if line.strip().startswith("-") and len(line.strip()) > 10
        ]
        if claims:
            source_claims.append((source, claims))

    # Compare claims across sources for contradiction signals
    negation_words = {"not", "no", "never", "neither", "don't", "doesn't", "isn't", "aren't", "won't", "shouldn't", "cannot"}
    opposite_pairs = [
        ("always", "never"), ("default", "optional"), ("required", "optional"),
        ("preferred", "deprecated"), ("recommended", "avoid"),
        ("enabled", "disabled"), ("true", "false"),
    ]

    for i in range(len(source_claims)):
        for j in range(i + 1, len(source_claims)):
            src_a, claims_a = source_claims[i]
            src_b, claims_b = source_claims[j]

            for ca in claims_a:
                ca_words = set(ca.lower().split())
                for cb in claims_b:
                    cb_words = set(cb.lower().split())

                    # Check for negation asymmetry
                    a_negated = bool(ca_words & negation_words)
                    b_negated = bool(cb_words & negation_words)

                    # Shared significant words (content overlap)
                    shared = (ca_words & cb_words) - {"the", "a", "is", "are", "in", "of", "to", "and", "for", "with", "it", "this", "that"}
                    if len(shared) < 2:
                        continue

                    # Contradiction signal: same topic, different negation
                    if a_negated != b_negated:
                        contradictions.append(Contradiction(
                            concept_page=page_name,
                            claim_a=ca, source_a=src_a,
                            claim_b=cb, source_b=src_b,
                            severity="direct",
                        ))
                        continue

                    # Check for opposite word pairs
                    for word_a, word_b in opposite_pairs:
                        if (word_a in ca.lower() and word_b in cb.lower()) or \
                           (word_b in ca.lower() and word_a in cb.lower()):
                            contradictions.append(Contradiction(
                                concept_page=page_name,
                                claim_a=ca, source_a=src_a,
                                claim_b=cb, source_b=src_b,
                                severity="tension",
                            ))
                            break

    return contradictions


def insert_contradiction_callouts(page_path: Path, contradictions: list[Contradiction]) -> None:
    """Insert Obsidian callout blocks for detected contradictions."""
    if not contradictions:
        return

    content = page_path.read_text(encoding="utf-8")
    callouts = []
    for c in contradictions:
        callout = (
            f"\n> [!warning] Contradiction ({c.severity})\n"
            f"> **[[{c.source_a}]]** says: \"{c.claim_a}\"\n"
            f"> **[[{c.source_b}]]** says: \"{c.claim_b}\"\n"
            f"> *Flagged during compilation on {_today()}*\n"
        )
        # Don't duplicate
        if callout.strip() not in content:
            callouts.append(callout)

    if callouts:
        content = content.rstrip() + "\n\n## Contradictions\n" + "\n".join(callouts) + "\n"
        page_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Compilation pipeline
# ---------------------------------------------------------------------------

@dataclass
class DetectedConnection:
    """A detected connection between two entities with type classification."""

    entity_a: ExtractedEntity
    entity_b: ExtractedEntity
    evidence: list[str]
    connection_type: str = "shared-context"  # shared-context | comparison | dependency | contradiction


_DEPENDENCY_WORDS = {"uses", "requires", "depends", "built", "powered", "leverages", "wraps", "extends"}
_NEGATION_WORDS = {"not", "no", "never", "don't", "doesn't", "isn't", "aren't", "won't"}


def _classify_connection(a: ExtractedEntity, b: ExtractedEntity, shared: list[str]) -> str:
    """Classify the connection type based on entity properties and shared evidence."""
    shared_text = " ".join(shared).lower()

    # Contradiction: shared claims with negation asymmetry
    for claim_pair in shared:
        if "<->" in claim_pair:
            parts = claim_pair.split("<->")
            if len(parts) == 2:
                left_neg = bool(set(parts[0].lower().split()) & _NEGATION_WORDS)
                right_neg = bool(set(parts[1].lower().split()) & _NEGATION_WORDS)
                if left_neg != right_neg:
                    return "contradiction"

    # Dependency: claims contain dependency language
    if any(w in shared_text for w in _DEPENDENCY_WORDS):
        return "dependency"

    # Comparison: both entities have the same entity_type
    if a.entity_type == b.entity_type and a.entity_type != "concept":
        return "comparison"

    return "shared-context"


def _detect_connections(entities: list[ExtractedEntity]) -> list[DetectedConnection]:
    """Detect pairs of entities that share claims or have overlapping content.

    Returns list of DetectedConnection with type classification.
    """
    connections: list[DetectedConnection] = []

    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            a, b = entities[i], entities[j]
            shared: list[str] = []

            # Check for shared significant words in claims
            for claim_a in a.source_claims:
                a_words = set(claim_a.lower().split()) - {"the", "a", "is", "are", "in", "of", "to", "and", "for", "with", "it", "this", "that"}
                for claim_b in b.source_claims:
                    b_words = set(claim_b.lower().split()) - {"the", "a", "is", "are", "in", "of", "to", "and", "for", "with", "it", "this", "that"}
                    overlap = a_words & b_words
                    if len(overlap) >= 3:
                        shared.append(f"{claim_a} <-> {claim_b}")

            # Check if one entity is mentioned in the other's description
            if a.name.lower() in b.description.lower() or b.name.lower() in a.description.lower():
                shared.append(f"{a.name} mentioned in {b.name}'s description")

            if shared:
                conn_type = _classify_connection(a, b, shared)
                connections.append(DetectedConnection(a, b, shared, conn_type))

    return connections


def create_connection_article(
    entity_a: ExtractedEntity,
    entity_b: ExtractedEntity,
    evidence: list[str],
    source_path: str,
    vault_dir: Path,
    connection_type: str = "shared-context",
) -> Path | None:
    """Create a connection article linking two concepts."""
    connections_dir = vault_dir / "connections"
    connections_dir.mkdir(parents=True, exist_ok=True)

    slug_a = entity_a.slug
    slug_b = entity_b.slug
    filename = f"{slug_a}--{slug_b}.md"
    page_path = connections_dir / filename

    # Don't create if already exists
    if page_path.exists():
        return None
    # Check reverse direction too
    reverse = connections_dir / f"{slug_b}--{slug_a}.md"
    if reverse.exists():
        return None

    source_stem = Path(source_path).stem if source_path else "unknown"
    evidence_text = "\n".join(f"- {e}" for e in evidence[:5])

    # Base content
    content = f"""---
title: "Connection: {entity_a.name} and {entity_b.name}"
tags: [connection, auto-compiled]
connection_type: {connection_type}
date: {_today()}
connects:
  - "[[{slug_a}]]"
  - "[[{slug_b}]]"
sources:
  - "[[{source_stem}]]"
created: {_today()}
---

# Connection: {entity_a.name} and {entity_b.name}

## The Connection

{entity_a.name} and {entity_b.name} are related through {connection_type.replace("-", " ")} in [[{source_stem}]].

## Evidence

{evidence_text}
"""

    # Comparison type gets extra sections
    if connection_type == "comparison":
        desc_a = entity_a.description[:100] if entity_a.description else "—"
        desc_b = entity_b.description[:100] if entity_b.description else "—"
        content += f"""
## Dimensions of Comparison

| Dimension | {entity_a.name} | {entity_b.name} |
|-----------|{'-' * max(3, len(entity_a.name))}|{'-' * max(3, len(entity_b.name))}|
| Type | {entity_a.entity_type} | {entity_b.entity_type} |
| Description | {desc_a} | {desc_b} |
| Sources | {len(entity_a.source_claims)} claims | {len(entity_b.source_claims)} claims |

## Key Differences

*Auto-populated from divergent claims — review and refine manually.*
"""

    content += f"""
## Related Concepts

- [[{slug_a}]] — {entity_a.description[:100] if entity_a.description else entity_a.name}
- [[{slug_b}]] — {entity_b.description[:100] if entity_b.description else entity_b.name}
"""
    page_path.write_text(content.strip() + "\n", encoding="utf-8")
    return page_path


def _append_build_log(report: CompilationReport, source_path: str, vault_dir: Path) -> None:
    """Append a compilation record to the build log."""
    log_path = vault_dir / "concepts" / "BUILD-LOG.md"

    if not log_path.exists():
        (vault_dir / "concepts").mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "---\ntags: [build-log, auto-compiled]\nsummary: \"Chronological record of entity compilation runs.\"\n---\n\n"
            "# Build Log\n\n",
            encoding="utf-8",
        )

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    source_stem = Path(source_path).stem if source_path else "unknown"

    created = [Path(p).stem for p in report.pages_created]
    updated = [Path(p).stem for p in report.pages_updated]
    connections = [Path(p).stem for p in report.connections_created]

    entry = f"## [{now}] Source: [[{source_stem}]]\n"
    if created:
        entry += f"- Created: {', '.join(created)}\n"
    if updated:
        entry += f"- Updated: {', '.join(updated)}\n"
    if connections:
        entry += f"- Connections: {', '.join(connections)}\n"
    if report.contradictions_found:
        entry += f"- Contradictions: {len(report.contradictions_found)}\n"
    entry += f"- Entities: {report.entities_processed} processed, {report.entities_skipped} skipped\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)

    _rotate_build_log_if_needed(vault_dir)


def _rotate_build_log_if_needed(vault_dir: Path, max_entries: int = 500) -> None:
    """Rotate BUILD-LOG.md when it exceeds max_entries compilation records."""
    log_path = vault_dir / "concepts" / "BUILD-LOG.md"
    if not log_path.exists():
        return

    try:
        content = log_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    entry_count = len(re.findall(r"^## \[", content, re.MULTILINE))
    if entry_count <= max_entries:
        return

    from datetime import datetime
    year = datetime.now().strftime("%Y")
    rotated = vault_dir / "concepts" / f"BUILD-LOG-{year}.md"

    # Don't overwrite an existing rotated log — append year suffix
    if rotated.exists():
        rotated = vault_dir / "concepts" / f"BUILD-LOG-{year}-{entry_count}.md"

    log_path.rename(rotated)

    # Create fresh BUILD-LOG.md
    log_path.write_text(
        "---\ntags: [build-log, auto-compiled]\nsummary: \"Chronological record of entity compilation runs.\"\n---\n\n"
        f"# Build Log\n\n> Rotated {entry_count} entries to [[{rotated.stem}]] on {_today()}\n\n",
        encoding="utf-8",
    )


def _collect_concept_entries(concepts_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Parse all concept pages in concepts_dir, grouping by entity type.

    Returns a dict mapping entity_type -> list of (slug, summary) tuples,
    sorted alphabetically (glob("*.md") yields sorted paths). Skips BUILD-LOG
    variants and INDEX files.

    Shared helper for both `generate_index()` (concepts-only catalog at
    concepts/INDEX.md) and `generate_root_index()` (whole-wiki catalog at
    vault-root INDEX.md).
    """
    entries: dict[str, list[tuple[str, str]]] = {}
    if not concepts_dir.exists():
        return entries

    skip_names = {"BUILD-LOG", "INDEX"}
    for md_file in sorted(concepts_dir.glob("*.md")):
        if md_file.stem in skip_names or md_file.stem.startswith("BUILD-LOG"):
            continue

        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        fm = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not fm:
            continue

        fm_text = fm.group(1)

        summary_m = re.search(r'summary:\s*"([^"]*)"', fm_text)
        summary = summary_m.group(1) if summary_m else md_file.stem.replace("-", " ").title()

        # Extract entity_type from tags (skip concept/auto-compiled/build-log/connection)
        tags_m = re.search(r"tags:\s*\[([^\]]+)\]", fm_text)
        entity_type = "concept"
        if tags_m:
            tags = [t.strip() for t in tags_m.group(1).split(",")]
            for t in tags:
                if t not in ("concept", "auto-compiled", "build-log", "connection"):
                    entity_type = t
                    break

        entries.setdefault(entity_type, []).append((md_file.stem, summary))

    return entries


def generate_index(vault_dir: Path) -> Path:
    """Generate concepts/INDEX.md — a static catalog of all concept pages.

    Groups entries by entity_type, sorted alphabetically.
    Works without Obsidian (plain markdown).
    """
    concepts_dir = vault_dir / "concepts"
    index_path = concepts_dir / "INDEX.md"
    if not concepts_dir.exists():
        concepts_dir.mkdir(parents=True, exist_ok=True)
        index_path.write_text("# Concept Index\n\n**0 concepts** | Last updated: n/a\n", encoding="utf-8")
        return index_path

    entries = _collect_concept_entries(concepts_dir)

    # Build the index
    total = sum(len(v) for v in entries.values())
    lines = [
        "---",
        "tags: [system, auto-compiled]",
        f"date: {_today()}",
        'summary: "Auto-generated concept catalog — static, works without Obsidian."',
        "---",
        "",
        "# Concept Index",
        "",
        f"**{total} concepts** | Last updated: {_today()}",
        "",
    ]

    for etype in sorted(entries.keys()):
        items = entries[etype]
        lines.append(f"## {etype.title()}")
        lines.append("")

        # Split alphabetically at 50+ entries
        if len(items) > 50:
            midpoint = len(items) // 2
            for i, (slug, summary) in enumerate(items):
                if i == 0:
                    lines.append(f"### A–{items[midpoint - 1][0][0]}")
                    lines.append("")
                elif i == midpoint:
                    lines.append("")
                    lines.append(f"### {items[midpoint][0][0]}–Z")
                    lines.append("")
                lines.append(f"- [[{slug}]] — {summary}")
        else:
            for slug, summary in items:
                lines.append(f"- [[{slug}]] — {summary}")

        lines.append("")

    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


# Root INDEX.md catalog shape — hard-coded canonical files at vault root.
# Missing files are silently skipped (fail-open — vault evolves over time).
_ROOT_IDENTITY_FILES: list[tuple[str, str]] = [
    ("SOUL", "AI personality and behavioral rules"),
    ("USER", "user profile, accounts, preferences"),
    ("SELF", "self-model"),
    ("MEMORY", "curated long-term memory"),
    ("GOALS", "quarterly objectives"),
    ("SCHEMA", "vault tag taxonomy and conventions"),
    ("SAFETY", "safety rules"),
    ("BACKLOG", "backlog"),
    ("HEARTBEAT", "heartbeat checklist"),
]

_ROOT_DIRECTORY_DESCRIPTIONS: dict[str, str] = {
    "daily": "session and heartbeat logs",
    "weekly": "weekly synthesis",
    "concepts": "auto-compiled entity pages (see concepts/INDEX.md)",
    "connections": "cross-domain connection articles",
    "drafts": "in-progress writing",
    "research": "research notes",
    "raw": "immutable original sources",
    "teams": "team session memory",
    "finances": "personal finance notes",
    "docs": "reference documentation",
    "hub": "hub/project pages",
    "books": "book notes and summaries",
    "playbooks": "operational playbooks",
}

# Max concept entries per type in root INDEX.md before falling back to
# a "→ +N more in [[concepts/INDEX]]" pointer. Keeps the root file
# scannable even as the vault grows past several hundred concepts.
_ROOT_INDEX_MAX_PER_TYPE = 25


def generate_root_index(vault_dir: Path) -> Path:
    """Generate {vault_dir}/INDEX.md — whole-wiki catalog at the vault root.

    Karpathy "LLM Wiki" root index pattern — a single first-read surface that
    an LLM can glance at to understand the shape of the wiki in one pass.
    Covers:
      - Identity / canonical files (SOUL, USER, SELF, MEMORY, ...)
      - Maps of Content (MOC-*.md at vault root)
      - Concepts by Type (reuses _collect_concept_entries, capped per type)
      - Top-level directories (excludes leading-underscore private dirs)

    Complements concepts/INDEX.md (which is the concept-only drill-down).
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    index_path = vault_dir / "INDEX.md"

    # --- Identity section (fail-open on missing files) ---
    identity_lines: list[str] = []
    canonical_count = 0
    for stem, default_desc in _ROOT_IDENTITY_FILES:
        md = vault_dir / f"{stem}.md"
        if not md.exists():
            continue
        summary = _read_summary(md) or default_desc
        identity_lines.append(f"- [[{stem}]] — {summary}")
        canonical_count += 1

    # --- Maps of Content section (glob MOC-*.md at vault root) ---
    moc_lines: list[str] = []
    for moc_file in sorted(vault_dir.glob("MOC-*.md")):
        summary = _read_summary(moc_file) or moc_file.stem.replace("-", " ").title()
        moc_lines.append(f"- [[{moc_file.stem}]] — {summary}")
        canonical_count += 1

    # --- Concepts by Type (reuse shared helper, cap per type) ---
    concepts_dir = vault_dir / "concepts"
    entries = _collect_concept_entries(concepts_dir)
    total_concepts = sum(len(v) for v in entries.values())

    concept_lines: list[str] = []
    for etype in sorted(entries.keys()):
        items = entries[etype]
        concept_lines.append(f"### {etype.title()} ({len(items)})")
        concept_lines.append("")
        shown = items[:_ROOT_INDEX_MAX_PER_TYPE]
        for slug, summary in shown:
            # Bare [[SLUG]] — Obsidian resolves globally via shortest-path and
            # the vault_lint resolver is filename-only. Concept slugs are
            # globally unique so path qualification isn't needed.
            concept_lines.append(f"- [[{slug}]] — {summary}")
        overflow = len(items) - len(shown)
        if overflow > 0:
            concept_lines.append(
                f"- *(+{overflow} more — see [[INDEX|concepts/INDEX]])*"
            )
        concept_lines.append("")

    # --- Directories (enumerate top-level subdirs, skip private + concepts) ---
    skip_dirs = {
        "concepts",       # already covered above
        "_archive", "_canvas", "_dashboards", "_state", "_templates",
        "_ops", ".obsidian", ".conversations", ".conversations-archived",
        ".nexus", ".workspaces", ".workspaces-archived", "archive",
    }
    dir_lines: list[str] = []
    dir_count = 0
    try:
        subdirs = sorted(
            p for p in vault_dir.iterdir()
            if p.is_dir() and p.name not in skip_dirs and not p.name.startswith(".")
        )
    except OSError:
        subdirs = []
    for sub in subdirs:
        desc = _ROOT_DIRECTORY_DESCRIPTIONS.get(sub.name, "")
        if desc:
            dir_lines.append(f"- `{sub.name}/` — {desc}")
        else:
            dir_lines.append(f"- `{sub.name}/`")
        dir_count += 1

    # --- Assemble ---
    today = _today()
    lines = [
        "---",
        "tags: [system, auto-compiled]",
        f"date: {today}",
        'summary: "Auto-generated root catalog — every page in the wiki, one link, one line."',
        "---",
        "",
        "# Wiki Index",
        "",
        f"**{total_concepts} concepts | {canonical_count} canonical | {dir_count} directories** | Last updated: {today}",
        "",
    ]

    if identity_lines:
        lines.append("## Identity")
        lines.append("")
        lines.extend(identity_lines)
        lines.append("")

    if moc_lines:
        lines.append("## Maps of Content")
        lines.append("")
        lines.extend(moc_lines)
        lines.append("")

    if concept_lines:
        lines.append("## Concepts by Type")
        lines.append("")
        lines.extend(concept_lines)

    if dir_lines:
        lines.append("## Directories")
        lines.append("")
        lines.extend(dir_lines)
        lines.append("")

    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


def compile_entities(
    entities: list[ExtractedEntity],
    source_path: str,
    vault_dir: Path,
    memory_dir: Path | None = None,
    event_type: str = "compile",
) -> CompilationReport:
    """Main compilation: for each entity, find or create concept page, check contradictions."""
    report = CompilationReport()

    # Filter by confidence threshold (stricter for daily logs)
    effective_threshold = _DAILY_LOG_THRESHOLD if _is_daily_log(source_path) else CONFIDENCE_THRESHOLD
    eligible = [e for e in entities if e.confidence >= effective_threshold]
    report.entities_skipped = len(entities) - len(eligible)
    report.entities_processed = len(eligible)

    if eligible:
        _enforce_source_frontmatter(source_path, vault_dir, operation=event_type)

    concept_names: list[str] = []

    for entity in eligible:
        existing = find_existing_concept(entity.name, vault_dir)

        if existing:
            update_concept_page(entity, source_path, existing)
            report.pages_updated.append(str(existing))

            # Check for contradictions on updated page
            contras = check_contradictions(existing)
            if contras:
                insert_contradiction_callouts(existing, contras)
                report.contradictions_found.extend(contras)
        else:
            page = create_concept_page(entity, source_path, vault_dir)
            report.pages_created.append(str(page))

        concept_names.append(entity.name)

    # Detect and create connection articles between related entities
    connections = _detect_connections(eligible)
    for conn in connections:
        conn_path = create_connection_article(
            conn.entity_a, conn.entity_b, conn.evidence,
            source_path, vault_dir, connection_type=conn.connection_type,
        )
        if conn_path:
            report.connections_created.append(str(conn_path))

    # Update source note's related: frontmatter
    src = Path(source_path)
    if src.exists():
        update_source_frontmatter(src, concept_names)

    # Reindex modified files
    if memory_dir:
        try:
            from recall_service import reindex_file
            all_paths = [Path(p) for p in report.pages_created + report.pages_updated + report.connections_created]
            if src.exists():
                all_paths.append(src)
            for p in all_paths:
                if p.exists():
                    reindex_file(p, memory_dir)
                    report.files_reindexed += 1
        except Exception:
            pass  # Reindex is best-effort

    # Append to build log
    if report.pages_created or report.pages_updated or report.connections_created:
        try:
            _append_build_log(report, source_path, vault_dir)
        except Exception:
            pass  # Build log is best-effort

    # Regenerate concept index (best-effort)
    if report.pages_created or report.pages_updated:
        try:
            generate_index(vault_dir)
        except Exception:
            pass

        # Regenerate root INDEX.md — whole-wiki catalog at vault root
        try:
            generate_root_index(vault_dir)
        except Exception:
            pass

    # Append to vault log — chronological record of wiki-evolution events
    if report.pages_created or report.pages_updated or report.connections_created:
        try:
            source_stem = Path(source_path).stem if source_path else "unknown"
            bullets = [
                f"source: [[{source_stem}]]",
                f"entities: {report.entities_processed} processed, {report.entities_skipped} skipped",
            ]
            if report.pages_created:
                bullets.append(f"pages: +{len(report.pages_created)} created")
            if report.pages_updated:
                bullets.append(f"pages: ~{len(report.pages_updated)} updated")
            if report.connections_created:
                bullets.append(f"connections: +{len(report.connections_created)}")
            if report.contradictions_found:
                bullets.append(f"contradictions: {len(report.contradictions_found)}")
            append_vault_log(vault_dir, event_type, source_stem, bullets=bullets)
        except Exception:
            pass  # Vault log is best-effort

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Backfill and sweep
# ---------------------------------------------------------------------------

_SKIP_DIRS = {
    "concepts", "connections", "qa", "raw",
    "_templates", "_dashboards", "_canvas", "_ops", ".obsidian",
    "daily", "weekly",
    "_state", "archive", "finances", "drafts", "teams",
    ".conversations", ".conversations-archived",
    ".nexus", ".workspaces", ".workspaces-archived",
}


def _is_compiled(file_path: Path, vault_dir: Path) -> bool:
    """Check if a file has already been compiled (has concept pages referencing it)."""
    concepts_dir = vault_dir / "concepts"
    if not concepts_dir.exists():
        return False
    stem = file_path.stem
    for concept_file in concepts_dir.glob("*.md"):
        try:
            text = concept_file.read_text(encoding="utf-8")
            if f"[[{stem}]]" in text:
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def backfill_vault(
    vault_dir: Path,
    memory_dir: Path | None = None,
    skip_compiled: bool = True,
    dry_run: bool = False,
    operation: str = "backfill",
) -> dict[str, int]:
    """Compile entities from all uncompiled vault notes.

    Scans all .md files in the vault (excluding concepts/, raw/, _templates/, etc.),
    extracts entities via heuristic, and compiles concept pages.
    """
    totals = {"files_scanned": 0, "files_compiled": 0, "files_skipped": 0,
              "pages_created": 0, "pages_updated": 0, "contradictions": 0}

    # Load schema once for the entire backfill run
    schema = load_schema(vault_dir)

    candidates: list[Path] = []
    for md_file in sorted(vault_dir.rglob("*.md")):
        # Skip infrastructure dirs
        rel_parts = md_file.relative_to(vault_dir).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        # Skip very small files
        if md_file.stat().st_size < 100:
            continue

        totals["files_scanned"] += 1

        if skip_compiled and _is_compiled(md_file, vault_dir):
            totals["files_skipped"] += 1
            continue

        candidates.append(md_file)

    _enforce_sources_frontmatter(candidates, vault_dir, operation=operation)

    for md_file in candidates:
        if dry_run:
            content = md_file.read_text(encoding="utf-8")
            entities = extract_entities_heuristic(content, str(md_file), schema=schema)
            eligible = [e for e in entities if e.confidence >= CONFIDENCE_THRESHOLD]
            if eligible:
                print(f"  [dry-run] {md_file.relative_to(vault_dir)} -> {len(eligible)} entities")
                totals["files_compiled"] += 1
            continue

        content = md_file.read_text(encoding="utf-8")
        entities = extract_entities_heuristic(content, str(md_file), schema=schema)

        if not any(e.confidence >= CONFIDENCE_THRESHOLD for e in entities):
            totals["files_skipped"] += 1
            continue

        report = compile_entities(entities, str(md_file), vault_dir, memory_dir)
        totals["files_compiled"] += 1
        totals["pages_created"] += len(report.pages_created)
        totals["pages_updated"] += len(report.pages_updated)
        totals["contradictions"] += len(report.contradictions_found)

        rel = md_file.relative_to(vault_dir)
        print(f"  {rel}: +{len(report.pages_created)} created, ~{len(report.pages_updated)} updated")

    # Regenerate index once after all files processed
    if not dry_run and (totals["pages_created"] or totals["pages_updated"]):
        try:
            idx = generate_index(vault_dir)
            print(f"  Index regenerated: {idx.relative_to(vault_dir)}")
        except Exception:
            pass

    return totals


def sweep_uncompiled(
    vault_dir: Path,
    memory_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Find vault notes without concept coverage and compile them.

    Lighter than backfill — only processes notes that have zero concept page references.
    Designed to run on a schedule (e.g., nightly cron).
    """
    return backfill_vault(
        vault_dir,
        memory_dir,
        skip_compiled=True,
        dry_run=dry_run,
        operation="sweep",
    )


def find_archivable(vault_dir: Path, days_threshold: int = 180) -> list[Path]:
    """Find concept pages with no inbound links from non-concept files AND stale dates."""
    concepts_dir = vault_dir / "concepts"
    if not concepts_dir.exists():
        return []

    cutoff = (date.today() - timedelta(days=days_threshold)).isoformat()

    # Build set of slugs referenced from non-concept/non-connection files
    referenced: set[str] = set()
    wikilink_re = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
    for md in sorted(vault_dir.rglob("*.md")):
        rel = md.relative_to(vault_dir)
        if rel.parts and rel.parts[0] in ("concepts", "connections"):
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in wikilink_re.finditer(content):
            referenced.add(m.group(1).strip())

    archivable = []
    for md in sorted(concepts_dir.glob("*.md")):
        if md.stem.startswith("BUILD-LOG") or md.stem == "INDEX":
            continue

        # Check if orphan
        if md.stem in referenced:
            continue

        # Check if stale
        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        date_m = re.search(r"date:\s*(\d{4}-\d{2}-\d{2})", content)
        if date_m and date_m.group(1) < cutoff:
            archivable.append(md)

    return archivable


def archive_concept(page_path: Path, vault_dir: Path) -> None:
    """Move a concept page to _archive/concepts/, regenerate INDEX.md, update backlinks."""
    archive_dir = vault_dir / "_archive" / "concepts"
    archive_dir.mkdir(parents=True, exist_ok=True)

    dest = archive_dir / page_path.name
    page_path.rename(dest)

    # Update wikilinks in other vault files: [[SLUG]] -> [[SLUG]] (archived)
    slug = page_path.stem
    for md in vault_dir.rglob("*.md"):
        if md == dest:
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f"[[{slug}]]" in content:
            updated = content.replace(f"[[{slug}]]", f"{slug} (archived)")
            md.write_text(updated, encoding="utf-8")

    # Regenerate index
    try:
        generate_index(vault_dir)
    except Exception:
        pass


def compile_single_log(
    log_path: Path,
    vault_dir: Path,
    memory_dir: Path | None = None,
) -> CompilationReport | None:
    """Compile entities from a single daily/weekly log. Used by reflection and synthesis hooks."""
    if not log_path.exists() or log_path.stat().st_size < 200:
        return None

    content = log_path.read_text(encoding="utf-8")
    entities = extract_entities_heuristic(content, str(log_path))

    # Daily logs use stricter threshold to filter operational noise
    effective_threshold = _DAILY_LOG_THRESHOLD if _is_daily_log(str(log_path)) else CONFIDENCE_THRESHOLD
    if not any(e.confidence >= effective_threshold for e in entities):
        return None

    return compile_entities(entities, str(log_path), vault_dir, memory_dir)


def _print_entities_json(entities: list[ExtractedEntity]) -> None:
    """Print entities as JSON to stdout."""
    data = [asdict(e) for e in entities]
    print(json.dumps(data, indent=2))


def _print_report(report: CompilationReport) -> None:
    """Print compilation report."""
    print(f"\n=== Compilation Report ===")
    print(f"Entities processed: {report.entities_processed}")
    print(f"Entities skipped (below {CONFIDENCE_THRESHOLD} confidence): {report.entities_skipped}")
    print(f"Pages created: {len(report.pages_created)}")
    for p in report.pages_created:
        print(f"  + {p}")
    print(f"Pages updated: {len(report.pages_updated)}")
    for p in report.pages_updated:
        print(f"  ~ {p}")
    if report.connections_created:
        print(f"Connections created: {len(report.connections_created)}")
        for p in report.connections_created:
            print(f"  <> {p}")
    if report.contradictions_found:
        print(f"Contradictions flagged: {len(report.contradictions_found)}")
        for c in report.contradictions_found:
            print(f"  ! {c.concept_page}: [{c.source_a}] vs [{c.source_b}] ({c.severity})")
    print(f"Files reindexed: {report.files_reindexed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Entity extraction and compilation for vault knowledge graph")
    sub = parser.add_subparsers(dest="command", required=True)

    # extract
    p_extract = sub.add_parser("extract", help="Extract entities from a source document")
    p_extract.add_argument("source", help="Path to source markdown file")

    # compile
    p_compile = sub.add_parser("compile", help="Extract entities and compile concept pages")
    p_compile.add_argument("source", nargs="?", help="Path to source markdown file")
    p_compile.add_argument("--entities", help="Path to JSON file with pre-extracted entities")
    p_compile.add_argument("--vault-dir", required=True, help="Path to vault root")
    p_compile.add_argument("--memory-dir", help="Path to memory dir for reindexing")

    # contradictions
    p_contra = sub.add_parser("contradictions", help="Check a concept page for contradictions")
    p_contra.add_argument("page", help="Path to concept page")

    # backfill
    p_backfill = sub.add_parser("backfill", help="Compile all uncompiled vault notes")
    p_backfill.add_argument("--vault-dir", required=True, help="Path to vault root")
    p_backfill.add_argument("--memory-dir", help="Path to memory dir for reindexing")
    p_backfill.add_argument("--include-compiled", action="store_true", help="Re-compile even already-compiled notes")
    p_backfill.add_argument("--dry-run", action="store_true", help="Preview without writing")

    # sweep
    p_sweep = sub.add_parser("sweep", help="Find and compile notes without concept coverage")
    p_sweep.add_argument("--vault-dir", required=True, help="Path to vault root")
    p_sweep.add_argument("--memory-dir", help="Path to memory dir for reindexing")
    p_sweep.add_argument("--dry-run", action="store_true", help="Preview without writing")

    # index
    p_index = sub.add_parser("index", help="Regenerate concepts/INDEX.md")
    p_index.add_argument("--vault-dir", required=True, help="Path to vault root")

    # lint
    p_lint = sub.add_parser("lint", help="Run vault health checks")
    p_lint.add_argument("--vault-dir", required=True, help="Path to vault root")
    p_lint.add_argument("--check", action="append", dest="checks", help="Run specific check(s)")
    p_lint.add_argument("--format", choices=["text", "json"], default="text")

    # archive
    p_archive = sub.add_parser("archive", help="Archive stale orphaned concept pages")
    p_archive.add_argument("--vault-dir", required=True, help="Path to vault root")
    p_archive.add_argument("--page", help="Archive a specific page by slug")
    p_archive.add_argument("--days", type=int, default=180, help="Staleness threshold in days")
    p_archive.add_argument("--dry-run", action="store_true", help="Preview without moving")

    # reindex
    p_reindex = sub.add_parser("reindex", help="Reindex a single file")
    p_reindex.add_argument("file", help="Path to file to reindex")
    p_reindex.add_argument("--memory-dir", required=True, help="Path to memory dir")

    # index-root — whole-wiki catalog at vault root (Karpathy LLM Wiki pattern)
    p_index_root = sub.add_parser(
        "index-root",
        help="Regenerate {vault}/INDEX.md — whole-wiki catalog (Karpathy root index)",
    )
    p_index_root.add_argument("--vault-dir", required=True, help="Path to vault root")

    # preserve-raw — copy a source into {vault}/raw/ as immutable archive
    p_preserve_raw = sub.add_parser(
        "preserve-raw",
        help="Copy source to {vault}/raw/ as immutable archive (Karpathy raw/ pattern)",
    )
    p_preserve_raw.add_argument("source", help="Path to source file to preserve")
    p_preserve_raw.add_argument("--vault-dir", required=True, help="Path to vault root")
    p_preserve_raw.add_argument(
        "--date-prefix",
        action="store_true",
        help="Always prefix destination with today's date (finance_ingest pattern). "
             "Default: collision-only prefix (vault-ingest pattern).",
    )
    p_preserve_raw.add_argument(
        "--on-collision",
        choices=["raise", "skip", "overwrite"],
        default="raise",
        help="What to do if the destination already exists: "
             "'raise' (default) fails loudly per the immutable-raw/ contract; "
             "'skip' returns the existing path unchanged (idempotent re-run); "
             "'overwrite' replaces the existing archive (legacy escape hatch).",
    )

    # fetch-url — gap-4 URL ingest: fetch + archive html+md to raw/clipped/, then compile
    p_fetch_url = sub.add_parser(
        "fetch-url",
        help="Fetch a URL, archive html+md to raw/clipped/, then compile entities",
    )
    p_fetch_url.add_argument("url", help="URL to fetch (https://...)")
    p_fetch_url.add_argument("--vault-dir", required=True, help="Path to vault root")
    p_fetch_url.add_argument("--memory-dir", help="Path to memory dir (defaults to vault-dir)")
    p_fetch_url.add_argument(
        "--no-compile",
        action="store_true",
        help="Archive only — skip the entity compilation step.",
    )

    args = parser.parse_args()

    if args.command == "extract":
        source = Path(args.source)
        if not source.exists():
            print(f"Error: {source} not found", file=sys.stderr)
            sys.exit(1)
        content = source.read_text(encoding="utf-8")
        entities = extract_entities_heuristic(content, str(source))
        _print_entities_json(entities)

    elif args.command == "compile":
        vault_dir = Path(args.vault_dir)
        memory_dir = Path(args.memory_dir) if args.memory_dir else None

        if args.entities:
            # Load pre-extracted entities from JSON
            with open(args.entities, encoding="utf-8") as f:
                raw = json.load(f)
            entities = [ExtractedEntity(**e) for e in raw]
            source_path = args.source or ""
        elif args.source:
            source = Path(args.source)
            if not source.exists():
                print(f"Error: {source} not found", file=sys.stderr)
                sys.exit(1)
            content = source.read_text(encoding="utf-8")
            entities = extract_entities_heuristic(content, str(source))
            source_path = str(source)
        else:
            print("Error: provide either source file or --entities JSON", file=sys.stderr)
            sys.exit(1)

        try:
            report = compile_entities(entities, source_path, vault_dir, memory_dir)
        except VaultFrontmatterError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        _print_report(report)

    elif args.command == "contradictions":
        page = Path(args.page)
        if not page.exists():
            print(f"Error: {page} not found", file=sys.stderr)
            sys.exit(1)
        contras = check_contradictions(page)
        if contras:
            print(json.dumps([asdict(c) for c in contras], indent=2))
        else:
            print("No contradictions detected.")

    elif args.command == "backfill":
        vault_dir = Path(args.vault_dir)
        memory_dir = Path(args.memory_dir) if args.memory_dir else None
        print(f"{'[DRY RUN] ' if args.dry_run else ''}Backfilling vault: {vault_dir}")
        try:
            totals = backfill_vault(
                vault_dir, memory_dir,
                skip_compiled=not args.include_compiled,
                dry_run=args.dry_run,
            )
        except VaultFrontmatterError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"\n=== Backfill {'Preview' if args.dry_run else 'Complete'} ===")
        print(f"Files scanned: {totals['files_scanned']}")
        print(f"Files compiled: {totals['files_compiled']}")
        print(f"Files skipped: {totals['files_skipped']}")
        print(f"Pages created: {totals['pages_created']}")
        print(f"Pages updated: {totals['pages_updated']}")
        if totals["contradictions"]:
            print(f"Contradictions flagged: {totals['contradictions']}")

    elif args.command == "sweep":
        vault_dir = Path(args.vault_dir)
        memory_dir = Path(args.memory_dir) if args.memory_dir else None
        print(f"{'[DRY RUN] ' if args.dry_run else ''}Sweeping for uncompiled notes: {vault_dir}")
        try:
            totals = sweep_uncompiled(vault_dir, memory_dir, dry_run=args.dry_run)
        except VaultFrontmatterError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"\n=== Sweep {'Preview' if args.dry_run else 'Complete'} ===")
        print(f"Files scanned: {totals['files_scanned']}")
        print(f"Files compiled: {totals['files_compiled']}")
        print(f"Files skipped: {totals['files_skipped']}")

    elif args.command == "index":
        vault_dir = Path(args.vault_dir)
        idx = generate_index(vault_dir)
        print(f"Index generated: {idx}")

    elif args.command == "lint":
        from vault_lint import run_lint as _run_lint
        vault_dir = Path(args.vault_dir)
        schema = load_schema(vault_dir)
        issues = _run_lint(vault_dir, schema=schema, checks=args.checks)
        if args.format == "json":
            import json as _json
            from dataclasses import asdict as _asdict
            print(_json.dumps([_asdict(i) for i in issues], indent=2))
        else:
            errors = [i for i in issues if i.severity == "error"]
            warnings = [i for i in issues if i.severity == "warning"]
            infos = [i for i in issues if i.severity == "info"]
            print(f"\n=== Vault Lint Report ===")
            print(f"Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(infos)}\n")
            for sev, group in [("ERROR", errors), ("WARNING", warnings), ("INFO", infos)]:
                if group:
                    print(f"--- {sev} ---")
                    for i in group:
                        print(f"  [{i.check}] {i.file}: {i.message}")
                    print()

    elif args.command == "archive":
        vault_dir = Path(args.vault_dir)
        if args.page:
            page_path = vault_dir / "concepts" / f"{args.page}.md"
            if not page_path.exists():
                print(f"Error: {page_path} not found", file=sys.stderr)
                sys.exit(1)
            if args.dry_run:
                print(f"  [dry-run] Would archive: {page_path.name}")
            else:
                archive_concept(page_path, vault_dir)
                print(f"  Archived: {args.page}")
        else:
            archivable = find_archivable(vault_dir, days_threshold=args.days)
            if not archivable:
                print("No archivable concept pages found.")
            else:
                for page in archivable:
                    if args.dry_run:
                        print(f"  [dry-run] Would archive: {page.stem}")
                    else:
                        archive_concept(page, vault_dir)
                        print(f"  Archived: {page.stem}")
                print(f"\n{'[DRY RUN] ' if args.dry_run else ''}{len(archivable)} pages {'would be ' if args.dry_run else ''}archived.")

    elif args.command == "reindex":
        from recall_service import reindex_file
        file_path = Path(args.file)
        memory_dir = Path(args.memory_dir)
        chunks = reindex_file(file_path, memory_dir)
        print(f"Reindexed {file_path.name}: {chunks} chunks")

    elif args.command == "index-root":
        vault_dir = Path(args.vault_dir)
        idx = generate_root_index(vault_dir)
        print(f"Root index generated: {idx}")

    elif args.command == "preserve-raw":
        source = Path(args.source)
        if not source.exists():
            print(f"Error: {source} not found", file=sys.stderr)
            sys.exit(1)
        vault_dir = Path(args.vault_dir)
        dest = preserve_raw(
            source,
            vault_dir,
            always_date_prefix=args.date_prefix,
            on_collision=args.on_collision,
        )
        print(dest)

    elif args.command == "fetch-url":
        from url_fetch import fetch_and_archive

        vault_dir = Path(args.vault_dir)
        try:
            html_path, md_path, content = fetch_and_archive(args.url, vault_dir)
        except Exception as e:
            print(f"Error fetching {args.url}: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Archived: {html_path}")
        print(f"Archived: {md_path}")
        if not args.no_compile:
            memory_dir = Path(args.memory_dir) if args.memory_dir else vault_dir
            md_text = md_path.read_text(encoding="utf-8")
            ents = extract_entities_heuristic(md_text, str(md_path))
            try:
                report = compile_entities(ents, str(md_path), vault_dir, memory_dir)
            except VaultFrontmatterError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            print(
                f"Compiled '{content.title or md_path.stem}': "
                f"{len(report.pages_created)} created, "
                f"{len(report.pages_updated)} updated, "
                f"{len(report.connections_created)} connections, "
                f"{len(report.contradictions_found)} contradictions."
            )


if __name__ == "__main__":
    main()
