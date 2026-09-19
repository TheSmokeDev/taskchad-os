from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from business_signal.models import (
    AuthorityClaim,
    AuthoritySignalPacket,
    AuthorityVisualBrief,
    authority_dedup_key,
    authority_signal_id,
)
from social import authority_cadence
from social.authority_cadence import (
    HeartbeatChecklist,
    repository_for_day,
    run_authority_cadence,
)


def _now(day: int, hour: int = 7) -> datetime:
    # September 7, 2026 is Monday. Pacific daylight time is UTC-7.
    return datetime(2026, 9, day, hour + 7, tzinfo=UTC)


def _packet(
    root: Path,
    *,
    token: str,
    observed: datetime,
    series: str = "GEO Signal",
    cta: str = "Read the source and test the pattern.",
    repository: str | None = None,
) -> Path:
    url = f"https://example.com/{token}"
    dedup = authority_dedup_key(url, token)
    packet = AuthoritySignalPacket(
        signal_id=authority_signal_id(dedup, observed),
        signal_type="repository_event" if repository else "practical_evidence",
        observed_at=observed,
        expires_at=observed + timedelta(days=14),
        dedup_key=dedup,
        audience="founders and operators",
        content_series=series,
        claims=(
            AuthorityClaim(
                text=f"This is one exact supported authority claim for {token}.",
                source_url=url,
                source_title=f"Source {token}",
                source_date=observed.date(),
                source_class="repository" if repository else "official_documentation",
                primary_source=True,
                confidence=0.9,
            ),
        ),
        prohibited_claims=(),
        privacy_notes=(),
        article_brief=f"Write a detailed source-backed article about {token}.",
        social_brief=f"Teach the practical source-backed lesson from {token}.",
        cta_brief=cta,
        repo_brief=f"Explain the verified repository event for {token}.",
        visual_brief=AuthorityVisualBrief(
            eyebrow="GEO SIGNAL",
            headline=f"A verified lesson from {token}",
            accent="SOURCE BACKED",
            subhead="One bounded claim",
            cta="Read the source",
        ),
        destination_repo=repository,
        article_route=None if repository else "/blog",
        evidence_class="verified_repository" if repository else "public_primary",
        first_person_allowed=False,
    )
    path = root / f"{packet.signal_id}.json"
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet.to_public_dict()), encoding="utf-8")
    return path


def _queue(root: Path, **_kwargs: Any) -> list[dict[str, Any]]:
    return [{"packet_path": str(path)} for path in sorted(root.glob("as_*.json"))]


def _heartbeat() -> HeartbeatChecklist:
    return HeartbeatChecklist(status="read", digest="a" * 64, chars=42, text="check")


def test_disabled_gate_performs_no_reads_calls_or_writes(tmp_path: Path) -> None:
    called: list[str] = []

    def bomb(*_args: Any, **_kwargs: Any) -> Any:
        called.append("called")
        raise AssertionError("disabled gate leaked")

    state = tmp_path / "state.json"
    result = run_authority_cadence(
        now=_now(7, 7),
        environ={},
        state_path=state,
        packet_dir=tmp_path / "packets",
        heartbeat_loader=bomb,
        refresh_runner=bomb,
        queue_loader=bomb,
        draft_creator=bomb,
    )

    assert result["status"] == "disabled"
    assert called == []
    assert not state.exists()


def test_auto_research_is_daily_and_idempotent(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def refresh(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "AUTHORITY_SILENT", "reasons": ["weak signal"]}

    kwargs = {
        "mode": "auto",
        "now": _now(7, 6) + timedelta(minutes=30),
        "environ": {"AUTHORITY_ENGINE_ENABLED": "true"},
        "state_path": tmp_path / "state.json",
        "packet_dir": tmp_path / "packets",
        "heartbeat_loader": _heartbeat,
        "refresh_runner": refresh,
    }
    first = run_authority_cadence(**kwargs)
    second = run_authority_cadence(**kwargs)

    assert first["research"]["status"] == "AUTHORITY_SILENT"
    assert second["status"] == "no_due_work"
    assert len(calls) == 1
    assert calls[0]["dry_run"] is False


def test_monday_packet_uses_strict_queue_bridge(tmp_path: Path) -> None:
    packets = tmp_path / "packets"
    _packet(packets, token="monday", observed=_now(6))
    captured: dict[str, Any] = {}

    def create(packet: AuthoritySignalPacket, **kwargs: Any) -> dict[str, Any]:
        captured["packet"] = packet
        captured["kwargs"] = kwargs
        return {"status": "queued", "post_id": 41, "reasons": []}

    result = run_authority_cadence(
        mode="slot",
        now=_now(7),
        environ={"AUTHORITY_ENGINE_ENABLED": "true"},
        state_path=tmp_path / "state.json",
        packet_dir=packets,
        heartbeat_loader=_heartbeat,
        queue_loader=lambda **kwargs: _queue(packets, **kwargs),
        draft_creator=create,
        deliver=False,
    )

    assert result["status"] == "queued"
    assert result["slot"] == "geo_signal"
    assert result["post_id"] == 41
    assert captured["packet"].content_series == "GEO Signal"
    assert captured["kwargs"]["deliver"] is False
    assert "autopilot" not in captured["kwargs"]


def test_empty_slot_is_terminal_no_filler(tmp_path: Path) -> None:
    called = False

    def create(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError

    kwargs = {
        "mode": "slot",
        "now": _now(9),  # Wednesday
        "environ": {"AUTHORITY_ENGINE_ENABLED": "true"},
        "state_path": tmp_path / "state.json",
        "packet_dir": tmp_path / "packets",
        "heartbeat_loader": _heartbeat,
        "queue_loader": lambda **_kwargs: [],
        "draft_creator": create,
    }
    first = run_authority_cadence(**kwargs)
    second = run_authority_cadence(**kwargs)

    assert first["status"] == "no_signal"
    assert second["status"] == "already_complete"
    assert called is False


def test_resource_drop_is_allowed_at_most_once_per_iso_week(tmp_path: Path) -> None:
    packets = tmp_path / "packets"
    _packet(
        packets,
        token="resource-monday",
        observed=_now(6),
        series="GEO Signal",
        cta="Comment and I will send the GEO playbook.",
    )
    _packet(
        packets,
        token="resource-wednesday",
        observed=_now(8),
        series="GEO Tip",
        cta="DM me for the implementation checklist.",
    )
    allowed: list[bool] = []

    def create(packet: AuthoritySignalPacket, **kwargs: Any) -> dict[str, Any]:
        allowed.append(kwargs["allow_resource_drop"])
        return {"status": "queued", "post_id": len(allowed), "reasons": []}

    common = {
        "environ": {"AUTHORITY_ENGINE_ENABLED": "true"},
        "state_path": tmp_path / "state.json",
        "packet_dir": packets,
        "heartbeat_loader": _heartbeat,
        "queue_loader": lambda **kwargs: _queue(packets, **kwargs),
        "draft_creator": create,
        "deliver": False,
    }
    monday = run_authority_cadence(mode="slot", now=_now(7), **common)
    wednesday = run_authority_cadence(mode="slot", now=_now(9), **common)

    assert monday["status"] == "queued"
    assert wednesday["status"] == "queued"
    assert allowed == [True, False]


def test_tuesday_bridge_absence_is_bounded_noop(tmp_path: Path) -> None:
    packets = tmp_path / "packets"
    _packet(packets, token="article-a", observed=_now(7), series="AI Search Teardown")
    _packet(packets, token="article-b", observed=_now(6), series="Citation Anatomy")
    handed_off: list[tuple[AuthoritySignalPacket, ...]] = []

    def unavailable(
        selected: tuple[AuthoritySignalPacket, ...], **_kwargs: Any
    ) -> dict[str, Any]:
        handed_off.append(selected)
        return {"status": "bridge_unavailable", "reason": "not installed"}

    result = run_authority_cadence(
        mode="slot",
        now=_now(8),
        environ={"AUTHORITY_ENGINE_ENABLED": "true"},
        state_path=tmp_path / "state.json",
        packet_dir=packets,
        heartbeat_loader=_heartbeat,
        queue_loader=lambda **kwargs: _queue(packets, **kwargs),
        article_handoff=unavailable,
    )

    assert result["status"] == "article_noop"
    assert result["detail"]["status"] == "bridge_unavailable"
    assert len(handed_off[0]) == 2


def test_tuesday_requires_two_unique_sources_before_handoff(tmp_path: Path) -> None:
    packets = tmp_path / "packets"
    _packet(packets, token="only-one", observed=_now(7), series="AI Search Teardown")
    result = run_authority_cadence(
        mode="slot",
        now=_now(8),
        environ={"AUTHORITY_ENGINE_ENABLED": "true"},
        state_path=tmp_path / "state.json",
        packet_dir=packets,
        heartbeat_loader=_heartbeat,
        queue_loader=lambda **kwargs: _queue(packets, **kwargs),
        article_handoff=lambda *_args, **_kwargs: pytest.fail("quality gate leaked"),
    )
    assert result["status"] == "no_signal"


def test_tuesday_missing_YourProduct_bridge_is_quiet_before_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from social import tenant_insights

    monkeypatch.setenv("YourProduct_REPO_PATH", str(tmp_path / "missing-YourProduct"))
    monkeypatch.setattr(
        tenant_insights,
        "create_insights_package",
        lambda *_args, **_kwargs: pytest.fail("unavailable bridge spent generation work"),
    )
    packets = tmp_path / "packets"
    _packet(packets, token="article-ready-a", observed=_now(7), series="GEO Signal")
    _packet(packets, token="article-ready-b", observed=_now(6), series="GEO Tip")
    state = tmp_path / "state.json"
    result = run_authority_cadence(
        mode="slot",
        now=_now(8),
        environ={"AUTHORITY_ENGINE_ENABLED": "true"},
        state_path=state,
        packet_dir=packets,
        heartbeat_loader=_heartbeat,
        queue_loader=lambda **kwargs: _queue(packets, **kwargs),
    )

    assert result["status"] == "article_noop"
    assert result["detail"]["status"] == "bridge_unavailable"
    assert authority_cadence._receipt_exit_code(result) == 0
    assert not json.loads(state.read_text(encoding="utf-8")).get("consumed_signal_ids")


def test_ready_default_article_bridge_preserves_handoff_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from social import tenant_insights

    captured: dict[str, Any] = {}
    packets = ("packet-a", "packet-b")
    monkeypatch.setattr(tenant_insights, "_YourProduct_bridge_unavailable_reason", lambda: None)

    def create(selected: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(packets=selected, **kwargs)
        return {"status": "awaiting_content_approval"}

    monkeypatch.setattr(tenant_insights, "create_insights_package", create)
    result = authority_cadence._default_article_handoff(packets, now=_now(8), deliver=False)
    assert result["status"] == "awaiting_content_approval"
    assert captured == {"packets": packets, "now": _now(8), "deliver": False}


def test_successful_article_package_consumes_both_packets_atomically(tmp_path: Path) -> None:
    packets = tmp_path / "packets"
    _packet(packets, token="article-primary", observed=_now(7), series="GEO Signal")
    _packet(packets, token="article-support", observed=_now(6), series="GEO Tip")
    state = tmp_path / "state.json"
    result = run_authority_cadence(
        mode="slot",
        now=_now(8),
        environ={"AUTHORITY_ENGINE_ENABLED": "true"},
        state_path=state,
        packet_dir=packets,
        heartbeat_loader=_heartbeat,
        queue_loader=lambda **kwargs: _queue(packets, **kwargs),
        article_handoff=lambda *_args, **_kwargs: {
            "status": "awaiting_content_approval",
            "package_id": "insights_test",
        },
        deliver=False,
    )
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert result["status"] == "article_handoff"
    assert len(saved["consumed_signal_ids"]) == 2
    assert len(saved["consumed_dedup_keys"]) == 2


def test_repository_rotation_is_locked() -> None:
    assert repository_for_day(date(2026, 9, 4)) == "hermes-talk"
    assert repository_for_day(date(2026, 9, 11)) == "taskchad-os"
    assert repository_for_day(date(2026, 9, 18)) == "hermes-talk"
    assert repository_for_day(date(2026, 9, 25)) == "geo-skills"
    assert repository_for_day(date(2026, 10, 2)) == "hermes-talk"


def test_packet_path_outside_authority_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "packets"
    outside = _packet(tmp_path / "outside", token="escape", observed=_now(6))
    result = run_authority_cadence(
        mode="slot",
        now=_now(7),
        environ={"AUTHORITY_ENGINE_ENABLED": "true"},
        state_path=tmp_path / "state.json",
        packet_dir=root,
        heartbeat_loader=_heartbeat,
        queue_loader=lambda **_kwargs: [{"packet_path": str(outside)}],
    )
    assert result["status"] == "no_signal"


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run_authority_cadence(now=datetime(2026, 9, 7, 7), environ={})


@pytest.mark.parametrize(
    "receipt",
    [
        {"status": "failed"},
        {"status": "error"},
        {"status": "ran", "research": {"status": "failed"}},
        {
            "status": "ran",
            "research": {"status": "success"},
            "slot": {"status": "failed"},
        },
        {"status": "no_draft", "detail": {"status": "failed"}},
        {
            "status": "no_draft",
            "detail": {"status": "skipped", "reasons": ["copy_generation_failed:RuntimeError"]},
        },
        {
            "status": "article_noop",
            "detail": {"status": "skipped", "reasons": ["article_generation_failed:RuntimeError"]},
        },
        {
            "status": "article_noop",
            "detail": {"status": "skipped", "reasons": ["article_media_failed:RuntimeError"]},
        },
    ],
)
def test_failure_receipts_have_nonzero_exit_code(receipt: dict[str, Any]) -> None:
    assert authority_cadence._receipt_exit_code(receipt) == 1


@pytest.mark.parametrize(
    "receipt",
    [
        {"status": status}
        for status in (
            "disabled",
            "no_signal",
            "no_slot",
            "no_due_work",
            "already_complete",
            "busy",
            "dry_run",
            "success",
            "AUTHORITY_SILENT",
            "queued",
            "article_handoff",
        )
    ]
    + [
        {
            "status": "ran",
            "research": {"status": "AUTHORITY_SILENT"},
            "slot": {"status": "no_signal"},
        },
        {"status": "already_complete", "detail": {"status": "failed"}},
        {"status": "article_noop", "detail": {"status": "bridge_unavailable"}},
        {
            "status": "article_noop",
            "detail": {"status": "skipped", "reasons": ["duplicate_package"]},
        },
        {"status": "no_draft", "detail": {"status": "skipped", "reasons": ["unsupported_claim"]}},
    ],
)
def test_successful_and_quiet_receipts_have_zero_exit_code(receipt: dict[str, Any]) -> None:
    assert authority_cadence._receipt_exit_code(receipt) == 0


@pytest.mark.parametrize("status, expected", [("failed", 1), ("no_signal", 0)])
def test_main_returns_exit_code_and_preserves_json_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str, expected: int
) -> None:
    # CLI wiring tests must not load the operator's config or invoke providers.
    monkeypatch.setitem(sys.modules, "config", ModuleType("config"))
    monkeypatch.setattr(sys, "argv", ["authority_cadence", "--mode", "slot", "--no-deliver"])
    captured: dict[str, Any] = {}

    def run(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": status, "mode": "slot"}

    monkeypatch.setattr(authority_cadence, "run_authority_cadence", run)
    assert authority_cadence.main() == expected
    assert json.loads(capsys.readouterr().out) == {"status": status, "mode": "slot"}
    assert captured == {"mode": "slot", "dry_run": False, "deliver": False}
