"""Behavioural invariants of the profile-owned learning record service."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from personas.learning.models import LearningError, LearningTarget
from personas.learning.service import LearningService


def make_service(tmp_path, name="sales"):
    base = tmp_path / name
    return LearningService(
        LearningTarget(name, base / "memory", base / "data", base / "state", base / "skills")
    )


def experience(service, key="turn-1", mode="real"):
    return service.capture_experience(key, "test", "Handle a price objection", mode=mode)


def expectation():
    return {
        "claim": "The prospect clarifies their price objection",
        "check_by": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        "resolution_rule": "A subsequent inbound message explicitly states the objection",
        "situation": {"segment": "qualified", "objection": "price"},
    }


def observation(service, exp, key="reply-1", **kwargs):
    return service.record_observation(
        exp["id"],
        {
            "status": "resolved",
            "quality": "direct",
            "evidence": {"message_id": key, "direction": "inbound"},
            "held": True,
            **kwargs,
        },
        source_key=key,
    )


def candidate(service, evidence_id, key="candidate-1", **kwargs):
    return service.propose_candidate(
        {
            "candidate_type": "procedure",
            "title": "Price discovery",
            "content": "Ask what the prospect expected before discussing a discount.",
            "applicability": "qualified prospect with a price objection",
            "evidence_ids": [evidence_id],
            **kwargs,
        },
        source_key=key,
    )


def activation(service, item):
    ev = service.record_evaluation(
        item["id"], {"passed": True, "mode": "qualification"}, run_key="fixture-eval"
    )
    return service.record_activation(
        item["id"],
        {
            "evaluation_id": ev["id"],
            "candidate_hash": item["content_hash"],
            "application_receipt": {"status": "applied", "fixture": True},
        },
        activation_key="fixture-activation",
    )


def test_empty_reads_do_not_create_profile(tmp_path):
    service = make_service(tmp_path)
    assert service.summary()["initialized"] is False
    assert service.list_records()["items"] == []
    assert service.get_record("missing") is None
    assert not (tmp_path / "sales").exists()


def test_idempotent_records_are_immutable_at_sql_and_service_boundaries(tmp_path):
    service = make_service(tmp_path)
    record = experience(service)
    assert experience(service) == record
    with pytest.raises(LearningError, match="different content"):
        service.capture_experience("turn-1", "test", "A different action")
    with sqlite3.connect(service.store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE records SET payload='{}'")
    assert service.store.get(record["id"])["task"] == record["task"]


def test_parallel_duplicate_capture_has_one_logical_record(tmp_path):
    service = make_service(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: experience(service), range(8)))
    assert len({item["id"] for item in records}) == 1
    assert len(service.store.all("experience")) == 1


def test_profile_row_isolation_and_cross_profile_references(tmp_path):
    a, b = make_service(tmp_path, "sales"), make_service(tmp_path, "crypto")
    first, second = experience(a), experience(b)
    assert first["id"] != second["id"]
    assert b.get_record(first["id"]) is None
    with pytest.raises(LearningError, match="another profile"):
        b.commit_expectation(first["id"], expectation())
    with sqlite3.connect(b.store.path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM records WHERE id=?", (first["id"],)).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "change",
    [
        {"situation": {}},
        {"check_by": "2020-01-01"},
        {"thesis_tags": ["free text sentence"]},
        {"confidence": 1.5},
        {"action": "maybe"},
    ],
)
def test_expectation_contract_refuses_uncheckable_or_misleading_records(tmp_path, change):
    service = make_service(tmp_path)
    exp = experience(service)
    with pytest.raises(LearningError):
        service.commit_expectation(exp["id"], expectation() | change)
    assert service.store.all("expectation") == []


def test_passes_and_backfill_remain_distinct(tmp_path):
    service = make_service(tmp_path)
    exp = experience(service)
    assert (
        service.commit_expectation(exp["id"], expectation() | {"action": "pass"})["action"]
        == "pass"
    )
    historical = experience(service, "old", "backfill")
    with pytest.raises(LearningError, match="preregistration"):
        service.commit_expectation(historical["id"], expectation())
    row = service.commit_expectation(
        historical["id"],
        expectation() | {"phase": "retrospective", "check_by": "2020-01-01T00:00:00Z"},
    )
    assert row["phase"] == "retrospective"


def test_unobserved_is_not_a_negative_result(tmp_path):
    service = make_service(tmp_path)
    exp = experience(service)
    with pytest.raises(LearningError, match="cannot be graded"):
        observation(service, exp, status="unresolvable", held=False)
    result = observation(
        service,
        exp,
        status="unresolvable",
        held=None,
        evidence={"availability": "permission_denied"},
    )
    assert result["held"] is None


def test_correction_invalidates_dependent_learning_without_rewriting_history(tmp_path):
    service = make_service(tmp_path)
    exp = experience(service)
    old = observation(service, exp)
    item = candidate(service, old["id"])
    activation(service, item)
    observation(service, exp, "correction", supersedes=old["id"], held=False)
    assert service.get_record(old["id"])["status"] == "superseded"
    assert service.get_record(item["id"])["status"] == "needs_reassessment"
    assert not service.render_context("prospect price objection").text
    with pytest.raises(LearningError, match="superseded"):
        service.evidence_records([old["id"]])


def test_full_procedure_delivery_receipt_and_budget_survival(tmp_path, monkeypatch):
    from personas.learning import promotion

    monkeypatch.setattr(promotion, "activation_is_applied", lambda service, row: True)
    service = make_service(tmp_path)
    exp = experience(service)
    item = candidate(service, observation(service, exp)["id"])
    activation(service, item)
    context = service.render_context("qualified prospect price objection", max_chars=2000)
    assert item["content"] in context.text
    assert service.render_context("qualified prospect price objection", max_chars=20).text == ""
    receipt = service.record_context_receipt(
        exp["id"], context, "identity" + context.text, attempt_key="a"
    )
    assert receipt["status"] == "delivered"
    trimmed = service.record_context_receipt(exp["id"], context, "identity", attempt_key="b")
    assert trimmed["status"] == "not_delivered"
    assert trimmed["included"] == []


def test_pause_preserves_state_and_can_resume(tmp_path):
    service = make_service(tmp_path)
    exp = experience(service)
    paused = service.set_paused(True)
    assert paused["configured_enabled"] is True and paused["enabled"] is False
    with pytest.raises(LearningError, match="paused"):
        experience(service, "blocked")
    assert service.get_record(exp["id"])
    assert service.set_paused(False)["enabled"] is True


def test_existing_profile_defaults_on_but_explicit_disable_survives_resume(tmp_path, monkeypatch):
    import personas

    base = tmp_path / "sales"
    base.mkdir()
    config = base / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    service = LearningService(
        LearningTarget(
            "sales", base / "memory", base / "data", base / "state", base / "skills", config
        )
    )
    monkeypatch.setattr(personas, "load_persona_config", lambda name: {})
    assert service.enabled()
    monkeypatch.setattr(
        personas, "load_persona_config", lambda name: {"learning": {"enabled": False}}
    )
    assert service.set_paused(False)["enabled"] is False
    monkeypatch.setattr(
        personas, "load_persona_config", lambda name: {"learning": {"enabled": "true"}}
    )
    with pytest.raises(LearningError, match="invalid persona learning"):
        service.enabled()


def test_unknown_or_corrupt_database_is_not_reinitialized(tmp_path):
    service = make_service(tmp_path)
    service.store.directory.mkdir(parents=True)
    service.store.path.write_bytes(b"not sqlite")
    before = service.store.path.read_bytes()
    with pytest.raises(LearningError):
        service.summary()
    with pytest.raises(LearningError):
        experience(service)
    assert service.store.path.read_bytes() == before


def test_pagination_claims_and_hash_binding(tmp_path):
    service = make_service(tmp_path)
    first = experience(service)
    experience(service, "two")
    page = service.list_records("experience", limit=1)
    assert page["next_cursor"]
    second = service.list_records("experience", limit=1, cursor=page["next_cursor"])
    assert second["items"][0]["id"] == first["id"] and second["next_cursor"] is None
    token = service.store.claim(first["id"], "test")
    assert service.store.claim(first["id"], "test") is None
    assert not service.store.release_claim(first["id"], "test", "wrong")
    assert service.store.release_claim(first["id"], "test", token)
    item = candidate(service, first["id"])
    with pytest.raises(LearningError, match="hash mismatch"):
        service.record_evaluation(item["id"], {"candidate_hash": "wrong"}, run_key="wrong")


def test_credentials_and_nan_are_never_persisted(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(LearningError, match="credentials"):
        service.capture_experience(
            "secret", "test", "task", metadata={"nested": {"access_token": "secret"}}
        )
    with pytest.raises(LearningError, match="finite JSON"):
        service.capture_experience("nan", "test", "task", metadata={"value": float("nan")})
    assert not service.store.path.exists()


@pytest.mark.parametrize(
    "key", ["apikey", "APIKEY", "jwt", "key_material", "key", "passphrase", "signing_key"]
)
def test_opaque_credential_fields_are_rejected_before_storage(tmp_path, key):
    service = make_service(tmp_path)
    with pytest.raises(LearningError, match="credentials"):
        service.capture_experience("secret", "test", "task", metadata={key: "opaque"})
    assert not service.store.path.exists()


def test_learning_budget_inherits_only_explicit_operator_meter(monkeypatch):
    from personas.learning.models import learning_model_budget

    monkeypatch.delenv("PERSONA_LEARNING_MODEL_BUDGET_USD", raising=False)
    monkeypatch.delenv("CHAT_MAX_BUDGET_USD", raising=False)
    assert learning_model_budget() is None
    monkeypatch.setenv("CHAT_MAX_BUDGET_USD", "0.5")
    assert learning_model_budget() == 0.5
    monkeypatch.setenv("PERSONA_LEARNING_MODEL_BUDGET_USD", "0.1")
    assert learning_model_budget() == 0.1
    monkeypatch.setenv("PERSONA_LEARNING_MODEL_BUDGET_USD", "nan")
    with pytest.raises(LearningError):
        learning_model_budget()


def test_shared_database_path_cannot_relabel_another_persona(tmp_path):
    service = make_service(tmp_path)
    experience(service)
    target = service.target
    wrong = LearningService(
        LearningTarget(
            "crypto", target.memory_dir, target.data_dir, target.state_dir, target.skills_dir
        )
    )
    with pytest.raises(LearningError, match="another profile"):
        wrong.summary()
    with pytest.raises(LearningError, match="another profile"):
        experience(wrong)


def test_stale_evaluation_cannot_reactivate_corrected_evidence(tmp_path):
    service = make_service(tmp_path)
    exp = experience(service)
    old = observation(service, exp)
    item = candidate(service, old["id"])
    ev = service.record_evaluation(item["id"], {"passed": True}, run_key="old")
    observation(service, exp, "new", supersedes=old["id"], held=False)
    with pytest.raises(LearningError, match="superseded"):
        service.record_activation(
            item["id"],
            {
                "evaluation_id": ev["id"],
                "candidate_hash": item["content_hash"],
                "application_receipt": {"status": "applied"},
            },
            activation_key="late",
        )
    assert service.get_record(item["id"])["status"] == "needs_reassessment"
    assert service.store.all("activation") == []


def test_preregistration_is_bound_to_the_actual_action_boundary(tmp_path):
    service = make_service(tmp_path)
    exp = experience(service)
    service.record_execution(
        exp["id"], {"success": True, "action_key": "first"}, attempt_key="first"
    )
    with pytest.raises(LearningError, match="after its execution"):
        service.commit_expectation(exp["id"], expectation(), action_key="first")
    row = service.commit_expectation(exp["id"], expectation(), action_key="second")
    service.record_execution(
        exp["id"], {"success": True, "action_key": "second"}, attempt_key="second"
    )
    same = {
        key: row[key]
        for key in (
            "claim",
            "check_by",
            "resolution_rule",
            "situation",
            "phase",
            "action",
            "thesis_tags",
        )
    }
    assert service.commit_expectation(exp["id"], same, action_key="second")["id"] == row["id"]


def test_journal_status_cannot_inject_unapplied_content(tmp_path):
    service = make_service(tmp_path)
    exp = experience(service)
    item = candidate(service, observation(service, exp)["id"])
    activation(service, item)
    assert service.render_context("qualified prospect price objection").text == ""


def test_context_selection_scales_without_per_method_connections(tmp_path, monkeypatch):
    import time

    from personas.learning import promotion

    service = make_service(tmp_path)
    monkeypatch.setattr(promotion, "activation_is_applied", lambda service, row: True)
    with service.store.atomic():
        for number in range(1000):
            item = service.store.put(
                "candidate",
                {
                    "candidate_type": "procedure",
                    "title": f"Price method {number}",
                    "content": "Ask a question.",
                    "applicability": "price objection",
                    "content_hash": str(number),
                    "evidence_ids": [],
                    "counterevidence_ids": [],
                    "status": "active_provisional",
                },
                key=str(number),
            )
            service.store.put(
                "activation",
                {
                    "candidate_id": item["id"],
                    "candidate_hash": str(number),
                    "method_status": "active_provisional",
                    "status": "active_provisional",
                },
                key=str(number),
            )
    start = time.perf_counter()
    context = service.render_context("price objection", max_chars=2000)
    elapsed = time.perf_counter() - start
    assert context.versions
    assert elapsed < 0.5, f"bounded context selection took {elapsed:.3f}s"
