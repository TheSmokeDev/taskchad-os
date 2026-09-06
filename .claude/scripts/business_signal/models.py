"""Core data types for the signal engine pipeline.

The legacy RSS/HARO lane intentionally keeps its small dataclasses.  The GEO
authority lane is a public-content boundary, so its wire objects use Pydantic
and reject unknown fields, unsafe URLs, stale timestamps, and ambiguous source
provenance before anything reaches the vault or a social queue.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AUTHORITY_PACKET_SCHEMA = "authority-signal/v1"

AuthoritySignalType = Literal[
    "platform_change",
    "practical_evidence",
    "repository_event",
]
AuthoritySeries = Literal[
    "GEO Signal",
    "GEO Tip",
    "Myth vs Receipt",
    "Citation Anatomy",
    "AI Search Teardown",
    "Stack Drop",
    "Repo Field Note",
    "Factory Floor / Dark Factory",
]
AuthoritySourceClass = Literal[
    "official_documentation",
    "primary_source",
    "repository",
    "vendor_research",
    "practitioner_self_report",
]
AuthorityEvidenceClass = Literal[
    "public_primary",
    "public_vendor_research",
    "public_practitioner_report",
    "verified_repository",
    "verified_operator_receipt",
]
AuthorityVisualMode = Literal[
    "educational_card",
    "receipt",
    "founder_editorial",
    "plain_scene",
]

_PUBLIC_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SIGNAL_ID_RE = re.compile(r"^as_[0-9]{8}_[0-9a-f]{16}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"(?:[A-Za-z]:\\Users\\|/home/|/Users/|\.homie[/\\])", re.IGNORECASE),
    re.compile(r"\b(?:client|project)[_-]?id\s*[:=]\s*\S+", re.IGNORECASE),
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {"api_key", "apikey", "auth", "key", "password", "secret", "signature", "token"}
)


def validate_public_source_url(value: str) -> str:
    """Return a normalized public HTTP(S) URL or raise ``ValueError``.

    Packet URLs are persisted and may later be shown to a model or operator.
    Local targets and credential-bearing query strings therefore fail closed.
    """

    normalized = str(value or "").strip()
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source URL is invalid") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("source URL must be an absolute public HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("source URL must not contain credentials")
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("source URL must not target a local host")
    try:
        import ipaddress

        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise ValueError("source URL must not target a private address")
    if port not in {None, 80, 443}:
        raise ValueError("source URL must use the standard HTTP(S) port")
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & _SENSITIVE_QUERY_KEYS:
        raise ValueError("source URL must not contain credential query parameters")
    return normalized


def assert_public_safe_text(value: str) -> str:
    """Reject obvious secret, private-path, and private-project identifiers."""

    text = str(value or "").strip()
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError("authority packet contains private or credential-like text")
    return text


def authority_dedup_key(*parts: str) -> str:
    """Build the stable SHA-256 dedup key used across research runs."""

    canonical = "\n".join(" ".join(str(part).lower().split()) for part in parts if part)
    if not canonical:
        raise ValueError("at least one non-empty value is required for a dedup key")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def authority_signal_id(dedup_key: str, observed_at: datetime) -> str:
    """Build a stable, date-scoped public signal identifier."""

    if not _SHA256_RE.fullmatch(dedup_key):
        raise ValueError("dedup_key must be a lowercase SHA-256 digest")
    return f"as_{observed_at:%Y%m%d}_{dedup_key[:16]}"


class _AuthorityModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class AuthorityClaim(_AuthorityModel):
    """One publishable claim bound to one exact public source."""

    text: str = Field(min_length=12, max_length=1_500)
    source_url: str = Field(min_length=10, max_length=2_048)
    source_title: str = Field(min_length=2, max_length=500)
    source_date: date | None = None
    source_class: AuthoritySourceClass
    primary_source: bool
    confidence: float = Field(ge=0.0, le=1.0)

    _safe_text = field_validator("text", "source_title")(assert_public_safe_text)
    _public_url = field_validator("source_url")(validate_public_source_url)

    @model_validator(mode="after")
    def _source_class_matches_primary_flag(self) -> AuthorityClaim:
        primary_classes = {"official_documentation", "primary_source", "repository"}
        expected = self.source_class in primary_classes
        if self.primary_source is not expected:
            raise ValueError("primary_source must agree with source_class")
        return self


class AuthorityVisualBrief(_AuthorityModel):
    """Argument-first visual specification consumed by the media lane."""

    mode: AuthorityVisualMode = "educational_card"
    eyebrow: str = Field(min_length=2, max_length=80)
    headline: str = Field(min_length=4, max_length=140)
    accent: str = Field(default="", max_length=80)
    subhead: str = Field(default="", max_length=220)
    cta: str = Field(default="", max_length=80)

    _safe_fields = field_validator("eyebrow", "headline", "accent", "subhead", "cta")(
        assert_public_safe_text
    )


class AuthoritySignalPacket(_AuthorityModel):
    """Versioned, public-safe handoff from research to editorial generation."""

    schema_version: Literal["authority-signal/v1"] = AUTHORITY_PACKET_SCHEMA
    signal_id: str
    signal_type: AuthoritySignalType
    observed_at: datetime
    expires_at: datetime
    dedup_key: str
    audience: str = Field(min_length=3, max_length=300)
    content_series: AuthoritySeries
    claims: tuple[AuthorityClaim, ...] = Field(min_length=1, max_length=8)
    prohibited_claims: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    privacy_notes: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    article_brief: str = Field(min_length=12, max_length=4_000)
    social_brief: str = Field(min_length=12, max_length=2_000)
    cta_brief: str = Field(min_length=2, max_length=500)
    repo_brief: str = Field(min_length=2, max_length=1_000)
    visual_brief: AuthorityVisualBrief
    destination_repo: str | None = None
    article_route: str | None = None
    evidence_class: AuthorityEvidenceClass
    first_person_allowed: bool = False

    _safe_required = field_validator(
        "audience", "article_brief", "social_brief", "cta_brief", "repo_brief"
    )(assert_public_safe_text)
    _safe_lists = field_validator("prohibited_claims", "privacy_notes")(
        lambda values: tuple(assert_public_safe_text(value) for value in values)
    )

    @field_validator("signal_id")
    @classmethod
    def _valid_signal_id(cls, value: str) -> str:
        if not _SIGNAL_ID_RE.fullmatch(value):
            raise ValueError("signal_id must use as_YYYYMMDD_<16 lowercase hex>")
        return value

    @field_validator("dedup_key")
    @classmethod
    def _valid_dedup_key(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("dedup_key must be a lowercase SHA-256 digest")
        return value

    @field_validator("destination_repo")
    @classmethod
    def _valid_destination_repo(cls, value: str | None) -> str | None:
        if value is not None and not _PUBLIC_REPO_RE.fullmatch(value):
            raise ValueError("destination_repo must be owner/name")
        return value

    @field_validator("article_route")
    @classmethod
    def _valid_article_route(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/blog") or "://" in value or ".." in value or "?" in value:
            raise ValueError("article_route must be a clean /blog path")
        return value.rstrip("/") or "/blog"

    @model_validator(mode="after")
    def _validate_packet_invariants(self) -> AuthoritySignalPacket:
        if self.observed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("observed_at and expires_at must be timezone-aware")
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be after observed_at")
        if self.expires_at > self.observed_at + timedelta(days=31):
            raise ValueError("authority packets may remain valid for at most 31 days")
        expected_id = authority_signal_id(self.dedup_key, self.observed_at)
        if self.signal_id != expected_id:
            raise ValueError("signal_id must be derived from dedup_key and observed_at")
        if not (self.destination_repo or self.article_route):
            raise ValueError("destination_repo or article_route is required")
        if self.content_series == "Repo Field Note" and not self.destination_repo:
            raise ValueError("Repo Field Note requires destination_repo")
        if self.first_person_allowed and self.evidence_class != "verified_operator_receipt":
            raise ValueError("first-person language requires verified_operator_receipt evidence")
        for claim in self.claims:
            if claim.source_date and claim.source_date > self.observed_at.date():
                raise ValueError("claim source_date cannot be after observed_at")
        return self

    def to_public_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible packet after one final safety scan."""

        payload = self.model_dump(mode="json")
        assert_public_safe_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return payload


@dataclass(slots=True)
class SignalItem:
    """A single fetched signal item flowing through the pipeline."""

    source: str
    title: str
    url: str
    summary: str
    relevance_score: float = 0.0
    tags: list[str] = field(default_factory=list)
    fetched_at: str = ""
    content_angle: str | None = None


@dataclass(slots=True)
class SignalDigest:
    """Aggregated output of a single signal engine run."""

    date: str
    items: list[SignalItem] = field(default_factory=list)
    drafts_created: list[str] = field(default_factory=list)
    sources_checked: int = 0
    sources_failed: int = 0
    total_fetched: int = 0
    total_triaged: int = 0
    markdown_body: str = ""
