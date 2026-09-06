"""Registered persona-private expectation capture; no external capability."""

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
    return 1
