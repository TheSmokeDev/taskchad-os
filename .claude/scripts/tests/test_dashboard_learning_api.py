"""Learning operator data flow, scope, redaction, and CLI/API parity.

Every service uses an explicit temporary LearningTarget. No provider invocation
or production profile mutation is part of this suite.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import click
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personas.learning import operator
from personas.learning.models import LearningError, LearningNotFound, LearningTarget
from personas.learning.service import LearningService


@pytest.fixture
def operator_app(tmp_path, monkeypatch):
    import dashboard_api
    from dashboard_learning_api import router

    services = {}
    for name in ("default", "sales", "crypto"):
        root = tmp_path / name
        services[name] = LearningService(
            LearningTarget(
                name,
                root / "memory",
                root / "data",
                root / "state",
                root / "skills",
            )
        )
    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "true")
    monkeypatch.setenv("HOMIE_KILLSWITCH_HARNESS_LEARNING", "enabled")
    monkeypatch.setattr(operator.redact_module, "_REDACT_ENABLED", True)

    def resolve(name):
        if name not in services:
            raise LearningNotFound("unknown temporary profile")
        return operator.LearningOperator(services[name])

    resolver = Mock(side_effect=resolve)
    monkeypatch.setattr(operator, "get_learning_operator", resolver)
    monkeypatch.setattr(
        dashboard_api,
        "_profile_disk_state",
        lambda name: "active" if name in services else "deleted",
    )
    app = FastAPI()

    @app.middleware("http")
    async def test_scope(request, call_next):
        scope = request.headers.get("x-test-personas")
        request.state.persona_scope = None if scope is None else frozenset(scope.split(","))
        return await call_next(request)

    app.include_router(router)
    return SimpleNamespace(client=TestClient(app), services=services, resolver=resolver)


def test_new_persona_learning_reads_do_not_create_state(operator_app):
    service = operator_app.services["default"]
    response = operator_app.client.get("/api/agents/default/learning")
    assert response.status_code == 200
    assert response.json()["initialized"] is False
    assert response.json()["active_methods"] == []
    assert operator_app.client.get("/api/agents/default/learning/records").json()["records"] == []
    assert not service.target.data_dir.exists()
    assert not service.target.state_dir.exists()


def test_report_period_validation_scope_and_explicit_inference(operator_app, monkeypatch):
    from cli_learning import register_learning_commands

    from personas.learning import reporting

    service = operator_app.services["sales"]
    idea = service.store.put(
        "understanding",
        {"content": "Ask what changed.", "status": "tentative", "scope": "discovery"},
        key="idea",
    )
    calls = []

    async def explain(_service, snapshot):
        calls.append(snapshot)
        return {
            "narrative": f"One tentative interpretation [{idea['id']}].",
            "provider": "fake",
            "model": "fake",
        }

    monkeypatch.setattr(reporting, "_runtime_narrative", explain)
    client = operator_app.client
    report = client.get("/api/agents/sales/learning/report").json()
    assert report["counts"]["understanding_changes"] == 1
    assert calls == []
    response = client.post("/api/agents/sales/learning/report")
    assert response.status_code == 200
    assert response.json()["narrative_status"] == "generated"
    assert len(calls) == 1
    assert client.get("/api/agents/sales/learning/report?since=not-a-date").status_code == 422
    assert (
        client.post(
            "/api/agents/crypto/learning/report", headers={"x-test-personas": "sales"}
        ).status_code
        == 403
    )
    assert len(calls) == 1

    @click.group()
    def group():
        pass

    register_learning_commands(group)
    result = CliRunner().invoke(group, ["report", "sales", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["counts"] == report["counts"]
    result = CliRunner().invoke(group, ["list", "sales", "--kind", "understanding", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["records"][0]["id"] == idea["id"]


def test_nested_opaque_credentials_are_redacted_in_records_history_and_cli(operator_app):
    service = operator_app.services["sales"]
    secrets = {
        key: f"opaque-value-{index}"
        for index, key in enumerate(
            (
                "auth",
                "accessToken",
                "bearer",
                "secret_value",
                "TALK_OPENAI_API_KEY",
                "x-api-key",
                "refreshToken",
                "apikey",
                "APIKEY",
                "FOO_APIKEY",
                "jwt",
                "key",
                "key_material",
                "passphrase",
                "signing_key",
            )
        )
    }
    record = service.store.put(
        "experience", {"nested": secrets | {"token_count": 12}}, key="opaque-keys"
    )
    service.store.event(record["id"], "audit", {"nested": secrets}, key="opaque-event")
    bodies = [
        operator_app.client.get(f"/api/agents/sales/learning/records/{record['id']}").json(),
        operator_app.client.get("/api/agents/sales/learning/records").json(),
        operator.LearningOperator(service).get_record(record["id"]),
    ]
    for body in bodies:
        encoded = json.dumps(body)
        assert all(value not in encoded for value in secrets.values())
    assert bodies[0]["payload"]["nested"]["token_count"] == 12


def test_invalid_cursor_is_validation_not_conflict(operator_app):
    response = operator_app.client.get("/api/agents/sales/learning/records?cursor=not-a-cursor")
    assert response.status_code == 422


def test_worker_failures_and_capture_gaps_are_visible_without_model_calls(operator_app):
    from personas.learning.queue import LearningQueue

    service = operator_app.services["sales"]
    record = service.capture_experience("coverage", "test", "Review price objection")
    service.store.event(record["id"], "coverage_failure", {"reason": "missing receipt"}, key="gap")
    queue = LearningQueue(service)
    queue.enqueue("experience", "queued-coverage", payload={"experience_id": record["id"]})
    job = queue.claim()
    queue.finish_stage(
        job, status="retry", failed_attempt=True, error="Provider temporarily unavailable"
    )
    response = operator_app.client.get("/api/agents/sales/learning").json()
    assert response["failures"] >= 2
    assert response["queue"]["jobs"][0]["last_error"] == "Provider temporarily unavailable"
    assert "token" not in response["queue"]["jobs"][0]


def test_learning_subrouter_is_mounted_in_dashboard_router(operator_app):
    import dashboard_api

    app = FastAPI()
    app.include_router(dashboard_api.router)
    client = TestClient(app)
    assert client.get("/api/agents/default/learning").status_code == 200


def test_scope_and_default_translation_precede_any_service_access(operator_app):
    client = operator_app.client
    for method, suffix in (
        ("get", ""),
        ("get", "/records"),
        ("get", "/lifecycle"),
        ("get", "/tuning"),
        ("post", "/tuning/run"),
        ("post", "/tuning/rollback"),
        ("post", "/pause"),
        ("post", "/resume"),
        ("post", "/activations/a/rollback"),
    ):
        response = getattr(client, method)(
            f"/api/agents/crypto/learning{suffix}", headers={"x-test-personas": "sales"}
        )
        assert response.status_code == 403
    assert client.get("/api/agents/main/learning").status_code == 422
    assert not operator_app.resolver.called


def test_scoped_records_redaction_links_and_profile_isolation(operator_app):
    sales = operator_app.services["sales"]
    crypto = operator_app.services["crypto"]
    other = crypto.store.put("experience", {"task": "private crypto task"}, key="a")
    evidence = sales.store.put("experience", {"task": "price objection"}, key="a")
    candidate = sales.store.put(
        "candidate",
        {
            "title": "Clarify value",
            "evidence_ids": [evidence["id"], other["id"]],
            "status": "proposed",
            "content": "Inspect C:\\private\\prospect.txt then /home/operator/prospects.csv",
            "nested": {
                "access_token": "synthetic-secret",
                "raw": "api_key=<REDACTED-openai>",
            },
        },
        key="candidate",
    )
    sales.store.event(
        candidate["id"],
        "status",
        {"status": "deferred", "reason": "Need more evidence"},
        key="defer",
    )
    response = operator_app.client.get(f"/api/agents/sales/learning/records/{candidate['id']}")
    assert response.status_code == 200
    assert response.json()["payload"]["status"] == "deferred"
    assert response.json()["payload"]["history"][0]["payload"]["reason"] == "Need more evidence"
    assert [link["id"] for link in response.json()["links"]] == [evidence["id"]]
    for forbidden in (
        "synthetic-secret",
        "<REDACTED-openai>",
        "C:\\private",
        "/home/operator",
    ):
        assert forbidden not in json.dumps(response.json())
    assert (
        operator_app.client.get(f"/api/agents/sales/learning/records/{other['id']}").status_code
        == 404
    )
    assert (
        operator_app.client.get("/api/agents/sales/learning/records/..%5Ccredentials").status_code
        == 422
    )


def test_pagination_attention_and_invalid_queries(operator_app):
    service = operator_app.services["sales"]
    expected = []
    for number, status in enumerate(
        ("failed", "complete", "deferred", "needs_reassessment", "unresolvable")
    ):
        expected.append(
            service.store.put(
                "experience", {"task": str(number), "status": status}, key=str(number)
            )
        )
    client = operator_app.client
    page = client.get("/api/agents/sales/learning/records?limit=2&kind=failure").json()
    assert [row["id"] for row in page["records"]] == [expected[4]["id"], expected[3]["id"]]
    next_page = client.get(
        f"/api/agents/sales/learning/records?limit=2&kind=failure&cursor={page['next_cursor']}"
    ).json()
    assert [row["id"] for row in next_page["records"]] == [expected[2]["id"], expected[0]["id"]]
    assert client.get("/api/agents/sales/learning").json()["failures"] == 4
    failures = client.get("/api/agents/sales/learning/records?kind=failure&status=deferred").json()
    assert [row["id"] for row in failures["records"]] == [expected[2]["id"]]
    for query in ("limit=0", "limit=101", "kind=secrets"):
        assert client.get(f"/api/agents/sales/learning/records?{query}").status_code == 422


def test_pause_resume_and_cli_share_one_service_without_enabling_config(operator_app, monkeypatch):
    from cli_learning import register_learning_commands

    @click.group()
    def commands():
        pass

    register_learning_commands(commands)
    runner = CliRunner()
    client = operator_app.client
    paused = client.post("/api/agents/default/learning/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert paused.json()["enabled"] is True  # configuration is enabled, even while paused
    result = runner.invoke(commands, ["summary", "default", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == paused.json()
    result = runner.invoke(commands, ["resume", "default", "--json"])
    assert result.exit_code == 0, result.output
    assert client.get("/api/agents/default/learning").json()["paused"] is False
    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "false")
    resumed = client.post("/api/agents/default/learning/resume").json()
    assert resumed["enabled"] is False


def test_service_failure_is_visible_and_does_not_expose_paths(operator_app):
    operator_app.resolver.side_effect = OSError("Cannot read C:\\private\\learning.db token=secret")
    response = operator_app.client.get("/api/agents/sales/learning")
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    assert "private" not in response.text


@pytest.mark.parametrize("conflict", ["return", "raise"])
def test_rollback_delegates_to_promotion_and_reports_conflict(operator_app, monkeypatch, conflict):
    from personas.learning import promotion

    service = operator_app.services["sales"]
    activation = service.store.put("activation", {"status": "active_provisional"}, key="act")
    rollback = Mock(
        return_value={"status": "conflict", "application_receipt": {"status": "conflict"}}
    )
    if conflict == "raise":
        rollback.side_effect = LearningError("Newer content conflicts with rollback")
    monkeypatch.setattr(promotion, "rollback_activation", rollback)
    response = operator_app.client.post(
        f"/api/agents/sales/learning/activations/{activation['id']}/rollback"
    )
    assert response.status_code == 409
    assert "conflicts" in response.json()["detail"]
    rollback.assert_called_once_with(
        service, activation["id"], reason="Operator requested rollback"
    )
    assert (
        operator_app.client.post(
            "/api/agents/crypto/learning/activations/missing/rollback"
        ).status_code
        == 404
    )


def test_cli_failure_is_one_redacted_json_object(operator_app):
    from cli_learning import learning_show

    result = CliRunner().invoke(learning_show, ["sales", "missing", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"success": False, "error": "Learning record not found"}


def test_redaction_disabled_never_exposes_raw_record_text(monkeypatch):
    monkeypatch.setattr(operator.redact_module, "_REDACT_ENABLED", False)
    assert "<REDACTED-openai>" not in operator.safe_text("<REDACTED-openai>")


def test_synthesis_projection_preserves_exact_ranges_and_actual_receipts(operator_app):
    from personas.learning.queue import LearningQueue

    service = operator_app.services["sales"]
    experience = service.capture_experience("source", "test", "Observe this situation")
    execution = service.record_execution(
        experience["id"],
        {
            "success": True,
            "model": "fake-model",
            "provider": "fake-provider",
            "learning_role": "synthesis_dream",
        },
        attempt_key="synthesis",
    )
    manifest = [
        {
            "ref": "episode:test",
            "revision": "v1",
            "kind": "episode",
            "start": 0,
            "end": 12,
            "complete": False,
            "text": "test excerpt",
        }
    ]
    cycle = service.store.put(
        "synthesis_cycle",
        {
            "synthesis_kind": "dream",
            "status": "retained",
            "input_hash": "hash",
            "input_manifest": manifest,
            "omitted_manifest": [{"ref": "episode:test", "revision": "v1", "start": 12, "end": 30}],
            "evidence_ids": [experience["id"]],
            "execution_id": execution["id"],
            "result_ids": [],
        },
        key="dream-cycle",
    )
    service.store.event(
        cycle["id"], "synthesis_reasoning", {"execution_id": execution["id"]}, key="reasoning"
    )
    queue = LearningQueue(service)
    queue.enqueue("dream", "dream", payload={"cycle_id": cycle["id"]})
    job = queue.claim()
    queue.finish_stage(job, status="deferred", error="Provider unavailable")
    client = operator_app.client
    first = client.get("/api/agents/sales/learning/lifecycle").json()
    view = first["recent_cycles"][0]
    assert view["consumption_status"] == "pending"
    assert view["partial_inputs"] == 1 and len(view["omitted_manifest"]) == 1
    assert len(view["model_calls"]) == 1
    assert view["model_calls"][0]["model"] == "fake-model"
    assert first["pending_stages"][0]["reason"] == "Provider unavailable"
    service.store.event(
        cycle["id"],
        "synthesis_consumed",
        {"manifest": manifest, "execution_id": execution["id"]},
        key="consumed",
    )
    second = client.get("/api/agents/sales/learning/lifecycle").json()
    assert second["recent_cycles"][0]["consumed_manifest"] == [
        {key: value for key, value in manifest[0].items() if key != "text"}
    ]
    detail = client.get(f"/api/agents/sales/learning/records/{cycle['id']}").json()
    assert detail["payload"]["input_manifest"] == manifest
    assert second["recent_cycles"][0]["projection_status"] == "pending"
    assert second["synthesis"]["dream"]["consumed_characters"] == 12
    assert client.get("/api/agents/sales/learning/records?kind=synthesis_cycle").status_code == 200


def test_tuning_reads_do_not_write_and_explicit_posts_delegate(operator_app, monkeypatch):
    from evolve import tuning

    service = operator_app.services["crypto"]
    client = operator_app.client
    response = client.get("/api/agents/crypto/learning/tuning")
    assert response.status_code == 200
    assert response.json()["min_cases"] == 60
    assert response.json()["validated_cases"] == 0
    assert not service.target.data_dir.exists()
    calls = []

    async def admit(actual):
        calls.append(("run", actual.target.persona_id))
        return {"status": "no_change", "reason": "insufficient_validated_cases"}

    def rollback(actual, *, reason):
        calls.append(("rollback", actual.target.persona_id, reason))
        return {"status": "rolled_back", "policy_id": "policy-test"}

    monkeypatch.setattr(tuning, "tune", admit)
    monkeypatch.setattr(tuning, "rollback_policy", rollback)
    assert calls == []
    assert client.post("/api/agents/crypto/learning/tuning/run").json()["status"] == "no_change"
    assert (
        client.post("/api/agents/crypto/learning/tuning/rollback").json()["status"] == "rolled_back"
    )
    assert calls == [("run", "crypto"), ("rollback", "crypto", "Operator requested rollback")]


def test_context_only_and_tuning_never_count_as_method_or_thinking_success(operator_app):
    service = operator_app.services["sales"]
    service.store.put(
        "cognitive_cycle", {"status": "completed", "execution_kind": "context_only"}, key="context"
    )
    service.store.put("tuning_run", {"status": "no_change"}, key="run")
    service.store.put(
        "tuning_evaluation", {"status": "completed", "passed": True}, key="tuning-check"
    )
    report = operator.LearningOperator(service).report()
    assert report["counts"]["context_only_cycles"] == 1
    assert report["counts"]["completed_cycles"] == 0
    assert report["counts"]["recorded_model_calls"] == 0
    assert report["counts"]["recall_tuning_runs"] == 1
    assert report["counts"]["recall_tuning_evaluations"] == 1
    assert report["counts"]["methods_adopted"] == 0
    assert report["counts"]["qualification_passes"] == 0


def test_actual_foreground_receipt_and_policy_activation_have_separate_counts(operator_app):
    service = operator_app.services["sales"]
    service.store.put(
        "cognitive_cycle",
        {
            "status": "completed",
            "execution_kind": "reasoning",
            "foreground_receipt": {
                "success": True,
                "model": "fake",
                "provider": "fake",
                "response_hash": "actual-output",
            },
        },
        key="foreground",
    )
    policy = service.store.put("tuning_policy", {"status": "active"}, key="policy")
    service.store.event(policy["id"], "tuning_activation", {"run_id": "run"}, key="activation")
    report = operator.LearningOperator(service).report()
    assert report["counts"]["recorded_model_calls"] == 1
    assert report["counts"]["recall_policy_activations"] == 1
    assert report["counts"]["methods_adopted"] == 0
    assert report["counts"]["understanding_changes"] == 0


def test_root_cli_registers_tuning_and_lifecycle_without_inference(operator_app, monkeypatch):
    from cli import main

    monkeypatch.setattr(
        LearningService, "for_persona", staticmethod(lambda name: operator_app.services[name])
    )
    runner = CliRunner()
    for command in ("tune", "status", "rollback"):
        result = runner.invoke(main, ["evolve", command, "--help"])
        assert result.exit_code == 0, result.output
        assert "--json" in result.output
    result = runner.invoke(main, ["evolve", "status", "--persona", "sales", "--json"])
    assert result.exit_code == 0, result.output
    assert (
        json.loads(result.output)
        == operator_app.client.get("/api/agents/sales/learning/tuning").json()
    )
    result = runner.invoke(main, ["profile", "learning", "lifecycle", "sales", "--json"])
    assert result.exit_code == 0, result.output
    assert (
        json.loads(result.output)
        == operator_app.client.get("/api/agents/sales/learning/lifecycle").json()
    )
    assert not operator_app.services["sales"].target.data_dir.exists()
