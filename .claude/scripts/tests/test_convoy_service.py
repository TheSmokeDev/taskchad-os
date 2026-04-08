"""Unit tests for convoy orchestration service.

Each test mirrors the MC donor function it replaces.
Parity oracle: mission-control/src/lib/convoy.ts
"""

import pytest

from orchestration.db import OrchestrationDB
from orchestration.convoy_service import ConvoyService
from orchestration.models import AddSubtaskInput, CreateConvoyInput, CreateSubtaskInput


@pytest.fixture
def svc():
    db = OrchestrationDB(":memory:")
    yield ConvoyService(db)
    db.close()


# ── Create ─────────────────────────────────────────────────────────────────


def test_create_convoy_basic(svc):
    # Parity: convoy.ts:createConvoy() — no subtasks
    inp = CreateConvoyInput(title="Basic", created_by="sb")
    result = svc.create_convoy(inp)
    assert result.convoy.id > 0
    assert result.convoy.title == "Basic"
    assert result.convoy.status == "draft"
    assert result.convoy.created_by == "sb"
    assert result.convoy.base_branch == "main"
    assert result.convoy.merge_strategy == "squash"
    assert result.convoy.total_subtasks == 0
    assert result.subtasks == []
    assert result.edges == []


def test_create_convoy_with_subtasks_auto_ready(svc):
    # Parity: convoy.ts:createConvoy() — subtasks with 0 deps become 'ready'
    inp = CreateConvoyInput(
        title="With Tasks",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="A"),
            CreateSubtaskInput(title="B"),
        ],
    )
    result = svc.create_convoy(inp)
    assert result.convoy.total_subtasks == 2
    assert len(result.subtasks) == 2
    assert result.subtasks[0].status == "ready"
    assert result.subtasks[1].status == "ready"
    assert result.subtasks[0].seq == 0
    assert result.subtasks[1].seq == 1


def test_create_convoy_with_deps(svc):
    # Parity: convoy.ts:createConvoy() — deps set remaining_dependencies
    inp = CreateConvoyInput(
        title="DAG",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="A"),                      # idx 0
            CreateSubtaskInput(title="B"),                      # idx 1
            CreateSubtaskInput(title="C", depends_on_subtask_indexes=[0, 1]),   # idx 2
        ],
    )
    result = svc.create_convoy(inp)
    assert result.subtasks[0].status == "ready"
    assert result.subtasks[1].status == "ready"
    assert result.subtasks[2].status == "pending"
    assert result.subtasks[2].remaining_dependencies == 2
    assert len(result.edges) == 2


# ── Cycle Detection ───────────────────────────────────────────────────────


def test_cycle_detection_rejects_cycle(svc):
    # Parity: convoy.ts:detectCycleInConvoy()
    inp = CreateConvoyInput(
        title="Cyclic",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="X", depends_on_subtask_indexes=[1]),
            CreateSubtaskInput(title="Y", depends_on_subtask_indexes=[0]),
        ],
    )
    with pytest.raises(ValueError, match="cycle"):
        svc.create_convoy(inp)


def test_no_false_cycle_on_diamond(svc):
    # Diamond: A->C, A->D, B->C, B->D — no cycle
    inp = CreateConvoyInput(
        title="Diamond",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="A"),                     # 0
            CreateSubtaskInput(title="B"),                     # 1
            CreateSubtaskInput(title="C", depends_on_subtask_indexes=[0, 1]),  # 2
            CreateSubtaskInput(title="D", depends_on_subtask_indexes=[0, 1]),  # 3
        ],
    )
    result = svc.create_convoy(inp)
    assert len(result.subtasks) == 4


# ── Dispatch ───────────────────────────────────────────────────────────────


def test_dispatch_subtask(svc):
    # Parity: convoy.ts:dispatchSubtask()
    inp = CreateConvoyInput(
        title="Dispatch Test",
        created_by="sb",
        subtasks=[CreateSubtaskInput(title="Task")],
    )
    result = svc.create_convoy(inp)
    task_id = result.subtasks[0].id

    svc.dispatch_subtask(task_id)

    convoy = svc.get_convoy(result.convoy.id)
    assert convoy.convoy.status == "active"  # draft -> active on first dispatch
    dispatched = [s for s in convoy.subtasks if s.id == task_id][0]
    assert dispatched.status == "dispatched"
    assert dispatched.dispatched_at is not None


def test_dispatch_non_ready_fails(svc):
    # Parity: convoy.ts:dispatchSubtask() — rejects non-ready
    inp = CreateConvoyInput(
        title="Dispatch Fail",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="A"),
            CreateSubtaskInput(title="B", depends_on_subtask_indexes=[0]),
        ],
    )
    result = svc.create_convoy(inp)
    pending_id = result.subtasks[1].id  # B is pending (depends on A)
    with pytest.raises(ValueError, match="not ready"):
        svc.dispatch_subtask(pending_id)


# ── Completion + Cascade ──────────────────────────────────────────────────


def test_complete_subtask_cascades(svc):
    # Parity: convoy.ts:handleSubtaskCompletion()
    inp = CreateConvoyInput(
        title="Cascade",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="A"),
            CreateSubtaskInput(title="B", depends_on_subtask_indexes=[0]),
        ],
    )
    result = svc.create_convoy(inp)
    a_id = result.subtasks[0].id
    b_id = result.subtasks[1].id

    svc.dispatch_subtask(a_id)
    newly_ready, done = svc.handle_subtask_completion(a_id)

    # B should now be ready
    assert any(s.id == b_id for s in newly_ready)
    assert not done


def test_convoy_completes_when_all_done(svc):
    # Parity: convoy.ts:checkConvoyCompletion() — all completed
    inp = CreateConvoyInput(
        title="Complete",
        created_by="sb",
        subtasks=[CreateSubtaskInput(title="Only")],
    )
    result = svc.create_convoy(inp)
    svc.dispatch_subtask(result.subtasks[0].id)
    _, done = svc.handle_subtask_completion(result.subtasks[0].id)
    assert done
    convoy = svc.get_convoy(result.convoy.id)
    assert convoy.convoy.status == "completed"
    assert convoy.convoy.completed_at is not None


# ── Failure ────────────────────────────────────────────────────────────────


def test_convoy_fails_when_majority_fail(svc):
    # Parity: convoy.ts:checkConvoyCompletion() — >50% failed
    inp = CreateConvoyInput(
        title="Fail Majority",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="F1"),
            CreateSubtaskInput(title="F2"),
            CreateSubtaskInput(title="F3"),
        ],
    )
    result = svc.create_convoy(inp)
    for s in result.subtasks:
        svc.dispatch_subtask(s.id)

    svc.handle_subtask_failure(result.subtasks[0].id, error_message="err1")
    convoy_failed = svc.handle_subtask_failure(result.subtasks[1].id, error_message="err2")

    assert convoy_failed  # 2/3 > 50%
    convoy = svc.get_convoy(result.convoy.id)
    assert convoy.convoy.status == "failed"


def test_failure_records_error_message(svc):
    inp = CreateConvoyInput(
        title="Error Msg",
        created_by="sb",
        subtasks=[CreateSubtaskInput(title="T")],
    )
    result = svc.create_convoy(inp)
    svc.dispatch_subtask(result.subtasks[0].id)
    svc.handle_subtask_failure(result.subtasks[0].id, error_message="something broke")
    convoy = svc.get_convoy(result.convoy.id)
    assert convoy.subtasks[0].error_message == "something broke"


# ── Status Transitions ────────────────────────────────────────────────────


def test_valid_status_transitions(svc):
    # Parity: convoy.ts:updateConvoyStatus()
    result = svc.create_convoy(CreateConvoyInput(title="Trans", created_by="sb"))
    cid = result.convoy.id

    # draft -> cancelled
    updated = svc.update_convoy_status(cid, "cancelled")
    assert updated.status == "cancelled"


def test_invalid_transition_raises(svc):
    result = svc.create_convoy(CreateConvoyInput(title="Invalid", created_by="sb"))
    with pytest.raises(ValueError, match="Cannot transition"):
        svc.update_convoy_status(result.convoy.id, "completed")


def test_cancel_cancels_non_terminal_subtasks(svc):
    # Parity: convoy.ts:updateConvoyStatus() — cancel cascades
    inp = CreateConvoyInput(
        title="Cancel Cascade",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="Done"),
            CreateSubtaskInput(title="Pending"),
        ],
    )
    result = svc.create_convoy(inp)
    svc.dispatch_subtask(result.subtasks[0].id)
    svc.handle_subtask_completion(result.subtasks[0].id)

    svc.update_convoy_status(result.convoy.id, "cancelled")
    convoy = svc.get_convoy(result.convoy.id)
    statuses = {s.title: s.status for s in convoy.subtasks}
    assert statuses["Done"] == "completed"  # terminal, not cancelled
    assert statuses["Pending"] == "cancelled"  # non-terminal, cancelled


# ── Add Subtasks ───────────────────────────────────────────────────────────


def test_add_subtasks(svc):
    # Parity: convoy.ts:addSubtasks()
    result = svc.create_convoy(CreateConvoyInput(title="Add", created_by="sb"))
    added = svc.add_subtasks(result.convoy.id, [
        AddSubtaskInput(title="New1"),
        AddSubtaskInput(title="New2"),
    ])
    assert len(added) == 2
    assert added[0].status == "ready"

    convoy = svc.get_convoy(result.convoy.id)
    assert convoy.convoy.total_subtasks == 2


def test_invalid_create_dependency_index_raises(svc):
    inp = CreateConvoyInput(
        title="Bad Index",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="A"),
            CreateSubtaskInput(title="B", depends_on_subtask_indexes=[99]),
        ],
    )
    with pytest.raises(ValueError, match="Invalid dependency index"):
        svc.create_convoy(inp)


def test_add_subtasks_requires_existing_subtask_ids(svc):
    result = svc.create_convoy(CreateConvoyInput(title="Add Dep", created_by="sb"))
    with pytest.raises(ValueError, match="Invalid dependency subtask id"):
        svc.add_subtasks(
            result.convoy.id,
            [AddSubtaskInput(title="New", depends_on_subtask_ids=[9999])],
        )


def test_multiple_dispatches_record_multiple_attempts(svc):
    result = svc.create_convoy(
        CreateConvoyInput(
            title="Attempts",
            created_by="sb",
            subtasks=[CreateSubtaskInput(title="Retry me")],
        )
    )
    subtask_id = result.subtasks[0].id

    svc.dispatch_subtask(subtask_id)
    svc.db.conn.execute("UPDATE subtasks SET status = 'ready' WHERE id = ?", (subtask_id,))
    svc.db.conn.commit()
    svc.dispatch_subtask(subtask_id)

    attempt_count = svc.db.conn.execute(
        "SELECT COUNT(*) as cnt FROM attempts WHERE subtask_id = ?",
        (subtask_id,),
    ).fetchone()["cnt"]
    assert attempt_count == 2


# ── Delete ─────────────────────────────────────────────────────────────────


def test_delete_convoy(svc):
    # Parity: convoy.ts:deleteConvoy()
    result = svc.create_convoy(CreateConvoyInput(title="Del", created_by="sb"))
    svc.delete_convoy(result.convoy.id)
    assert svc.get_convoy(result.convoy.id) is None


def test_delete_nonexistent_raises(svc):
    with pytest.raises(ValueError, match="not found"):
        svc.delete_convoy(9999)


# ── List + Filter ─────────────────────────────────────────────────────────


def test_list_convoys(svc):
    # Parity: convoy.ts:listConvoys()
    svc.create_convoy(CreateConvoyInput(title="C1", created_by="sb"))
    svc.create_convoy(CreateConvoyInput(title="C2", created_by="sb"))
    all_convoys = svc.list_convoys()
    assert len(all_convoys) == 2


def test_list_convoys_with_filter(svc):
    svc.create_convoy(CreateConvoyInput(title="Draft", created_by="sb"))
    r2 = svc.create_convoy(CreateConvoyInput(title="Active", created_by="sb",
                                              subtasks=[CreateSubtaskInput(title="T")]))
    svc.dispatch_subtask(r2.subtasks[0].id)  # activates convoy

    drafts = svc.list_convoys(status="draft")
    active = svc.list_convoys(status="active")
    assert len(drafts) == 1
    assert len(active) == 1
    assert drafts[0].title == "Draft"
    assert active[0].title == "Active"


# ── Ready Subtasks ─────────────────────────────────────────────────────────


def test_get_ready_subtasks(svc):
    # Parity: convoy.ts:getReadySubtasks()
    inp = CreateConvoyInput(
        title="Ready",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="R1"),
            CreateSubtaskInput(title="R2"),
            CreateSubtaskInput(title="Blocked", depends_on_subtask_indexes=[0]),
        ],
    )
    result = svc.create_convoy(inp)
    ready = svc.get_ready_subtasks(result.convoy.id)
    assert len(ready) == 2
    assert all(s.status == "ready" for s in ready)


# ── Full Lifecycle (GUI-off proof) ─────────────────────────────────────────


def test_full_lifecycle_gui_off(svc):
    """End-to-end: create -> dispatch -> complete with cascading deps -> convoy completes.
    Proves the framework works without Mission Control."""
    inp = CreateConvoyInput(
        title="Full Lifecycle",
        created_by="sb",
        subtasks=[
            CreateSubtaskInput(title="Foundation"),
            CreateSubtaskInput(title="Build", depends_on_subtask_indexes=[0]),
            CreateSubtaskInput(title="Deploy", depends_on_subtask_indexes=[1]),
        ],
    )
    result = svc.create_convoy(inp)

    # Foundation is ready
    ready = svc.get_ready_subtasks(result.convoy.id)
    assert len(ready) == 1
    assert ready[0].title == "Foundation"

    # Dispatch + complete Foundation -> Build becomes ready
    svc.dispatch_subtask(ready[0].id)
    newly_ready, _ = svc.handle_subtask_completion(ready[0].id)
    assert len(newly_ready) == 1
    assert newly_ready[0].title == "Build"

    # Dispatch + complete Build -> Deploy becomes ready
    svc.dispatch_subtask(newly_ready[0].id)
    newly_ready, _ = svc.handle_subtask_completion(newly_ready[0].id)
    assert len(newly_ready) == 1
    assert newly_ready[0].title == "Deploy"

    # Dispatch + complete Deploy -> convoy completes
    svc.dispatch_subtask(newly_ready[0].id)
    _, done = svc.handle_subtask_completion(newly_ready[0].id)
    assert done

    final = svc.get_convoy(result.convoy.id)
    assert final.convoy.status == "completed"
    assert final.convoy.completed_subtasks == 3
    assert final.convoy.total_subtasks == 3
