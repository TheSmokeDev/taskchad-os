"""Source ranges, repeat-safe projections and compatible queue entrypoints."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from personas.learning import synthesis_sources as sources
from personas.learning.models import LearningError, LearningTarget
from personas.learning.service import LearningService


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "persona"
    return LearningService(
        LearningTarget("tester", root / "memory", root / "data", root / "state", root / "skills")
    )


def _episode(service, name="old.md", body="One observed decision.\n"):
    path = service.target.memory_dir / "episodes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nstatus: open\ndate: 2000-01-01\n---\n## Summary\n" + body, encoding="utf-8"
    )
    return path


class _ProjectionStore:
    def __init__(self, cycles):
        self.cycles = cycles

    def get(self, cycle_id):
        return next((row for row in self.cycles if row["id"] == cycle_id), None)

    def all(self, kind):
        return [row for row in self.cycles if row["kind"] == kind]

    def events(self, cycle_id):
        row = self.get(cycle_id)
        return (
            [{"event_type": "synthesis_consumed", "payload": {"manifest": row["input_manifest"]}}]
            if row["status"] == "completed"
            else []
        )


def _cycle(identifier, manifest, status="completed", kind="dream"):
    return {
        "id": identifier,
        "kind": "synthesis_cycle",
        "synthesis_kind": kind,
        "status": status,
        "input_manifest": manifest,
    }


def test_old_omitted_sources_remain_collectable_without_mutation(service):
    path = _episode(service)
    before = path.read_bytes()
    first = sources.collect_sources(service, "dream")
    second = sources.collect_sources(service, "dream")
    assert first == second
    assert first[0]["ref"] == "memory:tester:episodes/old.md"
    assert first[0]["text"] == before.decode()
    assert first[0]["start"] == 0
    assert first[0]["end"] == len(first[0]["text"])
    assert first[0]["complete"] is True
    assert first[0]["evidence_ids"] == []
    assert first[0]["metadata"]["derived"] is True
    assert path.read_bytes() == before
    assert not service.store.path.exists()


def test_partial_retry_and_omitted_episode_never_claim_full_review(service):
    path = _episode(service, body="Real decision with enough text to split.\n")
    omitted = _episode(service, "omitted.md")
    row = next(
        row
        for row in sources.collect_sources(service, "dream")
        if row["metadata"]["relative_path"] == "episodes/old.md"
    )
    split = len(row["text"]) // 2
    first = {**row, "text": row["text"][:split], "end": split, "complete": False}
    second = {**row, "text": row["text"][split:], "start": split}
    cycles = [_cycle("first", [first])]
    service.store = _ProjectionStore(cycles)
    assert sources.project_completed_synthesis(service, "first", {}, [first])["consolidated"] == 0
    assert "status: open" in path.read_text()
    assert "status: open" in omitted.read_text()
    cycles.append(_cycle("second", [second]))
    assert sources.project_completed_synthesis(service, "second", {}, [second])["consolidated"] == 1
    before = path.read_bytes()
    assert sources.project_completed_synthesis(service, "second", {}, [second])["consolidated"] == 0
    assert path.read_bytes() == before
    assert "status: open" in omitted.read_text()


@pytest.mark.parametrize("bad", ["failed", "changed", "gap", "wrong_text"])
def test_invalid_or_failed_coverage_cannot_close_episode(service, bad):
    path = _episode(service, body="A full source revision.\n")
    row = sources.collect_sources(service, "dream")[0]
    split = len(row["text"]) // 2
    first = {**row, "text": row["text"][:split], "end": split, "complete": False}
    second = {**row, "text": row["text"][split:], "start": split}
    if bad == "gap":
        second.update(start=split + 1, text=row["text"][split + 1 :])
    if bad == "wrong_text":
        second["text"] = "x" * len(second["text"])
    if bad == "changed":
        path.write_text(path.read_text() + "New decision.\n", encoding="utf-8")
    service.store = _ProjectionStore(
        [
            _cycle("first", [first], "failed" if bad == "failed" else "completed"),
            _cycle("second", [second]),
        ]
    )
    assert sources.project_completed_synthesis(service, "second", {}, [second])["consolidated"] == 0
    assert "status: open" in path.read_text()
    if bad == "changed":
        assert sources.collect_sources(service, "dream")[0]["revision"] != row["revision"]


def test_reflection_never_closes_episode(service):
    path = _episode(service)
    rows = sources.collect_sources(service, "reflection")
    service.store = _ProjectionStore([_cycle("reflect", rows, kind="reflection")])
    assert sources.project_completed_synthesis(service, "reflect", {}, rows)["consolidated"] == 0
    assert "status: open" in path.read_text()


def test_projection_refuses_scanned_manifest_without_consumption(service):
    path = _episode(service)
    rows = sources.collect_sources(service, "dream")
    service.store = _ProjectionStore([_cycle("not-consumed", rows, status="retained")])
    with pytest.raises(LearningError, match="durable consumption"):
        sources.project_completed_synthesis(service, "not-consumed", {}, rows)
    assert "status: open" in path.read_text()


def test_changed_between_coverage_and_locked_write_stays_open(service, monkeypatch):
    import episodes

    path = _episode(service)
    rows = sources.collect_sources(service, "dream")
    service.store = _ProjectionStore([_cycle("dream", rows)])
    real_mark = episodes.mark_episodes_consolidated

    def concurrent_update(paths, **kwargs):
        path.write_text(path.read_text() + "A concurrent new source.\n", encoding="utf-8")
        return real_mark(paths, **kwargs)

    monkeypatch.setattr(episodes, "mark_episodes_consolidated", concurrent_update)
    assert sources.project_completed_synthesis(service, "dream", {}, rows)["consolidated"] == 0
    assert "status: open" in path.read_text()


def _debrief_service(service):
    record = {
        "id": "completed-debrief",
        "kind": "cognitive_cycle",
        "status": "retained",
        "evidence_ids": ["actual-source-1"],
        "created_at": "2026-09-10T10:00:00+00:00",
    }
    service.store = _ProjectionStore([record])
    return service


def test_debrief_projection_retry_reuses_same_inference_and_both_files(service):
    service = _debrief_service(service)
    output = {
        "conclusion": (
            "The completed session resolved the deployment question and retained the decision."
        ),
        "understanding": [{"content": "The observed deployment remains staged until approved."}],
        "investigations": [{"question": "Has the operator approved deployment?"}],
    }
    first = sources.project_completed_debrief(service, "completed-debrief", {"output": output})
    before = {str(path): path.read_bytes() for path in service.target.memory_dir.rglob("*.md")}
    second = sources.project_completed_debrief(service, "completed-debrief", output)
    assert first["episode_status"] == "written"
    assert second["episode_status"] == "duplicate"
    assert before == {
        str(path): path.read_bytes() for path in service.target.memory_dir.rglob("*.md")
    }
    assert len(list((service.target.memory_dir / "episodes").glob("*.md"))) == 1
    assert all(b"actual-source-1" in text for text in before.values())


def test_debrief_projection_after_partial_physical_failure_is_idempotent(service, monkeypatch):
    import shared

    service = _debrief_service(service)
    output = {
        "conclusion": (
            "A durable decision from the completed debrief should survive "
            "a temporary daily-file failure."
        )
    }
    original = shared.atomic_write_text
    monkeypatch.setattr(
        shared,
        "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    with pytest.raises(OSError):
        sources.project_completed_debrief(service, "completed-debrief", output)
    episode = next((service.target.memory_dir / "episodes").glob("*.md"))
    before = episode.read_bytes()
    monkeypatch.setattr(shared, "atomic_write_text", original)
    sources.project_completed_debrief(service, "completed-debrief", output)
    assert episode.read_bytes() == before
    assert len(list((service.target.memory_dir / "daily").glob("*.md"))) == 1


def test_no_change_debrief_does_not_create_memory_tree(service):
    service = _debrief_service(service)
    assert (
        sources.project_completed_debrief(service, "completed-debrief", {})["status"] == "no_change"
    )
    assert not service.target.memory_dir.exists()


@pytest.mark.parametrize(
    "module_name, method, kind",
    [
        ("memory_reflect", "run_reflection", "reflection"),
        ("memory_dream", "run_dream", "dream"),
    ],
)
def test_old_commands_only_admit_shared_queue(module_name, method, kind, monkeypatch):
    import importlib

    from personas.learning import legacy_adapters

    module = importlib.import_module(module_name)
    calls = []
    monkeypatch.setattr(
        legacy_adapters,
        "request_active_synthesis",
        lambda name, **kw: calls.append((name, kw)) or {"status": "queued"},
    )
    monkeypatch.setattr(module, "file_lock", lambda *a, **k: pytest.fail("legacy lock"))
    receipt = json.loads(asyncio.run(getattr(module, method)(test_mode=False)))
    assert receipt["status"] == "queued"
    assert calls[0][0] == kind


def test_flush_dry_run_is_honest_and_keeps_source(tmp_path, monkeypatch):
    import memory_flush
    from personas.learning import hooks

    path = tmp_path / "session-flush-cli-real-session-20260910-100000.md"
    path.write_text("Session: physical:session:42\n**User:** a decision\n", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(
        hooks, "enqueue_session_debrief", lambda **kw: pytest.fail("dry-run admission wrote")
    )
    monkeypatch.setattr(
        memory_flush, "run_with_runtime_lanes", lambda *a: pytest.fail("independent provider")
    )
    assert (
        json.loads(asyncio.run(memory_flush.run_flush(path, test_mode=True)))["status"] == "dry_run"
    )
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "receipt,removed",
    [
        ({"status": "queued"}, True),
        ({"status": "deferred", "outbox_id": "retained"}, True),
        ({"status": "disabled"}, False),
        ({"status": "failed"}, False),
    ],
)
def test_flush_cleanup_requires_durable_admission(tmp_path, monkeypatch, receipt, removed):
    import memory_flush
    from personas.learning import hooks

    path = tmp_path / "session-flush-cli-filename-id-20260910-100000.md"
    path.write_text("Session: physical:session:42\n**User:** a decision\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(hooks, "enqueue_session_debrief", lambda **kw: calls.append(kw) or receipt)
    asyncio.run(memory_flush.run_flush(path))
    assert path.exists() is not removed
    assert calls[0]["session_id"] == "physical:session:42"
    assert calls[0]["transcript"].startswith("Session: physical:session:42")


@pytest.mark.parametrize("module_name", ["persona_learning_tick", "persona_dream_tick"])
def test_old_fanout_commands_admit_without_children(module_name, monkeypatch):
    import importlib

    from personas.learning import legacy_adapters

    module = importlib.import_module(module_name)
    calls = []
    monkeypatch.setattr(module, "is_active_default_profile", lambda: True)
    monkeypatch.setattr(
        module, "list_profiles", lambda: [SimpleNamespace(name="tester", is_default=False)]
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **kw: pytest.fail("legacy child spawned")
    )
    monkeypatch.setattr(
        legacy_adapters,
        "admit_profile_synthesis",
        lambda *a, **kw: calls.append((a, kw)) or {"attempted": ["tester"], "failed": []},
    )
    outcome = module.run_tick(test_mode=True)
    assert calls[0][1]["test_mode"] is True
    assert not outcome.failed


def test_once_skips_disabled_profile_and_admits_next_eligible(service, monkeypatch):
    from personas.learning import legacy_adapters, synthesis
    from personas.learning import service as services

    profiles = [SimpleNamespace(name=name, is_default=False) for name in ("off", "ready", "later")]
    monkeypatch.setattr(services, "get_learning_service", lambda name: name)
    seen = []

    def request(target, *args, **kwargs):
        seen.append(target)
        return {"status": "disabled" if target == "off" else "queued"}

    monkeypatch.setattr(synthesis, "request_synthesis", request)
    receipt = legacy_adapters.admit_profile_synthesis("dream", profiles=profiles, once=True)
    assert receipt["attempted"] == ["ready"]
    assert seen == ["off", "ready"]


def _complete_synthesis(service, cycle_id, monkeypatch, calls):
    from personas.learning import synthesis, worker

    async def model(*args):
        calls.append(args)
        return (
            {
                "conclusion": "No additional supported change.",
                "understanding": [],
                "investigations": [],
                "proposals": [],
            },
            {"id": "fake-runtime-call", "provider": "fake", "model": "fake"},
        )

    monkeypatch.setattr(worker, "_runtime_role", model)
    monkeypatch.setattr(worker, "_check_stage_boundary", lambda: None)
    payload = {"cycle_id": cycle_id}
    stage = "synthesis_reason"
    while stage != "done":
        stage, payload = asyncio.run(
            synthesis.process_synthesis_stage(
                service, {"id": "test-job", "stage": stage, "payload": payload}
            )
        )


def test_actual_queue_continues_partial_episode_and_skips_consumed_revision(service, monkeypatch):
    from personas.learning import synthesis

    path = _episode(service, body="Long partial source. " * 370)
    monkeypatch.setenv("DREAM_SIGNAL_THRESHOLD", "1")
    first = synthesis.request_synthesis(service, "dream", source_key="nightly", force=True)
    cycle = service.store.get(first["cycle_id"])
    assert cycle["input_manifest"][0]["complete"] is False
    assert cycle["omitted_manifest"]
    calls = []
    _complete_synthesis(service, cycle["id"], monkeypatch, calls)
    assert "status: open" in path.read_text()
    second = synthesis.request_synthesis(service, "dream", source_key="retry", force=True)
    next_cycle = service.store.get(second["cycle_id"])
    assert next_cycle["input_manifest"][0]["start"] == cycle["input_manifest"][0]["end"]
    _complete_synthesis(service, next_cycle["id"], monkeypatch, calls)
    assert "status: consolidated" in path.read_text()
    unchanged = synthesis.request_synthesis(service, "dream", source_key="later", force=True)
    assert unchanged["status"] == "no_signal"
    assert len(calls) == 2
    path.write_text(
        path.read_text().replace("status: consolidated", "status: open") + "New observation.\n",
        encoding="utf-8",
    )
    revised = synthesis.request_synthesis(service, "dream", source_key="new-revision", force=True)
    assert revised["status"] == "queued"
    assert (
        service.store.get(revised["cycle_id"])["input_manifest"][0]["revision"]
        != cycle["input_manifest"][0]["revision"]
    )


def test_actual_queue_retry_does_not_repeat_completed_inference(service, monkeypatch):
    from personas.learning import synthesis, worker

    _episode(service)
    admission = synthesis.request_synthesis(service, "reflection", source_key="nightly")
    calls = []

    async def model(*args):
        calls.append(args)
        return (
            {
                "conclusion": "No additional change.",
                "understanding": [],
                "investigations": [],
                "proposals": [],
            },
            {"id": "executed", "model": "fake", "provider": "fake"},
        )

    monkeypatch.setattr(worker, "_runtime_role", model)
    monkeypatch.setattr(worker, "_check_stage_boundary", lambda: None)
    job = {
        "id": "test-job",
        "stage": "synthesis_reason",
        "payload": {"cycle_id": admission["cycle_id"]},
    }
    assert asyncio.run(synthesis.process_synthesis_stage(service, job))[0] == "synthesis_retain"
    assert asyncio.run(synthesis.process_synthesis_stage(service, job))[0] == "synthesis_retain"
    assert len(calls) == 1
    repeated = synthesis.request_synthesis(service, "reflection", source_key="different-hook")
    assert repeated["status"] == "coalesced"
    assert repeated["cycle_id"] == admission["cycle_id"]


def test_paused_preview_never_creates_a_store_or_queue(service, monkeypatch):
    from personas.learning import synthesis

    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "false")
    assert synthesis.request_synthesis(service, "dream", source_key="preview", test_mode=True)[
        "status"
    ] in {"disabled", "test_mode"}
    assert not service.target.data_dir.exists()


def test_long_flush_cleans_source_only_after_complete_durable_admission(tmp_path, monkeypatch):
    import memory_flush
    from personas.learning import hooks

    path = tmp_path / "flush-context-sanitized-id-20260910-100000.md"
    path.write_text("x" * 18000, encoding="utf-8")
    calls = []
    monkeypatch.setenv("HOMIE_LEARNING_SESSION_ID", "physical:session:unmodified")
    monkeypatch.setattr(
        hooks, "enqueue_session_debrief", lambda **kw: calls.append(kw) or {"status": "queued"}
    )
    receipt = json.loads(asyncio.run(memory_flush.run_flush(path)))
    assert receipt["status"] == "queued"
    assert not path.exists()
    assert len(calls[0]["transcript"]) == 18000
    assert calls[0]["session_id"] == "physical:session:unmodified"
