"""Typed `/persona grant|revoke` command surface (issue #427, epic #419).

The deterministic half of the hybrid surface in
``PRDs/active/PRD-persona-self-provisioning.architecture.md`` (Q1). The
operator types a command; this module parses it server-side, establishes WHO
is asking from the authenticated transport, and hands the result to the #426
executor — which is the only path that may mutate a persona's ``toolsets:``
list. No command text ever reaches an LLM (the cabinet
``cabinet/room_commands.py`` precedent), and no model output ever reaches the
executor.

**Why the role is trusted here and not re-derived.** ``IncomingMessage.user_role``
is the canonical role-ingress seam (``models.py`` — issue #424): every
remotely-reachable adapter now stamps it, at ingress, from ITS OWN
authenticated identity data — Telegram/Discord/Slack/WhatsApp compare the
sender against their own operator allowlist, Buzz resolves a signature-
verified pubkey through a per-pubkey role map, and the CLI stamps ``admin``
unconditionally because reaching it needs a shell on the box, which already
carries write access to the very ``config.yaml`` a grant edits. The dataclass
default is fail-closed ``"viewer"``, and a repo-wide static guard
(``tests/test_ingress_role_seam.py``) fails the build if any producer of an
``IncomingMessage`` forgets to state an explicit role. This module used to
re-derive the role from each platform's allowlist itself (three sibling
tickets — #427/#428/#429 — each had their own copy); doing that a SECOND
time here could only ever let this module quietly disagree with the gate
that actually admitted the message, so :func:`resolve_operator_identity` now
trusts the stamp directly and keeps only the checks that are genuinely this
module's own to make:

1. ``source == "interactive"`` — the stamp says WHO, not WHEN. A cron/tool/
   hook turn, or grant text recovered from a recalled document, must not
   authorize a mutation even when it happens to carry an admin role: the CLI
   stamps ``admin`` for every invocation regardless of ``source``, including
   a scripted query a scheduled job might run;
2. the platform is one this module recognizes as a role authority at all
   (see :data:`_TRUSTED_PLATFORMS`) — a positive allowlist, not a denylist,
   so an unrecognized or future platform is refused rather than silently
   trusted, matching the seam's own "unknown = viewer" default;
3. for WhatsApp and Slack specifically, that platform's own operator
   allowlist is actually configured (see :data:`_ALLOWLIST_ENV_VARS`) — a
   belt-and-suspenders check that can only ever REFUSE under the current
   wiring (the stamp cannot legitimately be ``admin`` here unless that
   allowlist is already set), kept so the refusal names the env var instead
   of the generic dispatch-level "Permission denied", and so this surface
   still fails closed if a future wiring change ever let a message reach it
   without crossing the admin-role dispatch gate first.

Everything below the async entrypoint is synchronous and pure-ish so it can be
tested without an event loop; :func:`run_persona_command` is the only async
seam and it does all of its blocking work (bindings file, active-profile read,
YAML read-modify-write, cross-process lock, ledger append) inside one
``asyncio.to_thread`` call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

USAGE = (
    "*Persona toolsets*\n"
    "`/persona grant <persona> <toolset>` — add a toolset bundle\n"
    "`/persona revoke <persona> <toolset>` — take it back\n"
    "In a persona channel the persona is optional: `/persona grant research_read`.\n"
    "Grants are audited and live on that homie's next turn."
)

# Platforms this module trusts as a role authority, per the #449 canonical
# role-ingress seam (models.resolve_ingress_role + the per-adapter stampers
# it lists in the module docstring). Deliberately a POSITIVE allowlist, not a
# denylist: a platform this module has never heard of — including a future
# one nobody updated this set for — is refused rather than silently trusted,
# matching the seam's own "unknown = viewer" default.
#
# "web" is left OUT on purpose. The retired ``ws_client.py`` relay uses the
# SAME ``Platform.WEB`` value as the live ``adapters/web.py`` (which always
# stamps "viewer") but resolves its own role from CLIENT-SUPPLIED JSON with
# no allowlist behind it (``user_data.get("role", "viewer")``) — a value this
# module cannot distinguish from an honest stamp, so a platform value of
# "web" carries no server-verified identity strong enough for a config
# mutation. "webhook" needs no special handling: its adapter hardcodes
# ``user_role="viewer"`` in the adapter file itself (not client-influenced),
# so it can never legitimately declare admin and the trust-list omission
# would be redundant, not load-bearing.
_TRUSTED_PLATFORMS = frozenset({"telegram", "discord", "slack", "whatsapp", "buzz", "cli"})

# Platform -> the env var naming its operator allowlist, for the
# belt-and-suspenders check described in the module docstring. Scoped to
# WhatsApp and Slack only (the #424 design note): Telegram and Discord have
# carried an allowlist since the bot's inception, while Slack and WhatsApp
# only gained a real fail-closed one in the #449 role-ingress seam.
_ALLOWLIST_ENV_VARS: dict[str, str] = {
    "whatsapp": "WHATSAPP_ALLOWED_NUMBERS",
    "slack": "CHAT_ALLOWED_USERS",
}

# The role string handed to the executor when this module could NOT
# authenticate the sender. It is not a real role in the ladder, which is the
# point: it lands in the ledger's ``actor_role`` column and in the executor's
# own refusal text ("got 'unauthenticated'"), so an unauthorized attempt reads
# as one rather than as a mysterious viewer.
_UNAUTHENTICATED_ROLE = "unauthenticated"

# A toolset name is an identifier, not prose. Shape-checked at this seam
# because a REVOKE deliberately skips the registry check downstream (removing
# reach must keep working for an unregistered name), so without this a chat
# turn could put arbitrary text into a ledger field.
_TOOLSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# The operator-facing env var behind the executor's kill switch
# (``services._TOOLSET_GRANT_KILL_SWITCH = "persona_mutation"``, resolved by
# ``kill_switches.is_disabled`` as ``HOMIE_KILLSWITCH_<NAME>``). Named in the
# refusal so an operator who hit their own emergency stop is told what to flip.
_TOOLSET_GRANT_KILL_SWITCH_ENV = "HOMIE_KILLSWITCH_PERSONA_MUTATION"


def _kill_switch_disabled_types() -> tuple[type[BaseException], ...]:
    """The kill-switch exception type, resolved at call time (Rule 3).

    Returned as a tuple so the ``except`` clause below can be empty-safe: when
    ``security.kill_switches`` will not import, this yields ``()``, which
    matches NOTHING, and the generic handler catches whatever was raised
    instead. Resolved through the module rather than a top-level
    ``from security.kill_switches import KillSwitchDisabled`` so a test that
    monkeypatches the module propagates here (the executor's own import is
    late-bound for the same reason).
    """
    try:
        from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr
    except Exception:  # noqa: BLE001 — no switch module, no special branch
        return ()
    exc_type = getattr(kill_switches, "KillSwitchDisabled", None)
    return (exc_type,) if isinstance(exc_type, type) else ()


@dataclass(frozen=True)
class ParsedCommand:
    """One parsed `/persona` invocation, or the text explaining why not.

    ``error`` non-empty means nothing should run: it is the operator-facing
    reply, already formatted. A parse never mutates and never raises.
    """

    operation: str = ""
    persona_id: str = ""
    toolset: str = ""
    error: str = ""


@dataclass(frozen=True)
class OperatorIdentity:
    """Who the executor is told is asking, resolved from the transport.

    ``reason`` is empty exactly when ``role`` is admin; otherwise it names the
    check that failed, in operator-readable terms, so a refusal explains
    itself instead of just saying no.
    """

    actor: str
    role: str
    surface: str
    channel_id: str
    trigger_text: str
    reason: str = ""


def _platform_value(incoming: Any) -> str:
    """The platform as a plain lowercase string, however it is carried."""
    platform = getattr(incoming, "platform", None)
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


def parse_persona_command(args: str) -> ParsedCommand:
    """Parse `grant|revoke [<persona>] <toolset>`. Never raises.

    Two positional forms, exactly as the ticket specifies: with a persona
    (works from any channel, homie-directed) and without one (a persona
    channel supplies it). Anything else comes back as usage text rather than
    a guess — an ambiguous grant is the one thing this surface must never
    resolve on its own.
    """
    try:
        parts = shlex.split(args or "")
    except ValueError as exc:
        # Unbalanced quotes. Report it; do not silently fall back to a naive
        # split, which would change what the operator's tokens mean.
        return ParsedCommand(error=f"Argument error: {exc}\n\n{USAGE}")

    tokens = [part.strip() for part in parts if part.strip()]
    if not tokens:
        return ParsedCommand(error=USAGE)

    action = tokens[0].casefold()
    rest = tokens[1:]
    if action in {"help", "?"}:
        return ParsedCommand(error=USAGE)
    if action not in {"grant", "revoke"}:
        # The rejected token is NOT echoed. Every reply here is persisted as an
        # assistant transcript row and replayed into the next engine turn's
        # recent-conversation region, so echoing operator text would defeat
        # this module's "command text never reaches an LLM" contract through
        # the reply instead of through the user row.
        return ParsedCommand(
            error=f"That is not a `/persona` subcommand.\n\n{USAGE}"
        )
    if not rest:
        return ParsedCommand(
            error=f"Usage: `/persona {action} <persona> <toolset>` "
            f"(the persona is optional inside a persona channel)."
        )
    if len(rest) > 2:
        return ParsedCommand(
            error=f"Too many arguments for `/persona {action}`. "
            f"One toolset at a time: `/persona {action} <persona> <toolset>`."
        )

    persona_id = rest[0] if len(rest) == 2 else ""
    toolset = rest[-1]
    if not _TOOLSET_NAME_RE.match(toolset):
        # Not echoed, same reason as the subcommand rejection above: a token
        # that failed this shape check is arbitrary operator text.
        return ParsedCommand(
            error="That is not a toolset name. Toolsets are identifiers like "
            "`research_read` or `repo_read`."
        )
    return ParsedCommand(operation=action, persona_id=persona_id, toolset=toolset)


def transcript_receipt(args: str) -> str:
    """The transcript row for one `/persona` turn — fully server-generated.

    This module's contract (module docstring) is that command text never
    reaches an LLM. The router's generic persist path stores ``incoming.text``
    verbatim as an ordinary user row, and ``engine.py`` replays stored user
    rows into ``# Recent Conversation Context`` on the NEXT engine turn — so
    without an override the raw command (and any free-form the operator typed
    into it) is read by the model one turn later, exactly what the contract
    forbids. Same defect and same remedy as the ``@persona learn`` surface
    (``router.py`` R2 MAJOR 2).

    Only three values survive into the receipt, and every one is
    shape-validated rather than trusted: the operation (a literal ``grant`` or
    ``revoke``), the persona (must clear ``validate_persona_name``), and the
    toolset (already matched against :data:`_TOOLSET_NAME_RE` by the parser).
    An unparseable or hostile invocation collapses to a fixed string carrying
    NO operator text at all.

    The LEDGER still records the verbatim trigger text — that is the audit
    trail and it is not an LLM input. This only governs the TRANSCRIPT.
    """
    parsed = parse_persona_command(args)
    if parsed.error or not parsed.operation:
        # Includes every parse rejection, so the rejected raw text (which is
        # the most likely place for free-form) never reaches the transcript.
        return "[server command] /persona (rejected at parse)"

    persona = parsed.persona_id
    if persona:
        try:
            from personas import validate_persona_name  # noqa: PLC0415 — lazy

            validate_persona_name(persona)
        except Exception:  # noqa: BLE001 — a bad name must not leak verbatim
            # The parser accepts any token in the persona slot (the executor is
            # what rejects it), so this is a real path: without it,
            # `/persona grant "<free form>" research_read` would write that
            # free form straight into the transcript.
            persona = "<invalid>"
    else:
        persona = "<channel>"

    return (
        f"[server command] /persona {parsed.operation} "
        f"persona={persona} toolset={parsed.toolset}"
    )


def _allowlist_configured(platform: str) -> bool:
    """True when *platform*'s adapter-level operator allowlist is non-empty.

    Filters blank entries rather than checking truthiness alone:
    ``CHAT_ALLOWED_USERS`` is built with ``.split(",")``, so an unset env var
    yields ``[""]`` — a non-empty list holding one empty string, which a bare
    ``bool(...)`` would misread as "configured".
    """
    env_var = _ALLOWLIST_ENV_VARS.get(platform, "")
    if not env_var:
        return True
    try:
        import config  # noqa: PLC0415 — Rule 3 module attr, resolved per call
    except Exception as exc:  # noqa: BLE001 — cannot verify, so treat as unset
        _logger.warning(
            "persona_grant_commands: config unavailable, cannot verify the "
            "%s allowlist (%s: %s)",
            platform,
            type(exc).__name__,
            exc,
        )
        return False
    allowed = getattr(config, env_var, None) or ()
    return any(str(entry).strip() for entry in allowed)


def resolve_operator_identity(incoming: Any) -> OperatorIdentity:
    """Build the actor/role the executor's ledger records. Trusts the stamp.

    See the module docstring for what this module still checks and why. The
    returned identity is always complete enough for the executor's
    operator-turn contract (actor / trigger text / surface / channel id),
    because a refusal that cannot be audited is worse than one that can.
    """
    from personas import toolset_grants as grants  # noqa: PLC0415 — lazy, chat slice

    platform = _platform_value(incoming)
    user = getattr(incoming, "user", None)
    user_id = str(getattr(user, "platform_id", "") or "").strip()
    channel = getattr(incoming, "channel", None)
    channel_id = str(getattr(channel, "platform_id", "") or "").strip()
    trigger_text = str(getattr(incoming, "text", "") or "").strip()
    declared = str(getattr(incoming, "user_role", "") or "").strip().lower()
    source = str(getattr(incoming, "source", "") or "")

    actor = f"{platform}:{user_id}" if platform and user_id else user_id

    if source != "interactive":
        # Raw equality, not a normalizer: a fail-open normalizer would map an
        # unknown source to the permissive value, and this is the check that
        # keeps recalled/ingested text and scheduled jobs from granting — the
        # CLI stamps admin unconditionally, so this is the only thing that
        # stops a scripted, non-interactive CLI query from self-authorizing.
        reason = f"this turn's source is {source or 'unset'!r}, not a live operator turn"
    elif platform not in _TRUSTED_PLATFORMS:
        # Not on the trust list at all — see _TRUSTED_PLATFORMS above for why
        # "web" is deliberately excluded.
        reason = f"{platform or 'unknown-platform'} is not a recognized operator surface"
    elif declared != grants.ADMIN_ROLE:
        # The adapter stamped something other than admin — viewer, operator,
        # or nothing at all. This module never promotes; it only trusts an
        # explicit admin stamp.
        reason = f"this surface stamped you {declared or 'viewer'!r}, not admin"
    elif not _allowlist_configured(platform):
        # Belt-and-suspenders (module docstring, check 3): under the current
        # wiring `declared` cannot legitimately be admin here unless this is
        # already true, so this branch can only ever refuse.
        reason = (
            f"{platform} has no operator allowlist configured "
            f"(set {_ALLOWLIST_ENV_VARS[platform]})"
        )
    else:
        reason = ""

    return OperatorIdentity(
        actor=actor,
        role=grants.ADMIN_ROLE if not reason else _UNAUTHENTICATED_ROLE,
        surface=platform,
        channel_id=channel_id,
        trigger_text=trigger_text,
        reason=reason,
    )


def resolve_channel_persona(incoming: Any) -> str:
    """The persona this channel speaks for, or ``""`` when there is none.

    Two sources, in precedence order:

    1. the Discord channel -> persona binding (a persona channel);
    2. the profile THIS PROCESS runs as, when it is a named persona — a
       persona bot is its own process with ``HOMIE_HOME`` forced to its own
       profile root, so its DMs and channels are that persona's.

    The ambient profile is the right answer HERE and only here: the question
    being asked is "whose channel is this", not "which file do I write". Every
    downstream path keys on the TARGET persona instead, and an explicit
    persona argument always wins over this resolution.

    Fail-open: a missing or corrupt bindings file yields no default, which
    surfaces as "name the homie", never as a wrong persona.
    """
    try:
        from discord_channel_bindings import (  # noqa: PLC0415 — lazy, does file IO
            resolve_discord_channel_binding,
        )

        binding = resolve_discord_channel_binding(incoming)
    except Exception as exc:  # noqa: BLE001 — a bad bindings file is not a grant error
        _logger.warning(
            "persona_grant_commands: channel binding unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
        binding = None
    if binding is not None:
        persona_id = str(getattr(binding, "persona_id", "") or "").strip()
        if persona_id:
            return persona_id

    try:
        import personas  # noqa: PLC0415 — lazy: reads the active-profile file
        from personas.core import (  # noqa: PLC0415 — same lazy import
            reject_sentinel_persona_name,
        )

        active = str(personas.get_active_profile_name() or "").strip()
        if not active:
            return ""
        # ``get_active_profile_name`` returns "default" | "<name>" | "custom",
        # and BOTH bare values are resolver sentinels rather than persona ids:
        # each roots ``get_persona_paths`` somewhere other than
        # <root>/profiles/<name>/, so defaulting a grant to one would write
        # config.yaml and the ledger into the ambient process root under an id
        # no persona owns. Asked as "is this a sentinel?" through the canonical
        # guard instead of compared against a literal set, so a third sentinel
        # added later is excluded here without anyone updating this line.
        reject_sentinel_persona_name(active)
    except ValueError:
        # A sentinel, so this channel has no persona to default to. The
        # operator gets "name the homie" — never a wrong (or ambient) persona.
        return ""
    except Exception as exc:  # noqa: BLE001 — same reason
        _logger.warning(
            "persona_grant_commands: active profile unreadable (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return ""
    return active


def _success_reply(result: Any) -> str:
    """Speak one executor outcome back. Every outcome is a real answer.

    ``already_granted`` / ``not_granted`` are not errors — nothing was wrong
    with the request, the persona was simply already in that state — so they
    read as statements of fact rather than failures.
    """
    from personas import toolset_grants as grants  # noqa: PLC0415 — lazy, chat slice

    persona_id = result.persona_id
    toolset = result.toolset
    if result.outcome == grants.OUTCOME_GRANTED:
        return f"`{toolset}` added to {persona_id} — live next turn."
    if result.outcome == grants.OUTCOME_REVOKED:
        return f"`{toolset}` removed from {persona_id} — live next turn."
    if result.outcome == grants.OUTCOME_ALREADY_GRANTED:
        return f"{persona_id} already has `{toolset}` — nothing changed."
    if result.outcome == grants.OUTCOME_NOT_GRANTED:
        held = ", ".join(f"`{name}`" for name in result.suggestions)
        holding = f" It holds: {held}." if held else " It holds no toolsets."
        return f"{persona_id} does not have `{toolset}`.{holding}"
    # Unreachable through the current executor; report rather than invent.
    return f"{persona_id}: `{toolset}` -> {result.outcome}."


def execute_persona_command(incoming: Any, args: str) -> str:
    """Parse, authenticate, and run one `/persona` command. Blocking.

    Returns the operator-facing reply for every path — this is a chat
    surface, so an exception escaping here would surface as the router's
    generic "Error executing /persona", losing the executor's honest text.

    A refusal is passed through VERBATIM (the ticket's contract for the
    unknown-toolset nearest-match text); only the authorization refusal gains
    a trailing clause naming which check failed, because "requires the admin
    role" alone does not tell an operator that, say, their id is missing from
    the allowlist.
    """
    parsed = parse_persona_command(args)
    if parsed.error:
        return parsed.error

    from personas import services as persona_services  # noqa: PLC0415 — lazy
    from personas import toolset_grants as grants  # noqa: PLC0415 — lazy
    from personas import validate_persona_name  # noqa: PLC0415 — lazy

    identity = resolve_operator_identity(incoming)
    persona_id = parsed.persona_id or resolve_channel_persona(incoming)
    if not persona_id:
        return (
            "Name the homie: `/persona "
            f"{parsed.operation} <persona> {parsed.toolset}`. "
            "The short form only works inside that persona's channel."
        )

    # Shape-check the persona name BEFORE the executor sees it. The executor
    # audits its refusals, and its ledger path is built from the persona name
    # (``toolset_grants.resolve_ledger_path`` -> ``get_persona_paths``), which
    # does not validate — so an unvalidated name from a chat turn would create
    # a directory outside the profile tree while being refused. Validating at
    # this seam means a hostile name costs nothing at all.
    try:
        validate_persona_name(persona_id)
    except ValueError as exc:
        # The rejected name goes to the LOG (an operator diagnostic, never an
        # LLM input) but NOT to the reply: the reply is persisted as an
        # assistant transcript row and replayed into the next engine turn, so
        # echoing the raw name would smuggle operator free-form into a prompt
        # through the reply — the same contract breach the sanitized user-row
        # receipt exists to prevent. The operator can still see what they typed
        # in their own message.
        _logger.warning(
            "persona_grant_commands: rejected persona name from %s: %s",
            identity.surface or "unknown",
            exc,
        )
        return (
            "refused: that is not a valid persona name. Persona ids are "
            "lowercase identifiers like `sales` or `ai-engineer`."
        )

    runner = (
        persona_services.add_persona_toolset
        if parsed.operation == grants.OPERATION_GRANT
        else persona_services.remove_persona_toolset
    )
    try:
        result = runner(
            persona_id,
            parsed.toolset,
            actor=identity.actor,
            actor_role=identity.role,
            trigger_text=identity.trigger_text,
            surface=identity.surface,
            channel_id=identity.channel_id,
        )
    except (
        grants.ToolsetGrantRefusedError,
        grants.ToolsetGrantAuditError,
        persona_services.ConfigShapeError,
    ) as exc:
        # One canonical REASON_* -> text mapping (#435), shared with any
        # future caller of the same executor — this surface no longer
        # invents its own phrasing per exception type.
        return persona_services.describe_grant_failure(
            exc, persona_id=persona_id, identity_reason=identity.reason
        )
    except _kill_switch_disabled_types() as exc:  # type: ignore[misc]
        # An operator emergency stop is NOT a crash, and it must not read like
        # one. It previously fell into the generic handler below and surfaced
        # as "Persona grant failed: KillSwitchDisabled: ..." — indistinguishable
        # from an unexpected fault and silent about which switch to flip.
        #
        # Caught rather than re-raised, deliberately. ``requireEnabled``
        # increments the refusal counter AND writes the audit row BEFORE it
        # raises (``security/kill_switches.py`` — "callers MUST handle the
        # exception"), so nothing observable is lost here; and re-raising would
        # hand the exception to ``ExtensionManager.dispatch``, whose blanket
        # ``except Exception`` renders it as the generic "Error executing
        # /persona: ..." — strictly less honest than naming the switch. Same
        # shape as the sibling ``/curriculum`` handler
        # (``core_handlers.py`` — explicit branch, named switch, no re-raise).
        _logger.warning(
            "persona_grant_commands: %s refused by kill switch: %s",
            parsed.operation,
            exc,
        )
        return (
            f"Persona toolset changes are disabled by the operator kill switch "
            f"({_TOOLSET_GRANT_KILL_SWITCH_ENV}). Nothing was written."
        )
    except Exception as exc:  # noqa: BLE001 — chat surface: report, never crash
        # An OSError from a failed write lands here; it is already recorded, so
        # the operator gets the reason rather than a router-level
        # "Error executing /persona".
        _logger.error(
            "persona_grant_commands: %s %s for %s failed: %s: %s",
            parsed.operation,
            parsed.toolset,
            persona_id,
            type(exc).__name__,
            exc,
        )
        return f"Persona {parsed.operation} failed: {type(exc).__name__}: {exc}"

    return _success_reply(result)


async def run_persona_command(incoming: Any, args: str) -> str:
    """Async seam. All blocking work happens inside the worker thread.

    ``incoming`` and ``args`` are passed as values — evaluating them costs
    nothing on the loop — while every read that touches disk (channel
    bindings, active profile, the persona's config.yaml, the cross-process
    lock, the ledger append) is resolved INSIDE
    :func:`execute_persona_command`. The executor's own docstring requires
    this: it does synchronous file IO under a bounded cross-process lock, so
    a contended writer would otherwise wedge the bot's event loop.
    """
    return await asyncio.to_thread(execute_persona_command, incoming, args)


__all__ = [
    "USAGE",
    "OperatorIdentity",
    "ParsedCommand",
    "execute_persona_command",
    "parse_persona_command",
    "resolve_channel_persona",
    "resolve_operator_identity",
    "run_persona_command",
    "transcript_receipt",
]
