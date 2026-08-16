"""GA4 fleet write tools — persona hands on the analytics fleet, gated.

Epic #465 ticket 1a PR 2, sibling of ``tool_impl_x_write``. Same contract:
granting ``ga4_fleet_write`` lets a persona PROPOSE a write; it never
executes one. The handlers validate and resolve the COMPLETE target at
propose time, store it in the proposal payload, and return the approval
card. Execution happens later, in ``personas.action_proposals.decide_action``.

**The snapshot is the authorization (Codex R1 BLOCKER).** The stored payload
carries the full validated target — analytics account, domain, measurement
id, property/stream, app_dir, Vercel project AND scope — so the execution
token's payload hash binds all of it, and the card renders every externally
relevant field (the full-render rule: nothing hidden). At execute time the
executor re-reads the physical fleet artifacts and compares: ANY drift
between the approved snapshot and the files on disk is an honest refusal
receipt with zero provider calls, and execution targets the STORED values,
never fresh lookups.

Both tools are ``dedicated_gate=True``: never elevatable, never on the base
bootstrap. Handlers are sync, return plain strings, and never raise into the
dispatch loop. All Google/Vercel I/O lives in the executor path;
``decide_action`` already runs off the event loop in the async carriers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_logger = logging.getLogger(__name__)

TOOLSET = "ga4_fleet_write"

TOOL_PROVISION = "ga4_provision_site"
TOOL_DEPLOY = "ga4_deploy_tag"

# Fleet brand ids are slugs (YourBusiness, wayward-insurance, …). Anything else
# never reaches a stored payload.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _fleet() -> Any:
    """Late module-attr lookup (Rule 3) — tests patch the client module."""
    from integrations import ga4_admin_api  # noqa: PLC0415

    return ga4_admin_api


def _kill_switch_check() -> None:
    """The FIRST thing every handler does (Codex R2): OFF must be
    side-effect-free. Credential preflights can refresh OAuth and rewrite the
    token file; config reads touch disk. A disabled gate must surface before
    any of that — KillSwitchDisabled propagates into dispatch, which converts
    it to an error result for the model, never a crash."""
    from personas import action_proposals  # noqa: PLC0415
    from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr

    kill_switches.requireEnabled(
        action_proposals.KILL_SWITCH_NAME, caller="runtime.tool_impl_ga4_write"
    )


def _validate_slug(raw: Any) -> tuple[str, str]:
    """Shape + physical desired-config membership. Returns ``(slug, error)``."""
    if not isinstance(raw, str):
        return "", f"error: brand_slug must be a string, got {type(raw).__name__}"
    slug = raw.strip()
    if not _SLUG_RE.match(slug):
        return "", f"error: {slug[:64]!r} is not a fleet brand slug"
    try:
        config = _fleet().load_fleet_config()
    except Exception as exc:  # noqa: BLE001 — the reason IS the answer
        return "", f"error: fleet config invalid or unavailable: {type(exc).__name__}: {exc}"
    if _fleet().get_brand(slug, config) is None:
        return "", f"error: {slug!r} is not in the GA4 fleet config — nothing proposed"
    return slug, ""


def _edit_credentials_error() -> str:
    """Preflight the edit credential at PROPOSE time. "" means available.

    A proposal the executor could only fail is a bad card: the operator would
    approve an action that cannot run. Failing closed HERE turns a dead
    approval into an honest error string, and no Google call is ever made.
    """
    try:
        from integrations import auth  # noqa: PLC0415 — Rule 3 module attr

        auth.get_ga4_admin_credentials()
        return ""
    except Exception as exc:  # noqa: BLE001 — credential failures are answers
        return f"error: GA4 edit credential unavailable: {exc}"


# ── Target snapshots — one builder each, shared by handler and executor ────
#
# The handler stores the builder's output in the proposal payload; the
# executor re-runs it against the physical files and compares. One builder,
# so the approved thing and the re-checked thing can never diverge in shape.


def _provision_target(slug: str) -> tuple[dict[str, Any] | None, str]:
    """The complete provision target from the desired config, or an error."""
    api = _fleet()
    try:
        config = api.load_fleet_config()
    except Exception as exc:  # noqa: BLE001
        return None, f"fleet config unavailable: {type(exc).__name__}: {exc}"
    brand = api.get_brand(slug, config)
    if brand is None:
        return None, f"{slug!r} left the GA4 fleet config"
    account = str(brand.get("account") or config.get("account") or "").strip()
    domain = str(brand.get("domain") or "").strip().lower()
    if not account:
        return None, f"`{slug}` has no analytics account (brand or fleet default)"
    if not domain:
        return None, f"`{slug}` has no domain in the fleet config"
    return {
        "account": account,
        "domain": domain,
        # Bound into the snapshot (Codex R2 B2): a declared property_resource
        # is the exact resource the approval converges on.
        "property_resource": str(brand.get("property_resource") or "").strip(),
        "time_zone": str(config.get("time_zone") or "America/Los_Angeles"),
        "currency_code": str(config.get("currency_code") or "USD"),
    }, ""


def _deploy_target(slug: str) -> tuple[dict[str, Any] | None, str]:
    """The complete deploy target: desired config + checksummed live state.

    The measurement id comes ONLY from the live state (the skill's
    checksummed artifact, rows keyed ``brand_id``) — never from the desired
    config, which is intent, not fact. A domain disagreement between the two
    is called out, not papered over.
    """
    api = _fleet()
    try:
        config = api.load_fleet_config()
    except Exception as exc:  # noqa: BLE001
        return None, f"fleet config unavailable: {type(exc).__name__}: {exc}"
    brand = api.get_brand(slug, config)
    if brand is None:
        return None, f"{slug!r} left the GA4 fleet config"
    domain = str(brand.get("domain") or "").strip().lower()
    project = str(brand.get("vercel_project") or "").strip()
    app_dir = str(brand.get("app_dir") or "").strip()
    if not project:
        return None, (
            f"`{slug}` has no vercel_project in the fleet config — refusing "
            "to fall back to ambient Vercel linkage"
        )
    if not app_dir:
        return None, f"`{slug}` has no app_dir in the fleet config"
    try:
        state = api.load_live_state()
    except Exception as exc:  # noqa: BLE001
        return None, f"live state unavailable: {type(exc).__name__}: {exc}"
    live = api.get_live_brand(slug, state)
    if live is None:
        return None, (
            f"`{slug}` has no live-state row — approve a ga4_provision_site "
            "action and run the ga4-ops apply flow first"
        )
    measurement_id = str(live.get("measurement_id") or "").strip()
    if not measurement_id:
        return None, (
            f"`{slug}` has no measurement id in the live state — provision first"
        )
    live_domain = str(live.get("domain") or "").strip().lower()
    if live_domain and live_domain != domain:
        return None, (
            f"live state disagrees with the desired config for `{slug}`: "
            f"domain {live_domain} vs {domain} — reconcile the artifacts first"
        )
    try:
        # The checksum proves the state wasn't tampered; THIS proves it
        # belongs to this fleet: same brand set, fields agree, complete rows
        # (a signed but PARTIAL state can no longer drive a deploy, R3 B2).
        api.validate_live_state_against_config(config, state)
    except Exception as exc:  # noqa: BLE001
        return None, f"live state fails fleet validation: {type(exc).__name__}: {exc}"
    # The approval binds the RESOLVED Vercel team — never an empty scope that
    # lets the CLI's cross-team search pick a same-named project (#465 R3).
    scope = api.resolve_vercel_scope()
    if not api.is_valid_vercel_scope(scope):
        return None, (
            f"resolved Vercel scope {scope!r} is not a team slug — set "
            "GA4_VERCEL_SCOPE to the fleet's team"
        )
    return {
        "domain": domain,
        "measurement_id": measurement_id,
        "property": str(live.get("property") or ""),
        "stream": str(live.get("stream") or ""),
        "app_dir": app_dir,
        "vercel_project": project,
        "vercel_scope": scope,
    }, ""


def _snapshot_drift(stored: Any, current: dict[str, Any] | None) -> str:
    """"" when the stored snapshot matches the physical one; else the drift."""
    if not isinstance(stored, dict):
        return "the approved payload carries no target snapshot"
    if current is None:
        return "the target can no longer be resolved from the fleet artifacts"
    mismatched = [
        f"{key} (approved {stored.get(key)!r}, now {current.get(key)!r})"
        for key in stored
        if stored.get(key) != current.get(key)
    ]
    if mismatched:
        return "fleet artifacts drifted since approval: " + "; ".join(mismatched[:6])
    return ""


def _provision_summary(slug: str, target: dict[str, Any]) -> str:
    resource = target.get("property_resource") or "new (canonical name)"
    return (
        f"Provision GA4 for `{slug}` — property ({resource}) + web stream for "
        f"https://{target['domain']} under analytics account {target['account']} "
        f"(tz {target['time_zone']}, {target['currency_code']}; converges if "
        "they already exist)."
    )


def _deploy_summary(slug: str, target: dict[str, Any]) -> str:
    scope = target["vercel_scope"]
    return (
        f"Deploy the GA4 tag for `{slug}` — set "
        f"NEXT_PUBLIC_GA_MEASUREMENT_ID={target['measurement_id']} on Vercel "
        f"project `{target['vercel_project']}` (production, scope `{scope}`) for "
        f"app apps/{target['app_dir']}, run a PRODUCTION DEPLOY of that project, "
        f"then poll https://{target['domain']} until it serves exactly "
        f"{target['measurement_id']}."
    )


def _propose(persona_id: str, tool_name: str, arguments: dict[str, Any], summary: str) -> str:
    """Record the proposal and return the card, or an honest error string."""
    from personas import action_proposals  # noqa: PLC0415 — Rule 3 module attr

    proposal = action_proposals.propose_action(persona_id, tool_name, arguments, summary)
    if proposal is None:
        return (
            f"error: could not create an approval proposal for {tool_name}; "
            "nothing was recorded or executed"
        )
    return action_proposals.card_text(proposal)


def _ga4_provision_site(brand_slug: Any = None, _persona_id: str = "", **_: Any) -> str:
    """Propose provisioning a fleet brand's GA4 property + stream."""
    _kill_switch_check()
    persona = str(_persona_id or "").strip()
    if not persona:
        return "error: persona identity missing — refusing to propose"
    slug, error = _validate_slug(brand_slug)
    if error:
        return error
    target, error = _provision_target(slug)
    if error:
        return f"error: {error}"
    cred_error = _edit_credentials_error()
    if cred_error:
        return cred_error
    return _propose(
        persona,
        TOOL_PROVISION,
        {"brand_slug": slug, "target": target},
        _provision_summary(slug, target),
    )


def _ga4_deploy_tag(brand_slug: Any = None, _persona_id: str = "", **_: Any) -> str:
    """Propose deploying a fleet brand's already-provisioned GA4 tag.

    No Google credential preflight here (Codex R2): deploy is Vercel +
    urllib only — it never calls the Google API, so it must not depend on a
    credential it never uses.
    """
    _kill_switch_check()
    persona = str(_persona_id or "").strip()
    if not persona:
        return "error: persona identity missing — refusing to propose"
    slug, error = _validate_slug(brand_slug)
    if error:
        return error
    target, error = _deploy_target(slug)
    if error:
        return f"error: {error}"
    return _propose(
        persona,
        TOOL_DEPLOY,
        {"brand_slug": slug, "target": target},
        _deploy_summary(slug, target),
    )


# ── Executors — the only code here that touches Google or Vercel ───────────
#
# Called by action_proposals.decide_action with the STORED payload and the
# one-use execution token minted by the winning CAS. Order per executor:
# drift re-check (no side effects) -> token consume (atomic, once) -> the
# stored snapshot drives every provider call. A direct in-process call
# without a valid token fails closed before any of it.


def _refusal(tool: str, slug: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": tool,
        "results": [
            {"handle": slug, "status": "refused", "detail": detail, "screenshot": None}
        ],
    }


def _execute_provision_site(
    *, persona_id: str, action_id: str, execution_token: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from personas import action_proposals  # noqa: PLC0415 — Rule 3 module attr

    args = dict(arguments or {})
    slug = str(args.get("brand_slug") or "")
    stored = args.get("target")
    current, resolve_error = _provision_target(slug)
    drift = _snapshot_drift(stored, current)
    if drift:
        detail = resolve_error if current is None else drift
        return _refusal(TOOL_PROVISION, slug, f"refused: {detail}")
    if not action_proposals.consume_execution_token(persona_id, action_id, execution_token, args):
        return _refusal(
            TOOL_PROVISION, slug,
            "execution token invalid, already consumed, or bound to a different payload",
        )
    target = dict(stored)
    target["id"] = slug
    outcome = _fleet().reconcile_site(target)

    property_ok = outcome["property_status"] in {"created", "existed"}
    stream_ok = outcome["stream_status"] in {"created", "existed"}
    property_detail = (
        f"{outcome['property_status']} {outcome['property']}".strip()
        if property_ok
        else outcome["property_detail"]
    )
    if stream_ok:
        stream_detail = f"{outcome['stream_status']} {outcome['stream']}".strip()
    elif property_ok:
        # The partial must name what DID land — a created property is a real
        # resource, and a note that only says "stream failed" hides it.
        stream_detail = (
            f"{outcome['stream_detail']} "
            f"(property {outcome['property']} was {outcome['property_status']})"
        )
    else:
        stream_detail = outcome["stream_detail"]
    row: dict[str, Any] = {
        "handle": slug,
        "status": "provisioned" if property_ok else "error",
        "detail": "",
        "screenshot": None,
        "property": "ok" if property_ok else "failed",
        "property_detail": property_detail,
        "stream": "ok" if stream_ok else "failed",
        "stream_detail": stream_detail,
    }
    if property_ok and stream_ok:
        row["detail"] = (
            f"property {outcome['property']}, stream {outcome['stream']}, "
            f"measurement {outcome['measurement_id']}"
        )
    else:
        row["detail"] = property_detail if not property_ok else f"stream step: {stream_detail}"
    return {"ok": property_ok and stream_ok, "action": TOOL_PROVISION, "results": [row]}


def _execute_deploy_tag(
    *, persona_id: str, action_id: str, execution_token: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from personas import action_proposals  # noqa: PLC0415 — Rule 3 module attr

    args = dict(arguments or {})
    slug = str(args.get("brand_slug") or "")
    stored = args.get("target")
    current, resolve_error = _deploy_target(slug)
    drift = _snapshot_drift(stored, current)
    if drift:
        detail = resolve_error if current is None else drift
        return _refusal(TOOL_DEPLOY, slug, f"refused: {detail}")
    if not action_proposals.consume_execution_token(persona_id, action_id, execution_token, args):
        return _refusal(
            TOOL_DEPLOY, slug,
            "execution token invalid, already consumed, or bound to a different payload",
        )
    target = dict(stored)

    # Deploy (operator ruling, epic #465): sync the Vercel env, run the
    # production deploy for the approved project, then POLL production until
    # the exact tag is live or the bounded deadline passes. Every substep is
    # receipted (Codex R2): a landed link beside a failed env is a PARTIAL
    # with the link named, and a deployed-but-not-yet-live tag is a PARTIAL
    # that says so — never a silent "failed", never a premature "executed".
    api = _fleet()
    row: dict[str, Any] = {"handle": slug, "status": "error", "detail": "", "screenshot": None}
    try:
        app_path = api.resolve_app_path(target["app_dir"])
    except Exception as exc:  # noqa: BLE001 — the reason is the row's detail
        row["detail"] = str(exc)[:300]
        return {"ok": False, "action": TOOL_DEPLOY, "results": [row]}
    sync = api.sync_vercel_env(
        app_path,
        target["measurement_id"],
        project=target["vercel_project"],
        scope=target["vercel_scope"],
    )
    steps = sync.get("steps") or {}
    if steps.get("link") != "ok":
        if sync.get("link_changed"):
            # vercel link deletes .vercel/project.json before its network
            # work — a FAILED link can still change physical linkage. State
            # changed, so the receipt is a partial, never "nothing happened"
            # (Codex R3).
            row["status"] = "link_mutated"
            row["env_sync"] = "failed"
            row["detail"] = (
                f"vercel link failed AND the app's physical .vercel linkage "
                f"changed: {sync['detail']}"
            )
        else:
            row["detail"] = f"vercel link failed: {sync['detail']}"
        return {"ok": False, "action": TOOL_DEPLOY, "results": [row]}
    if steps.get("env") != "ok":
        row["status"] = "linked"
        row["env_sync"] = "failed"
        row["env_detail"] = sync["detail"]
        row["detail"] = f"linked {target['vercel_project']}; env write failed"
        return {"ok": False, "action": TOOL_DEPLOY, "results": [row]}
    row["env_sync"] = "ok"
    if steps.get("deploy") != "ok":
        row["status"] = "env_synced"
        row["deploy"] = "failed"
        row["deploy_detail"] = sync["detail"]
        row["detail"] = "env var set; production deploy did not complete"
        return {"ok": False, "action": TOOL_DEPLOY, "results": [row]}

    row["deploy"] = "ok"
    row["status"] = "deployed"
    row["detail"] = f"vercel {'+'.join(sync.get('ran') or [])} on {target['vercel_project']}"
    verification = api.verify_tag_live_until(target["domain"], target["measurement_id"])
    row["verification"] = "verified" if verification["ok"] else "failed"
    row["verification_detail"] = verification["detail"]
    return {
        "ok": row["verification"] == "verified",
        "action": TOOL_DEPLOY,
        "results": [row],
    }


_SPECS: tuple[tuple[str, str, dict[str, Any], Any, Any], ...] = (
    (
        TOOL_PROVISION,
        "Provision a fleet brand's GA4 property and web data stream (create-or-get; "
        "converges, never duplicates). This is a WRITE: calling it creates an "
        "operator-approval proposal naming the exact account and domain, and "
        "returns an approval card — nothing happens until the operator approves "
        "the exact proposal with /act approve.",
        {
            "type": "object",
            "properties": {
                "brand_slug": {
                    "type": "string",
                    "description": "Fleet brand id from the GA4 fleet config.",
                },
            },
            "required": ["brand_slug"],
        },
        _ga4_provision_site,
        _execute_provision_site,
    ),
    (
        TOOL_DEPLOY,
        "Deploy a provisioned fleet brand's GA4 tag: set NEXT_PUBLIC_GA_MEASUREMENT_ID "
        "on its Vercel project (production) and verify the live site serves the exact "
        "measurement id. This is a WRITE: calling it creates an operator-approval "
        "proposal naming the exact measurement id, project, scope, app dir and "
        "domain, and returns an approval card — nothing happens until the operator "
        "approves the exact proposal with /act approve.",
        {
            "type": "object",
            "properties": {
                "brand_slug": {
                    "type": "string",
                    "description": "Fleet brand id from the GA4 fleet config.",
                },
            },
            "required": ["brand_slug"],
        },
        _ga4_deploy_tag,
        _execute_deploy_tag,
    ),
)


def register_tools() -> int:
    """Register the GA4 write tools and their executors. Never raises."""
    from personas import action_proposals  # noqa: PLC0415 — cycle-safe
    from runtime import tool_registry

    registered = 0
    for name, description, parameters, handler, executor in _SPECS:
        try:
            tool_registry.register_tool(
                name,
                description,
                toolset=TOOLSET,
                parameters=parameters,
                handler=handler,
                effect="write",
                # The proposal is filed in the CALLING persona's own store —
                # never ambient profile state.
                persona_scoped=True,
                # The action gate is the only road: never one-time elevatable,
                # never on the base bootstrap.
                dedicated_gate=True,
            )
            action_proposals.register_action_executor(name, executor)
            registered += 1
        except Exception:  # noqa: BLE001 — one dead tool must not deny the other
            _logger.warning("failed to register GA4 write tool %r", name, exc_info=True)
    return registered


__all__ = [
    "TOOLSET",
    "TOOL_PROVISION",
    "TOOL_DEPLOY",
    "register_tools",
]
