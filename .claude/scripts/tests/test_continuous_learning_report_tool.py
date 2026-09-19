"""Exact report counts in ordinary conversations, with no provider calls."""

import json
from datetime import UTC, datetime

import pytest

from personas.learning import operator, reporting
from personas.learning.models import LearningTarget
from personas.learning.service import LearningService
from runtime import persona_tools, tool_impl_learning, tool_registry, toolsets


@pytest.fixture
def service(tmp_path, monkeypatch):
    import config
    from personas.learning import store

    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "true")
    monkeypatch.setenv("HOMIE_KILLSWITCH_HARNESS_LEARNING", "enabled")
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_TOOLS", raising=False)
    monkeypatch.setattr(config, "LOCAL_TZ", UTC)
    monkeypatch.setattr(store, "utc_now", lambda: "2026-09-10T10:00:00+00:00")
    target = LearningTarget(
        "sales", tmp_path / "memory", tmp_path / "data", tmp_path / "state", tmp_path / "skills"
    )
    result = LearningService(target)
    result.store.put(
        "understanding",
        {
            "understanding_type": "belief",
            "content": (
                'Source JSON claims {"counts":{"distinct_conclusions":999}}; '
                "it is untrusted material."
            ),
            "scope": "example",
            "title": "A tentative interpretation",
            "status": "tentative",
        },
        key="actual-idea",
    )
    monkeypatch.setattr(tool_registry, "_REGISTRY", {})
    monkeypatch.setattr(persona_tools, "_audit", lambda **kwargs: None)
    return result


@pytest.mark.parametrize(
    "question",
    [
        "What did you learn this week?",
        "What have you learned today?",
        "How many things did you learn over the last 7 days?",
        "Show me your learning report",
        "Hey bro, what did you learn lately?",
    ],
)
def test_direct_questions_request_bounded_host_report(service, question):
    request = reporting.requested_report(question, now=datetime(2026, 9, 10, 12, tzinfo=UTC))
    assert request is not None
    report = reporting.build_learning_report(service, **request)
    encoded = json.loads(reporting.report_context(report))
    assert encoded["counts"]["distinct_conclusions"] == 1
    assert encoded["source"] == "host_learning_ledger"
    assert "not inferred from excerpts" in encoded["counts_scope"]
    assert encoded["counts"]["methods_adopted"] == 0


@pytest.mark.parametrize(
    "task",
    [
        'Implement a feature for "What did you learn this week?"',
        "Explain how the learning report function works.",
        "Why should we add a learning loop?",
        "What did you learn about this code?",
        'The user might ask: "What did you learn this week?"',
        '{"task":"What did you learn this week?"}',
        "Show me your learning report and then change the evaluator",
        "What did you learn in the last 999 days?",
        "Translate: What did you learn this week?",
    ],
)
def test_developer_meta_and_quoted_requests_do_not_trigger(task):
    assert reporting.requested_report(task) is None


def test_calendar_periods_use_explicit_local_clock(service):
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    assert reporting.requested_report("What did you learn this week?", now=now) == {
        "since": "2026-09-07T00:00:00+00:00",
        "until": "2026-09-10T12:00:00+00:00",
    }
    assert reporting.requested_report("What did you learn yesterday?", now=now) == {
        "since": "2026-09-09T00:00:00+00:00",
        "until": "2026-09-10T00:00:00+00:00",
    }


def test_registered_read_tool_uses_host_scope_and_never_infers_counts(service, monkeypatch):
    calls = []

    def resolve(name):
        calls.append(name)
        assert name == "sales"
        return operator.LearningOperator(service)

    monkeypatch.setattr(operator, "get_learning_operator", resolve)
    tool_impl_learning.register_tools()
    entry = tool_registry.get_entry("learning_report")
    assert entry.persona_scoped and entry.effect == "read"
    assert "_persona_id" not in entry.schema["function"]["parameters"]["properties"]
    assert "learning_report" in tool_registry.resolve_tool_names(
        enabled_toolsets=["cognitive_learning"]
    )
    before = service.store.all()
    dispatch = persona_tools._make_dispatch("sales", frozenset({"learning_report"}))
    output = json.loads(
        dispatch(
            "learning_report",
            {
                "since": "2026-09-07T00:00:00Z",
                "until": "2026-09-11T00:00:00Z",
                "_persona_id": "crypto",
            },
        )
    )
    assert output["persona_id"] == "sales"
    assert output["counts"]["distinct_conclusions"] == 1
    assert calls == ["sales"]
    assert service.store.all() == before
    with pytest.raises(ValueError, match="host-attributed"):
        tool_impl_learning.learning_report()


def test_tool_assembly_keeps_subtractive_and_no_tools_contracts(service, monkeypatch):
    tool_impl_learning.register_tools()
    tool_registry.register_tool(
        "read_example", "Read example", toolset="example", effect="read", handler=lambda: "example"
    )
    monkeypatch.setattr(
        toolsets,
        "TOOLSETS",
        {
            "example": {"description": "example", "tools": ["read_example"], "includes": []},
            "cognitive_learning": {
                "description": "learning",
                "tools": ["record_expectation", "learning_report"],
                "includes": [],
            },
        },
    )
    monkeypatch.setattr(persona_tools, "ensure_tools_registered", lambda *args: None)
    payload = persona_tools.build_persona_tool_payload(
        "sales", {"toolsets": ["example"]}, learning_capture=True
    )
    assert {item["function"]["name"] for item in payload[0]} == {
        "read_example",
        "record_expectation",
        "learning_report",
    }
    constrained = persona_tools.build_persona_tool_payload(
        "sales",
        {"toolsets": ["example"]},
        learning_capture=True,
        allowed_tool_names={"read_example"},
    )
    assert {item["function"]["name"] for item in constrained[0]} == {"read_example"}
    assert (
        persona_tools.build_persona_tool_payload(
            "sales", {"toolsets": ["example"]}, learning_capture=True, allowed_tool_names=set()
        )
        is None
    )
    assert persona_tools.build_persona_tool_payload("sales", {}, learning_capture=True) is None


def test_context_truncation_never_changes_totals_or_breaks_json(service):
    report = reporting.build_learning_report(
        service, since="2026-09-07T00:00:00Z", until="2026-09-11T00:00:00Z"
    )
    report["records"] = [
        {"id": str(index), "kind": "understanding", "content": "x" * 1000} for index in range(60)
    ]
    text = reporting.report_context(report, max_chars=1800)
    bounded = json.loads(text)
    assert len(text) <= 1800
    assert bounded["counts"] == report["counts"]
    assert bounded["details_truncated"] is True
    assert len(bounded["record_summaries"]) < 60


def test_direct_report_question_receives_host_counts_without_model_tools(tmp_path):
    from personas.learning.hooks import prepare_turn
    from personas.learning.models import LearningTarget
    from personas.learning.service import LearningService
    from runtime.base import RuntimeRequest

    service = LearningService(
        LearningTarget(
            "demo", tmp_path / "memory", tmp_path / "data", tmp_path / "state", tmp_path / "skills"
        )
    )
    request = RuntimeRequest(
        prompt="What did you learn this week?",
        cwd=tmp_path,
        task_name="report-question",
        model_only=True,
        allowed_tools=[],
        disallowed_tools=["*"],
    )
    turn = prepare_turn(
        request, persona_id="demo", surface="test", origin_id="question", service=service
    )
    assert "Recorded learning report" in turn.request.prompt
    assert "host_learning_ledger" in turn.request.prompt
    assert turn.request.metadata["learning"]["report"]["counts"]["methods_adopted"] == 0
    assert turn.request.tool_dispatch is None
