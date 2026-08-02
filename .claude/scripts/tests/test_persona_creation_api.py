from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import dashboard_api
from personas.creation import (
    PersonaCreationSpec,
    compile_creation_plan,
    get_creation_catalog,
)
from personas.provisioning import ProvisionPaths

CHANNEL_ID = "987654321098765432"


def _paths(tmp_path: Path) -> ProvisionPaths:
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "env_groups": {
                    "runtime_core": ["OPENAI_API_KEY"],
                    "vault_memory": ["HOMIE_VAULT_DIR"],
                    "business_profile": ["BUSINESS_EMAIL"],
                },
                "skill_groups": {},
                "profile_defaults": {
                    "env_groups": ["runtime_core", "vault_memory"],
                    "skill_groups": [],
                    "skills": [],
                },
                "profiles": {},
            }
        ),
        encoding="utf-8",
    )
    master_env = tmp_path / "master.env"
    master_env.write_text(
        "OPENAI_API_KEY=private-test-value\n"
        "HOMIE_VAULT_DIR=C:/vault\n"
        "BUSINESS_EMAIL=ops@example.com\n",
        encoding="utf-8",
    )
    bindings = tmp_path / "bindings.json"
    bindings.write_text('{"guild_id":"test","channels":{}}\n', encoding="utf-8")
    return ProvisionPaths(
        homie_root=tmp_path / "homie",
        bindings_file=bindings,
        capability_matrix_file=matrix,
        master_env_file=master_env,
    )


@pytest.fixture
def creation_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, ProvisionPaths]:
    paths = _paths(tmp_path)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(
        ProvisionPaths,
        "defaults",
        classmethod(lambda cls: paths),
    )
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )

    app = FastAPI()

    @app.middleware("http")
    async def _scope(request: Request, call_next):
        raw = request.headers.get("x-test-persona-scope")
        request.state.persona_scope = (
            None
            if raw is None
            else frozenset(value for value in raw.split(",") if value)
        )
        request.state.workspace_id = 1
        request.state.is_admin = raw is None
        return await call_next(request)

    app.include_router(dashboard_api.router)
    return TestClient(app), paths


def _body(persona_id: str) -> dict:
    return {
        "persona_id": persona_id,
        "display_name": "API Surface Engineer",
        "template": "ai-engineer",
        "role": "Inspect API architecture and propose bounded changes.",
        "model": "claude-opus-4-7",
        "domain": "api-engineering",
        "channel_intent": {
            "kind": "discord",
            "channel_id": CHANNEL_ID,
            "name": "api-surface",
        },
        "operator_exec": False,
    }


def test_templates_preview_and_apply_use_the_same_python_plan(
    creation_api: tuple[TestClient, ProvisionPaths],
) -> None:
    client, paths = creation_api
    templates = client.get("/api/agents/templates")
    assert templates.status_code == 200
    assert templates.json()["templates"] == list(get_creation_catalog())

    body = _body("api-surface")
    preview_response = client.post("/api/agents/preview", json=body)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    expected_plan = compile_creation_plan(
        PersonaCreationSpec(
            persona_id="api-surface",
            template_id="ai-engineer",
            display_name="API Surface Engineer",
            role="Inspect API architecture and propose bounded changes.",
            model="claude-opus-4-7",
            domain="api-engineering",
            discord_channel_id=CHANNEL_ID,
            discord_channel_name="api-surface",
        )
    )
    expected_json_plan = json.loads(json.dumps(expected_plan.as_dict()))
    assert preview["plan"] == expected_json_plan

    apply_body = {
        **body,
        "expected_preview_hash": preview["preview_hash"],
        "expected_state_hash": preview["state_hash"],
    }
    created = client.post(
        "/api/agents",
        json=apply_body,
        headers={"x-operator-id": "api-test"},
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["preview_hash"] == preview["preview_hash"]
    assert payload["receipt"]["plan"] == preview["plan"]
    assert payload["receipt"]["outcome"] == "created"
    assert payload["receipt"]["transaction_id"]
    assert "private-test-value" not in created.text

    profile = paths.profiles_root / "api-surface"
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert config["persona"]["display_name"] == "API Surface Engineer"
    assert config["persona"]["role"].startswith("Inspect API")
    assert config["model"]["preferred"] == "claude-opus-4-7"
    bindings = json.loads(paths.bindings_file.read_text(encoding="utf-8"))
    assert bindings["channels"][CHANNEL_ID]["name"] == "api-surface"


def test_cross_grain_and_hostile_boolean_refuse_with_row_unchanged(
    creation_api: tuple[TestClient, ProvisionPaths],
) -> None:
    client, paths = creation_api
    body = _body("forbidden-persona")
    before = paths.bindings_file.read_bytes()

    denied = client.post(
        "/api/agents",
        json=body,
        headers={"x-test-persona-scope": "other-persona"},
    )
    assert denied.status_code == 403
    assert paths.bindings_file.read_bytes() == before
    assert not (paths.profiles_root / "forbidden-persona").exists()

    hostile = client.post(
        "/api/agents",
        json={**_body("hostile-bool"), "operator_exec": "false"},
    )
    assert hostile.status_code == 422
    assert paths.bindings_file.read_bytes() == before
    assert not (paths.profiles_root / "hostile-bool").exists()


def test_preview_hash_pair_is_contractually_all_or_nothing(
    creation_api: tuple[TestClient, ProvisionPaths],
) -> None:
    client, paths = creation_api
    refused = client.post(
        "/api/agents",
        json={**_body("half-hash"), "expected_preview_hash": "a" * 64},
    )
    assert refused.status_code == 422
    assert not (paths.profiles_root / "half-hash").exists()
