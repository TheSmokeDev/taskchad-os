"""Test PRD-8 Phase 5b / WS3.3 — commands.py registration + Phase 4 disjointness.

Asserts:

* 3 new COMMANDS rows (cabinet, standup, discuss) with type=router, role=admin
* Cabinet category in CATEGORIES
* 3 new CORE_INTENTS entries with the correct keyword sets
* Defensive disjointness: NO `/voice` command, no voice keywords overlap
* No 'main' literal as a default roster id (Q4 canonical-id alignment)
* No direct `from cabinet ...` import in chat-process modules

These tests are pure-static (AST + import) — no async, no HTTP.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402


# ---------------------------------------------------------------------------
# COMMANDS rows
# ---------------------------------------------------------------------------


def _row_for(name: str) -> tuple[str, str, str, str] | None:
    for row in commands.COMMANDS:
        if row[0] == name:
            return row
    return None


def test_three_commands_rows() -> None:
    """All 3 new COMMANDS rows are registered as router/admin."""
    for name in ("cabinet", "standup", "discuss"):
        row = _row_for(name)
        assert row is not None, f"missing COMMANDS row for /{name}"
        assert row[2] == "router", f"/{name} must be router-type, not engine"
        assert row[3] == "admin", f"/{name} must be admin-only role"


def test_cabinet_category_exists() -> None:
    cats = {c[0]: c[1] for c in commands.CATEGORIES}
    assert "Cabinet" in cats, "Cabinet category missing from CATEGORIES"
    assert set(cats["Cabinet"]) == {"cabinet", "standup", "discuss"}


# ---------------------------------------------------------------------------
# CORE_INTENTS entries (broad-query intents)
# ---------------------------------------------------------------------------


def _intents_for(command: str) -> list[tuple[list[str], str, bool]]:
    return [(kws, c, b) for kws, c, b in commands.CORE_INTENTS if c == command]


def test_core_intents_added() -> None:
    """All 3 cabinet CORE_INTENTS entries exist."""
    for cmd in ("cabinet", "standup", "discuss"):
        rows = _intents_for(cmd)
        assert len(rows) >= 1, f"missing CORE_INTENTS entry for /{cmd}"


def test_core_intents_not_included_in_brief() -> None:
    """Cabinet intents must NOT be in 'show me everything' broad queries
    (they spawn LLM workloads, not data fetches)."""
    for cmd in ("cabinet", "standup", "discuss"):
        rows = _intents_for(cmd)
        for _kws, _c, included in rows:
            assert included is False, (
                f"/{cmd} CORE_INTENTS entry must have included_in_brief=False"
            )


def test_cabinet_intent_keywords() -> None:
    """Verify cabinet keywords match the PRP-locked set."""
    rows = _intents_for("cabinet")
    found = False
    for kws, _c, _b in rows:
        if {"group chat", "all agents discuss", "cabinet meeting"} <= set(kws):
            found = True
    assert found, "cabinet IntentSpec keywords drift from PRP-locked set"


def test_standup_intent_keywords() -> None:
    rows = _intents_for("standup")
    found = False
    for kws, _c, _b in rows:
        if {"standup", "team standup", "rotating speakers"} <= set(kws):
            found = True
    assert found


def test_discuss_intent_keywords() -> None:
    rows = _intents_for("discuss")
    found = False
    for kws, _c, _b in rows:
        if {"debate", "discuss this with the team", "open debate"} <= set(kws):
            found = True
    assert found


# ---------------------------------------------------------------------------
# Phase 4 disjointness — NO /voice claim today (defensive against future
# voice slash command in Phase 4)
# ---------------------------------------------------------------------------


def test_phase_5b_does_not_introduce_voice_command() -> None:
    """R1 M2 framing fix: Phase 5b proactively avoids the `/voice` namespace.

    Phase 4 ships voice via inline markers (`voice.py` + `voice_markers.py`),
    NOT a /voice slash command. Phase 5b must NOT introduce /voice — so a
    future Phase 4 enhancement can claim it without collision.
    """
    cmd_names = {row[0] for row in commands.COMMANDS}
    assert "voice" not in cmd_names, (
        "Phase 5b must NOT register /voice (R1 M2 / defensive disjointness)"
    )
    assert "voice" not in core_handlers.CORE_HANDLERS, (
        "core_handlers must NOT have a 'voice' handler (R1 M2)"
    )


def test_cabinet_keywords_disjoint_with_voice() -> None:
    """Verify cabinet IntentSpec keywords have empty intersection with
    common voice-trigger phrases (defensive against future Phase 4 voice
    IntentSpec)."""
    cabinet_kws: set[str] = set()
    for cmd in ("cabinet", "standup", "discuss"):
        for kws, _c, _b in _intents_for(cmd):
            cabinet_kws |= set(kws)

    voice_triggers = {
        "respond with voice", "voice reply", "send a voice note",
        "speak", "voice message", "voicemail", "audio reply",
    }
    overlap = cabinet_kws & voice_triggers
    assert not overlap, (
        f"cabinet/standup/discuss keywords overlap with voice triggers: {overlap}"
    )


# ---------------------------------------------------------------------------
# No direct `from cabinet ...` import in chat process (R2 NB2)
# ---------------------------------------------------------------------------


def test_no_direct_cabinet_import_in_chat() -> None:
    """Cross-process invariant: chat-process modules MUST go via cabinet_api
    HTTP. Direct `from cabinet.text_orchestrator import ...` etc. is
    forbidden because the orchestrator + channel registries live in the
    SEPARATE orchestration API process.

    Allowlist: `.claude/chat/cabinet_text.py` is an existing Phase 5a
    re-export shim and is NOT consumed by any chat-process handler today.
    The shim's own `from cabinet ...` is allowed because the shim itself
    is loaded only when callers want the Python surface (none in 5b).
    """
    chat_dir = _SCRIPTS.parent / "chat"
    allowlist = {chat_dir / "cabinet_text.py"}
    forbidden_prefixes = (
        "from cabinet.",
        "from cabinet_text_service",
        "import cabinet.",
    )
    offenders: list[tuple[Path, int, str]] = []
    for py in chat_dir.rglob("*.py"):
        if py in allowlist:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            for prefix in forbidden_prefixes:
                if stripped.startswith(prefix):
                    offenders.append((py, lineno, stripped))
    assert not offenders, (
        f"forbidden direct cabinet/* imports in chat process: {offenders}"
    )


# ---------------------------------------------------------------------------
# Q4 canonical-id alignment — no 'main' as default roster id
# ---------------------------------------------------------------------------


def test_default_canonical_id_not_main() -> None:
    """R1 minor + Q4 class-of-bug regression: roster default must be
    'default' (framework canonical), NEVER 'main' (upstream string).

    Static grep over cabinet_api.py + the cabinet handler section of
    core_handlers.py for any roster-related literal asserts no
    `'"main"'` appears as a default agent id. The only legitimate
    'main' string in the codebase is in cabinet_pin/cabinet_unpin which
    rejects `agentId='main'` at the API boundary — and those live in
    `dashboard_api.py`, not in the files this test scans.
    """
    cabinet_api_src = (_SCRIPTS / "integrations" / "cabinet_api.py").read_text(
        encoding="utf-8",
    )
    # Scan every string literal in cabinet_api.py — none should be the
    # bare "main".
    module = ast.parse(cabinet_api_src)
    for node in ast.walk(module):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value != "main", (
                f"cabinet_api.py uses literal 'main' at line {node.lineno} — "
                "Q4 canonical-id alignment lock requires 'default' instead."
            )

    # Scan the cabinet handler functions in core_handlers.py.
    core_path = _SCRIPTS.parent / "chat" / "core_handlers.py"
    core_module = ast.parse(core_path.read_text(encoding="utf-8"))
    cabinet_func_names = {
        "handle_cabinet", "handle_standup", "handle_discuss",
        "_cabinet_usage_text", "_format_meeting_list",
    }
    for node in ast.walk(core_module):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name in cabinet_func_names:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    assert sub.value != "main", (
                        f"{core_path.name}:{sub.lineno} "
                        f"(in {node.name}) uses bare 'main' literal — "
                        "Q4 canonical-id alignment lock requires 'default'."
                    )


# ---------------------------------------------------------------------------
# Router dispatch smoke (CORE_HANDLERS keys are slashless)
# ---------------------------------------------------------------------------


def test_router_dispatch_for_cabinet_command() -> None:
    """The router relies on CORE_HANDLERS dispatch — no router.py edits
    needed (Phase 5 R1 B3 codified). Verify all 3 keys are slashless."""
    for key in ("cabinet", "standup", "discuss"):
        assert key in core_handlers.CORE_HANDLERS
        assert "/" not in key
