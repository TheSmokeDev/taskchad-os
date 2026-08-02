"""Called-Shots track-record callback + shared persona resolution (T3 #189).

The "remember last time" surface: when an operator turn touches a domain the
ledger has RESOLVED history for, ONE compact private-context line rides the
turn prompt so the reply can say "you 2 / me 1 on pricing" with REAL numbers.

Contract (epic #186, Kimi-gate + Codex R1 clarifications):
  - Numbers ALWAYS come from ``called_shots.track_record`` — and ``ok`` is
    checked FIRST: an unreadable ledger renders NOTHING (never an affirmative
    "no history" claim). Domain enumeration goes through the service's
    ``list_resolved_domains`` (None = unreadable → skip silently).
  - AUTONOMOUS surface: gated by the ``CALLED_SHOTS_ENABLED`` soft toggle AND
    the hard kill-switch (checked, not raised — this path must never break a
    turn). Operator-initiated ``/shots`` does NOT ride this gate.
  - Deterministic domain match only — normalized-domain substring presence in
    the operator text. NO LLM, NO embedding call in the hot path.
  - Fires once per (conversation, domain): the CALLER owns marking — this
    builder only CHECKS ``fired_keys`` and returns ``decision["dedup_key"]``;
    the engine marks it fired only after accepting the rendered block (which
    is stats-first by construction, so truncation can never strip the counts).
  - Whole-body fail-open: any failure returns ("", decision) — a bare, correct
    turn, never a broken one.

``resolve_active_persona`` is the ONE persona-grain resolver for the T3
surfaces (command seam + callback) — record/read must key at the same grain
(Rule 4); T2's challenge recorder should import THIS resolver, not grow a
second one. It fails CLOSED: a resolution ERROR returns ``None`` (callers
refuse/skip — never operate on a guessed persona); only the SEMANTIC
main-profile cases ("default"/"custom"/unset) map to "default".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

# Boot-shim: mirror called_shots.py so a standalone import resolves the
# active profile the same way the rest of the slice does.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_MIN_TEXT_CHARS = 12  # below this a turn can't meaningfully name a domain
_MAX_DOMAIN_DISPLAY = 48  # domain is untrusted display text — cap + sanitize
_DOMAIN_SANITIZE_RE = re.compile(r"[\r\n#*_`\[\]>|]+")


def resolve_active_persona() -> str | None:
    """The ledger persona_id for THIS process — one resolver for all T3 seams.

    SEMANTIC mapping (resolution succeeded): real named personas keep their
    name; the main Homie ("default"/"custom"/empty) keys as "default".
    ERROR path (resolution FAILED): returns ``None`` — fail CLOSED. Callers
    must refuse (command) or skip (callback/sweep); persona-scoped ledger
    operations must never run on a guessed identity (Rule 4).
    """
    try:
        from personas import activity as _activity

        name = (_activity.get_active_profile_name() or "").strip()
        if not name or name in ("default", "custom"):
            return "default"
        return name
    except Exception as exc:
        print(
            f"[shots_callback] persona resolution failed (fail-closed): {exc!r}",
            flush=True,
        )
        return None


def _display_domain(domain: str) -> str:
    """Sanitize the (untrusted, LLM-authored) domain for prompt display."""
    clean = _DOMAIN_SANITIZE_RE.sub(" ", domain).strip()
    if len(clean) > _MAX_DOMAIN_DISPLAY:
        clean = clean[: _MAX_DOMAIN_DISPLAY - 1] + "…"
    return clean or "general"


def build_shots_callback(
    message_text: str,
    persona_id: str | None,
    *,
    fired_keys: Any,
    conversation_key: str,
    db_path: str | Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """One compact track-record context line for this turn, or ("", decision).

    ``fired_keys`` is only CHECKED here (``in``); the caller marks
    ``decision["dedup_key"]`` fired after accepting the block. Stats are
    rendered FIRST so no truncation of the (sanitized, capped) domain can
    ever strip the counts.
    """
    decision: dict[str, Any] = {
        "fired": False,
        "reason": "gate_closed",
        "domain": "",
        "dedup_key": None,
    }
    try:
        from security import kill_switches  # Rule 3 — module-attribute lookup

        from config import get_called_shots_settings

        if persona_id is None:
            # Fail-closed resolver upstream — never read on a guessed persona.
            decision["reason"] = "persona_unresolvable"
            return "", decision
        if kill_switches.is_disabled("called_shots"):
            decision["reason"] = "kill_switch"
            return "", decision
        settings = get_called_shots_settings()
        if not settings.enabled:
            decision["reason"] = "soft_off"
            return "", decision
        text = (message_text or "").strip()
        if len(text) < _MIN_TEXT_CHARS:
            decision["reason"] = "too_short"
            return "", decision

        from cognition import called_shots as _cs

        domains = _cs.list_resolved_domains(persona_id, db_path=db_path)
        if domains is None:
            # Unreadable is not "no history" — skip silently (Kimi m1 family).
            decision["reason"] = "ledger_unreadable"
            return "", decision
        if not domains:
            decision["reason"] = "no_resolved_domains"
            return "", decision
        text_norm = text.casefold()
        # Longest domain first — "pricing strategy" wins over "pricing".
        # The probe's strip().casefold() is defensive (LOW-2): T1's
        # _normalize_domain already strips+casefolds at write; this guards
        # rows minted by any future write path without re-deciding the grain.
        match = next(
            (
                d
                for d in sorted(domains, key=len, reverse=True)
                if d.strip().casefold() in text_norm
            ),
            None,
        )
        if match is None:
            decision["reason"] = "no_domain_match"
            return "", decision
        decision["domain"] = match

        key = (conversation_key or "global", match)
        decision["dedup_key"] = key
        if key in fired_keys:
            decision["reason"] = "deduped"
            return "", decision

        record = _cs.track_record(persona_id, match, db_path=db_path)
        if not record.ok:
            # Kimi m1: an unreadable ledger must never render as "no history".
            decision["reason"] = "ledger_unreadable"
            return "", decision
        if record.resolved <= 0:
            decision["reason"] = "no_resolved_rows"
            return "", decision

        push_part = f" / push {record.push}" if record.push else ""
        # STATS FIRST (survive any tail truncation), sanitized domain LAST.
        line = (
            "# Called-Shots Track Record (private context — mention only if "
            "relevant, one line, no gloating)\n"
            f"Settled {record.resolved} bet(s): operator right "
            f"{record.operator_right} / me right {record.homie_right}"
            f"{push_part} — on '{_display_domain(match)}'."
        )

        decision.update(fired=True, reason="fired")
        return line, decision
    except Exception as exc:  # whole-body fail-open — bare turn, never broken
        decision["reason"] = "error"
        print(f"[shots_callback] non-blocking failure: {exc!r}", flush=True)
        return "", decision
