"""Stale called-shots sweep — T3 #189 (epic #186).

Rides the EXISTING daily-reflection post-step chain (no new scheduled task):
a pure-Python age check over the open ledger. Shots older than
``CALLED_SHOTS_STALE_AGE_DAYS`` get ONE daily-log receipt line so the
operator sees unsettled bets during the normal morning read — pull-only v1
(no push notification, no heartbeat wiring).

Gating (Kimi L1 contract): this is an AUTONOMOUS surface —
``CALLED_SHOTS_ENABLED`` soft-OFF darkens it, and the hard kill-switch is
CHECKED (never raised — a cron post-step must not throw). Operator ``/shots``
commands are deliberately NOT gated by the soft toggle.

Silent contract: ``SHOTS_SWEEP_SILENT`` when disabled, empty, or all-fresh —
the DREAM_SILENT pattern. Failure never escapes to the caller; the reflect
post-step wraps this anyway (belt + suspenders).
"""

from __future__ import annotations

from datetime import UTC, datetime


def run_called_shots_sweep(test_mode: bool = False) -> str:
    """Sweep open shots for staleness; receipt to the daily log.

    Returns ``SHOTS_SWEEP_SILENT`` or a one-line summary string.
    """
    try:
        from security import kill_switches  # Rule 3 — module-attribute lookup

        from config import get_called_shots_settings

        if kill_switches.is_disabled("called_shots"):
            return "SHOTS_SWEEP_SILENT"
        settings = get_called_shots_settings()
        if not settings.enabled:  # autonomous surface — soft toggle applies
            return "SHOTS_SWEEP_SILENT"

        from cognition import called_shots as _cs

        try:
            # ro probe (MINOR-2): the legacy list_open rides the schema-
            # CREATING _connect — an enabled-but-unused install would
            # manufacture an empty ledger on every daily reflection. The
            # checked read can never create the DB and is failure-honest.
            open_shots, ok = _cs.list_open_checked(None)
        except kill_switches.KillSwitchDisabled:
            return "SHOTS_SWEEP_SILENT"  # flipped between check and call
        if not ok:
            # Honest failure: an unreadable ledger must never read as
            # "nothing stale" (the dishonest-empty class). The reflect
            # post-step prints any non-SILENT return, so this receipt lands
            # in the reflect log verbatim.
            return "ledger UNREADABLE (see bot log)"
        if not open_shots:
            return "SHOTS_SWEEP_SILENT"

        now = datetime.now(UTC)
        threshold = float(settings.stale_age_days)
        stale = []
        for shot in open_shots:
            age = _cs.shot_age_days(shot.created_at, now)
            if age is not None and age >= threshold:
                stale.append((shot, int(age)))
        if not stale:
            return "SHOTS_SWEEP_SILENT"

        detail = ", ".join(
            f"#{shot.id} {shot.persona_id}/{shot.domain or 'general'} ({age}d)"
            for shot, age in stale
        )
        receipt = (
            f"Stale called-shots ({len(stale)} open >= {int(threshold)}d): "
            f"{detail} — settle with /shots resolve <id> <outcome> "
            f"(void = strike a bad bet)"
        )
        if not test_mode:
            try:
                from shared import append_to_daily_log

                append_to_daily_log(receipt, "Called Shots")
            except Exception as exc:  # receipt failure never fails the sweep
                print(f"[shots-sweep] daily-log receipt failed: {exc!r}", flush=True)
        return f"SHOTS_SWEEP: {len(stale)} stale open shot(s)"
    except Exception as exc:  # never escape into the reflection pipeline
        print(f"[shots-sweep] non-blocking failure: {exc!r}", flush=True)
        return "SHOTS_SWEEP_SILENT"
