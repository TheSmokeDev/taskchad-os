"""Test PRD-8 Phase 5a / WS1.0 (NB2) — RuntimeRequest extension.

(a) AST parity test — all 19 EXISTING fields preserved with original
    defaults AND the 4 NEW additive fields exist with `None` defaults.
(b) Lower-bound adapter behavior — `ClaudeSdkRuntime` forwards
    `disallowed_tools` and `mcp_servers` into the SDK options dict
    (R3-NM2 lower-bound proof: options-dict construction; not
    subprocess/CLI propagation).
(c) Cabinet integration — `cabinet_tool_policy(...)` results thread
    onto `RuntimeRequest.allowed_tools` + `disallowed_tools` + `mcp_servers`.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from runtime.base import RuntimeRequest
from runtime.claude_sdk import ClaudeSdkRuntime
from runtime.profiles import RuntimeProfile


_BASE_PY = (
    Path(__file__).resolve().parent.parent / "runtime" / "base.py"
)


# (a) AST parity test ─────────────────────────────────────────────────────


def _runtime_request_dataclass_fields() -> list[ast.AnnAssign]:
    src = _BASE_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls: ast.ClassDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeRequest":
            cls = node
            break
    assert cls is not None, "RuntimeRequest class not found in runtime/base.py"
    fields: list[ast.AnnAssign] = []
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append(stmt)
    return fields


# Names + (default-source-or-None) pairs the EXISTING 19 fields must have.
# Captured verbatim from runtime/base.py:16-37 prior to WS1.0.
_EXISTING_FIELDS_EXPECTED: list[tuple[str, str | None]] = [
    ("prompt", None),  # required
    ("cwd", None),  # required
    ("task_name", None),  # required
    ("capability", "TEXT_REASONING"),
    ("model", "None"),
    ("fallback_model", "None"),
    ("max_turns", "1"),
    ("max_budget_usd", "None"),
    ("allowed_tools", "field(default_factory=list)"),
    ("permission_mode", "None"),
    ("setting_sources", "field(default_factory=list)"),
    ("system_prompt", "None"),
    ("hooks", "None"),
    ("thinking", "None"),
    ("env", "None"),
    ("resume", "None"),
    ("stderr", "None"),
    ("allow_fallback", "True"),
    ("runtime_lane", "None"),
]

# 4 NEW additive fields (WS1.0 / NB2). All default to `None`.
_NEW_FIELDS_EXPECTED: list[str] = [
    "disallowed_tools",
    "mcp_servers",
    "metadata",
    "auth_profile",
]


def test_runtime_request_existing_fields_preserved() -> None:
    """R3-NB1 — all 19 existing fields present with their original defaults."""
    fields = _runtime_request_dataclass_fields()
    by_name: dict[str, ast.AnnAssign] = {
        f.target.id: f for f in fields if isinstance(f.target, ast.Name)
    }

    missing = [name for name, _ in _EXISTING_FIELDS_EXPECTED if name not in by_name]
    assert not missing, f"missing existing RuntimeRequest fields: {missing}"

    # Spot-check the defaults haven't drifted on a sampling of high-signal
    # fields (model=None, allowed_tools=field(default_factory=list),
    # max_turns=1, allow_fallback=True).
    samples = [
        ("model", "None"),
        ("max_turns", "1"),
        ("allowed_tools", "field(default_factory=list)"),
        ("allow_fallback", "True"),
    ]
    for name, expected_src in samples:
        node = by_name[name]
        assert node.value is not None, f"{name} default lost"
        actual = ast.unparse(node.value).strip()
        assert actual == expected_src, (
            f"RuntimeRequest.{name} default drifted: expected {expected_src!r}, got {actual!r}"
        )

    # AST disclaimer per R3-NB1: NO `messages` field — single `prompt: str`.
    assert "messages" not in by_name, (
        "RuntimeRequest must NOT have a `messages` field — it carries a single `prompt: str`"
    )


def test_runtime_request_four_new_fields_exist_with_none_default() -> None:
    """WS1.0 — disallowed_tools, mcp_servers, metadata, auth_profile (None defaults)."""
    fields = _runtime_request_dataclass_fields()
    by_name: dict[str, ast.AnnAssign] = {
        f.target.id: f for f in fields if isinstance(f.target, ast.Name)
    }
    for name in _NEW_FIELDS_EXPECTED:
        assert name in by_name, f"new RuntimeRequest field '{name}' missing"
        node = by_name[name]
        assert node.value is not None, f"{name} default missing"
        actual = ast.unparse(node.value).strip()
        assert actual == "None", (
            f"RuntimeRequest.{name} default must be None (Rule 1), got {actual!r}"
        )


def test_runtime_request_constructible_with_new_fields_default_none() -> None:
    """Smoke test — the dataclass actually constructs and exposes the new fields."""
    req = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="t",
    )
    assert req.disallowed_tools is None
    assert req.mcp_servers is None
    assert req.metadata is None
    assert req.auth_profile is None


# (b) Lower-bound adapter behavior — ClaudeSdkRuntime options forwarding ──


@pytest.mark.asyncio
async def test_claude_sdk_runtime_forwards_disallowed_and_mcp() -> None:
    """R3-NM2 lower-bound proof.

    Build a RuntimeRequest with `disallowed_tools=['Bash']` and
    `mcp_servers=['gmail']`. Patch claude_agent_sdk's symbols so the
    options dict construction at `runtime/claude_sdk.py:183-214` is
    visible to the test, then assert the dict carries both values.
    """
    from unittest.mock import patch

    captured: dict[str, object] = {}

    class _DummyOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def _empty_query(prompt, options):  # noqa: ARG001
        # Async iterator returning nothing — exits the async-for cleanly.
        if False:
            yield None

    profile = RuntimeProfile(
        key="primary-claude",
        provider="claude",
        model="claude-haiku-4-5-20251001",
    )
    runtime = ClaudeSdkRuntime(profile)
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="cabinet_test",
        capability="text_reasoning",
        max_turns=1,
        allowed_tools=["Read"],
        disallowed_tools=["Bash"],
        mcp_servers=["gmail"],
    )

    # Patch the SDK module symbols ClaudeSdkRuntime imports lazily.
    with patch("claude_agent_sdk.ClaudeAgentOptions", _DummyOptions), \
         patch("claude_agent_sdk.query", _empty_query):
        await runtime.run(request)

    assert captured.get("disallowed_tools") == ["Bash"], (
        f"disallowed_tools not forwarded into options dict: {captured!r}"
    )
    assert captured.get("mcp_servers") == ["gmail"], (
        f"mcp_servers not forwarded into options dict: {captured!r}"
    )
    # Sanity: existing fields still forwarded.
    assert captured.get("allowed_tools") == ["Read"]


# (c) Cabinet integration — tool_policy threads onto RuntimeRequest ──────


def test_cabinet_tool_policy_threads_onto_runtime_request() -> None:
    """`cabinet_tool_policy()` produces 3 lists; all three end up on RuntimeRequest."""
    from cabinet.tool_policy import cabinet_tool_policy, filter_mcp_servers

    policy = cabinet_tool_policy("ops", persona_tools=["Bash", "mcp:gmail"])
    mcp_filtered = filter_mcp_servers({"gmail": {"cmd": "gmail-mcp"}, "asana": {"cmd": "asana-mcp"}}, policy)

    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="cabinet",
        allowed_tools=list(policy.allowed_tools),
        disallowed_tools=list(policy.disallowed_tools),
        mcp_servers=list(mcp_filtered.keys()),
    )

    assert "Bash" in request.allowed_tools
    assert "Read" in request.allowed_tools  # SAFE_READONLY auto-included.
    assert request.disallowed_tools is not None and "Write" in request.disallowed_tools
    assert request.mcp_servers == ["gmail"]


# Ensure asyncio plugin is available for the async test.
pytest_plugins = ("pytest_asyncio",)


# Sanity: this module still imports without circular deps.
def test_module_loads() -> None:
    assert inspect.isclass(RuntimeRequest)
