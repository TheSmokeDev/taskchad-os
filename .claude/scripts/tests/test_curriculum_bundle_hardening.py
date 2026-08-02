from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from curriculum.bundle import CurriculumBundle, _document, _read_document
from curriculum.paths import CurriculumPaths


def _paths(tmp_path: Path) -> CurriculumPaths:
    profile = tmp_path / "profile"
    data = profile / "data"
    memory = profile / "memory"
    curriculum_data = data / "curricula"
    bundle_root = memory / "curricula" / "ai-engineering"
    return CurriculumPaths(
        persona_id="ai-engineer",
        profile_root=profile,
        data_root=data,
        memory_root=memory,
        curriculum_data=curriculum_data,
        bundle_root=bundle_root,
        artifacts_root=curriculum_data / "artifacts",
        raw_root=curriculum_data / "raw",
        vendor_root=curriculum_data / "vendor",
        ledger_path=curriculum_data / "curriculum.db",
        staging_path=profile / "state" / "memory-candidates.jsonl",
    )


def _video(video_id: str = "AbC_-19") -> dict[str, str]:
    return {
        "video_id": video_id,
        "source_id": "channel",
        "url": f"https://youtube.com/watch?v={video_id}",
        "title": "Evidence systems",
        "channel": "Channel",
        "topic": "harnesses-evals",
    }


def _analysis(video_id: str = "AbC_-19", timestamp: str = "00:00:01") -> str:
    return (
        "# Executive takeaway\nEvidence matters.\n"
        "## Evidence ledger\n"
        f"- [youtube:{video_id} @ {timestamp}] A grounded claim; demo; high.\n"
        "## Canonical concepts\nEvidence ledgers.\n"
    )


def _write_raw(bundle: CurriculumBundle, video_id: str = "AbC_-19", transcript: str = "") -> Path:
    video = _video(video_id)
    return bundle.write_raw(
        source_id=video["source_id"],
        video_id=video_id,
        title=video["title"],
        url=video["url"],
        transcript_source="captions",
        transcript=transcript or "[00:00:01] Grounded evidence.",
    )


def _write_dossier(
    bundle: CurriculumBundle,
    raw: Path,
    *,
    video_id: str = "AbC_-19",
    analysis: str = "",
) -> Path:
    digest = hashlib.sha256(
        _read_document(raw)[1].split("\n", 2)[-1].strip().encode("utf-8")
    ).hexdigest()
    return bundle.write_source_dossier(
        video=_video(video_id),
        transcript_source="captions",
        analysis_markdown=analysis or _analysis(video_id),
        provider="test",
        model="test",
        runtime_lane="generic_runtime",
        raw_path=raw,
        raw_digest=digest,
    )


def test_video_identity_filename_survives_case_and_sanitization_collisions(
    tmp_path: Path,
) -> None:
    bundle = CurriculumBundle(_paths(tmp_path), "ai-engineering")
    identities = ("CaseID", "caseid", "punct.a", "punct/a")
    paths = [_write_raw(bundle, value) for value in identities]

    assert len({path.name.casefold() for path in paths}) == len(identities)
    assert paths[0].name.startswith("CaseID--")
    assert paths[1].name.startswith("caseid--")
    assert paths[2].name != paths[3].name


def test_existing_raw_is_rehashed_instead_of_trusting_frontmatter(
    tmp_path: Path,
) -> None:
    bundle = CurriculumBundle(_paths(tmp_path), "ai-engineering")
    raw = _write_raw(bundle)
    original = raw.read_text(encoding="utf-8")
    raw.write_text(
        original.replace("Grounded evidence.", "Tampered evidence."),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="digest does not match stored body"):
        _write_raw(bundle)


@pytest.mark.parametrize(
    "analysis, expected",
    [
        (
            "# Executive takeaway\nX\n## Evidence ledger\n- Claim without time.\n",
            "no timestamp citation",
        ),
        (
            _analysis(video_id="WRONG"),
            "wrong video_id",
        ),
        (
            _analysis(timestamp="00:09:59"),
            "absent from raw evidence",
        ),
    ],
)
def test_dossier_preflight_rejects_bad_citations_without_bundle_changes(
    tmp_path: Path, analysis: str, expected: str
) -> None:
    paths = _paths(tmp_path)
    bundle = CurriculumBundle(paths, "ai-engineering")
    raw = _write_raw(bundle)
    watched = (
        paths.bundle_root / "index.md",
        paths.bundle_root / "log.md",
    )
    before = {path: path.read_text(encoding="utf-8") for path in watched}

    with pytest.raises(ValueError, match=expected):
        _write_dossier(bundle, raw, analysis=analysis)

    assert not list((paths.bundle_root / "sources").glob("*.md"))
    assert not list((paths.bundle_root / "concepts").glob("*.md"))
    assert {path: path.read_text(encoding="utf-8") for path in watched} == before


def test_engine_dossier_binds_confined_raw_digest_and_exact_timestamp(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    bundle = CurriculumBundle(paths, "ai-engineering")
    raw = _write_raw(bundle)
    dossier = _write_dossier(bundle, raw)
    metadata, _ = _read_document(dossier)

    assert metadata["raw_evidence"]["path"] == raw.relative_to(paths.curriculum_data).as_posix()
    assert (
        metadata["raw_evidence"]["sha256"]
        == hashlib.sha256(b"[00:00:01] Grounded evidence.").hexdigest()
    )
    assert metadata["raw_evidence"]["immutable"] is True
    assert bundle.validate() == []


def test_preflight_rejects_unconfined_raw_reference_without_writes(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    bundle = CurriculumBundle(paths, "ai-engineering")
    outside = tmp_path / "outside.md"
    outside.write_text("not profile evidence", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes profile boundary"):
        bundle.write_source_dossier(
            video=_video(),
            transcript_source="captions",
            analysis_markdown=_analysis(),
            provider="test",
            model="test",
            runtime_lane="generic_runtime",
            raw_path=outside,
            raw_digest="0" * 64,
        )

    assert not paths.bundle_root.exists()


def test_validator_detects_dossier_source_and_explicit_id_mismatch(
    tmp_path: Path,
) -> None:
    bundle = CurriculumBundle(_paths(tmp_path), "ai-engineering")
    raw = _write_raw(bundle)
    dossier = _write_dossier(bundle, raw)
    metadata, body = _read_document(dossier)
    alternate = raw.with_name("alternate.md")
    alternate.write_text(raw.read_text(encoding="utf-8"), encoding="utf-8")
    metadata["raw_evidence"]["path"] = alternate.relative_to(
        bundle.paths.curriculum_data
    ).as_posix()
    metadata["sources"][0]["url"] = "https://youtube.com/watch?v=OtherID"
    body = body.replace(
        "[youtube:AbC_-19 @ 00:00:01]",
        "[youtube:OtherID @ 00:00:01]",
    )
    dossier.write_text(_document(metadata, body), encoding="utf-8")

    errors = bundle.validate()
    assert any("raw path/video identity mismatch" in error for error in errors)
    assert any("raw/dossier source_url mismatch" in error for error in errors)
    assert any("source URL/video identity mismatch" in error for error in errors)
    assert any("cites wrong video_id OtherID" in error for error in errors)


def test_validator_detects_raw_tamper_and_expired_freshness(tmp_path: Path) -> None:
    bundle = CurriculumBundle(_paths(tmp_path), "ai-engineering")
    raw = _write_raw(bundle)
    dossier = _write_dossier(bundle, raw)
    raw.write_text(
        raw.read_text(encoding="utf-8").replace("Grounded evidence.", "Changed evidence."),
        encoding="utf-8",
    )
    metadata, body = _read_document(dossier)
    metadata["stale_after"] = "2000-01-01"
    dossier.write_text(_document(metadata, body), encoding="utf-8")

    errors = bundle.validate()
    assert any("freshness has expired" in error for error in errors)
    assert any("raw digest does not match transcript" in error for error in errors)
    assert any("dossier digest does not match transcript" in error for error in errors)


def test_post_write_validation_failure_rolls_back_all_mutable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    bundle = CurriculumBundle(paths, "ai-engineering")
    raw = _write_raw(bundle)
    watched = (
        paths.bundle_root / "index.md",
        paths.bundle_root / "log.md",
    )
    before = {path: path.read_text(encoding="utf-8") for path in watched}
    monkeypatch.setattr(bundle, "validate", lambda: ["forced failure"])

    with pytest.raises(ValueError, match="forced failure"):
        _write_dossier(bundle, raw)

    assert not list((paths.bundle_root / "sources").glob("*.md"))
    assert not list((paths.bundle_root / "concepts").glob("*.md"))
    assert {path: path.read_text(encoding="utf-8") for path in watched} == before


def test_structure_migrated_source_can_validate_without_engine_raw(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    bundle = CurriculumBundle(paths, "ai-engineering")
    bundle.ensure()
    now = datetime.now(UTC)
    target = paths.bundle_root / "sources" / "private-seed.md"
    metadata = {
        "type": "source",
        "title": "Private seed",
        "description": "Migrated synthesized source.",
        "status": "active",
        "stale_after": (now + timedelta(days=30)).date().isoformat(),
        "sources": [{"id": "seed:local:source"}],
        "generated": {
            "actor": "thehomie/curriculum-seed-migrator",
            "at": now.isoformat(),
        },
        "verified": {
            "level": "structure-migrated",
            "at": now.isoformat(),
        },
        "video_id": "seed-video",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _document(metadata, "# Private seed\n\n## Sources\n\n- Private."),
        encoding="utf-8",
    )

    assert bundle.validate() == []
