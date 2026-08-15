"""Unified proactive brief builder for living-loop entrypoints."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from cognition.scheduled_payload import (
    build_scheduled_cognition_payload,
    render_identity_context,
)
from cognition.self_model import build_self_model_state, render_self_model_state_section


@dataclass(frozen=True)
class ProactiveBrief:
    """Rendered cognition brief plus source metadata for proof surfaces."""

    section: str
    source_paths: dict[str, str]
    include_identity: bool = False


def build_proactive_brief(
    memory_dir: Path,
    *,
    daily_dir: Path | None = None,
    inference_state_file: Path | None = None,
    include_identity: bool = False,
    header: str = "## Proactive Brief",
    max_daily_chars: int = 1200,
    max_heartbeat_chars: int = 1500,
) -> ProactiveBrief:
    """Build the shared proactive cognition brief.

    This is intentionally read-only. It gives chat bootstrap, heartbeat, and
    scheduled cognition one canonical proactive context path without granting
    any automatic memory mutation behavior.
    """

    memory_dir = Path(memory_dir)
    daily_root = Path(daily_dir) if daily_dir is not None else memory_dir / "daily"
    payload = build_scheduled_cognition_payload(
        memory_dir,
        inference_state_file=inference_state_file,
    )

    sections: list[str] = []
    if include_identity:
        identity = render_identity_context(payload)
        if identity:
            sections.append(identity)
    if payload.active_inference_section:
        sections.append(payload.active_inference_section)
    if payload.working_memory_section:
        sections.append(payload.working_memory_section)

    if inference_state_file is not None:
        self_model_state = build_self_model_state(Path(inference_state_file))
        sections.append(render_self_model_state_section(self_model_state))

    daily_signal = _read_recent_daily_signal(daily_root, max_daily_chars)
    if daily_signal:
        sections.append("## Recent Daily Signal\n\n" + daily_signal)

    heartbeat_policy = _read_limited(memory_dir / "HEARTBEAT.md", max_heartbeat_chars)
    if heartbeat_policy:
        sections.append("## Heartbeat Checklist\n\n" + heartbeat_policy)

    body = "\n\n".join(sections)
    section = f"{header}\n\n{body}" if body else ""
    return ProactiveBrief(
        section=section,
        source_paths={
            "memory_dir": str(memory_dir),
            "daily_dir": str(daily_root),
            "inference_state_file": str(inference_state_file or ""),
            "heartbeat_file": str(memory_dir / "HEARTBEAT.md"),
            "working_file": str(memory_dir / "WORKING.md"),
        },
        include_identity=include_identity,
    )


def build_proactive_brief_section(
    memory_dir: Path,
    *,
    daily_dir: Path | None = None,
    inference_state_file: Path | None = None,
    include_identity: bool = False,
    header: str = "## Proactive Brief",
) -> str:
    """Return only the rendered proactive brief section."""

    return build_proactive_brief(
        memory_dir,
        daily_dir=daily_dir,
        inference_state_file=inference_state_file,
        include_identity=include_identity,
        header=header,
    ).section


def _read_recent_daily_signal(daily_dir: Path, max_chars: int) -> str:
    try:
        files = sorted(Path(daily_dir).glob("*.md"), reverse=True)
    except OSError:
        return ""
    for path in files[:2]:
        text = _read_limited(path, max_chars)
        if text:
            return f"### {path.stem}\n\n{text}"
    return ""


def _read_limited(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_newline = cut.rfind("\n")
    if last_newline > max_chars // 2:
        cut = cut[:last_newline]
    return cut + "\n[TRUNCATED]"


# =============================================================================
# Session Opening Brief (Living Mind Act 4) — the 6:30am moment.
#
# Deterministic, zero-LLM assembly of a "while you were out" block that the
# engine suffixes onto RuntimeRequest.prompt on the first interactive engine
# turn after a meaningful absence. The existing builders above are untouched;
# this is a NEW surface beside them (the module the PRD names).
# =============================================================================

_BRIEF_MARKER_FILE_NAME = "session-brief-owed.json"
_BRIEF_HEADER = "# Session Opening Brief (deliver first)"
_SECTION_WHAT = "## What changed while away"
_SECTION_SELF = "## Self updates (memory amendments)"
_SECTION_MID = "## Mid-flight (open threads)"

# Local copy of living_memory's bullet shape (`- [YYYY-MM-DD] content`) —
# do NOT import private helpers cross-module.
_WM_BULLET_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] (.+)$")

_COFOUNDER_LEDGER_NAME = "cofounder_delegation.jsonl"
# Local mirror of cofounder.agenda.AGENDAS_SUBDIR — importing that module runs
# the persona boot-shim (env overrides), which the chat path must not trigger.
_COFOUNDER_AGENDAS_SUBDIR = "agendas"
_COFOUNDER_AUTOPILOT_APPROVER = "cofounder-autopilot"
_COFOUNDER_TERMINAL_STATUSES = frozenset({"done", "failed"})
# Bounds the completion scan to report.py's archon-poll lookback window
# (COFOUNDER_REPORT_POLL_DAYS default) — older agendas can no longer flip.
_COFOUNDER_AGENDA_SCAN_FILES = 7
_OPS_HISTORY_RELPATH = ("_ops", "history.md")
_JOURNAL_MAX_FILES = 4
_JOURNAL_LINES_PER_FILE = 2
_JOURNAL_LINE_CAP = 320


def normalize_physical_timestamp(value: datetime | str | None) -> datetime | None:
    """Normalize ANY physical timestamp to naive local time (R1 B3).

    The ONLY timestamp-conversion owner in this act. Applied to every
    physical timestamp before any comparison: SQLite ``updated_at`` strings
    (naive), Postgres ``updated_at`` (driver-AWARE datetimes — TIMESTAMPTZ),
    clear-event ISO strings (naive local), amendment ``applied_at``
    (aware-UTC ISO), and the brief-owed marker payload.

    ``None``/empty -> None. ``str`` -> ``datetime.fromisoformat``
    (unparseable -> None). Aware datetime -> ``.astimezone()`` local naive;
    naive -> as-is. Never raises.
    """
    try:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                value = datetime.fromisoformat(text)
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value
    except Exception:
        return None


def read_brief_owed(*, state_dir: Path | None = None) -> datetime | None:
    """Read the brief-owed marker's away boundary, or None (fail-open).

    The marker (R1 B4) records exactly one fact with no other physical home:
    "a brief is still owed for this away window, boundary = T". It is NOT an
    alternate source of away truth — sessions + clear events stay the record.
    Corrupt or missing marker -> None; never raises.
    """
    try:
        if state_dir is None:
            from config import STATE_DIR

            state_dir = STATE_DIR
        path = Path(state_dir) / _BRIEF_MARKER_FILE_NAME
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return normalize_physical_timestamp(data.get("last_activity"))
    except Exception:
        return None


def write_brief_owed(
    last_activity: datetime,
    *,
    state_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically write the brief-owed marker (fail-open, never raises)."""
    try:
        if state_dir is None:
            from config import STATE_DIR

            state_dir = STATE_DIR
        if now is None:
            now = datetime.now()
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / _BRIEF_MARKER_FILE_NAME
        payload = {
            "last_activity": last_activity.isoformat(),
            "detected_at": now.isoformat(),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def clear_brief_owed(*, state_dir: Path | None = None) -> None:
    """Best-effort marker delete (fail-open, never raises).

    Consumed ONLY by a completed engine-turn brief decision (fired OR any
    suppressed outcome) — never by router persistence.
    """
    try:
        if state_dir is None:
            from config import STATE_DIR

            state_dir = STATE_DIR
        (Path(state_dir) / _BRIEF_MARKER_FILE_NAME).unlink(missing_ok=True)
    except Exception:
        pass


@dataclass(frozen=True)
class SessionOpeningBrief:
    """Result of one session-opening-brief decision."""

    prompt_block: str          # "" unless fired
    fired: bool
    away_hours: float | None   # None when no_history (or disabled pre-compare)
    fresh_items: int
    suppressed_reason: str     # "" | "disabled" | "no_history" | "not_away" | "no_fresh_items"


def _bullet_date(bullet: str) -> _date | None:
    """Parse the day-resolution date of a WORKING.md bullet, or None."""
    match = _WM_BULLET_RE.match(bullet.strip())
    if not match:
        return None
    try:
        return _date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _sanitize_line(text: str, max_chars: int) -> str:
    """Deterministic single-line sanitizer (the episodes.py shape — local
    copy; do NOT import private helpers cross-module).

    Strips control chars/newlines, backticks and quotes, collapses
    whitespace, trims to ``max_chars`` at a word boundary.
    """
    s = str(text or "")
    s = s.replace("`", "").replace('"', "").replace("'", "")
    s = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_chars:
        cut = s[:max_chars]
        boundary = cut.rfind(" ")
        if boundary > 0:
            cut = cut[:boundary]
        s = cut.strip()
    return s


def _bullet_line(bullet: str) -> str:
    """Render a WORKING.md bullet as a sanitized single brief line."""
    text = bullet.strip()
    if text.startswith("- "):
        text = text[2:]
    return "- " + _sanitize_line(text, 160)


def _read_cofounder_delegations(
    path: Path, last_activity: datetime
) -> list[dict[str, str]]:
    """Agenda lines the co-founder actually SENT after the away boundary.

    The append-only delegation ledger is the physical record of a send —
    an agenda item's own status can be written by other paths.
    """
    events: list[dict[str, str]] = []
    if not path.is_file():
        return events
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("outcome") != "sent":
            continue
        sent_at = normalize_physical_timestamp(row.get("timestamp"))
        if sent_at is None or sent_at <= last_activity:
            continue
        events.append(
            {
                "at": sent_at.isoformat(),
                "kind": "sent",
                "line": str(row.get("line") if row.get("line") is not None else "?"),
                "persona": str(row.get("persona") or "?"),
                "approved_by": str(row.get("approved_by") or ""),
                "detail": str(row.get("detail") or ""),
            }
        )
    return events


def _read_cofounder_completions(
    agenda_dir: Path, last_activity: datetime
) -> list[dict[str, str]]:
    """Agenda lines flipped to a terminal status after the away boundary.

    ``reported_at`` is stamped by report.py's single flip site on every
    status change, so there is no separate completion clock to read.
    """
    events: list[dict[str, str]] = []
    for path in sorted(agenda_dir.glob("AGENDA-*.json"), reverse=True)[
        :_COFOUNDER_AGENDA_SCAN_FILES
    ]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status not in _COFOUNDER_TERMINAL_STATUSES:
                continue
            flipped = normalize_physical_timestamp(item.get("reported_at"))
            if flipped is None or flipped <= last_activity:
                continue
            events.append(
                {
                    "at": flipped.isoformat(),
                    "kind": status,
                    "line": str(item.get("n") if item.get("n") is not None else "?"),
                    "persona": str(item.get("persona") or "?"),
                    "approved_by": "",
                    "detail": str(
                        item.get("result_summary") or item.get("task") or ""
                    ),
                }
            )
    return events


def _cofounder_line(event: dict[str, str]) -> str:
    """Render one co-founder outcome as a sanitized single brief line."""
    line = _sanitize_line(event.get("line", "") or "?", 8)
    persona = _sanitize_line(event.get("persona", "") or "?", 40)
    detail = _sanitize_line(event.get("detail", "") or "(no detail)", 80)
    kind = event.get("kind", "")
    if kind == "sent":
        approver = event.get("approved_by", "")
        who = (
            "self-assigned"
            if approver == _COFOUNDER_AUTOPILOT_APPROVER
            else _sanitize_line(approver, 40)
        )
        suffix = f" ({who})" if who else ""
        return f"- cofounder: delegated line {line} to {persona}{suffix}: {detail}"
    return f"- cofounder: line {line} ({persona}) {kind}: {detail}"


def read_recent_operator_journal_lines(
    memory_dir: Path,
    *,
    since: datetime,
    max_files: int = _JOURNAL_MAX_FILES,
    lines_per_file: int = _JOURNAL_LINES_PER_FILE,
) -> list[str]:
    """Read the operator's recent daily journals plus the latest ops receipt.

    This is the deterministic floor for morning orientation. It deliberately
    reads physical notes instead of inferring project state from an agenda
    label. Each source is bounded and fail-open; returned lines are sanitized
    before they enter a runtime prompt.
    """

    root = Path(memory_dir)
    earliest = since.date()
    rendered: list[str] = []
    try:
        candidates: list[tuple[_date, Path]] = []
        for path in (root / "daily").glob("*.md"):
            try:
                day = _date.fromisoformat(path.stem[:10])
            except ValueError:
                continue
            if day >= earliest:
                candidates.append((day, path))
        for day, path in sorted(candidates, reverse=True)[:max_files]:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            bullets = _recent_note_bullets(text, lines_per_file)
            for bullet in bullets:
                rendered.append(
                    f"- journal ({day.isoformat()}): "
                    f"{_sanitize_line(bullet, _JOURNAL_LINE_CAP)}"
                )
    except OSError:
        pass

    # The ops ledger records what vault-ops actually checked and what it
    # changed. Its latest dated block is often the best reconciliation signal
    # when a stale WORKING.md bullet conflicts with a verified context pass.
    ops_rendered: list[str] = []
    try:
        ops_path = root.joinpath(*_OPS_HISTORY_RELPATH)
        ops_text = ops_path.read_text(encoding="utf-8")
        for bullet in _recent_ops_bullets(ops_text[-6000:], since.date(), 2):
            ops_rendered.append(
                "- vault-ops receipt: "
                + _sanitize_line(bullet, _JOURNAL_LINE_CAP)
            )
    except OSError:
        pass
    return ops_rendered + rendered


def _recent_note_bullets(text: str, limit: int) -> list[str]:
    """Newest meaningful markdown bullets, preserving their source wording."""

    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if not body or body.startswith("[["):
            continue
        lines.append(body)
    return lines[-max(0, int(limit)):][::-1]


def _recent_ops_bullets(text: str, earliest: _date, limit: int) -> list[str]:
    """Bullets from the newest dated ops block, only when it is in range."""

    matches = list(
        re.finditer(r"^#{2,3}\s+(\d{4}-\d{2}-\d{2})\b.*$", text, re.MULTILINE)
    )
    for match in reversed(matches):
        try:
            day = _date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if day < earliest:
            return []
        bullets = _recent_note_bullets(text[match.end():], 50)
        preferred: list[str] = []
        for label in (
            "**Impact**:",
            "**Observed state**:",
            "**Actions**:",
            "**Gap found**:",
            "**Files**:",
            "**Flags**:",
        ):
            newest = next((b for b in bullets if b.startswith(label)), None)
            if newest is not None:
                preferred.append(newest)
        chosen = preferred or bullets
        return chosen[:max(0, int(limit))]
    return []


def build_session_opening_brief(
    memory_dir: Path,
    *,
    last_activity: datetime | None,
    now: datetime | None = None,
    settings=None,
    ledger_file: Path | None = None,
    delegation_ledger_file: Path | None = None,
    agenda_dir: Path | None = None,
) -> SessionOpeningBrief:
    """Build the session-opening brief block (Living Mind Act 4).

    Gate ordering (cheapest first, NO source reads before the gate):
    disabled -> no_history -> not_away (INCLUSIVE boundary: exactly the
    threshold fires) -> source reads -> no_fresh_items -> fired.

    Boredom contract (R1 B2 — ONE contract): ``fresh_items`` counts ONLY
    change-source items — fresh observations (day-floor), fresh
    ``[heartbeat]``-tagged thread bullets (Act 1 promotions ARE changes),
    episodes-since (strict instant), applied amendments-since (strict
    instant), co-founder outcomes-since (strict instant — sent delegations
    and terminal agenda flips, so overnight autonomous work ALONE can wake a
    brief; today's still-proposed lines are orientation, not change, and stay
    in the passive portfolio region). Open threads are CONTEXT ONLY: they
    render in Mid-flight when the brief already fired and NEVER count, fresh
    or stale, manual-tagged or not — except the single explicit
    ``[heartbeat]`` exception above.

    Zero LLM calls, zero writes (the ledger heal inside ``read_all`` is the
    documented exception — the same heal every cron consumer performs, under
    the same lock). Each source read is its own try/except: a partial
    failure degrades to an empty section, never kills the others.
    """
    if settings is None:
        from config import get_session_brief_settings

        settings = get_session_brief_settings()
    if now is None:
        now = datetime.now()
    if not settings.enabled:
        return SessionOpeningBrief("", False, None, 0, "disabled")
    if last_activity is None:
        return SessionOpeningBrief("", False, None, 0, "no_history")
    away_hours = (now - last_activity).total_seconds() / 3600.0
    if away_hours < settings.away_hours:
        return SessionOpeningBrief("", False, away_hours, 0, "not_away")

    boundary_date = last_activity.date()

    fresh_obs: list[str] = []
    fresh_hb_threads: list[str] = []
    open_threads: list[str] = []
    try:
        import living_memory  # lazy — rides the scheduled_payload sys.path bootstrap

        data = living_memory.read_working_memory(Path(memory_dir))
        open_threads = list(data.open_threads)
        for bullet in data.heartbeat_observations:
            day = _bullet_date(bullet)
            if day is not None and day >= boundary_date:
                fresh_obs.append(bullet)
        for bullet in open_threads:
            if "[heartbeat]" not in bullet:
                continue
            day = _bullet_date(bullet)
            if day is not None and day >= boundary_date:
                fresh_hb_threads.append(bullet)
    except Exception:
        fresh_obs, fresh_hb_threads, open_threads = [], [], []

    episode_entries: list[dict[str, str]] = []
    try:
        import episodes  # lazy — same bootstrap

        for path in episodes.list_episodes_since(
            Path(memory_dir), since=last_activity
        ):
            try:
                fm = episodes.read_episode_frontmatter(path)
            except Exception:
                continue
            if fm:
                episode_entries.append(fm)
    except Exception:
        episode_entries = []

    fresh_amendments: list = []
    try:
        if ledger_file is None:
            from config import AMENDMENT_LEDGER_FILE

            ledger_file = AMENDMENT_LEDGER_FILE
        ledger_path = Path(ledger_file)
        if ledger_path.is_file():
            from cognition.amendments import ProposalLedger, ledger_file_lock

            # read_all() self-heals (REWRITES malformed rows) — the lock is
            # mandatory (the memory_reflect pattern). <= ~once/day by
            # construction, not hot-path ledger traffic.
            with ledger_file_lock(ledger_path):
                proposals = ProposalLedger(ledger_path).read_all()
            dated: list[tuple[datetime, object]] = []
            for proposal in proposals:
                if getattr(proposal, "status", "") != "applied":
                    continue
                applied = normalize_physical_timestamp(
                    getattr(proposal, "applied_at", None)
                )
                if applied is None or applied <= last_activity:
                    continue
                dated.append((applied, proposal))
            dated.sort(key=lambda item: item[0], reverse=True)
            fresh_amendments = [proposal for _, proposal in dated]
    except Exception:
        fresh_amendments = []

    cofounder_events: list[dict[str, str]] = []
    try:
        if delegation_ledger_file is None:
            from config import DATA_DIR

            delegation_ledger_file = Path(DATA_DIR) / _COFOUNDER_LEDGER_NAME
        cofounder_events.extend(
            _read_cofounder_delegations(Path(delegation_ledger_file), last_activity)
        )
    except Exception:
        pass
    try:
        if agenda_dir is None:
            from config import get_cofounder_settings

            agenda_dir = (
                Path(get_cofounder_settings().projects_dir) / _COFOUNDER_AGENDAS_SUBDIR
            )
        cofounder_events.extend(
            _read_cofounder_completions(Path(agenda_dir), last_activity)
        )
    except Exception:
        pass
    cofounder_events.sort(key=lambda event: event["at"], reverse=True)

    journal_lines: list[str] = []
    try:
        journal_lines = read_recent_operator_journal_lines(
            Path(memory_dir), since=last_activity
        )
    except Exception:
        journal_lines = []

    fresh_items = (
        len(fresh_obs)
        + len(fresh_hb_threads)
        + len(episode_entries)
        + len(fresh_amendments)
        + len(cofounder_events)
        + len(journal_lines)
    )
    if fresh_items < settings.min_fresh_items:
        return SessionOpeningBrief("", False, away_hours, fresh_items, "no_fresh_items")

    prompt_block = _render_session_brief(
        away_hours=away_hours,
        last_activity=last_activity,
        fresh_obs=fresh_obs,
        fresh_hb_threads=fresh_hb_threads,
        episode_entries=episode_entries,
        fresh_amendments=fresh_amendments,
        cofounder_events=cofounder_events,
        journal_lines=journal_lines,
        open_threads=open_threads,
        settings=settings,
    )
    return SessionOpeningBrief(prompt_block, True, away_hours, fresh_items, "")


def _render_session_brief(
    *,
    away_hours: float,
    last_activity: datetime,
    fresh_obs: list[str],
    fresh_hb_threads: list[str],
    episode_entries: list[dict[str, str]],
    fresh_amendments: list,
    cofounder_events: list[dict[str, str]],
    journal_lines: list[str],
    open_threads: list[str],
    settings,
) -> str:
    """Deterministic render with cap-priority semantics (R1 M4).

    Per-source caps (``max_per_section``) apply FIRST, then the total
    ``max_chars`` cap. The instruction block is RESERVED budget — never
    truncated. One item from EACH fired fresh source is reserved before any
    section deep-fills. Remaining budget fills sections in order (What
    changed -> Self updates -> Mid-flight) so context-only threads drop
    first under pressure. Truncation is newline-boundary with an explicit
    ``[TRUNCATED]`` marker. Section headers render only when non-empty.
    """
    cap = max(1, int(settings.max_per_section))

    def newest_first(bullets: list[str]) -> list[str]:
        # Appends land at the section END, so the tail is newest.
        return list(bullets[-cap:][::-1])

    obs_lines = [_bullet_line(b) for b in newest_first(fresh_obs)]
    hb_lines = [_bullet_line(b) for b in newest_first(fresh_hb_threads)]
    episode_lines: list[str] = []
    for fm in episode_entries[:cap]:
        surface = _sanitize_line(fm.get("surface", "") or "unknown", 40)
        day = _sanitize_line(fm.get("date", "") or "?", 20)
        summary = _sanitize_line(fm.get("summary", "") or "(no summary)", 160)
        episode_lines.append(f"- session ({surface}, {day}): {summary}")
    amendment_lines: list[str] = []
    for proposal in fresh_amendments[:cap]:
        target = _sanitize_line(getattr(proposal, "target_file", "") or "?", 60)
        summary = _sanitize_line(
            getattr(proposal, "summary", "") or "(no summary)", 160
        )
        amendment_lines.append(f"- {target}: {summary}")
    cofounder_lines = [_cofounder_line(event) for event in cofounder_events[:cap]]
    journal_lines = journal_lines[:cap]
    # Fresh [heartbeat] threads already render in "What changed" — keep them
    # out of Mid-flight so one bullet never appears twice in one brief.
    promoted = set(fresh_hb_threads)
    mid_lines = [
        _bullet_line(b)
        for b in newest_first([b for b in open_threads if b not in promoted])
    ]

    instruction = (
        f"{_BRIEF_HEADER}\n\n"
        f"The operator is opening a new working session after ~{away_hours:.1f}h "
        f"away (last activity {last_activity:%Y-%m-%d %H:%M}). OPEN your reply "
        "with a short first-person brief — you kept watch while they were out — "
        "using the items below as evidence. Reconcile the recent journals and "
        "vault-ops receipt against older thread or agenda labels. Lead with "
        "what the operator has actually been working on and what needs attention "
        "now; keep it tight and do not repeat stale news. Never describe a draft, "
        "generated document, delegated line, or healthy status as executed work. "
        "After the brief, answer the operator's "
        "message. If anything here conflicts with the live conversation, the "
        "conversation wins."
    )

    what_display = (
        journal_lines + obs_lines + hb_lines + episode_lines + cofounder_lines
    )
    what_reserved: list[int] = []
    offset = 0
    for group in (
        journal_lines,
        obs_lines,
        hb_lines,
        episode_lines,
        cofounder_lines,
    ):
        if group:
            what_reserved.append(offset)
        offset += len(group)
    section_specs: list[tuple[str, list[str], list[int]]] = [
        (_SECTION_WHAT, what_display, what_reserved),
        (_SECTION_SELF, amendment_lines, [0] if amendment_lines else []),
        (_SECTION_MID, mid_lines, []),  # context only — never reserved
    ]

    included = {section: [False] * len(items) for section, items, _ in section_specs}
    opened = {section: False for section, _, _ in section_specs}
    max_chars = int(settings.max_chars)
    total = len(instruction)

    def _try_add(section: str, items: list[str], idx: int, *, force: bool) -> bool:
        nonlocal total
        cost = len(items[idx]) + 1
        if not opened[section]:
            cost += len(section) + 3
        if not force and total + cost > max_chars:
            return False
        total += cost
        opened[section] = True
        included[section][idx] = True
        return True

    # Reservation pass — instruction + one item per fired fresh source are
    # included regardless of the cap.
    for section, items, reserved_idx in section_specs:
        for idx in reserved_idx:
            _try_add(section, items, idx, force=True)
    # Deep-fill pass — stop at the first overflow; Mid-flight is last in add
    # order, so context drops first.
    truncated = False
    for section, items, _ in section_specs:
        stop = False
        for idx in range(len(items)):
            if included[section][idx]:
                continue
            if not _try_add(section, items, idx, force=False):
                truncated = True
                stop = True
                break
        if stop:
            break

    parts = [instruction]
    for section, items, _ in section_specs:
        chosen = [items[i] for i in range(len(items)) if included[section][i]]
        if chosen:
            parts.append(section + "\n" + "\n".join(chosen))
    block = "\n\n".join(parts)
    if truncated:
        block += "\n[TRUNCATED]"
    return block


__all__ = (
    "ProactiveBrief",
    "build_proactive_brief",
    "build_proactive_brief_section",
    "SessionOpeningBrief",
    "build_session_opening_brief",
    "read_recent_operator_journal_lines",
    "normalize_physical_timestamp",
    "read_brief_owed",
    "write_brief_owed",
    "clear_brief_owed",
)
