"""Skill-from-experience loop (WS4 / B5) — native-command 4-registration test.

The `/skills` operator command is a NEW native command. A native command needs
ALL FOUR registrations or it silently half-works (#54 native-command bug class):

  1. a COMMANDS row (router-type, operator role);
  2. membership in the `Memory` CATEGORIES group;
  3. membership in the TELEGRAM_NATIVE_COMMANDS curated menu tuple;
  4. a slashless handler in CORE_HANDLERS (router dispatch goes via the manager,
     no router.py edit — same as the cabinet precedent).

Pure-static (import-only) — no async, no HTTP. Mirrors test_commands_cabinet.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402


def _row_for(name: str) -> tuple[str, str, str, str] | None:
    for row in commands.COMMANDS:
        if row[0] == name:
            return row
    return None


# --- Surface 1: COMMANDS row ---


def test_skills_commands_row() -> None:
    """/skills is a router-type, operator-role COMMANDS row."""
    row = _row_for("skills")
    assert row is not None, "missing COMMANDS row for /skills"
    assert row[2] == "router", "/skills must be router-type (handled instantly)"
    assert row[3] == "operator", "/skills must be operator-role (default-deny gate)"


# --- Surface 2: CATEGORIES (Memory group) ---


def test_skills_in_memory_category() -> None:
    cats = {c[0]: c[1] for c in commands.CATEGORIES}
    assert "Memory" in cats, "Memory category missing from CATEGORIES"
    assert "skills" in cats["Memory"], "/skills must be in the Memory CATEGORIES group"


# --- Surface 3: TELEGRAM_NATIVE_COMMANDS ---


def test_skills_in_native_menu() -> None:
    assert "skills" in commands.TELEGRAM_NATIVE_COMMANDS, (
        "/skills must be in TELEGRAM_NATIVE_COMMANDS (native menu)"
    )


# --- Surface 4: CORE_HANDLERS routing (slashless key) ---


def test_skills_routes_via_core_handlers() -> None:
    assert "skills" in core_handlers.CORE_HANDLERS, (
        "/skills handler missing from CORE_HANDLERS (4th registration)"
    )
    assert "/" not in "skills", "CORE_HANDLERS keys are slashless"
    assert callable(core_handlers.CORE_HANDLERS["skills"]) or hasattr(
        core_handlers.CORE_HANDLERS["skills"], "__call__"
    )


def test_skills_handler_is_handle_skills() -> None:
    """The registered handler is core_handlers.handle_skills (not a typo target)."""
    assert core_handlers.CORE_HANDLERS["skills"] is core_handlers.handle_skills


# --- Menu projection: the registry surfaces /skills end-to-end ---


def test_skills_appears_in_projected_menu() -> None:
    """get_telegram_bot_commands() projects /skills with its description (proves the
    COMMANDS description and the native tuple agree — the menu actually grows)."""
    menu = dict(commands.get_telegram_bot_commands())
    assert "skills" in menu, "/skills not projected into the Telegram bot menu"
    assert menu["skills"], "/skills menu entry has an empty description"


# --- Handler dispatch smoke: review / promote / reject branches resolve ---


def test_skills_handler_subcommands_dispatch(monkeypatch) -> None:
    """handle_skills routes review/promote/reject to skill_promotion and returns
    friendly text — promote fires ONLY with operator_approved=True (default-deny)."""
    import asyncio

    from cognition import skill_promotion

    calls: dict[str, object] = {}

    def _fake_list_promotable(threshold=None):
        calls["review"] = True
        return [{"name": "daily-spend-query", "verdict": "safe", "recurrence_count": 3}]

    def _fake_promote(name, *, operator_approved, override_caution=False):
        calls["promote"] = {
            "name": name,
            "operator_approved": operator_approved,
            "override_caution": override_caution,
        }
        return {"status": "promoted", "path": f"/x/{name}/SKILL.md", "verdict": "safe"}

    def _fake_reject(name, reason):
        calls["reject"] = {"name": name, "reason": reason}
        return {"status": "rejected"}

    monkeypatch.setattr(skill_promotion, "list_promotable", _fake_list_promotable)
    monkeypatch.setattr(skill_promotion, "promote", _fake_promote)
    monkeypatch.setattr(skill_promotion, "reject_skill", _fake_reject)

    handler = core_handlers.CORE_HANDLERS["skills"]

    # review
    out = asyncio.run(handler(None, None, "review"))
    assert calls.get("review") is True
    assert "daily-spend-query" in out

    # promote — default-deny: handler injects operator_approved=True
    out = asyncio.run(handler(None, None, "promote daily-spend-query"))
    assert calls["promote"]["name"] == "daily-spend-query"
    assert calls["promote"]["operator_approved"] is True
    assert calls["promote"]["override_caution"] is False
    assert "promoted" in out.lower()

    # promote --override-caution flag is parsed
    asyncio.run(handler(None, None, "promote daily-spend-query --override-caution"))
    assert calls["promote"]["override_caution"] is True

    # reject — distinct verb, carries a reason via the `|` delimiter (F1: the
    # name is the full remainder so the reason MUST be pipe-delimited).
    out = asyncio.run(handler(None, None, "reject daily-spend-query | no longer needed"))
    assert calls["reject"]["name"] == "daily-spend-query"
    assert calls["reject"]["reason"] == "no longer needed"
    assert "reject" in out.lower()

    # empty args returns usage (no dispatch)
    out = asyncio.run(handler(None, None, ""))
    assert "review" in out.lower() and "promote" in out.lower()


# --- F1: multi-word draft names survive the command parser ---


def test_skills_promote_multiword_name(monkeypatch) -> None:
    """`/skills promote Daily Spend` must look up the FULL name "Daily Spend",
    not just the first token "Daily" (write_skill keeps the display name with
    spaces; recurrence + usage sidecar are keyed on that exact name)."""
    import asyncio

    from cognition import skill_promotion

    seen: dict[str, object] = {}

    def _fake_promote(name, *, operator_approved, override_caution=False):
        seen["name"] = name
        seen["operator_approved"] = operator_approved
        seen["override_caution"] = override_caution
        return {"status": "promoted", "path": f"/x/{name}/SKILL.md", "verdict": "safe"}

    monkeypatch.setattr(skill_promotion, "promote", _fake_promote)
    handler = core_handlers.CORE_HANDLERS["skills"]

    asyncio.run(handler(None, None, "promote Daily Spend"))
    assert seen["name"] == "Daily Spend", "multi-word name was truncated to first token"
    assert seen["operator_approved"] is True
    assert seen["override_caution"] is False


def test_skills_promote_multiword_name_with_override(monkeypatch) -> None:
    """`--override-caution` is parsed even with a spaced name, and the flag is
    stripped out of the name regardless of where it appears."""
    import asyncio

    from cognition import skill_promotion

    seen: dict[str, object] = {}

    def _fake_promote(name, *, operator_approved, override_caution=False):
        seen["name"] = name
        seen["override_caution"] = override_caution
        return {"status": "promoted", "path": f"/x/{name}/SKILL.md", "verdict": "caution"}

    monkeypatch.setattr(skill_promotion, "promote", _fake_promote)
    handler = core_handlers.CORE_HANDLERS["skills"]

    # flag trailing the spaced name
    asyncio.run(handler(None, None, "promote Daily Spend --override-caution"))
    assert seen["name"] == "Daily Spend"
    assert seen["override_caution"] is True

    # flag between name tokens is still stripped from the name
    asyncio.run(handler(None, None, "promote Daily --override-caution Spend"))
    assert seen["name"] == "Daily Spend"
    assert seen["override_caution"] is True


def test_skills_reject_multiword_name_with_reason(monkeypatch) -> None:
    """`/skills reject Daily Spend | too risky` → reject_skill("Daily Spend",
    "too risky"). The reason is delimited by a single `|`; the name keeps its
    spaces."""
    import asyncio

    from cognition import skill_promotion

    seen: dict[str, object] = {}

    def _fake_reject(name, reason):
        seen["name"] = name
        seen["reason"] = reason
        return {"status": "rejected"}

    monkeypatch.setattr(skill_promotion, "reject_skill", _fake_reject)
    handler = core_handlers.CORE_HANDLERS["skills"]

    asyncio.run(handler(None, None, "reject Daily Spend | too risky"))
    assert seen["name"] == "Daily Spend", "multi-word name was truncated"
    assert seen["reason"] == "too risky"


def test_skills_reject_multiword_name_no_reason(monkeypatch) -> None:
    """With no `|`, the whole remainder is the name and the reason defaults."""
    import asyncio

    from cognition import skill_promotion

    seen: dict[str, object] = {}

    def _fake_reject(name, reason):
        seen["name"] = name
        seen["reason"] = reason
        return {"status": "rejected"}

    monkeypatch.setattr(skill_promotion, "reject_skill", _fake_reject)
    handler = core_handlers.CORE_HANDLERS["skills"]

    asyncio.run(handler(None, None, "reject Daily Spend"))
    assert seen["name"] == "Daily Spend"
    assert seen["reason"] == "operator_rejected"


# --- Rec 1: every promote() status has a friendly refusal line ---


def test_promote_status_text_covers_all_statuses() -> None:
    """Rec 1: every status the promote() contract can return has a friendly
    refusal line, so the operator never sees a bare status token. Statuses come
    from the promote() docstring contract."""
    contract_statuses = {
        "promoted",
        "already_promoted",
        "promote_target_invalid",
        "killswitch_disabled",
        "not_eligible",
        "not_found",
        "scan_dangerous",
        "scan_caution",
        "not_approved",
        "move_failed",
    }
    missing = contract_statuses - set(core_handlers._SKILL_PROMOTE_STATUS_TEXT)
    assert not missing, f"promote statuses missing friendly text: {sorted(missing)}"
