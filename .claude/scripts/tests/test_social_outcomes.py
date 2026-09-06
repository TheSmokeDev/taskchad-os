from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from social.outcomes import (
    build_outcome,
    list_outcomes,
    parse_outcome_arguments,
    record_outcome,
    validate_metrics,
)

WHEN = datetime(2026, 9, 3, 15, 30, tzinfo=UTC)


def test_metric_contract_separates_counts_from_github_deltas() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        validate_metrics({"post_saves": -1})
    assert validate_metrics({"stars_delta": -2}) == {"stars_delta": -2}
    with pytest.raises(ValueError, match="unknown outcome metrics"):
        validate_metrics({"likes": 10})


def test_outcome_is_deterministic_and_never_attributed_as_conversion() -> None:
    first = build_outcome(
        "repo:your-github-user/geo-skills",
        {"stars_delta": 5, "forks_delta": 1},
        observed_at=WHEN,
        recorded_at=WHEN,
        note="Seven-day observation window",
    )
    second = build_outcome(
        "repo:your-github-user/geo-skills",
        {"forks_delta": 1, "stars_delta": 5},
        observed_at=WHEN,
        recorded_at=WHEN,
        note="Seven-day observation window",
    )

    assert first.outcome_id == second.outcome_id
    assert first.conversion_attribution is False
    assert first.github_attribution == "correlated_movement_not_conversion"


def test_record_is_idempotent_and_writes_socials_experience(tmp_path: Path) -> None:
    store = tmp_path / "social_outcomes.jsonl"
    profile = tmp_path / "socials"
    kwargs = {
        "observed_at": WHEN,
        "recorded_at": WHEN,
        "note": "Operator entered after reviewing the public receipt.",
        "store_path": store,
        "persona_root": profile,
        "reindex": False,
    }
    first = record_outcome("post:42", {"post_saves": 8}, **kwargs)
    second = record_outcome("post:42", {"post_saves": 8}, **kwargs)

    assert first["status"] == "written"
    assert second["status"] == "duplicate"
    assert len(store.read_text(encoding="utf-8").splitlines()) == 1
    note_path = profile / "memory" / "experience" / "2026-09-03.md"
    body = note_path.read_text(encoding="utf-8")
    assert body.count("## 15:30 - ingest: social-outcome:") == 1
    assert '"capability_effect": "none"' in body
    assert "Grants no tools, capabilities, autonomy" in body


def test_list_outcomes_filters_subject_and_ignores_malformed_rows(tmp_path: Path) -> None:
    store = tmp_path / "outcomes.jsonl"
    for subject, minute in (("post:1", 1), ("post:2", 2), ("post:1", 3)):
        observed = WHEN.replace(minute=minute)
        record_outcome(
            subject,
            {"profile_views": minute},
            observed_at=observed,
            recorded_at=observed,
            store_path=store,
            persona_root=tmp_path / "profile",
            reindex=False,
        )
    with store.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")

    rows = list_outcomes(subject_id="post:1", store_path=store)
    assert [row["metrics"]["profile_views"] for row in rows] == [3, 1]


def test_parse_outcome_arguments_supports_all_manual_metrics() -> None:
    subject, metrics, note = parse_outcome_arguments(
        'article:/blog/geo gsc-impressions=120 ai_citations=2 note="verified in GSC"'
    )
    assert subject == "article:/blog/geo"
    assert metrics == {"ai_citations": 2, "gsc_impressions": 120}
    assert note == "verified in GSC"


def test_secret_like_notes_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="credential-like"):
        record_outcome(
            "post:9",
            {"qualified_dms": 1},
            note="api_key=do-not-store-this-value",
            store_path=tmp_path / "outcomes.jsonl",
            persona_root=tmp_path / "profile",
            reindex=False,
        )


def test_jsonl_row_carries_correlation_label(tmp_path: Path) -> None:
    store = tmp_path / "outcomes.jsonl"
    record_outcome(
        "repo:TheSmokeDev/taskchad-os",
        {"repo_clones_delta": 4},
        observed_at=WHEN,
        recorded_at=WHEN,
        store_path=store,
        persona_root=tmp_path / "profile",
        reindex=False,
    )
    row = json.loads(store.read_text(encoding="utf-8"))
    assert row["github_attribution"] == "correlated_movement_not_conversion"
    assert row["conversion_attribution"] is False
