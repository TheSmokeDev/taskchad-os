"""Persona-private expectation capture and read-only learning reports."""

from __future__ import annotations

import json


def record_expectation(
    claim: str,
    check_by: str,
    resolution_rule: str,
    situation: dict,
    *,
    _persona_id: str | None = None,
    **optional,
) -> str:
    from personas.learning.hooks import record_actor_expectation

    if not _persona_id:
        raise ValueError("expectations require a host-attributed persona")
    payload = {
        "claim": claim,
        "check_by": check_by,
        "resolution_rule": resolution_rule,
        "situation": situation,
    }
    payload.update(
        {
            key: value
            for key, value in optional.items()
            if key in {"domain", "subject", "confidence", "action", "thesis_tags"}
        }
    )
    record = record_actor_expectation(payload, persona_id=_persona_id)
    return json.dumps({"expectation_id": record["id"], "status": "committed_before_action"})


def learning_report(
    since: str | None = None,
    until: str | None = None,
    *,
    _persona_id: str | None = None,
) -> str:
    """Read this caller's host-counted learning; never invokes a model or network."""
    from personas.learning import operator, reporting

    if not _persona_id:
        raise ValueError("learning reports require a host-attributed persona")
    report = operator.get_learning_operator(_persona_id).report(since=since, until=until)
    return reporting.report_context(report)


def register_tools() -> int:
    from runtime import tool_registry

    tool_registry.register_tool(
        "record_expectation",
        "Record your testable expectation BEFORE the next meaningful action. "
        "This only saves your own prediction; it grants no action authority.",
        toolset="cognitive_learning",
        effect="write",
        persona_scoped=True,
        parameters={
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "check_by": {
                    "type": "string",
                    "description": "Observation deadline as timezone-aware ISO instant.",
                },
                "resolution_rule": {
                    "type": "string",
                    "description": "What observable evidence decides whether the claim held.",
                },
                "situation": {
                    "type": "object",
                    "description": "Relevant current circumstances; never secrets.",
                },
                "domain": {"type": "string"},
                "subject": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "action": {"type": "string", "enum": ["act", "pass"]},
            },
            "required": ["claim", "check_by", "resolution_rule", "situation"],
            "additionalProperties": False,
        },
        handler=record_expectation,
    )
    tool_registry.register_tool(
        "learning_report",
        "Read your own recorded learning for a period. Counts are computed by the host, "
        "not inferred from examples. Use this for what you learned, changed understanding, "
        "open investigations, and actual method adoption. No model or external call occurs.",
        toolset="cognitive_learning",
        effect="read",
        persona_scoped=True,
        parameters={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "Inclusive timezone-aware ISO timestamp; default seven days ago."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": "Exclusive timezone-aware ISO timestamp; default now.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=learning_report,
    )
    return 2
