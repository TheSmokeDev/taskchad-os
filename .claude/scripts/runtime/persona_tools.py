"""Assemble a persona's tool payload — the seam where scoping becomes real.

This is the join that makes epic #236 visible: a persona's DECLARED scope
(`toolsets:` / `tools:` in its config) becomes an OpenAI-format tool array plus
the single dispatcher that executes it.

    persona config  ->  PersonaToolScope  ->  tool_defs + tool_dispatch
                                              |
                                              +-> kill switch
                                              +-> scope re-check
                                              +-> registry handler
                                              +-> audit row

Both persona turn surfaces call this — the chat engine and the cabinet
orchestrator. They had OPPOSITE bugs (chat handed every persona the same
`DEFAULT_AGENT_TOOLSET`; cabinet handed them nothing) and one shared root cause:
neither resolved a per-persona scope. Fixing only one leaves the other wrong in
its own direction, so the builder lives here, in the slice that owns the
registry, and both surfaces consume it.

Why the dispatcher is built HERE and not by the caller: it is the chokepoint.
Kill switch, scope enforcement, and audit all live inside it, so a caller cannot
assemble tools without also getting the guardrails. A caller that could build
`tool_defs` and supply its own dispatcher would be one refactor away from an
unaudited execution path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from collections.abc import Callable, Collection
from typing import Any

_logger = logging.getLogger(__name__)

# Operator kill switch. Ships ON — a kill switch turns a feature OFF, it does
# not birth it dark. `HOMIE_KILLSWITCH_PERSONA_TOOLS=disabled` makes every
# persona turn assemble with NO tools, which is the pre-epic behavior rather
# than an error: the persona still answers, just without acting.
KILL_SWITCH_NAME = "persona_tools"

# Audit rows are written through the existing security audit surface when it is
# available. Tool execution must not depend on the audit store being present —
# but an audit FAILURE must be visible, never swallowed into nothing.
_AUDIT_ACTION = "persona_tool_call"

# Every named persona gets this read/request-only bootstrap on authenticated
# chat turns, regardless of which domain toolsets an older profile declared.
# These tools discover instructions and ask for authority; none performs the
# requested domain action itself.
PERSONA_CHAT_BASE_TOOLS: tuple[str, ...] = (
    "skills_list",
    "skill_view",
    "request_tool",
)


def ensure_tools_registered(
    required_tools: Collection[str] | None = None,
) -> None:
    """Converge the registry for the tools required by this assembly.

    Registration hangs off ASSEMBLY rather than a boot path on purpose: the
    chat process and the orchestration API process are separate interpreters
    with different entrypoints, and a boot-time hook would have to be added to
    each (and every future one). Hooking the single place that actually needs a
    populated registry means a new entrypoint cannot forget.

    Rule 2 — the previous version set a `_TOOLS_REGISTERED = True` flag in a
    `finally`, so a FAILED registration marked itself done. One transient
    import error and that process served every persona zero tools forever,
    while the flag insisted the work had happened (adversarial review, Codex).
    The flag was meta claiming to be truth.

    The guard reads the registry itself and, when a caller supplies the
    structural toolset names it needs, refuses to treat a merely non-empty
    registry as complete. Registration is idempotent, so a transient partial
    import converges on a later turn without requiring a process restart.

    Failure stays swallowed: a registration problem must degrade to "fewer
    tools", never to a persona that cannot take its turn.
    """
    from runtime import tool_registry

    registered = {entry.name for entry in tool_registry.list_registered()}
    required = {
        str(name).strip()
        for name in (required_tools or ())
        if str(name).strip()
    }
    if required:
        if required.issubset(registered):
            return
    elif registered:
        return

    try:
        from runtime import tool_impl

        count = tool_impl.register_tools()
        _logger.info("registered %d framework tools", count)
    except Exception:
        _logger.warning(
            "framework tool registration failed while resolving required tools %s",
            sorted(required),
            exc_info=True,
        )


def build_persona_tool_payload(
    persona_id: str,
    config: dict[str, Any] | None,
    *,
    request_context: dict[str, Any] | None = None,
    elevation_grant: Any | None = None,
    allowed_tool_names: Collection[str] | None = None,
) -> tuple[list[dict[str, Any]], Callable[..., Any]] | None:
    """Build ``(tool_defs, tool_dispatch)`` for one persona, or None.

    Returns None — meaning "assemble this turn with no tools" — when the
    persona declares no scope outside an authenticated chat turn, when the
    scope resolves to nothing, or when the kill switch is off. Named personas
    on a chat surface receive only the safe ``request_tool`` bridge even when
    an older profile omitted ``safe_core``; this lets them ask without giving
    them the requested authority.

    ``allowed_tool_names`` is a subtractive surface policy.  It can only
    remove tools already granted by the persona config; it can never mint a
    capability. Scheduled workers use it to reuse the audited dispatcher
    without inheriting interactive shell, browser, credential, or live-order
    authority.

    Fails CLOSED at every seam. A registry import failure, a config shape
    surprise, or a kill-switch exception all produce None rather than an
    unscoped grant, because the failure mode of "no tools" is a persona that
    says what it could not do, while the failure mode of "wrong tools" is a
    persona acting outside its scope.
    """
    try:
        from security import kill_switches

        if kill_switches.is_disabled(KILL_SWITCH_NAME):
            _logger.info(
                "persona tools disabled by kill switch; %s assembles with no tools",
                persona_id,
            )
            return None
    except Exception:
        # Kill-switch module unavailable — proceed. The switch is an operator
        # OFF control, not the thing that grants capability, so its absence
        # must not silently disable a working feature.
        pass

    try:
        from personas.services import resolve_persona_tool_scope

        scope = resolve_persona_tool_scope(config or {})
    except Exception:
        _logger.warning(
            "could not resolve tool scope for persona %s; assembling with no tools",
            persona_id,
            exc_info=True,
        )
        return None

    has_request_surface = bool(
        request_context
        and request_context.get("turn_id")
        and request_context.get("channel_id")
    )
    if scope.is_empty and not has_request_surface:
        return None

    try:
        from runtime import tool_registry

        required_names = set(
            tool_registry.resolve_tool_names(
                enabled_toolsets=list(scope.toolsets) or None,
            )
        )
        required_names.update(scope.tools)
        if has_request_surface:
            required_names.update(PERSONA_CHAT_BASE_TOOLS)
        ensure_tools_registered(required_names)
        tool_defs = tool_registry.get_tool_definitions(
            enabled_toolsets=list(scope.toolsets) or None,
        )
        # Individual grants are additive and still registry-gated: an unknown
        # name contributes nothing rather than becoming a hole in the model.
        # This is an explicit per-persona authority grant and may intentionally
        # cross toolset ownership (for example, one reviewed read tool without
        # its whole pack). Blueprints never emit these implicitly; operator-exec
        # remains false unless the reviewed config names such a tool directly.
        granted_names = {(d.get("function") or {}).get("name") for d in tool_defs}
        for name in scope.tools:
            if name in granted_names:
                continue
            entry = tool_registry.get_entry(name)
            if entry is None:
                _logger.warning(
                    "persona %s grants unregistered tool %r — skipped", persona_id, name
                )
                continue
            tool_defs.append(copy.deepcopy(entry.schema))
            granted_names.add(name)

        # Universal named-persona bootstrap for authenticated chat turns.
        # Skill discovery is read-only; request_tool only creates a pending
        # request. None of these grants the domain verb the persona is seeking.
        if has_request_surface:
            for base_name in PERSONA_CHAT_BASE_TOOLS:
                if base_name in granted_names:
                    continue
                base_entry = tool_registry.get_entry(base_name)
                valid = base_entry is not None and base_entry.handler is not None
                if base_name == "request_tool":
                    valid = bool(
                        valid
                        and base_entry.dispatch_context_scoped
                        and not base_entry.elevatable
                        and not base_entry.dedicated_gate
                    )
                if not valid:
                    _logger.warning(
                        "persona %s cannot assemble base capability %s",
                        persona_id,
                        base_name,
                    )
                    return None
                tool_defs.append(copy.deepcopy(base_entry.schema))
                granted_names.add(base_name)

        # A one-time grant is additive to this request only.  It never mutates
        # the persona config and it is revalidated against registry metadata at
        # assembly, after the process-local grant has already been consumed.
        if elevation_grant is not None:
            elevated_name = str(getattr(elevation_grant, "tool_name", "") or "")
            entry = tool_registry.get_entry(elevated_name)
            if (
                entry is None
                or entry.handler is None
                or not entry.elevatable
                or entry.dedicated_gate
            ):
                _logger.warning(
                    "refused invalid one-time grant for %s/%s",
                    persona_id,
                    elevated_name or "unknown",
                )
                return None
            if elevated_name not in granted_names:
                tool_defs.append(copy.deepcopy(entry.schema))
                granted_names.add(elevated_name)

        if allowed_tool_names is not None:
            allowed = {
                str(name).strip()
                for name in allowed_tool_names
                if str(name).strip()
            }
            tool_defs = [
                definition
                for definition in tool_defs
                if (definition.get("function") or {}).get("name") in allowed
            ]
            granted_names.intersection_update(allowed)
    except Exception:
        _logger.warning(
            "tool assembly failed for persona %s; assembling with no tools",
            persona_id,
            exc_info=True,
        )
        return None

    if not tool_defs:
        return None

    bounded_context = dict(request_context or {})
    bounded_context["persona_id"] = persona_id
    # The request bridge needs to know what is ALREADY granted so it cannot
    # manufacture approval theater for a tool the persona could simply call.
    bounded_context["granted_tools"] = sorted(n for n in granted_names if n)
    dispatch = _make_dispatch(
        persona_id,
        frozenset(n for n in granted_names if n),
        request_context=bounded_context,
        elevation_grant=elevation_grant,
    )
    return tool_defs, dispatch


def persona_tool_scope_version(
    persona_id: str, definitions: list[dict[str, Any]] | None
) -> str | None:
    """Hash the exact persona-scoped definition snapshot carried by a turn."""

    if not definitions:
        return None
    canonical = json.dumps(
        {"persona_id": persona_id, "tool_defs": definitions},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _make_dispatch(
    persona_id: str,
    granted: frozenset[str],
    *,
    request_context: dict[str, Any] | None = None,
    elevation_grant: Any | None = None,
) -> Callable[..., Any]:
    """Build the one dispatcher for this persona's turn.

    ``granted`` is captured at ASSEMBLY time and re-checked at CALL time. That
    is not redundant with the adapters' own scope guards — those check what the
    model was OFFERED this turn, while this checks what the PERSONA was granted.
    A bridge call, a replayed name from history, or a future disclosure path
    (#245) could present a name the model was never offered directly, and the
    persona's grant is the boundary that must hold regardless of how the call
    arrived.
    """

    elevated_name = str(getattr(elevation_grant, "tool_name", "") or "")
    elevation_used = False

    def dispatch(name: str, arguments: Any = None) -> str:
        nonlocal elevation_used
        started = time.monotonic()
        outcome = "completed"
        detail = ""
        try:
            # Re-checked per call: the kill switch is an operator control and
            # must take effect mid-turn, not only at assembly.
            try:
                from security import kill_switches

                if kill_switches.is_disabled(KILL_SWITCH_NAME):
                    outcome = "kill_switch"
                    return json.dumps(
                        {"error": "persona tool execution is disabled by operator kill switch"}
                    )
            except Exception:
                pass

            if name not in granted:
                outcome = "out_of_scope"
                _logger.warning(
                    "persona %s attempted out-of-scope tool %r (granted: %s)",
                    persona_id,
                    name,
                    ", ".join(sorted(granted)) or "none",
                )
                return json.dumps(
                    {"error": f"tool {name!r} is not in this persona's granted scope"}
                )

            from runtime import tool_registry

            entry = tool_registry.get_entry(name)
            if entry is None or entry.handler is None:
                # Declared in a toolset but never wired. LOUD, because the
                # config looks correct and the model will otherwise be told a
                # capability exists that has no implementation behind it.
                outcome = "no_handler"
                _logger.error(
                    "tool %r is in persona %s's scope but has no handler registered",
                    name,
                    persona_id,
                )
                return json.dumps({"error": f"tool {name!r} has no handler registered"})

            if elevation_grant is not None and name == elevated_name:
                from runtime import persona_elevation

                if elevation_used:
                    outcome = "elevation_consumed"
                    return json.dumps(
                        {"error": "the approved one-time tool call was already used"}
                    )
                if not persona_elevation.arguments_match(elevation_grant, arguments):
                    outcome = "elevation_argument_mismatch"
                    return json.dumps(
                        {
                            "error": (
                                "arguments do not match the operator-approved payload; "
                                "call the tool with the exact approved arguments"
                            )
                        }
                    )
                # Consume BEFORE the handler: a failing or partially mutating
                # exact call must never be silently retryable under one grant.
                elevation_used = True

            # Three argument shapes, and the middle one was a real bug: the
            # original `entry.handler(arguments)` fallback passed None
            # POSITIONALLY when a model called a zero-argument tool, so every
            # no-arg handler — `crypto_funding`, `crypto_desk_snapshot`,
            # `browser_status`, `skills_list` — raised
            # "takes 0 positional arguments but 1 was given". The tools were
            # registered, scoped, and reachable, and simply could not be called.
            # Persona-private handlers opt into a structural registry marker.
            # The dispatcher injects identity under a reserved internal
            # keyword, overwriting any provider-supplied spoof. A marked
            # handler that forgets to accept identity fails loudly here instead
            # of silently reading operator-global state.
            scoped_kwargs = (
                {"_persona_id": persona_id}
                if entry.persona_scoped
                else {}
            )
            if entry.dispatch_context_scoped:
                scoped_kwargs["_dispatch_context"] = dict(request_context or {})
            if isinstance(arguments, dict):
                call_kwargs = dict(arguments)
                call_kwargs.update(scoped_kwargs)
                result = entry.handler(**call_kwargs)
            elif arguments is None:
                result = entry.handler(**scoped_kwargs)
            else:
                # A non-dict, non-None argument is a provider sending a bare
                # scalar. Pass it through rather than guessing a keyword — the
                # handler's own signature decides whether it is acceptable.
                result = entry.handler(arguments, **scoped_kwargs)
            return result if isinstance(result, str) else json.dumps(result, default=str)
        except Exception as exc:
            # Surfaced to the MODEL as a result, never raised into the turn. A
            # failing tool is conversational input — same contract as both
            # lanes — so the model can explain or recover.
            outcome = "failed"
            detail = f"{type(exc).__name__}: {exc}"
            _logger.warning("persona %s tool %r raised: %s", persona_id, name, exc)
            return json.dumps({"error": detail})
        finally:
            _audit(
                persona_id=persona_id,
                tool_name=name,
                outcome=outcome,
                detail=detail,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    return dispatch


def _audit(
    *,
    persona_id: str,
    tool_name: str,
    outcome: str,
    detail: str,
    elapsed_ms: int,
) -> None:
    """Write one audit row per invocation. Never raises into the tool path.

    Carries the persona because the whole point of scoping is per-persona
    attribution — an audit trail that records "a tool ran" without saying WHOSE
    turn ran it cannot answer the only question worth asking after an incident.

    An audit-write failure is logged at ERROR rather than swallowed: losing the
    trail is itself the incident-response failure, and a silent one is worse
    than a noisy one.
    """
    payload = {"tool": tool_name, "outcome": outcome, "elapsed_ms": elapsed_ms}
    if detail:
        payload["detail"] = detail

    try:
        # Late-bind, exactly as `kill_switches.requireEnabled` does: the import
        # is deferred so tests can monkeypatch `dashboard_api._audit_write`, and
        # so the runtime slice does not hard-depend on the dashboard slice.
        from dashboard_api import _audit_write

        _audit_write(
            operator_id=f"persona:{persona_id}",
            action=_AUDIT_ACTION,
            target_persona_id=persona_id,
            outcome=outcome,
            detail=payload,
            blocked=outcome in {
                "out_of_scope",
                "kill_switch",
                "no_handler",
                "elevation_consumed",
                "elevation_argument_mismatch",
            },
        )
    except Exception as exc:  # noqa: BLE001 — audit is best-effort, never fatal
        # ERROR, not debug: losing the trail IS the incident-response failure,
        # and a silent one is worse than a noisy one. The structured log line
        # below becomes the fallback trail so the row is not simply lost.
        _logger.error("persona tool audit-write failed for %s/%s: %s", persona_id, tool_name, exc)
        _logger.info(
            "persona_tool_call %s",
            json.dumps({"persona_id": persona_id, **payload}, sort_keys=True),
        )


__all__ = [
    "PERSONA_CHAT_BASE_TOOLS",
    "KILL_SWITCH_NAME",
    "build_persona_tool_payload",
    "ensure_tools_registered",
    "persona_tool_scope_version",
]
