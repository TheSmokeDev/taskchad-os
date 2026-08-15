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


# --- #429: /skills link — linked-skill intake surface ---
#
# The role and the target persona are RESOLVED SERVER-SIDE here, not taken
# from the message. Post-#449 (the canonical role-ingress seam), the role is
# read straight off `IncomingMessage.user_role` — every remote adapter is now
# the SOLE authority that stamps it, at ingress, from its own authenticated
# identity data (Telegram/Discord/Slack/WhatsApp/webhook all call
# `models.resolve_ingress_role()` against their own configured allowlist; the
# CLI stamps its own operator constant). `handle_skills` no longer re-derives
# a second, bespoke role check — it trusts the stamp. The dataclass default is
# "viewer" (fail-closed): an ingress path that forgets to stamp a role gets
# the least privilege, never admin.


def _discord_turn(text: str, *, user_id: str, channel_id: str, user_role: str = "viewer"):
    """A real IncomingMessage — no stand-in objects.

    `user_role` defaults to "viewer" (the model default, fail-closed) rather
    than being left unset, so a caller must EXPLICITLY opt a turn into
    "admin" — mirroring what the real Discord adapter does at ingress via
    `resolve_ingress_role()` before this handler ever sees the turn.
    """
    from models import Channel, IncomingMessage, Platform, User

    return IncomingMessage(
        text=text,
        user=User(Platform.DISCORD, user_id, "Some User"),
        channel=Channel(Platform.DISCORD, channel_id, is_dm=False),
        platform=Platform.DISCORD,
        user_role=user_role,
    )


def test_incoming_message_user_role_defaults_to_viewer() -> None:
    """Fail-closed model default (R3): an ingress path that forgets to stamp
    a role must never hand out admin."""
    turn = _discord_turn("/skills link https://x", user_id="222", channel_id="c1")
    assert turn.user_role == "viewer"


def test_resolve_requesting_persona_prefers_the_channel_binding(
    tmp_path, monkeypatch
) -> None:
    """A slash command is dispatched by the MAIN process, so the ambient profile
    is `default` — the install target must come from the CHANNEL."""
    import json

    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps({"channels": {"c1": {"kind": "persona", "persona": "sales"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_CHANNEL_BINDINGS_FILE", str(bindings))

    bound = _discord_turn("/skills link https://x", user_id="111", channel_id="c1")
    unbound = _discord_turn("/skills link https://x", user_id="111", channel_id="c9")

    assert core_handlers.resolve_requesting_persona(bound) == "sales"
    # No binding -> falls back to the ambient profile (correct for CLI/Telegram
    # and for a persona bot running as its own process).
    assert core_handlers.resolve_requesting_persona(unbound) != "sales"


def test_skills_link_passes_the_resolved_role_and_persona_to_intake(
    tmp_path, monkeypatch
) -> None:
    """`handle_skills` reads `incoming.user_role` STRAIGHT off the stamp — no
    second, platform-specific config lookup. Post-#449 the adapter is the
    sole role authority: it stamps "admin" only after verifying its own
    allowlist (e.g. Discord's `resolve_ingress_role()` against
    `DISCORD_ALLOWED_USERS`), so a handler-level test only needs to prove the
    stamp flows straight through — not re-derive the adapter's own check."""
    import asyncio
    import json

    from cognition import skill_intake

    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps({"channels": {"c1": {"kind": "persona", "persona": "sales"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_CHANNEL_BINDINGS_FILE", str(bindings))

    seen: dict[str, object] = {}

    async def _fake_intake(source, **kwargs):
        seen["source"] = source
        seen.update(kwargs)
        return skill_intake.SkillIntakeResult(
            ok=True, outcome="assigned", message="Added *x* to *sales*'s kit."
        )

    monkeypatch.setattr(skill_intake, "intake_linked_skill", _fake_intake)
    handler = core_handlers.CORE_HANDLERS["skills"]

    turn = _discord_turn(
        "/skills link https://example.com/skill",
        user_id="111",
        channel_id="c1",
        user_role="admin",
    )
    out = asyncio.run(handler(None, turn, "link https://example.com/skill"))

    assert seen["source"] == "https://example.com/skill"
    assert seen["persona_id"] == "sales"
    assert seen["actor_role"] == "admin"
    assert seen["actor"] == "111"
    assert seen["surface"] == "discord"
    assert seen["channel_id"] == "c1"
    assert seen["trigger_text"] == "/skills link https://example.com/skill"
    assert "sales" in out

    # A turn the adapter never verified (left at the fail-closed "viewer"
    # default) reaches intake as viewer, so the executor refuses it — the
    # handler applies NO config lookup of its own that could upgrade it.
    stranger = _discord_turn(
        "/skills link https://example.com/skill", user_id="222", channel_id="c1"
    )
    asyncio.run(handler(None, stranger, "link https://example.com/skill"))
    assert seen["actor_role"] == "viewer"


def test_skills_link_without_a_source_returns_usage() -> None:
    import asyncio

    handler = core_handlers.CORE_HANDLERS["skills"]
    out = asyncio.run(handler(None, None, "link"))
    assert "/skills link" in out
    assert "Operator only" in out


def test_skills_usage_advertises_link() -> None:
    assert "link" in core_handlers._SKILLS_USAGE
