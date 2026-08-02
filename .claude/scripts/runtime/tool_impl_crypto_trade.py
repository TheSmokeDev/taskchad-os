"""The order path — mandate, pre-trade evaluation, and bracket submission.

This is the half of the desk that can move money, and it is wired LAST and
deliberately, because the existing slice already got the hard part right and
the job here is to expose it without weakening it.

THE AUTHORIZATION CHAIN, verified by running it rather than by reading it:

    load_mandate()                 -> MandateLoad(state=ABSENT|VALID|EXPIRED|…)
    OrderGuard(mode=DRY_RUN)       -> DRY_RUN is the DEFAULT, not a setting
    ExecutionClient(guard)
    BracketPlan(...)               -> plain scalars, no object graph needed
    client.submit_bracket(plan)    -> BracketReport

With no mandate file on disk, a DRY_RUN bracket returns::

    BracketStatus.REFUSED  reasons=('entry-leg-denied', 'mandate-probe-unbound')

That is the whole design in one line. The desk is not authorized by default,
DRY_RUN is not a safety setting the model can flip, and the refusal names its
own cause. `mandate.json` is an operator artifact with an expiry — a persona
cannot write itself one through these tools, and no tool here accepts a
`mode=live` argument. Going live is an operator act on disk, not a parameter.

WHY THE SUBMIT TOOL EXISTS AT ALL when it currently always refuses: because a
refusal that names its cause is how the persona LEARNS the shape of the gate.
A desk that cannot attempt an order cannot discover it is unauthorized, cannot
report why, and cannot tell the operator what is missing. The alternative —
withholding the tool — produces "I can't do that" with no diagnosis, which is
exactly the failure this epic exists to kill.

Read tools here (`crypto_mandate_read`, `crypto_preflight`) are the ones that
should be used constantly. `crypto_submit_bracket` is the one that proves the
gate holds.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 4000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


def _crypto_mandate_read(**_: Any) -> str:
    """Is the desk authorized to trade right now, and until when?

    The first thing a trading persona should ask. Reads the physical mandate
    file (Rule 2 — the authorization is the file on disk, never a cached claim
    about it), so an expiry that has passed is visible immediately rather than
    at submission time.
    """
    try:
        from cognition import crypto_mandate
    except ImportError as exc:
        return f"error: mandate module unavailable ({exc})"
    try:
        load = crypto_mandate.load_mandate()
    except Exception as exc:  # noqa: BLE001
        return f"error: mandate read failed: {type(exc).__name__}: {exc}"

    state = getattr(load, "state", None)
    authorized = bool(getattr(load, "is_authorized", False))
    lines = [
        f"Trading authorization: {'AUTHORIZED' if authorized else 'NOT AUTHORIZED'}",
        f"- state: {getattr(state, 'value', state)}",
    ]
    reason = getattr(load, "reason", "") or ""
    if reason:
        lines.append(f"- reason: {reason}")
    # `days_remaining` and `summary` are METHODS on MandateLoad, not properties.
    # Reading them as attributes renders "<bound method ...>" into the model's
    # context — a bug that looks like data and is worse than an omission,
    # because it is not obviously wrong to a reader.
    for label, attr in (("days remaining", "days_remaining"), ("mandate", "summary")):
        value = getattr(load, attr, None)
        try:
            value = value() if callable(value) else value
        except Exception:  # noqa: BLE001
            continue
        if value not in (None, ""):
            lines.append(f"- {label}: {value}")
    if not authorized:
        lines.append(
            "\nNo order will execute in this state, including DRY_RUN. A mandate is "
            "an operator-authored file with an expiry; it cannot be created from here."
        )
    return _truncate("\n".join(lines))


def _build_guard():
    """Construct the guard with the on-disk mandate bound, if one exists.

    `OrderGuard()` defaults to `GuardMode.DRY_RUN` and that default is never
    overridden here — no argument of any tool in this module can reach `mode`.
    Binding the mandate probe is what lets a VALID mandate actually authorize;
    with no mandate the probe stays unbound and the guard refuses, which is the
    correct and desired outcome.
    """
    from cognition import crypto_order_guard

    guard_kwargs: dict[str, Any] = {}
    try:
        # `mandate_probe_from_disk` is the slice's OWN adapter and the only
        # correct way to bind authorization here. Two reasons it beats
        # hand-rolling the translation, both learned by getting it wrong first:
        #
        #   * `Mandate` (nested on-disk document) and `MandateSnapshot` (the
        #     flat record the guard evaluates) are DIFFERENT shapes. Passing
        #     the wrong one produced `mandate-stage-error` — a fail-closed
        #     refusal that is indistinguishable, from the outside, from an
        #     unauthorized desk. A mis-wire that looks exactly like the correct
        #     denial is the worst kind.
        #   * It re-reads the file on EVERY probe (Rule 2), so an expiry that
        #     passes mid-session denies immediately. A snapshot captured once
        #     at construction would keep authorizing against a mandate that had
        #     already lapsed.
        guard_kwargs["mandate_probe"] = crypto_order_guard.mandate_probe_from_disk()
    except Exception:  # noqa: BLE001
        # Fail CLOSED: leave the probe unbound, which denies at stage 1. There
        # is no path here that yields a MORE permissive guard than the file.
        _logger.warning("mandate bind failed; guard stays unauthorized", exc_info=True)

    return crypto_order_guard.OrderGuard(**guard_kwargs)


def _plan_from_scalars(
    *,
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    quantity: float,
    target: float | None,
    leverage: float,
    request_id: str,
):
    from cognition import crypto_execution

    return crypto_execution.BracketPlan(
        request_id=request_id,
        symbol=symbol.strip(),
        # Verified at runtime: the geometry check requires 'long'/'short' and
        # REFUSES 'buy'/'sell' by name. Normalising here turns the most likely
        # model phrasing into a valid plan instead of a confusing refusal.
        side={"buy": "long", "sell": "short"}.get(
            side.strip().lower(), side.strip().lower()
        ),
        entry=float(entry),
        stop=float(stop),
        quantity=float(quantity),
        notional_usd=float(entry) * float(quantity),
        target=float(target) if target else None,
        leverage=float(leverage or 1.0),
    )


def _render_report(report: Any) -> str:
    """Render a BracketReport for the model.

    THE FIELD NAMED `simulated` IS NOT THE DRY-RUN PROOF. Verified in source
    (`crypto_execution.py:1367`): the DRY_RUN success path constructs
    `BracketReport(status=BracketStatus.SIMULATED, ...)` and never passes
    `simulated=`, so the field sits at its dataclass default of False. The only
    place it is ever set True is the PROTECTION_MISSING branch, where it exists
    to satisfy an invariant that a naked-exposure report must justify itself.

    Reading the field would therefore print "simulated: False" on a SUCCESSFUL
    dry run — telling the persona nothing was simulated when everything was,
    from which it could reasonably conclude a real order went to a venue. The
    authoritative signal is the STATUS.
    """
    status = getattr(status_obj := getattr(report, "status", None), "value", status_obj)
    simulated = str(status).lower() == "simulated"
    lines = [
        f"status: {status}",
        f"reached a venue: {'NO — simulated only' if simulated else 'see status'}",
        f"legs: {len(getattr(report, 'legs', ()) or ())}",
    ]
    reasons = list(getattr(report, "reasons", ()) or ())
    if reasons:
        lines.append("reasons:")
        lines.extend(f"  - {r}" for r in reasons)
    # Both of these are incident conditions, not ordinary results. A stop that
    # failed to place after an entry filled is an UNPROTECTED position, and
    # burying it in a field dump is how it gets missed.
    if getattr(report, "naked_exposure", False):
        lines.append(
            "\n*** NAKED EXPOSURE — an entry exists without its protective stop. "
            "This needs attention NOW. ***"
        )
    if getattr(report, "reconcile_required", False):
        lines.append(
            "\n*** RECONCILE REQUIRED — venue state is uncertain. Do not resubmit "
            "before reconciling; a blind retry can double the position. ***"
        )
    return "\n".join(lines)


def _crypto_preflight(
    symbol: str = "",
    side: str = "long",
    entry: float = 0.0,
    stop: float = 0.0,
    quantity: float = 0.0,
    target: float = 0.0,
    leverage: float = 1.0,
    **_: Any,
) -> str:
    """Would this trade be ALLOWED? Evaluates the guard without submitting.

    The highest-value tool on the desk: it answers "does this pass the risk
    gates" before anything is attempted, so a persona can size down or abandon
    rather than learn the answer from a refusal.
    """
    if not symbol.strip():
        return "error: symbol is required"

    # Deliberately NOT routed through submit_bracket or OrderGuard.evaluate.
    # Both CLAIM the request_id in the physical guard ledger on ALLOW, so a
    # "just asking" tool would burn a day-count slot and make the second
    # identical check return a replay DENY. A preview that mutates the thing it
    # previews is not a preview. `evaluate_risk_gate` is the genuinely
    # side-effect-free check: trading state, halt sentinel, kill switches,
    # stoploss guard, cooldown, drawdown — repeatable, no ledger write.
    try:
        from cognition import crypto_protections
    except ImportError as exc:
        return f"error: protections module unavailable ({exc})"

    try:
        history = crypto_protections.TradeHistory.unavailable(
            "no closed-trade history supplied to this check"
        )
        decision = crypto_protections.evaluate_risk_gate(
            pair=symbol.strip(),
            side={"buy": "long", "sell": "short"}.get(
                side.strip().lower(), side.strip().lower()
            ),
            intent="open",
            history=history,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("risk-gate preflight failed", exc_info=True)
        return f"error: preflight failed: {type(exc).__name__}: {exc}"

    lines = [
        "PREFLIGHT — risk gate only. Nothing was submitted and no order slot was claimed.",
        f"- allowed: {getattr(decision, 'allowed', '?')}",
        f"- trading state: {getattr(getattr(decision, 'state', None), 'value', getattr(decision, 'state', '?'))}",
    ]
    if not getattr(decision, "certain", True):
        # An UNCERTAIN gate has not proven the trade safe; it has failed to
        # prove it unsafe. Those are different, and only one of them is a
        # green light.
        lines.append(
            "- CERTAINTY: NOT CERTAIN — inputs were incomplete. Treat this as "
            "'unproven', never as 'approved'."
        )
    for reason in getattr(decision, "reasons", ()) or ():
        lines.append(f"- {reason}")
    lines.append(
        "\nNOTE: this checks trading state and circuit breakers only. Mandate "
        "caps (notional, leverage, daily count) are enforced at submission — "
        "use crypto_mandate_read to see what is authorized."
    )
    return _truncate("\n".join(lines))


def _submit(
    *,
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    quantity: float,
    target: float,
    leverage: float,
    request_id: str,
    dry_run_only: bool,
) -> str:
    from cognition import crypto_execution  # noqa: F401 — import-time check

    guard = _build_guard()
    client = crypto_execution.ExecutionClient(guard)
    plan = _plan_from_scalars(
        symbol=symbol,
        side=side,
        entry=entry,
        stop=stop,
        quantity=quantity,
        target=target or None,
        leverage=leverage,
        request_id=request_id,
    )
    return _render_report(client.submit_bracket(plan))


def _crypto_submit_bracket(
    symbol: str = "",
    side: str = "long",
    entry: float = 0.0,
    stop: float = 0.0,
    quantity: float = 0.0,
    target: float = 0.0,
    leverage: float = 1.0,
    request_id: str = "",
    **_: Any,
) -> str:
    """Submit an entry+stop(+target) bracket through the guard.

    Requires a stop. That is not a style preference — `BracketPlan` geometry
    validation rejects a plan without one, because an entry with no stop is an
    unbounded loss, and the guard is the layer that refuses to let a model
    forget.
    """
    if not symbol.strip():
        return "error: symbol is required"
    if not entry or not stop or not quantity:
        return "error: entry, stop and quantity are all required (a bracket needs a stop)"
    try:
        report = _submit(
            symbol=symbol,
            side=side,
            entry=entry,
            stop=stop,
            quantity=quantity,
            target=target,
            leverage=leverage,
            # `/` in a symbol is rejected by request-id validation, so the
            # default is slugged. An operator-supplied id is passed through
            # untouched — silently rewriting an idempotency key would break the
            # retry guarantee it exists to provide.
            request_id=(request_id or "").strip()
            or "agent-"
            + "".join(c if c.isalnum() else "-" for c in symbol.strip().lower()),
            dry_run_only=False,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("bracket submit failed", exc_info=True)
        return f"error: submit failed: {type(exc).__name__}: {exc}"
    return _truncate(report)


_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any, str], ...] = (
    (
        "crypto_mandate_read",
        "crypto",
        "Check whether the desk is authorized to trade, and for how much longer. Ask "
        "this FIRST — with no valid mandate nothing executes, not even a dry run.",
        {"type": "object", "properties": {}},
        _crypto_mandate_read,
        "read",
    ),
    (
        "crypto_preflight",
        "crypto",
        "Ask whether a proposed trade would be ALLOWED, without submitting it. Returns "
        "the guard's decision and every reason. Use before sizing a real position.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "e.g. BTC/USDT"},
                "side": {"type": "string", "enum": ["long", "short"]},
                "entry": {"type": "number"},
                "stop": {"type": "number", "description": "Required — no stop, no bracket."},
                "quantity": {"type": "number"},
                "target": {"type": "number", "description": "Optional take-profit."},
                "leverage": {"type": "number"},
            },
            "required": ["symbol", "side", "entry", "stop", "quantity"],
        },
        _crypto_preflight,
        "read",
    ),
    (
        "crypto_submit_bracket",
        "crypto",
        "Submit an entry + stop (+ optional target) bracket order through the risk "
        "guard. The guard runs in DRY_RUN unless the operator has authorized live "
        "trading on disk; a stop is mandatory. Read the returned reasons carefully.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["long", "short"]},
                "entry": {"type": "number"},
                "stop": {"type": "number", "description": "Mandatory protective stop."},
                "quantity": {"type": "number"},
                "target": {"type": "number"},
                "leverage": {"type": "number"},
                "request_id": {
                    "type": "string",
                    "description": "Idempotency key — reuse it to retry the SAME order safely.",
                },
            },
            "required": ["symbol", "side", "entry", "stop", "quantity"],
        },
        _crypto_submit_bracket,
        "execute",
    ),
)


def register_tools() -> int:
    """Register the order-path tools. Never raises; returns the count."""
    from runtime import tool_registry

    registered = 0
    for name, toolset, description, parameters, handler, effect in _SPECS:
        try:
            tool_registry.register_tool(
                name,
                description,
                toolset=toolset,
                parameters=parameters,
                handler=handler,
                effect=effect,
                elevatable=name != "crypto_submit_bracket",
                dedicated_gate=name == "crypto_submit_bracket",
            )
            registered += 1
        except Exception:  # noqa: BLE001
            _logger.warning("failed to register trade tool %r", name, exc_info=True)
    return registered


__all__ = ["register_tools"]
