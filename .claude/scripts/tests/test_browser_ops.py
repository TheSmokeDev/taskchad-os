from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import browser_ops  # type: ignore[import-not-found]  # noqa: E402


def _ready() -> dict[str, object]:
    return {
        "enabled": True,
        "status": "ready",
        "cdp_port": 9222,
        "cdp_reachable": True,
        "browser": "Chrome/126",
        "visible_guard": "visible",
        "tab_count": 3,
        "agent_browser_command_source": "path",
        "reason": "ready",
    }


def _stream(*, port: int) -> dict[str, object]:
    assert port == 9222
    return {
        "enabled": True,
        "connected": True,
        "port": 31137,
        "screencasting": False,
        "reason": "ready",
    }


def test_capability_pack_is_safe_and_policy_rich(monkeypatch) -> None:
    monkeypatch.setattr(browser_ops, "browser_readiness", _ready)
    monkeypatch.setattr(browser_ops, "browser_stream_status", _stream)
    monkeypatch.setattr(
        browser_ops,
        "load_agent_browser_core_guide",
        lambda **_kwargs: {
            "available": True,
            "source": "agent-browser skills get core",
            "content": "Use snapshot -i -c.",
            "truncated": False,
            "reason": "loaded",
        },
    )

    pack = browser_ops.build_browserops_capability_pack(
        "open https://example.com/path?token=secret#frag",
        include_core_guide=True,
    )
    dumped = json.dumps(pack)

    assert pack["specialist"]["name"] == "Browser Homie"
    assert pack["readiness"]["cdp_port"] == 9222
    assert pack["stream"]["port"] == 31137
    assert pack["controls"]["headless_fallback"] is False
    assert pack["linkedin_operator"]["mode"] == "draft_approve_execute"
    assert any(workflow["workflow_id"] == "browserops.context" for workflow in pack["workflows"])
    assert "agent-browser skills get core" in dumped
    assert "snapshot -i -c" in dumped
    assert "Heartbeat may propose LinkedIn ideas" in dumped
    assert "secret" not in dumped
    assert "#frag" not in dumped


def test_prefetch_context_loads_browser_best_practices(monkeypatch) -> None:
    monkeypatch.setattr(browser_ops, "browser_readiness", _ready)
    monkeypatch.setattr(browser_ops, "browser_stream_status", _stream)
    monkeypatch.setattr(
        browser_ops,
        "load_agent_browser_core_guide",
        lambda **_kwargs: {
            "available": True,
            "source": "agent-browser skills get core",
            "content": "Snapshot first, click refs, then snapshot again.",
            "truncated": False,
            "reason": "loaded",
        },
    )

    context = browser_ops.build_browserops_prefetch_context(
        "go to LinkedIn and check my profile"
    )

    assert "BrowserOps Specialist Context" in context
    assert "Browser Homie" in context
    assert "agent-browser skills get core" in context
    assert "snapshot -i -c" in context
    assert "LinkedIn operator model" in context
    assert "`/linkedin` for LinkedIn post drafting" in context
    assert "explicit approval" in context
    assert "headless" in context


def test_guide_loader_failure_redacts_urls() -> None:
    def runner(*_args, **_kwargs):
        raise RuntimeError("failed at https://example.com/path?token=secret#frag")

    guide = browser_ops.load_agent_browser_core_guide(runner=runner)
    dumped = json.dumps(guide)

    assert guide["available"] is False
    assert "https://example.com/path" in dumped
    assert "secret" not in dumped
    assert "#frag" not in dumped
