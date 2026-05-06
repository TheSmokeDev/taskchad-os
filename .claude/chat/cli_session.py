"""Session Click group — `thehomie session list/show/resume`.

PRD-7 §7.10 / Phase 4 (PRP-7d) operator surface:

- ``thehomie session list``     human/JSON listing of recent sessions
- ``thehomie session show``     metadata + recent messages for one session
- ``thehomie session resume``   thin re-exec of ``thehomie chat --resume <id>``

Defaults follow PRD §7.10: ``source IN ('tool', 'hook')`` is hidden, ``cron``
remains visible. ``--all`` and ``--source <tag>`` are the operator escape
hatches.

Argument-shape contract (PRD §7.10 / R1 B3): ``show`` and ``resume`` take the
**stable composite/runtime session_id STRING** — NOT the SQLite autoincrement
primary key. The downstream ``thehomie chat --resume <id>`` consumer in
``adapters/cli_adapter.py`` already accepts both the composite and runtime
forms; this module re-execs with the EXACT ``runtime_session_id`` stored on
the row so the resume flow stays Hermes-faithful (R2 M5 / R3 NNM2 / R4).

This module sits in ``.claude/chat/`` next to ``cli.py`` so that ``sys.path``
setup performed by ``cli.py`` (lines 18-24) is in effect when this module
is imported.  It must NOT hardcode ``.claude/data/...`` paths — the canonical
path resolution lives in ``config.CHAT_DB_PATH`` and the active backend
(SQLite or Postgres) is chosen by ``get_session_store()``.
"""

from __future__ import annotations

import json as json_mod
import os
import shutil
import sys
from datetime import datetime
from typing import Any

import click

from session import (
    SOURCE_VALUES,
    get_session_store,
)

from config import CHAT_DB_PATH


__all__ = ["session"]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _iso(value: Any) -> str:
    """Serialize a datetime (or anything stringy) as ISO-8601."""

    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else ""


def _fmt_relative_time(when: datetime | None) -> str:
    """Compact 'N units ago' for human table rows."""

    if when is None:
        return "-"
    try:
        delta = int((datetime.now() - when).total_seconds())
    except Exception:
        return _iso(when)
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _summary_to_dict(summary: Any) -> dict[str, Any]:
    """Map a SessionSummary dataclass to a dict in pinned field order.

    Field order matches the SessionSummary dataclass (R3 minor / consumer
    contract): internal_id, session_id, platform, source, message_count,
    updated_at, runtime_session_id. Datetimes are ISO-8601 strings so the
    payload is deterministic JSON.
    """

    return {
        "internal_id": summary.internal_id,
        "session_id": summary.session_id,
        "platform": summary.platform,
        "source": summary.source,
        "message_count": summary.message_count,
        "updated_at": _iso(summary.updated_at),
        "runtime_session_id": summary.runtime_session_id,
    }


def _print_summary_table(rows: list[Any]) -> None:
    """Print a simple aligned table of SessionSummary rows."""

    if not rows:
        click.echo("No sessions found.")
        return

    # Build cell strings first so we can compute column widths.
    headers = [
        "ID",
        "Platform",
        "Updated",
        "Source",
        "Msgs",
        "session_id",
    ]
    cells: list[list[str]] = []
    for r in rows:
        cells.append([
            str(r.internal_id),
            r.platform or "",
            _fmt_relative_time(r.updated_at),
            r.source or "",
            str(r.message_count),
            r.session_id or "",
        ])

    widths = [
        max(len(headers[i]), max((len(c[i]) for c in cells), default=0))
        for i in range(len(headers))
    ]

    def _fmt_row(values: list[str]) -> str:
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values))

    click.echo(_fmt_row(headers))
    for c in cells:
        click.echo(_fmt_row(c))


# ── `thehomie session ...` group ────────────────────────────────────────────


@click.group()
def session() -> None:
    """Inspect and resume chat sessions.

    Default lists hide ``tool`` and ``hook`` sources (PRD §7.10). Use
    ``--all`` to see everything or ``--source <tag>`` to filter.
    """


@session.command("list")
@click.option(
    "--all",
    "all_sources",
    is_flag=True,
    help="Show all sources (override the default hidden set).",
)
@click.option(
    "--source",
    type=click.Choice(SOURCE_VALUES, case_sensitive=True),
    default=None,
    help=(
        "Filter to a single source tag (one of: interactive, tool, cron, hook). "
        "Values are case-sensitive (lowercase only)."
    ),
)
@click.option("--platform", default=None, help="Filter by platform (cli, telegram, slack, ...).")
@click.option("--limit", type=int, default=20, show_default=True, help="Maximum rows to return.")
@click.option("--json", "json_mode", is_flag=True, help="Emit JSON instead of a human table.")
def session_list(
    all_sources: bool,
    source: str | None,
    platform: str | None,
    limit: int,
    json_mode: bool,
) -> None:
    """List recent chat sessions.

    Default behavior hides ``tool`` and ``hook`` sources per PRD §7.10 — the
    canonical ``SOURCE_HIDDEN_BY_DEFAULT`` is resolved inside
    ``list_recent`` via the None sentinel pattern (Rule 1 / R1 B5).
    """

    store = get_session_store(CHAT_DB_PATH)
    summaries = store.list_recent(
        platform=platform,
        source=source,
        limit=limit,
        all_sources=all_sources,
    )

    if json_mode:
        payload = [_summary_to_dict(s) for s in summaries]
        click.echo(json_mod.dumps(payload, default=str))
        return

    _print_summary_table(summaries)


@session.command("show")
@click.argument("session_id", type=str)
@click.option("--json", "json_mode", is_flag=True, help="Emit JSON instead of a human display.")
@click.option(
    "--messages",
    type=int,
    default=20,
    show_default=True,
    help="How many recent messages to include.",
)
def session_show(session_id: str, json_mode: bool, messages: int) -> None:
    """Show metadata and recent messages for a session.

    ``<session_id>`` is the stable composite/runtime session id STRING
    (PRD §7.10 / R1 B3) — NOT the SQLite autoincrement primary key.  The
    store tries the composite ``session_id`` column first, then falls back
    to ``runtime_session_id`` so operators can paste either form from
    quiet-JSON output.
    """

    store = get_session_store(CHAT_DB_PATH)
    sess = store.get_by_session_id(session_id)
    if sess is None:
        click.echo(f"Session not found: {session_id}", err=True)
        sys.exit(1)

    recent: list[Any] = []
    list_messages = getattr(store, "list_messages", None)
    if callable(list_messages):
        try:
            recent = list_messages(sess.session_id, limit=messages)
        except Exception:
            # Message history is best-effort; never let it break `show`.
            recent = []

    if json_mode:
        payload = {
            "session_id": sess.session_id,
            "runtime_session_id": sess.runtime_session_id,
            "platform": sess.platform,
            "channel_id": sess.channel_id,
            "thread_id": sess.thread_id,
            "user_id": sess.user_id,
            "source": sess.source,
            "status": sess.status,
            "mode": sess.mode,
            "runtime_lane": sess.runtime_lane,
            "runtime_provider": sess.runtime_provider,
            "runtime_model": sess.runtime_model,
            "runtime_profile_key": sess.runtime_profile_key,
            "message_count": sess.message_count,
            "total_cost_usd": sess.total_cost_usd,
            "tool_call_count": sess.tool_call_count,
            "created_at": _iso(sess.created_at),
            "updated_at": _iso(sess.updated_at),
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "created_at": _iso(m.created_at),
                }
                for m in recent
            ],
        }
        click.echo(json_mod.dumps(payload, default=str))
        return

    click.echo(f"Session {sess.session_id}")
    click.echo(f"  Platform:   {sess.platform}")
    click.echo(f"  Source:     {sess.source}")
    click.echo(f"  Status:     {sess.status}")
    click.echo(f"  Mode:       {sess.mode}")
    click.echo(f"  Lane:       {sess.runtime_lane}")
    click.echo(f"  Provider:   {sess.runtime_provider}")
    click.echo(f"  Model:      {sess.runtime_model or '-'}")
    click.echo(f"  Profile:    {sess.runtime_profile_key or '-'}")
    click.echo(f"  Created:    {_iso(sess.created_at)}")
    click.echo(f"  Updated:    {_iso(sess.updated_at)}")
    click.echo(f"  Messages:   {sess.message_count}")
    click.echo(f"  Cost USD:   {sess.total_cost_usd:.4f}")
    click.echo(f"  Tool calls: {sess.tool_call_count}")
    click.echo(f"  Runtime ID: {sess.runtime_session_id}")
    if recent:
        click.echo("Recent messages:")
        for m in recent:
            preview = (m.content or "").replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:197] + "..."
            click.echo(f"  [{m.role}] {preview}")


@session.command("resume")
@click.argument("session_id", type=str)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the resume command as JSON instead of re-execing.",
)
def session_resume(session_id: str, dry_run: bool) -> None:
    """Resume an existing session.

    ``<session_id>`` is the stable composite/runtime session id STRING
    (PRD §7.10 / R1 B3). The session is resolved via ``get_by_session_id``
    and the EXACT ``runtime_session_id`` stored on the row is what
    ``thehomie chat --resume <id>`` is invoked with — Hermes-faithful
    re-exec (R2 M5 + R3 NNM2 + R4).

    With ``--dry-run`` the command emits a JSON envelope with shape
    ``{"resume_argv": [...], "target": "<runtime_session_id>"}`` so a
    synthetic / shell-malicious id cannot masquerade as a flag (R3 minor —
    display safety).  Live invocation re-execs via ``os.execvp`` so the
    process is replaced (no nested process tree).
    """

    store = get_session_store(CHAT_DB_PATH)
    sess = store.get_by_session_id(session_id)
    if sess is None:
        click.echo(f"Session not found: {session_id}", err=True)
        sys.exit(1)

    target = sess.runtime_session_id or sess.session_id
    resume_argv = ["thehomie", "chat", "--resume", target]

    if dry_run:
        click.echo(
            json_mod.dumps(
                {"resume_argv": resume_argv, "target": target},
                default=str,
            )
        )
        return

    # Live re-exec.  Prefer `os.execvp("thehomie", ...)` when a `thehomie`
    # entry-point binary is on PATH.  Fall back to re-execing the current
    # Python interpreter against `chat.cli` so resume still works in dev
    # checkouts that run via `uv run python -m chat.cli`.
    thehomie_path = shutil.which("thehomie")
    try:
        if thehomie_path:
            os.execvp("thehomie", resume_argv)
        else:
            python_argv = [
                sys.executable,
                "-m",
                "chat.cli",
                "chat",
                "--resume",
                target,
            ]
            os.execvp(sys.executable, python_argv)
    except OSError as exc:
        click.echo(f"Failed to re-exec for resume: {exc}", err=True)
        sys.exit(1)
