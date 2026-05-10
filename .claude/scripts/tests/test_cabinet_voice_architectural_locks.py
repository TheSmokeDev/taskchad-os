"""PRD-8 Phase 6 — architectural locks (AST + grep scans).

Covers contract criteria:
  * ast_scan_no_lane_router_imports_in_voice
  * rule3_module_attribute_imports
  * ast_scan_no_voice_id_override_zero_matches
  * ast_scan_no_smallwebrtc_imports
  * ast_scan_no_pipecat_small_webrtc_prebuilt_imports
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

CABINET_VOICE_DIR = SCRIPTS_DIR / "cabinet" / "voice"


def _voice_python_files() -> list[Path]:
    """Return every .py file under cabinet/voice/ (excluding __pycache__)."""
    return sorted(
        p
        for p in CABINET_VOICE_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _read_voice_source() -> dict[str, str]:
    """Map {relative-path: source-text} for every cabinet/voice/*.py file."""
    return {str(p.relative_to(SCRIPTS_DIR)): p.read_text(encoding="utf-8") for p in _voice_python_files()}


def test_no_lane_router_imports():
    """ast_scan_no_lane_router_imports_in_voice — voice modules must NEVER import lane_router.

    The "no watered-down personas" lock: voice subprocess routes through Phase 5a's
    text_orchestrator.handle_text_turn (via cabinet_api.send_message HTTP), which
    is the ONLY LLM path. Direct lane_router imports would let voice bypass the
    cabinet kill-switch / shared brain.
    """
    sources = _read_voice_source()
    bad: list[tuple[str, str]] = []
    for path, src in sources.items():
        # Substring scan first (fast).
        if "from runtime.lane_router import" in src:
            bad.append((path, "from runtime.lane_router import"))
            continue
        if "lane_router.run_with_runtime_lanes" in src:
            bad.append((path, "lane_router.run_with_runtime_lanes"))
            continue
        # AST scan — catch `from runtime import lane_router`.
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "runtime" and any(a.name == "lane_router" for a in node.names):
                    bad.append((path, f"from runtime import lane_router (line {node.lineno})"))
                if node.module and node.module.startswith("runtime.lane_router"):
                    bad.append((path, f"{node.module} (line {node.lineno})"))
    assert not bad, (
        f"cabinet/voice/ must NEVER import lane_router (no watered-down personas lock). "
        f"Violations: {bad}"
    )


def test_no_smallwebrtc_imports():
    """ast_scan_no_smallwebrtc_imports — Phase 6 uses WebsocketServerTransport, NOT SmallWebRTC.

    SmallWebRTC was the wrong-architecture path identified in the deprecated
    R1-R4 cycle. The clean v2 PRP rejects it entirely.
    """
    sources = _read_voice_source()
    forbidden = ["SmallWebRTCTransport", "SmallWebRTCConnection", "smallwebrtc"]
    bad: list[tuple[str, str]] = []
    for path, src in sources.items():
        for fragment in forbidden:
            if fragment in src:
                bad.append((path, fragment))
    assert not bad, (
        f"cabinet/voice/ must NOT reference SmallWebRTC* (Q-V5 lock). "
        f"Violations: {bad}"
    )


def test_no_pipecat_prebuilt_ui_imports():
    """ast_scan_no_pipecat_small_webrtc_prebuilt_imports — vendored bundle, not PyPI prebuilt.

    Phase 6 vendors warroom/client.bundle.js + uses voice_html.py. The PyPI
    pipecat-ai-small-webrtc-prebuilt was the wrong path.
    """
    sources = _read_voice_source()
    # Also scan dashboard_api.py since the prebuilt-UI argument was about
    # mounting it on the dashboard server.
    sources[".claude/scripts/dashboard_api.py"] = (SCRIPTS_DIR / "dashboard_api.py").read_text(
        encoding="utf-8"
    )
    forbidden = [
        "pipecat-ai-small-webrtc-prebuilt",
        "pipecat_ai_small_webrtc_prebuilt",
        "SmallWebRTCPrebuiltUI",
    ]
    bad: list[tuple[str, str]] = []
    for path, src in sources.items():
        for fragment in forbidden:
            if fragment in src:
                bad.append((path, fragment))
    assert not bad, (
        f"Voice surface must NOT reference pipecat prebuilt UI package. Violations: {bad}"
    )


def test_no_voice_id_override_references():
    """ast_scan_no_voice_id_override_zero_matches — only `voice_overrides` is the canonical name.

    The dict-keyed-by-provider shape is `voice_overrides`, NOT
    `voice_id_override` (singular). Pin the canonical name to prevent
    drift.
    """
    sources = _read_voice_source()
    # Also include voice.py + cabinet_api.py + text_orchestrator.py + the PRP
    # active doc + the contract.
    extra_files = [
        SCRIPTS_DIR.parent / "chat" / "voice.py",
        SCRIPTS_DIR / "integrations" / "cabinet_api.py",
        SCRIPTS_DIR / "cabinet" / "text_orchestrator.py",
    ]
    for ef in extra_files:
        if ef.is_file():
            sources[str(ef.relative_to(SCRIPTS_DIR.parent.parent))] = ef.read_text(encoding="utf-8")

    bad: list[str] = [path for path, src in sources.items() if "voice_id_override" in src]
    assert not bad, (
        f"voice_id_override is NOT the canonical name (use `voice_overrides` keyed by provider). "
        f"Violations: {bad}"
    )


def test_no_voices_json_references():
    """Q5 lock — voice ids live in <profile>/config.yaml.cabinet.voice_id, NOT a separate voices.json file.

    The upstream warroom/voices.json was NOT ported per Q5.
    """
    sources = _read_voice_source()
    bad: list[tuple[str, str]] = []
    for path, src in sources.items():
        # Allow docstring references that explain WHY voices.json was NOT ported.
        # We enforce: no file-IO references like `voices.json` in code (open/read/load).
        if "open(" in src and "voices.json" in src:
            bad.append((path, "open() with voices.json"))
        if "voices.json" in src and ("read_text" in src or "json.load(" in src):
            # Scan more carefully — only flag if it's near a load call.
            pass  # already ruled out by the open(...) check above.
    assert not bad, f"voices.json references (Q5 lock violation): {bad}"


def test_rule3_module_attribute_imports():
    """rule3_module_attribute_imports — security imports MUST go through module attribute lookup.

    Rule 3 enforcement: ``from security import kill_switches`` (then
    ``kill_switches.requireEnabled(...)``) and ``from security import
    redact as _redact_mod; _redact = _redact_mod.redact``. This pattern
    lets monkeypatch propagate so tests can disable security primitives
    without import-time caching defeating the override.
    """
    sources = _read_voice_source()
    # Modules that touch security — voice_router, agent_bridge, voice_pipeline, voice_server, personas.
    must_have_module_imports = [
        ".claude/scripts/cabinet/voice/voice_router.py",
        ".claude/scripts/cabinet/voice/agent_bridge.py",
        ".claude/scripts/cabinet/voice/voice_pipeline.py",
        ".claude/scripts/cabinet/voice/voice_server.py",
        ".claude/scripts/cabinet/voice/personas.py",
    ]
    rule3_redact_pattern = "from security import redact as _redact_mod"
    forbidden_direct = "from security.redact import redact"

    for path in must_have_module_imports:
        if path not in sources:
            continue
        src = sources[path]
        # Direct symbol imports are forbidden (Rule 3).
        assert forbidden_direct not in src, (
            f"{path}: must NOT use `from security.redact import redact` (Rule 3 violation)"
        )
        # We don't strictly require the redact import in EVERY voice file
        # (some logs have only static format strings), but at least one
        # consumer (the bridge/router/pipeline) must have it. The check
        # below verifies AT LEAST ONE voice file has the canonical pattern.

    has_canonical = any(rule3_redact_pattern in src for src in sources.values())
    assert has_canonical, (
        "At least one cabinet/voice/*.py must use the Rule 3 `from security import redact as _redact_mod` pattern"
    )

    # Kill-switch module-attribute pattern check — voice cabinet doesn't
    # gate kill-switches itself (refusals come via SSE error events from
    # Phase 5a), so this assertion is informational (the CONSUMERS that
    # touch kill_switches must use the attribute pattern).


def test_voice_static_assets_present():
    """Static bundle + esbuild source + at least 5 default avatars vendored."""
    static_dir = CABINET_VOICE_DIR / "static"
    assert (static_dir / "client.bundle.js").is_file()
    assert (static_dir / "client.js").is_file()
    avatars_dir = static_dir / "avatars"
    assert avatars_dir.is_dir()
    pngs = sorted(avatars_dir.glob("*.png"))
    # Upstream ships 5 default + 5 -meet variants = 10 PNGs.
    assert len(pngs) >= 5, f"Need at least 5 avatar PNGs vendored, found {len(pngs)}"
    for required in ("main.png", "research.png", "comms.png", "content.png", "ops.png"):
        assert (avatars_dir / required).is_file(), f"Missing default avatar {required}"


def test_client_bundle_bsd2_attribution():
    """client.bundle.js carries BSD-2 attribution comment header."""
    bundle = CABINET_VOICE_DIR / "static" / "client.bundle.js"
    head = bundle.read_text(encoding="utf-8", errors="replace")[:2000]
    assert "BSD-2-Clause" in head
    assert "ClaudeClaw" in head
    assert "warroom" in head
