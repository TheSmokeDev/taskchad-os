from __future__ import annotations

import core_handlers
import pytest
from cognition import shots_callback

import curriculum.service as curriculum_service


@pytest.mark.asyncio
async def test_curriculum_persona_token_is_position_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class FakeService:
        def grade(self, proposal_id: str, grade: str, *, note: str):
            captured.update(
                proposal_id=proposal_id,
                grade=grade,
                note=note,
            )
            return {"success": True, "persona_id": "ai-engineer"}

    monkeypatch.setattr(
        shots_callback, "resolve_active_persona", lambda: "default"
    )

    def fake_service(persona_id: str):
        captured["persona_id"] = persona_id
        return FakeService()

    monkeypatch.setattr(
        curriculum_service, "get_curriculum_service", fake_service
    )
    response = await core_handlers.handle_curriculum(
        None,
        None,
        "grade proposal-1 B useful outcome persona=ai-engineer",
    )

    assert '"success": true' in response
    assert captured == {
        "persona_id": "ai-engineer",
        "proposal_id": "proposal-1",
        "grade": "B",
        "note": "useful outcome",
    }


@pytest.mark.asyncio
async def test_curriculum_rejects_duplicate_persona_tokens() -> None:
    response = await core_handlers.handle_curriculum(
        None,
        None,
        "status persona=ai-engineer persona=founder-operator",
    )
    assert "at most one persona" in response
