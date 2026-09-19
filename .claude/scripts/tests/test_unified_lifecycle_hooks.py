"""Real persistence across lifecycle adapters; no provider or live data access."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from personas.learning import hooks, lifecycle_outbox
from personas.learning.models import LearningTarget
from personas.learning.service import LearningService
from runtime.base import RuntimeRequest, RuntimeResult


def service(tmp_path):
    return LearningService(
        LearningTarget(
            "default",
            tmp_path / "memory",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "skills",
        )
    )


def transcript(written="first", content="Revisit the rejected candle"):
    return "\n".join(
        json.dumps(row)
        for row in [
            {"type": "session_signal", "written_at": written},
            {
                "message": {"role": "user", "content": content},
                "created_at": "2026-09-10T12:00:00",
                "source_message_id": 1,
                "source_ref": "chat-message:session:1",
                "source_revision": "rev1",
            },
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "PRIVATE_NOT_FOR_PERSISTENCE"},
                        {"type": "text", "text": "Need the next candle before a conclusion."},
                    ],
                },
                "source_message_id": 2,
                "source_ref": "chat-message:session:2",
                "source_revision": "rev2",
            },
        ]
    )


def test_wrapper_and_export_time_do_not_duplicate_debrief(tmp_path):
    svc = service(tmp_path)
    first = hooks.enqueue_session_debrief(
        persona_id="default",
        session_id="session",
        surface="session_clear",
        reason="clear",
        transcript=transcript(),
        service=svc,
    )
    second = hooks.enqueue_session_debrief(
        persona_id="default",
        session_id="session",
        surface="session_end",
        reason="ended",
        transcript=transcript("later"),
        service=svc,
    )
    assert second == first
    assert len(svc.store.all("experience")) == len(svc.store.all("observation")) == 1
    assert len(svc.store.all("cognitive_cycle")) == 1
    assert "PRIVATE_NOT_FOR_PERSISTENCE" not in str(svc.store.all())
    source = svc.store.all("observation")[0]["evidence"]["source_evidence"]
    assert [row["source_ref"] for row in source] == [
        "chat-message:session:1",
        "chat-message:session:2",
    ]
    third = hooks.enqueue_session_debrief(
        persona_id="default",
        session_id="session",
        surface="session_end",
        reason="ended",
        transcript=transcript("later", "Correction: the candle did not close"),
        service=svc,
    )
    assert third["cognitive_cycle_id"] != first["cognitive_cycle_id"]


def test_canonical_transcript_is_idempotent_and_filters_private_blocks():
    saved = lifecycle_outbox.canonical_session_transcript(transcript())
    assert lifecycle_outbox.canonical_session_transcript(saved) == saved
    assert "PRIVATE_NOT_FOR_PERSISTENCE" not in saved


def test_long_transcript_tail_survives_outage_restart_and_reaches_bounded_prompt(
    tmp_path, monkeypatch
):
    svc = service(tmp_path)
    original = "\n".join(
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": (f"message-{index} " + "evidence " * 70)
                    if index < 40
                    else "TAIL_EVIDENCE_CORRECTION: prior reversal claim was wrong",
                },
                "source_ref": f"chat-message:long:{index}",
                "source_revision": f"revision-{index}",
            }
        )
        for index in range(41)
    )
    monkeypatch.setattr(
        svc, "capture_experience", lambda *a, **kw: (_ for _ in ()).throw(OSError("outage"))
    )
    queued = hooks.enqueue_session_debrief(
        persona_id="default", session_id="long", surface="clear", transcript=original, service=svc
    )
    assert queued["outbox_id"] and queued["status"] == "deferred"
    restarted = LearningService(svc.target)
    assert lifecycle_outbox.replay_pending(restarted)["delivered"] == 1
    observations = sorted(restarted.store.all("observation"), key=lambda r: r["evidence"]["start"])
    canonical = lifecycle_outbox.canonical_session_transcript(original)
    assert "".join(r["evidence"]["text"] for r in observations) == canonical
    assert all(
        r["evidence"]["end"] - r["evidence"]["start"] == len(r["evidence"]["text"])
        for r in observations
    )
    from personas.learning.cognition import _bounded_source

    assert "TAIL_EVIDENCE_CORRECTION" in str([_bounded_source(row) for row in observations])
    cycles = restarted.store.all("cognitive_cycle")
    tail = next(
        row for row in observations if "TAIL_EVIDENCE_CORRECTION" in row["evidence"]["text"]
    )
    assert any(tail["id"] in cycle["evidence_ids"] for cycle in cycles)
    again = hooks.enqueue_session_debrief(
        persona_id="default",
        session_id="long",
        surface="end",
        transcript=original,
        service=restarted,
    )
    assert again["source_fully_retained"] and len(restarted.store.all("cognitive_cycle")) == len(
        cycles
    )
    assert len(restarted.store.all("experience")) == 1


@pytest.mark.parametrize("actual", [False, True])
def test_foreground_receipt_records_one_completed_reorientation_without_queue(tmp_path, actual):
    svc = service(tmp_path)
    receipt = (
        {
            "success": True,
            "provider": "kimi",
            "model": "k3",
            "runtime_lane": "generic_runtime",
            "output_hash": "abcdef",
        }
        if actual
        else {}
    )
    request = RuntimeRequest(
        prompt="Explain this rejection",
        cwd=".",
        task_name="chat",
        metadata={"foreground_cognition": {"runtime_receipt": receipt}},
    )
    turn = hooks.prepare_turn(
        request, persona_id="default", surface="chat_engine", origin_id="message-1", service=svc
    )
    cycle = svc.store.all("cognitive_cycle")[0]
    assert cycle["status"] == "completed"
    assert cycle["model_call_count"] == int(actual)
    assert cycle["execution_kind"] == ("reasoning" if actual else "context_only")
    from personas.learning.queue import LearningQueue

    assert not [job for job in LearningQueue(svc).list() if job.get("record_id") == cycle["id"]]
    assert turn.request.metadata["learning"]["reorientation"]["model_call_count"] == int(actual)


def test_generic_foreground_cognition_keeps_configured_model(monkeypatch):
    from cognition.runtime_bridge import apply_runtime_result, render_runtime_request
    from cognition.working_memory import WorkingMemory

    from runtime import selection

    monkeypatch.setattr(
        selection, "resolve_runtime_selection", lambda: SimpleNamespace(lane="generic_runtime")
    )
    wm = WorkingMemory(soul_name="persona")
    request = render_runtime_request(wm, "Reorient", "quality")
    assert request.model is None and request.model_only
    assert request.allowed_tools == [] and request.disallowed_tools == ["*"]
    wm, _ = apply_runtime_result(
        wm,
        RuntimeResult(
            text="A private analysis", provider="kimi", model="k3", runtime_lane="generic_runtime"
        ),
        instruction="Reorient",
    )
    receipt = dict(wm.memories[-1].metadata)["runtime_receipt"]
    assert receipt["provider"] == "kimi" and receipt["model"] == "k3"
    assert "A private analysis" not in str(receipt)


def test_retained_context_survives_foreground_budget_and_matches_later_inclusion(tmp_path):
    from cognition.cognitive_pass import _bounded_monologue_wm
    from cognition.runtime_bridge import apply_runtime_result
    from cognition.working_memory import Memory, WorkingMemory

    from personas.learning.models import LearningContext, content_hash

    svc = service(tmp_path)
    text = "Retained observation: wait for a closed candle before assessing the rejection."
    context = LearningContext(text, (), content_hash(text))
    wm = WorkingMemory(soul_name="persona").with_memory(
        Memory(role="system", region="identity", content="identity " * 6000)
    )
    wm = wm.with_memory(
        Memory(
            role="system",
            region="persona_learning",
            content=text,
            metadata=(("context_hash", context.context_hash),),
        )
    )
    bounded = _bounded_monologue_wm(wm, max_chars=27000)
    assert bounded.to_system_prompt().count(text) == 1
    inferred, _ = apply_runtime_result(
        bounded,
        RuntimeResult(
            text="The observation remains tentative",
            provider="kimi",
            model="k3",
            runtime_lane="generic_runtime",
        ),
        instruction="Reorient",
    )
    receipt = dict(inferred.memories[-1].metadata)["runtime_receipt"]
    turn = hooks.prepare_turn(
        RuntimeRequest(
            prompt="Explain this chart",
            cwd=".",
            task_name="chat",
            metadata={"foreground_cognition": {"runtime_receipt": receipt}},
        ),
        persona_id="default",
        surface="chat_engine",
        origin_id="next",
        service=svc,
        prepared_context=context,
    )
    assert turn.request.prompt.count(text) == 1
    cycle = svc.store.all("cognitive_cycle")[0]
    assert cycle["context_delivery"] == "executed" and cycle["context_hash"] == context.context_hash


def test_session_end_hook_reuses_clear_durable_outbox(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "hooks" / "session-end-flush.py"
    spec = importlib.util.spec_from_file_location("unified_session_end_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ensure_directories", lambda: None)
    monkeypatch.setattr(module, "log_hook_execution", lambda *a: None)
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "same",
                    "source": "clear",
                    "learning_debrief_receipt": {"outbox_id": "durable"},
                }
            )
        ),
    )
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: pytest.fail("duplicate flush"))
    module.main()
