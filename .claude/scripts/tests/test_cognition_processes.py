"""Tests for cognition.processes — mental process detection and weights."""

from __future__ import annotations

import pytest

from cognition.processes import (
    MentalProcess,
    ProcessState,
    detect_process,
    get_process_weights,
    PROCESS_WEIGHTS,
)


# === Signal detection tests ===


def test_detect_planning_lets_plan():
    result, reason = detect_process("let's plan the API architecture")
    assert result == MentalProcess.PLANNING
    assert reason == "planning_signal"


def test_detect_planning_how_should_we():
    result, reason = detect_process("how should we approach the SEO strategy?")
    assert result == MentalProcess.PLANNING
    assert reason == "planning_signal"


def test_detect_planning_strategy():
    result, reason = detect_process("strategy for the brand fleet rollout")
    assert result == MentalProcess.PLANNING
    assert reason == "planning_signal"


def test_detect_monitoring_check_on():
    result, reason = detect_process("check on the server health")
    assert result == MentalProcess.MONITORING
    assert reason == "monitoring_signal"


def test_detect_monitoring_status_of():
    result, reason = detect_process("status of the outreach campaigns")
    assert result == MentalProcess.MONITORING
    assert reason == "monitoring_signal"


def test_detect_monitoring_any_alerts():
    result, reason = detect_process("any alerts from the business?")
    assert result == MentalProcess.MONITORING
    assert reason == "monitoring_signal"


def test_detect_learning_remember():
    result, reason = detect_process("remember that my email is X@y.com")
    assert result == MentalProcess.LEARNING
    assert reason == "learning_signal"


def test_detect_learning_from_now_on():
    result, reason = detect_process("from now on use Outlook for business email")
    assert result == MentalProcess.LEARNING
    assert reason == "learning_signal"


def test_detect_learning_fyi():
    result, reason = detect_process("fyi the teller cert expires next week")
    assert result == MentalProcess.LEARNING
    assert reason == "learning_signal"


def test_detect_execution_build():
    result, reason = detect_process("build this feature now")
    assert result == MentalProcess.EXECUTION
    assert reason == "execution_signal"


def test_detect_execution_deploy():
    result, reason = detect_process("deploy the cancellation recovery fix")
    assert result == MentalProcess.EXECUTION
    assert reason == "execution_signal"


def test_detect_execution_create():
    result, reason = detect_process("create the weekly synthesis report")
    assert result == MentalProcess.EXECUTION
    assert reason == "execution_signal"


# === Default/no-transition tests ===


def test_detect_default_short():
    result, reason = detect_process("hi")
    assert reason == "no_transition"


def test_detect_default_ambiguous():
    result, reason = detect_process("what happened with the recovery campaigns?")
    assert result == MentalProcess.DEFAULT
    assert reason == "no_transition"


def test_detect_default_data_query():
    result, reason = detect_process("show me the last 5 leads")
    assert result == MentalProcess.DEFAULT
    assert reason == "no_transition"


def test_detect_default_thanks():
    result, reason = detect_process("thanks for the update")
    assert result == MentalProcess.DEFAULT
    assert reason == "no_transition"


# === Explicit override tests ===


def test_explicit_override_planning():
    result, reason = detect_process("switch to planning mode")
    assert result == MentalProcess.PLANNING
    assert reason == "explicit_override"


def test_explicit_override_monitoring():
    result, reason = detect_process("enter monitoring mode")
    assert result == MentalProcess.MONITORING
    assert reason == "explicit_override"


def test_explicit_override_exec():
    result, reason = detect_process("go to exec mode")
    assert result == MentalProcess.EXECUTION
    assert reason == "explicit_override"


def test_explicit_override_short_text():
    """Explicit override works even on short text."""
    result, reason = detect_process("switch to plan mode")
    assert result == MentalProcess.PLANNING
    assert reason == "explicit_override"


def test_explicit_override_invalid_mode():
    """Invalid mode name stays at current."""
    result, reason = detect_process("switch to banana mode")
    assert result == MentalProcess.DEFAULT
    assert reason == "no_transition"


# === Process weights tests ===


def test_process_weights_default_empty():
    w = get_process_weights(MentalProcess.DEFAULT)
    assert w == {}


def test_process_weights_planning():
    w = get_process_weights(MentalProcess.PLANNING)
    assert w["durable_memory"] == 1.5
    assert w["continuity"] == 1.5
    assert w["recalled_memory"] == 1.3
    assert w["prefetched_context"] == 0.7


def test_process_weights_monitoring():
    w = get_process_weights(MentalProcess.MONITORING)
    assert w["prefetched_context"] == 1.5
    assert w["recalled_memory"] == 0.7


def test_process_weights_learning():
    w = get_process_weights(MentalProcess.LEARNING)
    assert w["user_model"] == 1.5


def test_process_weights_execution():
    w = get_process_weights(MentalProcess.EXECUTION)
    assert w["continuity"] == 1.5
    assert w["procedural_memory"] == 1.5


def test_all_processes_have_weights():
    for process in MentalProcess:
        assert process in PROCESS_WEIGHTS


# === ProcessState tests ===


def test_process_state_defaults():
    s = ProcessState()
    assert s.active == MentalProcess.DEFAULT
    assert s.previous == MentalProcess.DEFAULT


def test_process_state_custom():
    s = ProcessState(active=MentalProcess.PLANNING, session_id="test-123")
    assert s.active == MentalProcess.PLANNING
    assert s.session_id == "test-123"


# === Real-world message validation ===


REAL_WORLD_MESSAGES = [
    # Monitoring
    ("check on the server health", MentalProcess.MONITORING),
    ("any alerts from the business?", MentalProcess.MONITORING),
    ("how are the leads doing today?", MentalProcess.MONITORING),
    ("status of the outreach campaigns", MentalProcess.MONITORING),
    # Planning
    ("let's plan the outreach scheduler activation", MentalProcess.PLANNING),
    ("how should we approach the brand fleet SEO?", MentalProcess.PLANNING),
    ("let's design the multi-agent architecture", MentalProcess.PLANNING),
    ("what's the approach for the Wells Fargo integration?", MentalProcess.PLANNING),
    # Execution
    ("deploy the cancellation recovery fix", MentalProcess.EXECUTION),
    ("fix this leads query city bug", MentalProcess.EXECUTION),
    ("build the inbound SMS dashboard", MentalProcess.EXECUTION),
    ("create the weekly synthesis report", MentalProcess.EXECUTION),
    # Learning
    ("remember that the operator can't see personal email", MentalProcess.LEARNING),
    ("from now on use Outlook for business email", MentalProcess.LEARNING),
    ("my new phone number is 555-1234", MentalProcess.LEARNING),
    ("fyi the teller cert expires next week", MentalProcess.LEARNING),
    # Default
    ("what happened with the recovery campaigns?", MentalProcess.DEFAULT),
    ("show me the last 5 leads", MentalProcess.DEFAULT),
    ("thanks for the update", MentalProcess.DEFAULT),
]


@pytest.mark.parametrize("msg,expected", REAL_WORLD_MESSAGES)
def test_real_world_detection(msg, expected):
    result, _ = detect_process(msg)
    assert result == expected, f"'{msg}' should be {expected.value}, got {result.value}"
