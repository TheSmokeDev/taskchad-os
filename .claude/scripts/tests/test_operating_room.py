"""Operating Room and Capability Gateway tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for path in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import config  # noqa: E402
from orchestration.capability_gateway import collect_capability_gateway_status  # noqa: E402
from orchestration.db import OrchestrationDB  # noqa: E402
from orchestration.operating_room import OperatingRoomService, operating_room_result_to_dict  # noqa: E402


def test_operating_room_builds_public_safe_proof_packet() -> None:
    db = OrchestrationDB(":memory:")
    try:
        result = OperatingRoomService(db).run_operating_room(
            goal="Launch the Homie Operating Room demo",
            run_tick=True,
        )
        payload = operating_room_result_to_dict(result)
        proof = payload["proof_packet"]
        proof_json = json.dumps(proof)

        assert payload["team_room"]["meeting_mode"] == "facilitated_boardroom"
        assert proof["product_surface"] == "homie_operating_room"
        assert proof["sanitized"] is True
        assert proof["team_id"] == payload["team_room"]["team_id"]
        assert proof["convoy_id"] == payload["team_room"]["convoy_id"]
        assert proof["workflow_id"] == "growth_boardroom"
        assert proof["vote_board"]
        assert proof["interrupts"]
        assert proof["owner_actions"]
        assert proof["tick_summary"] is not None
        assert "Final Team Room brief" in proof["final_brief"]
        assert "session_id" not in proof_json
        assert "claim_token" not in proof_json
        assert "system_prompt" not in proof_json
        assert "authorization" not in proof_json.lower()

        row = db.conn.execute(
            "SELECT metadata FROM team_sessions WHERE id = ?",
            (proof["team_id"],),
        ).fetchone()
        metadata = json.loads(row["metadata"])
        assert metadata["operating_room_run_id"] == proof["run_id"]
        assert metadata["operating_room_proof"]["sanitized"] is True
    finally:
        db.close()


def test_operating_room_api_returns_proof_packet(tmp_path) -> None:
    db_path = tmp_path / "operating_room_api.db"
    with patch.object(config, "ORCHESTRATION_DB_PATH", db_path):
        import importlib
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, team_svc = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = team_svc
        try:
            response = TestClient(api_mod.app).post(
                "/api/team/operating-room/run",
                json={
                    "goal": "Launch the Homie Operating Room demo",
                    "allow_live_agent_run": True,
                },
            )

            assert response.status_code == 200
            body = response.json()
            assert body["run_id"].startswith("opr-")
            assert body["proof_packet"]["sanitized"] is True
            assert body["proof_packet"]["team_id"] == body["team_room"]["team_id"]
            assert body["proof_packet"]["progress"]["total"] == 21
            assert "tick_summary" in body["proof_packet"]
        finally:
            db.close()


def test_capability_gateway_status_shape() -> None:
    payload = collect_capability_gateway_status()

    assert payload["status"] == "ok"
    assert payload["runtime"]["selected_lane"]
    assert isinstance(payload["capabilities"]["items"], list)
    assert isinstance(payload["toolsets"], list)
    assert isinstance(payload["integrations"]["items"], list)
    assert payload["approval_policy"]["default_deny"] is True
    assert payload["outbound_messaging"]["requires_operator_confirmation"] is True
