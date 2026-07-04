"""Curated catalog of starter scheduled-job suggestions.

These are the built-in automations The Homie offers a new operator out of the
box — the ``catalog`` source of the unified suggestion surface. Each entry is a
ready-to-run ``/api/scheduled`` create body wrapped as a suggestion; the
operator accepts via ``/suggestions``. Nothing here auto-schedules.

Adding a catalog entry: append a CatalogEntry. Keep prompts self-contained
(scheduled jobs run with no chat context) and schedules to exactly 5 cron fields
(the ``/api/scheduled`` guard rejects anything else). The ``job_spec`` is passed
verbatim to the injected creator on accept.

Ported from Hermes v0.18 ``cron/suggestion_catalog.py`` (algorithm verbatim).
Re-anchors for The Homie:
  * ``job_spec`` shape ``{persona_id, prompt, schedule, next_run}`` (the
    ``/api/scheduled`` create body) instead of the Hermes ``create_job`` kwargs.
  * schedules are 5-field cron ("*/30 * * * *", not Hermes "every 30m").
  * ``seed_catalog_suggestions`` defaults ``add_fn`` to
    ``orchestration.suggestions.add_suggestion`` via a lazy in-body import.
  * the Hermes ``classify_items`` script-path helper is dropped (no such script
    ships in The Homie).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["CatalogEntry", "CATALOG", "seed_catalog_suggestions"]


@dataclass(frozen=True)
class CatalogEntry:
    """A curated starter automation offered as a suggestion."""

    key: str                  # stable dedup key (never re-offered once dismissed)
    title: str
    description: str
    job_spec: dict[str, Any]  # /api/scheduled create body


# The curated set. Every schedule is a 5-field cron expression.
CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        key="catalog:daily-briefing",
        title="Daily briefing",
        description="Every morning at 8am, a short briefing: today's calendar, "
        "weather, and anything urgent waiting on you.",
        job_spec={
            "persona_id": "default",
            "prompt": (
                "Produce a concise morning briefing for the user: today's "
                "calendar events, the local weather, and any urgent items "
                "(unread important email, due tasks). Keep it short and "
                "scannable. If you have no connected data sources, give a brief "
                "general good-morning with the date and offer to connect "
                "calendar/email."
            ),
            "schedule": "0 8 * * *",
            "next_run": None,
        },
    ),
    CatalogEntry(
        key="catalog:important-mail",
        title="Important-mail monitor",
        description="Check your inbox every 30 minutes and ping you ONLY about "
        "mail that actually needs attention — never the newsletters.",
        job_spec={
            "persona_id": "default",
            "prompt": (
                "Check the user's inbox for new messages since the last run. "
                "For each candidate, judge urgency against this rule: surface "
                "only mail that needs a reply today, is from a manager/family "
                "member, or mentions a deadline. Deliver ONLY what clears that "
                "bar. If nothing does, respond with [SILENT] so the user is not "
                "pinged. Requires a connected mail source; if none is "
                "configured, explain how to connect one and then stop."
            ),
            "schedule": "*/30 * * * *",
            "next_run": None,
        },
    ),
    CatalogEntry(
        key="catalog:weekly-review",
        title="Weekly review",
        description="Every Sunday evening, a recap of the week: what got done, "
        "what's still open, and what's coming up next week.",
        job_spec={
            "persona_id": "default",
            "prompt": (
                "Produce a weekly review for the user: summarize what was "
                "accomplished this week, list still-open items, and preview "
                "next week's calendar. Pull from whatever sources are connected "
                "(calendar, task tools, recent conversations). Keep it tight."
            ),
            "schedule": "0 18 * * 0",
            "next_run": None,
        },
    ),
    CatalogEntry(
        key="catalog:vault-sweep",
        title="Vault entity-compilation sweep",
        description="Every night at 3am, compile new notes into concept pages "
        "so the knowledge graph stays current.",
        job_spec={
            "persona_id": "default",
            "prompt": (
                "Run the vault entity-compilation sweep: find notes without "
                "concept coverage, compile their entities into concept pages, "
                "and flag any contradictions between sources. Report a short "
                "summary of what was compiled. If nothing needs compiling, "
                "respond with [SILENT]."
            ),
            "schedule": "0 3 * * *",
            "next_run": None,
        },
    ),
]


def seed_catalog_suggestions(
    *,
    add_fn: Callable[..., dict[str, Any] | None] | None = None,
    keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Register catalog entries as pending suggestions.

    ``add_fn`` defaults to ``orchestration.suggestions.add_suggestion``
    (injectable for tests). ``keys`` restricts to specific catalog entries; omit
    to seed all. Entries already dismissed/accepted (by dedup key) or beyond the
    pending cap are skipped by the store, so re-seeding is safe and idempotent.
    Returns the list of suggestion records actually created.
    """
    if add_fn is None:
        from orchestration.suggestions import add_suggestion as add_fn  # noqa: E501

    wanted = set(keys) if keys else None
    created: list[dict[str, Any]] = []
    for entry in CATALOG:
        if wanted is not None and entry.key not in wanted:
            continue
        rec = add_fn(
            title=entry.title,
            description=entry.description,
            source="catalog",
            job_spec=dict(entry.job_spec),
            dedup_key=entry.key,
        )
        if rec is not None:
            created.append(rec)
    return created
