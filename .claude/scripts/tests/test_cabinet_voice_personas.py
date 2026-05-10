"""PRD-8 Phase 6 / WS1 — voice personas port tests.

Covers contract criteria:
  * shared_rules_constant_verbatim
  * auto_router_persona_constant_verbatim
  * get_persona_dict_safe_access
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from cabinet.voice import personas as voice_personas  # noqa: E402


def test_shared_rules_verbatim():
    """SHARED_RULES is the verbatim port of warroom/personas.py:21-42.

    Spot-check key sentences that MUST be preserved verbatim. The full
    string is part of the persona prompt, so we don't hash-compare (the
    test would lock prompt edits). Instead we verify the load-bearing
    rule sentences (no em dashes, no AI clichés, no sycophancy, etc).
    """
    s = voice_personas.SHARED_RULES
    # Key rule sentences — all verbatim from upstream.
    assert "HARD RULES (never break these):" in s
    assert "No em dashes. Ever." in s
    assert 'Never say "Certainly", "Great question", "I\'d be happy to", "As an AI", "absolutely", or any variation.' in s
    assert "No sycophancy." in s
    assert "Don't narrate what you're about to do. Just do it." in s
    assert "Keep responses conversational and concise." in s
    assert "HOW YOU OPERATE:" in s
    assert "Answer from your own knowledge first." in s
    assert "Only delegate when:" in s
    assert "delegate_to_agent" in s
    assert "answer_as_agent" not in s  # belongs to AUTO_ROUTER_PERSONA, not SHARED_RULES


def test_auto_router_verbatim():
    """AUTO_ROUTER_PERSONA is the verbatim port of warroom/personas.py:106-134."""
    s = voice_personas.AUTO_ROUTER_PERSONA
    # Key router instruction sentences from upstream.
    assert "You are the front desk of the War Room." in s
    assert "main: Hand of the King." in s
    assert "research: Grand Maester." in s
    assert "comms: Master of Whisperers." in s
    assert "content: Royal Bard." in s
    assert "ops: Master of War." in s
    assert "YOUR JOB IS TO ROUTE, NOT TO ANSWER." in s
    assert "answer_as_agent" in s
    # AUTO_ROUTER_PERSONA composes with SHARED_RULES suffix per upstream.
    assert voice_personas.SHARED_RULES in s


def test_get_persona_dict_safe_access(monkeypatch):
    """get_persona uses dict.get() — never attribute access on cfg.

    load_persona_config returns dict[str, Any] (services.py:393); attribute
    access on a missing key would raise AttributeError mid-meeting. Verify
    the lookup falls through cleanly when ``cabinet`` is missing or not a
    dict.
    """
    fake_personas_module = MagicMock()
    # Case 1: persona has no cabinet block.
    fake_personas_module.load_persona_config.return_value = {
        "persona": {"name": "Research"},
    }

    with patch.dict("sys.modules", {"personas": fake_personas_module}):
        prompt = voice_personas.get_persona("research", mode="direct")
    # Falls through to _generate_persona — must contain SHARED_RULES.
    assert voice_personas.SHARED_RULES in prompt

    # Case 2: cabinet block is not a dict (defense against malformed config).
    fake_personas_module.load_persona_config.return_value = {
        "cabinet": ["not", "a", "dict"],
    }
    with patch.dict("sys.modules", {"personas": fake_personas_module}):
        prompt = voice_personas.get_persona("research", mode="direct")
    assert voice_personas.SHARED_RULES in prompt

    # Case 3: cabinet.voice_persona_prompt set — used + composed with SHARED_RULES.
    custom_prompt = "You are the YourBusiness SEO Lead."
    fake_personas_module.load_persona_config.return_value = {
        "cabinet": {"voice_persona_prompt": custom_prompt},
    }
    with patch.dict("sys.modules", {"personas": fake_personas_module}):
        prompt = voice_personas.get_persona("seo_lead", mode="direct")
    assert custom_prompt in prompt
    assert voice_personas.SHARED_RULES in prompt


def test_q4_main_to_default_translation(monkeypatch):
    """Q4 lock: wire id "main" resolves to internal id "default" at lookup."""
    captured_ids: list[str] = []

    def fake_load(persona_id):
        captured_ids.append(persona_id)
        return {}

    fake_personas_module = MagicMock()
    fake_personas_module.load_persona_config = fake_load

    with patch.dict("sys.modules", {"personas": fake_personas_module}):
        voice_personas.get_persona("main", mode="direct")

    # The lookup MUST have used "default", not "main".
    assert captured_ids == ["default"]


def test_q4_non_main_passes_through(monkeypatch):
    """Q4 lock: non-"main" persona ids pass through untranslated."""
    captured_ids: list[str] = []

    def fake_load(persona_id):
        captured_ids.append(persona_id)
        return {}

    fake_personas_module = MagicMock()
    fake_personas_module.load_persona_config = fake_load

    with patch.dict("sys.modules", {"personas": fake_personas_module}):
        voice_personas.get_persona("research", mode="direct")

    assert captured_ids == ["research"]


def test_get_persona_auto_mode_injects_dynamic_roster(tmp_path, monkeypatch):
    """auto-mode replaces the canonical role descriptions with the roster file content."""
    # Write a fake roster file.
    fake_roster_path = tmp_path / "cabinet-roster.json"
    fake_roster_path.write_text(
        '[{"id":"sales","description":"YourBusiness Sales Lead. Direct insurance leads."}]'
    )

    # Re-route the personas module's roster path read.
    from cabinet.voice import config as voice_config
    monkeypatch.setattr(voice_config, "ROSTER_PATH", fake_roster_path)

    prompt = voice_personas.get_persona("main", mode="auto")
    # The dynamic roster line must appear.
    assert "sales" in prompt
    # Header/instructions stay verbatim.
    assert "YOUR JOB IS TO ROUTE, NOT TO ANSWER." in prompt


def testresolve_internal_persona_id_explicit():
    """resolve_internal_persona_id is exported + has the Q4 translation."""
    assert voice_personas.resolve_internal_persona_id("main") == "default"
    assert voice_personas.resolve_internal_persona_id("research") == "research"
    assert voice_personas.resolve_internal_persona_id("ops") == "ops"


def test_generate_persona_fallback_includes_shared_rules():
    """_generate_persona always composes with SHARED_RULES suffix."""
    prompt = voice_personas._generate_persona("totally_new_agent")
    assert voice_personas.SHARED_RULES in prompt
    assert "Totally_New_Agent" in prompt or "totally_new_agent" in prompt.lower()


def test_no_agent_personas_dict_imported():
    """Q5 lock — AGENT_PERSONAS hardcoded dict NOT imported into Homie."""
    assert not hasattr(voice_personas, "AGENT_PERSONAS"), (
        "AGENT_PERSONAS must NOT be ported per Q5 single-config-yaml lock"
    )
