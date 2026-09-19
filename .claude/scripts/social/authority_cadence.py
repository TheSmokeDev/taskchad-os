"""Deterministic scheduler for the source-backed GEO Authority lane.

The old social cadence chooses a random topic.  This module does the opposite:
it runs one bounded research pass, selects one still-valid
``AuthoritySignalPacket`` for a named editorial slot, and hands the exact
packet to the strict LinkedIn authority bridge.  Empty, expired, duplicate, or
unsupported slots are truthful no-ops; they never fall back to filler.

The module is inert unless ``AUTHORITY_ENGINE_ENABLED=true``.  In particular,
the disabled path resolves no persona files, imports no research providers,
and performs no network, model, queue, Telegram, or state writes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

_PACIFIC = ZoneInfo("America/Los_Angeles")
_ENGINE_FLAG = "AUTHORITY_ENGINE_ENABLED"
_PERSONA_ID = "socials"
_STATE_SCHEMA = 1
_MAX_HEARTBEAT_CHARS = 8_000
_OPERATION_STALE_AFTER = timedelta(hours=2)
_RESEARCH_DUE = time(hour=6, minute=30)
_SLOT_DUE = time(hour=7, minute=0)
_ROTATION_ANCHOR_MONDAY = date(2026, 8, 31)

REPO_ROTATION: tuple[str, ...] = (
    "hermes-talk",
    "taskchad-os",
    "hermes-talk",
    "geo-skills",
)

_MONDAY_SERIES: tuple[str, ...] = (
    "GEO Signal",
    "Citation Anatomy",
    "AI Search Teardown",
    "Stack Drop",
    "GEO Tip",
    "Myth vs Receipt",
)
_WEDNESDAY_SERIES: tuple[str, ...] = (
    "GEO Tip",
    "Myth vs Receipt",
    "Citation Anatomy",
    "AI Search Teardown",
    "GEO Signal",
)
_ARTICLE_SERIES: tuple[str, ...] = (
    "AI Search Teardown",
    "Citation Anatomy",
    "Myth vs Receipt",
    "GEO Tip",
    "GEO Signal",
    "Stack Drop",
    "Factory Floor / Dark Factory",
)
_EVIDENCE_RANK = {
    "verified_operator_receipt": 5,
    "verified_repository": 4,
    "public_primary": 3,
    "public_vendor_research": 2,
    "public_practitioner_report": 1,
}
_RESOURCE_TERMS = (
    "playbook",
    "template",
    "checklist",
    "resource",
    "stack",
    "guide",
)
_RESOURCE_ACTIONS = ("comment", "dm", "message", "reply", "send")

CadenceMode = Literal["auto", "research", "slot"]
SlotKind = Literal["geo_signal", "article", "geo_howto", "repo_field_note"]


@dataclass(frozen=True, slots=True)
class HeartbeatChecklist:
    """Read-only proof that the authority job consumed Socials HEARTBEAT."""

    status: str
    digest: str | None = None
    chars: int = 0
    text: str = field(default="", repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {"status": self.status, "digest": self.digest, "chars": self.chars}


@dataclass(frozen=True, slots=True)
class CadenceReceipt:
    status: str
    mode: str
    local_date: str
    slot: str | None = None
    signal_id: str | None = None
    post_id: int | None = None
    repository: str | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    heartbeat: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _engine_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return str(values.get(_ENGINE_FLAG, "false")).strip().lower() == "true"


def _resolve_state_path() -> Path:
    import config

    return Path(config.STATE_DIR) / "authority-cadence-state.json"


def _resolve_packet_dir() -> Path:
    from business_signal.config import AUTHORITY_SIGNAL_DIR

    return Path(AUTHORITY_SIGNAL_DIR)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"schema_version": _STATE_SCHEMA}
    if not isinstance(payload, dict) or payload.get("schema_version") != _STATE_SCHEMA:
        return {"schema_version": _STATE_SCHEMA}
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    from shared import atomic_write_text

    state["schema_version"] = _STATE_SCHEMA
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _read_authority_heartbeat() -> HeartbeatChecklist:
    """Read Socials HEARTBEAT as an advisory checklist, never as control data."""

    try:
        from personas.core import get_persona_paths

        path = get_persona_paths(_PERSONA_ID)["memory"] / "HEARTBEAT.md"
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HeartbeatChecklist(status="missing")
    except (OSError, UnicodeError, ValueError):
        return HeartbeatChecklist(status="unreadable")
    text = raw[:_MAX_HEARTBEAT_CHARS]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return HeartbeatChecklist(status="read", digest=digest, chars=len(raw), text=text)


def _local_now(now: datetime | None) -> datetime:
    resolved = now or datetime.now(UTC)
    if resolved.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return resolved.astimezone(_PACIFIC)


def _operation_key(kind: str, local_day: date) -> str:
    return f"{kind}:{local_day.isoformat()}"


def _claim_operation(path: Path, key: str, now: datetime) -> tuple[str, dict[str, Any]]:
    """Atomically claim one day/slot, recovering only genuinely stale claims."""

    from shared import file_lock

    with file_lock(path, timeout=10.0):
        state = _load_state(path)
        completed = state.setdefault("completed", {})
        if key in completed:
            return "completed", dict(completed[key])
        inflight = state.setdefault("inflight", {})
        started_raw = inflight.get(key)
        if started_raw:
            try:
                started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
            except ValueError:
                started = now - _OPERATION_STALE_AFTER - timedelta(seconds=1)
            if started.tzinfo:
                elapsed = now.astimezone(UTC) - started.astimezone(UTC)
                if elapsed < _OPERATION_STALE_AFTER:
                    return "busy", {"started_at": started.isoformat()}
        inflight[key] = now.astimezone(UTC).isoformat()
        _save_state(path, state)
        return "claimed", state


def _finish_operation(
    path: Path,
    key: str,
    receipt: CadenceReceipt,
    *,
    terminal: bool,
    consumed_signal_id: str | None = None,
    consumed_dedup_key: str | None = None,
    additional_consumed: Sequence[tuple[str, str]] = (),
    resource_week: str | None = None,
) -> None:
    from shared import file_lock

    with file_lock(path, timeout=10.0):
        state = _load_state(path)
        state.setdefault("inflight", {}).pop(key, None)
        if terminal:
            state.setdefault("completed", {})[key] = receipt.as_dict()
        if consumed_signal_id:
            state.setdefault("consumed_signal_ids", {})[consumed_signal_id] = key
        if consumed_dedup_key:
            state.setdefault("consumed_dedup_keys", {})[consumed_dedup_key] = key
        for signal_id, dedup_key in additional_consumed:
            state.setdefault("consumed_signal_ids", {})[signal_id] = key
            state.setdefault("consumed_dedup_keys", {})[dedup_key] = key
        if resource_week:
            state.setdefault("resource_drop_weeks", {})[resource_week] = key
        _save_state(path, state)


def _slot_for_day(day: date) -> SlotKind | None:
    return {
        0: "geo_signal",
        1: "article",
        2: "geo_howto",
        4: "repo_field_note",
    }.get(day.weekday())


def repository_for_day(day: date) -> str:
    """Return the locked four-week Friday rotation for a Pacific-local date."""

    monday = day - timedelta(days=day.weekday())
    weeks = (monday - _ROTATION_ANCHOR_MONDAY).days // 7
    return REPO_ROTATION[weeks % len(REPO_ROTATION)]


def _week_key(day: date) -> str:
    year, week, _weekday = day.isocalendar()
    return f"{year}-W{week:02d}"


def _is_resource_drop_cta(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(term in normalized for term in _RESOURCE_TERMS) and any(
        action in normalized for action in _RESOURCE_ACTIONS
    )


def _packet_sort_key(packet: Any, preferred: Sequence[str]) -> tuple[Any, ...]:
    try:
        series_rank = len(preferred) - preferred.index(packet.content_series)
    except ValueError:
        series_rank = 0
    return (
        series_rank,
        _EVIDENCE_RANK.get(str(packet.evidence_class), 0),
        packet.observed_at,
        packet.signal_id,
    )


def _load_valid_packets(
    *,
    packet_dir: Path,
    now: datetime,
    queue_loader: Callable[..., list[dict[str, Any]]] | None,
) -> list[Any]:
    from business_signal.models import AuthoritySignalPacket

    if queue_loader is None:
        from business_signal.authority import list_authority_queue

        queue_loader = list_authority_queue
    rows = queue_loader(output_dir=packet_dir, now=now.astimezone(UTC), limit=100)
    root = packet_dir.resolve(strict=False)
    packets: list[AuthoritySignalPacket] = []
    for row in rows:
        try:
            path = Path(str(row["packet_path"])).resolve(strict=False)
            path.relative_to(root)
            if path.stat().st_size > 1_000_000:
                continue
            packet = AuthoritySignalPacket.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if packet.expires_at <= now.astimezone(UTC):
                continue
            packet.to_public_dict()
            packets.append(packet)
        except (KeyError, OSError, UnicodeError, ValueError):
            continue
    return packets


def _select_packet(
    packets: Sequence[Any],
    *,
    slot: SlotKind,
    day: date,
    state: Mapping[str, Any],
) -> Any | None:
    used_ids = set((state.get("consumed_signal_ids") or {}).keys())
    used_dedup = set((state.get("consumed_dedup_keys") or {}).keys())
    available = [
        packet
        for packet in packets
        if packet.signal_id not in used_ids and packet.dedup_key not in used_dedup
    ]
    if slot == "repo_field_note":
        repository = repository_for_day(day)
        available = [
            packet
            for packet in available
            if packet.content_series == "Repo Field Note"
            and packet.destination_repo
            and packet.destination_repo.rsplit("/", 1)[-1].casefold() == repository
        ]
        preferred = ("Repo Field Note",)
    elif slot == "article":
        available = [packet for packet in available if packet.article_route == "/blog"]
        preferred = _ARTICLE_SERIES
    elif slot == "geo_signal":
        available = [
            packet for packet in available if packet.content_series in _MONDAY_SERIES
        ]
        preferred = _MONDAY_SERIES
    else:
        available = [
            packet for packet in available if packet.content_series in _WEDNESDAY_SERIES
        ]
        preferred = _WEDNESDAY_SERIES
    return max(available, key=lambda packet: _packet_sort_key(packet, preferred), default=None)


def _select_article_packets(
    packets: Sequence[Any],
    *,
    state: Mapping[str, Any],
) -> tuple[Any, Any] | None:
    """Choose exactly two unconsumed packets with two URLs and primary proof."""

    used_ids = set((state.get("consumed_signal_ids") or {}).keys())
    used_dedup = set((state.get("consumed_dedup_keys") or {}).keys())
    available = [
        packet
        for packet in packets
        if packet.signal_id not in used_ids
        and packet.dedup_key not in used_dedup
        and packet.article_route == "/blog"
        and packet.content_series in _ARTICLE_SERIES
    ]
    available.sort(
        key=lambda packet: _packet_sort_key(packet, _ARTICLE_SERIES), reverse=True
    )
    for lead_index, lead in enumerate(available):
        lead_urls = {claim.source_url for claim in lead.claims}
        for supporting in available[lead_index + 1 :]:
            all_claims = (*lead.claims, *supporting.claims)
            urls = lead_urls | {claim.source_url for claim in supporting.claims}
            if len(urls) >= 2 and any(claim.primary_source for claim in all_claims):
                return lead, supporting
    return None


def _normalize_result(value: Any) -> dict[str, Any]:
    if hasattr(value, "as_dict") and callable(value.as_dict):
        result = value.as_dict()
    elif is_dataclass(value):
        result = asdict(value)
    elif isinstance(value, dict):
        result = value
    else:
        result = {"status": "unknown", "value": str(value)[:500]}
    return json.loads(json.dumps(result, default=str))


def _run_awaitable(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RuntimeError("authority cadence sync entrypoint cannot run inside an event loop")


def _default_refresh_runner(*, dry_run: bool, observed_at: datetime) -> Any:
    from business_signal.authority import run_authority_refresh

    return run_authority_refresh(dry_run=dry_run, observed_at=observed_at)


def _default_draft_creator(packet: Any, **kwargs: Any) -> Any:
    from social.authority_content import create_authority_linkedin_draft

    return create_authority_linkedin_draft(packet, **kwargs)


def _default_article_handoff(packets: Sequence[Any], **kwargs: Any) -> Any:
    try:
        from social.tenant_insights import (
            _YourProduct_bridge_unavailable_reason,
            create_insights_package,
        )
    except ImportError:
        return {
            "status": "bridge_unavailable",
            "reason": "Tenant Insights preview bridge is not installed in this runtime",
        }
    if reason := _YourProduct_bridge_unavailable_reason():
        return {"status": "bridge_unavailable", "reason": reason}
    return create_insights_package(packets, **kwargs)


def _run_research(
    *,
    local: datetime,
    state_path: Path,
    checklist: HeartbeatChecklist,
    dry_run: bool,
    refresh_runner: Callable[..., Any] | None,
) -> CadenceReceipt:
    key = _operation_key("research", local.date())
    if not dry_run:
        claim, previous = _claim_operation(state_path, key, local)
        if claim != "claimed":
            return CadenceReceipt(
                status="already_complete" if claim == "completed" else "busy",
                mode="research",
                local_date=local.date().isoformat(),
                reasons=(f"research operation is {claim}",),
                heartbeat=checklist.public_dict(),
                detail=previous,
            )
    runner = refresh_runner or _default_refresh_runner
    try:
        result = _run_awaitable(
            runner(dry_run=dry_run, observed_at=local.astimezone(UTC))
        )
        detail = _normalize_result(result)
        status = str(detail.get("status") or "unknown")
        terminal = dry_run or status in {"success", "AUTHORITY_SILENT", "dry_run"}
        receipt = CadenceReceipt(
            status=status,
            mode="research",
            local_date=local.date().isoformat(),
            reasons=tuple(str(item) for item in detail.get("reasons", [])[:20]),
            heartbeat=checklist.public_dict(),
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - scheduled receipt must survive providers
        terminal = False
        receipt = CadenceReceipt(
            status="failed",
            mode="research",
            local_date=local.date().isoformat(),
            reasons=(f"refresh_failed:{type(exc).__name__}",),
            heartbeat=checklist.public_dict(),
        )
    if not dry_run:
        _finish_operation(state_path, key, receipt, terminal=terminal)
    return receipt


def _run_slot(
    *,
    local: datetime,
    state_path: Path,
    packet_dir: Path,
    checklist: HeartbeatChecklist,
    dry_run: bool,
    db_path: str | Path | None,
    deliver: bool,
    queue_loader: Callable[..., list[dict[str, Any]]] | None,
    draft_creator: Callable[..., Any] | None,
    article_handoff: Callable[..., Any] | None,
) -> CadenceReceipt:
    slot = _slot_for_day(local.date())
    if slot is None:
        return CadenceReceipt(
            status="no_slot",
            mode="slot",
            local_date=local.date().isoformat(),
            reasons=("No authority content slot is scheduled for this Pacific-local day",),
            heartbeat=checklist.public_dict(),
        )
    key = _operation_key(f"slot:{slot}", local.date())
    if not dry_run:
        claim, previous = _claim_operation(state_path, key, local)
        if claim != "claimed":
            return CadenceReceipt(
                status="already_complete" if claim == "completed" else "busy",
                mode="slot",
                local_date=local.date().isoformat(),
                slot=slot,
                reasons=(f"slot operation is {claim}",),
                heartbeat=checklist.public_dict(),
                detail=previous,
            )

    try:
        state = _load_state(state_path)
        packets = _load_valid_packets(
            packet_dir=packet_dir,
            now=local,
            queue_loader=queue_loader,
        )
        article_packets = (
            _select_article_packets(packets, state=state) if slot == "article" else None
        )
        packet = (
            article_packets[0]
            if article_packets is not None
            else _select_packet(packets, slot=slot, day=local.date(), state=state)
        )
        if packet is None or (slot == "article" and article_packets is None):
            receipt = CadenceReceipt(
                status="no_signal",
                mode="slot",
                local_date=local.date().isoformat(),
                slot=slot,
                repository=(
                    repository_for_day(local.date()) if slot == "repo_field_note" else None
                ),
                reasons=("No fresh, validated, unconsumed packet matched this slot",),
                heartbeat=checklist.public_dict(),
            )
            if not dry_run:
                _finish_operation(state_path, key, receipt, terminal=True)
            return receipt

        resource_week = _week_key(local.date())
        is_resource = _is_resource_drop_cta(packet.cta_brief)
        already_used = resource_week in (state.get("resource_drop_weeks") or {})
        allow_resource = is_resource and not already_used

        if dry_run:
            return CadenceReceipt(
                status="dry_run",
                mode="slot",
                local_date=local.date().isoformat(),
                slot=slot,
                signal_id=packet.signal_id,
                repository=packet.destination_repo,
                reasons=("No model, media, queue, Telegram, or article bridge call was made",),
                heartbeat=checklist.public_dict(),
                detail={
                    "allow_resource_drop": allow_resource,
                    "supporting_signal_ids": (
                        [item.signal_id for item in article_packets[1:]]
                        if article_packets
                        else []
                    ),
                },
            )

        if slot == "article":
            handoff = article_handoff or _default_article_handoff
            detail = _normalize_result(
                handoff(
                    article_packets,
                    now=local.astimezone(UTC),
                    deliver=deliver,
                )
            )
            handoff_status = str(detail.get("status") or "unknown")
            queued = handoff_status in {
                "queued",
                "preview_pending",
                "created",
                "awaiting_content_approval",
            }
            receipt = CadenceReceipt(
                status="article_handoff" if queued else "article_noop",
                mode="slot",
                local_date=local.date().isoformat(),
                slot=slot,
                signal_id=packet.signal_id,
                reasons=(str(detail.get("reason")),) if detail.get("reason") else (),
                heartbeat=checklist.public_dict(),
                detail=detail,
            )
            _finish_operation(
                state_path,
                key,
                receipt,
                terminal=True,
                consumed_signal_id=packet.signal_id if queued else None,
                consumed_dedup_key=packet.dedup_key if queued else None,
                additional_consumed=(
                    ((article_packets[1].signal_id, article_packets[1].dedup_key),)
                    if queued and article_packets is not None
                    else ()
                ),
            )
            return receipt

        creator = draft_creator or _default_draft_creator
        detail = _normalize_result(
            creator(
                packet,
                now=local.astimezone(UTC),
                allow_resource_drop=allow_resource,
                db_path=db_path,
                deliver=deliver,
            )
        )
        queued = detail.get("status") == "queued" and detail.get("post_id") is not None
        receipt = CadenceReceipt(
            status="queued" if queued else "no_draft",
            mode="slot",
            local_date=local.date().isoformat(),
            slot=slot,
            signal_id=packet.signal_id,
            post_id=int(detail["post_id"]) if queued else None,
            repository=packet.destination_repo,
            reasons=tuple(str(item) for item in detail.get("reasons", [])[:20]),
            heartbeat=checklist.public_dict(),
            detail=detail,
        )
        _finish_operation(
            state_path,
            key,
            receipt,
            terminal=True,
            consumed_signal_id=packet.signal_id if queued else None,
            consumed_dedup_key=packet.dedup_key if queued else None,
            resource_week=resource_week if queued and allow_resource else None,
        )
        return receipt
    except Exception as exc:  # noqa: BLE001 - one bad slot cannot become filler
        receipt = CadenceReceipt(
            status="failed",
            mode="slot",
            local_date=local.date().isoformat(),
            slot=slot,
            reasons=(f"slot_failed:{type(exc).__name__}",),
            heartbeat=checklist.public_dict(),
        )
        if not dry_run:
            _finish_operation(state_path, key, receipt, terminal=False)
        return receipt


def run_authority_cadence(
    *,
    mode: CadenceMode = "auto",
    now: datetime | None = None,
    dry_run: bool = False,
    state_path: Path | None = None,
    packet_dir: Path | None = None,
    db_path: str | Path | None = None,
    deliver: bool = True,
    environ: Mapping[str, str] | None = None,
    heartbeat_loader: Callable[[], HeartbeatChecklist] | None = None,
    refresh_runner: Callable[..., Any] | None = None,
    queue_loader: Callable[..., list[dict[str, Any]]] | None = None,
    draft_creator: Callable[..., Any] | None = None,
    article_handoff: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run a due research/slot tick while preserving the disabled hard gate."""

    if mode not in {"auto", "research", "slot"}:
        raise ValueError("mode must be auto, research, or slot")
    local = _local_now(now)
    if not _engine_enabled(environ):
        return CadenceReceipt(
            status="disabled",
            mode=mode,
            local_date=local.date().isoformat(),
            reasons=(f"{_ENGINE_FLAG}=false",),
        ).as_dict()

    resolved_state = Path(state_path) if state_path is not None else _resolve_state_path()
    resolved_packets = Path(packet_dir) if packet_dir is not None else _resolve_packet_dir()
    checklist = (heartbeat_loader or _read_authority_heartbeat)()

    if mode == "research":
        return _run_research(
            local=local,
            state_path=resolved_state,
            checklist=checklist,
            dry_run=dry_run,
            refresh_runner=refresh_runner,
        ).as_dict()
    if mode == "slot":
        return _run_slot(
            local=local,
            state_path=resolved_state,
            packet_dir=resolved_packets,
            checklist=checklist,
            dry_run=dry_run,
            db_path=db_path,
            deliver=deliver,
            queue_loader=queue_loader,
            draft_creator=draft_creator,
            article_handoff=article_handoff,
        ).as_dict()

    output: dict[str, Any] = {
        "status": "no_due_work",
        "mode": "auto",
        "local_date": local.date().isoformat(),
        "heartbeat": checklist.public_dict(),
    }
    state = _load_state(resolved_state)
    research_key = _operation_key("research", local.date())
    if local.timetz().replace(tzinfo=None) >= _RESEARCH_DUE and research_key not in (
        state.get("completed") or {}
    ):
        output["research"] = _run_research(
            local=local,
            state_path=resolved_state,
            checklist=checklist,
            dry_run=dry_run,
            refresh_runner=refresh_runner,
        ).as_dict()
        output["status"] = "ran"

    state = _load_state(resolved_state)
    slot = _slot_for_day(local.date())
    slot_key = _operation_key(f"slot:{slot}", local.date()) if slot else None
    if (
        slot is not None
        and local.timetz().replace(tzinfo=None) >= _SLOT_DUE
        and slot_key not in (state.get("completed") or {})
    ):
        output["slot"] = _run_slot(
            local=local,
            state_path=resolved_state,
            packet_dir=resolved_packets,
            checklist=checklist,
            dry_run=dry_run,
            db_path=db_path,
            deliver=deliver,
            queue_loader=queue_loader,
            draft_creator=draft_creator,
            article_handoff=article_handoff,
        ).as_dict()
        output["status"] = "ran"
    return output


def _receipt_exit_code(receipt: Mapping[str, Any]) -> int:
    """Expose operational failures without treating editorial no-ops as errors."""
    status = receipt.get("status")
    if status in {"failed", "error"}:
        return 1
    if status == "ran":
        return max(
            (
                _receipt_exit_code(child)
                for key in ("research", "slot")
                if isinstance(child := receipt.get(key), Mapping)
            ),
            default=0,
        )
    # Draft/article bridges preserve generation failures as skipped receipts.
    # Only these explicit failures are errors; quality gates and duplicates are
    # successful no-ops. Do not inspect historical detail on already_complete.
    if status in {"no_draft", "article_noop"}:
        detail = receipt.get("detail")
        if isinstance(detail, Mapping):
            if detail.get("status") in {"failed", "error"}:
                return 1
            failure_reasons = {
                "copy_generation_failed",
                "article_generation_failed",
                "article_media_failed",
            }
            if any(
                str(reason).split(":", 1)[0] in failure_reasons
                for reason in detail.get("reasons", ())
            ):
                return 1
    return 0


def main() -> int:
    # The scheduled process gets its feature flag from the framework .env.
    # Importing config loads that file but performs no provider or browser IO.
    import config  # noqa: F401

    parser = argparse.ArgumentParser(description="GEO Authority coordinated cadence")
    parser.add_argument("--mode", choices=("auto", "research", "slot"), default="auto")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve due work without model, media, queue, Telegram, or bridge writes",
    )
    parser.add_argument(
        "--no-deliver",
        action="store_true",
        help="Queue a live draft without sending its Telegram review package",
    )
    args = parser.parse_args()
    result = run_authority_cadence(
        mode=args.mode,
        dry_run=args.dry_run,
        deliver=not args.no_deliver,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return _receipt_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CadenceReceipt",
    "HeartbeatChecklist",
    "REPO_ROTATION",
    "repository_for_day",
    "run_authority_cadence",
]
