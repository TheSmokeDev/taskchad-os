from __future__ import annotations

import json
from pathlib import Path

from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING, TOOL_REASONING
from runtime.framework_registry import (
    discover_framework_registry,
    discover_mcp_servers,
    discover_skills,
    redact_mcp_config,
    render_framework_tool_map,
)
from runtime.prompt_builder import render_cli_prompt


def _write_skill(path: Path, *, name: str, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_discovers_skills_and_excludes_generated(tmp_path: Path) -> None:
    _write_skill(
        tmp_path / ".claude" / "skills" / "direct-integrations" / "SKILL.md",
        name="direct-integrations",
        description="Query direct APIs without browser fallback.",
    )
    # Nested, hand-authored (non-generated) skill is still discovered.
    _write_skill(
        tmp_path / ".claude" / "skills" / "mcp-client" / "nested" / "SKILL.md",
        name="mcp-nested",
        description="A nested hand-authored skill.",
    )
    # Default-deny: auto-drafted skills under generated/ are unscanned + ungated,
    # so they must NOT appear in the generic-lane framework tool map.
    _write_skill(
        tmp_path / ".claude" / "skills" / "generated" / "seo" / "geo" / "SKILL.md",
        name="geo-audit",
        description="Audit generated GEO content.",
    )

    skills = discover_skills(tmp_path)

    names = [skill.name for skill in skills]
    assert "direct-integrations" in names
    assert "mcp-nested" in names
    assert "geo-audit" not in names  # generated/ excluded by default-deny
    assert ".claude/skills/generated/seo/geo/SKILL.md" not in [s.path for s in skills]


def test_discovers_mcp_servers_and_redacts_secret_values(tmp_path: Path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "brave-search": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
                        "env": {"BRAVE_API_KEY": "actual-secret-value"},
                        "url": "https://example.test/mcp?api_key=abc123&mode=read",
                    },
                    "zapier": {
                        "url": "https://mcp.zapier.com/api/v1/connect",
                        "api_key": "do-not-render",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    servers = discover_mcp_servers(tmp_path, config_path=config_path)

    assert [server.name for server in servers] == ["brave-search"]
    assert servers[0].transport == "stdio"
    assert servers[0].config["env"] == {"BRAVE_API_KEY": "<env:BRAVE_API_KEY>"}
    assert "actual-secret-value" not in json.dumps(servers[0].config)
    assert "api_key=<redacted>" in servers[0].config["url"]
    assert "mode=read" in servers[0].config["url"]


def test_redact_mcp_config_handles_env_placeholders_and_literal_secrets() -> None:
    redacted = redact_mcp_config(
        {
            "api_key": "${EXA_API_KEY}",
            "client_secret": "x" * 40,
            "url": "https://example.test/sse?token=super-secret&workspace=main",
        }
    )

    assert redacted["api_key"] == "<env:EXA_API_KEY>"
    assert redacted["client_secret"] == "<redacted>"
    assert "token=<redacted>" in redacted["url"]
    assert "workspace=main" in redacted["url"]


def test_render_framework_tool_map_is_compact_and_excludes_zapier(tmp_path: Path) -> None:
    _write_skill(
        tmp_path / ".claude" / "skills" / "mcp-client" / "SKILL.md",
        name="mcp-client",
        description="List and call configured MCP tools on demand.",
    )
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "exa": {"command": "npx", "args": ["-y", "exa-mcp"], "env": {"EXA_API_KEY": "secret"}},
                    "zapier": {"url": "https://mcp.zapier.com/api/v1/connect"},
                }
            }
        ),
        encoding="utf-8",
    )

    rendered = render_framework_tool_map(tmp_path)

    assert "Framework tool map" in rendered
    assert "mcp-client" in rendered
    assert "exa" in rendered
    assert "EXA_API_KEY" in rendered
    assert "secret" not in rendered
    assert "zapier" not in rendered.lower()


def test_prompt_builder_injects_framework_map_only_for_tool_reasoning(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path / ".claude" / "skills" / "direct-integrations" / "SKILL.md",
        name="direct-integrations",
        description="Query direct APIs.",
    )
    tool_request = RuntimeRequest(
        prompt="check email",
        cwd=tmp_path,
        task_name="chat_turn",
        capability=TOOL_REASONING,
    )
    text_request = RuntimeRequest(
        prompt="summarize",
        cwd=tmp_path,
        task_name="summary",
        capability=TEXT_REASONING,
    )

    tool_prompt = render_cli_prompt(tool_request)
    text_prompt = render_cli_prompt(text_request)

    assert "Framework tool map" in tool_prompt
    assert "direct-integrations" in tool_prompt
    assert "Framework tool map" not in text_prompt


def test_prompt_builder_can_omit_empty_framework_map_for_tests() -> None:
    request = RuntimeRequest(
        prompt="go",
        cwd=".",
        task_name="chat_turn",
        capability=TOOL_REASONING,
    )

    rendered = render_cli_prompt(request, framework_tool_map="")

    assert "Framework tool map" not in rendered


def test_registry_records_config_source(tmp_path: Path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {"exa": {"command": "npx"}}}), encoding="utf-8")

    registry = discover_framework_registry(tmp_path, mcp_config_path=config_path)

    assert registry.mcp_config_path == config_path
    assert registry.mcp_servers[0].source == ".mcp.json"


def test_discover_skills_fences_persona_scoped_promoted_skills(
    tmp_path: Path, monkeypatch
) -> None:
    """#429 codex R5 BLOCKER: the generic-lane discovery returned EVERY
    promoted SKILL.md with no scope check — the tool map then handed any
    runtime a readable path to a skill scoped to somebody else, hidden only by
    the incidental rendering cap. Promoted skills now need a positive
    unrestricted marker to enter the framework registry; hand-authored skills
    are never gated (they never went through the promotion gate)."""
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)

    _write_skill(
        tmp_path / ".claude" / "skills" / "promoted" / "sales-research" / "SKILL.md",
        name="sales-research",
        description="Scoped to sales only.",
    )
    _write_skill(
        tmp_path / ".claude" / "skills" / "promoted" / "global-skill" / "SKILL.md",
        name="global-skill",
        description="Globally promoted.",
    )
    _write_skill(
        tmp_path / ".claude" / "skills" / "promoted" / "orphan-skill" / "SKILL.md",
        name="orphan-skill",
        description="Promoted with no scope row.",
    )
    _write_skill(
        tmp_path / ".claude" / "skills" / "hand-authored" / "SKILL.md",
        name="hand-authored",
        description="Never went through the gate.",
    )

    skill_usage.record_recurrence("sales-research", path="x")
    skill_usage.record_persona_assignment("sales-research", "sales")
    skill_usage.record_recurrence("global-skill", path="x")
    skill_usage.mark_scope_unrestricted("global-skill")

    names = [skill.name for skill in discover_skills(tmp_path)]
    assert "hand-authored" in names
    assert "global-skill" in names
    assert "sales-research" not in names
    assert "orphan-skill" not in names  # fail closed: no row, no reach
