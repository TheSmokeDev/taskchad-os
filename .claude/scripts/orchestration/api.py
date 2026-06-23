"""Thin local FastAPI control surface for convoy + mailbox orchestration.

Binds to loopback (127.0.0.1) by default. No authentication required for
local-only callers. Handlers validate input, map to service DTOs, call the
service, and serialize output. ZERO business logic lives here.

Trust boundary: see contract.py LOCAL API TRUST BOUNDARY docstring.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orchestration.capability_gateway import collect_capability_gateway_status
from orchestration.contract import DEFAULT_WORKSPACE_ID
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.executor import ExecutorRegistry, create_default_registry
from orchestration.live_safety import LiveExecutionRefused, require_live_agent_run
from orchestration.mailbox_service import MailboxService
from orchestration.models import (
    AddSubtaskInput,
    AddTeamMemberInput,
    CreateConvoyInput,
    CreateSubtaskInput,
    CreateTeamSessionInput,
    ProgressReport,
    SendMessageInput,
)
from orchestration.observability import orchestration_span, update_observation
from orchestration.operating_room import OperatingRoomService, operating_room_result_to_dict
from orchestration.route_policy import (
    enforce_policy,
    resolve_policy,
    resolve_route_template,
)
from orchestration.team_executor import TeamExecutorService, executor_result_to_dict
from orchestration.team_loop import (
    TeamLoopService,
    TeamTickService,
    result_to_dict,
    tick_result_to_dict,
)
from orchestration.team_room import TeamRoomWorkflowService, team_room_workflow_result_to_dict
from orchestration.team_service import TeamService
from orchestration.tenant_auth import is_multi_tenant_mode, resolve_tenant_binding

logger = logging.getLogger(__name__)


def _operator_surface(request: Request | None) -> str:
    if request is None:
        return "api"
    return request.headers.get("x-operator-surface", "api")

# ── Config ────────────────────────────────────────────────────────────────

API_HOST: str = os.getenv("ORCHESTRATION_API_HOST", "127.0.0.1")
# ``API_PORT`` is profile-aware (PRP-7c Phase 3 / R2 NB1): resolution happens
# lazily at attribute access via the module-level ``__getattr__`` below. This
# keeps ``from orchestration.api import API_PORT`` consumers working while
# deferring resolution until AFTER ``apply_persona_override()`` has run in
# ``run_api.py:14`` (i.e., the right profile's ``HOMIE_HOME`` is active).
ALLOW_NON_LOOPBACK: bool = os.getenv(
    "ORCHESTRATION_API_ALLOW_NON_LOOPBACK", ""
).strip().lower() in {"1", "true", "yes", "on"}

_LOOPBACK_ADDRS = {"127.0.0.1", "::1"}

if API_HOST not in _LOOPBACK_ADDRS and not ALLOW_NON_LOOPBACK:
    raise RuntimeError(
        "ORCHESTRATION_API_HOST must stay loopback unless "
        "ORCHESTRATION_API_ALLOW_NON_LOOPBACK=true is explicitly set"
    )
if API_HOST not in _LOOPBACK_ADDRS:
    logger.warning(
        "ORCHESTRATION_API_HOST=%s is not loopback. Remote access without auth is a security risk.",
        API_HOST,
    )

# Optional bearer token — when set, ALL requests must present it.
# Required when ALLOW_NON_LOOPBACK=true; safe to set for loopback-only deployments too.
ORCHESTRATION_API_TOKEN: str | None = os.getenv("ORCHESTRATION_API_TOKEN", "").strip() or None

if API_HOST not in _LOOPBACK_ADDRS and ORCHESTRATION_API_TOKEN is None:
    raise RuntimeError(
        "ORCHESTRATION_API_ALLOW_NON_LOOPBACK=true requires ORCHESTRATION_API_TOKEN to be set. "
        "Remote access without authentication is a security risk. "
        "Set ORCHESTRATION_API_TOKEN=<secret> or revert to loopback."
    )


def _tenant_enforcement_enabled() -> bool:
    """Phase-A multi-tenant enforcement gate — resolved at CALL TIME (Rule 1), default OFF.

    Multi-tenant request-path enforcement engages ONLY when
    ``HOMIE_TENANT_ENFORCEMENT`` is truthy AND ≥1 active tenant row exists. The
    default-OFF posture is load-bearing security: creating ``tenant_tokens`` rows
    does NOTHING to the request path until an operator EXPLICITLY opts in. That
    makes the Phase-A "half-locked" state (Phase B's all-route deny-by-default not
    yet shipped — unthreaded routes like ``/api/executor/callback`` and the team
    mutators still default to workspace 1) UNREACHABLE in a default deployment,
    and keeps zero-/any-row back-compat byte-identical. Phase B flips this ON only
    after every route is workspace-scoped. Do NOT enable in production until then.
    """
    return os.getenv("HOMIE_TENANT_ENFORCEMENT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def __getattr__(name: str) -> Any:
    """PRP-7c Phase 3: lazy ``API_PORT`` resolver.

    Routes through ``personas.services.get_orchestration_api_port()`` so the
    port follows the active profile (default profile keeps the legacy 4322;
    named profiles get a deterministic offset, env overrides win at every
    rank). Resolution happens at attribute access time, AFTER
    ``apply_persona_override()`` has run in ``run_api.py:14``.
    """
    if name == "API_PORT":
        from personas.services import get_orchestration_api_port
        return get_orchestration_api_port()
    raise AttributeError(f"module 'orchestration.api' has no attribute {name!r}")


# ── Service singletons ───────────────────────────────────────────────────


def _get_services() -> tuple[
    OrchestrationDB, ConvoyService, MailboxService, ExecutorRegistry, TeamService
]:
    """Instantiate orchestration DB + services + executor registry."""
    import config

    db_path = getattr(config, "ORCHESTRATION_DB_PATH", None)
    if db_path is None:
        raise RuntimeError("ORCHESTRATION_DB_PATH not configured")
    # check_same_thread=False: FastAPI runs sync handlers in a threadpool,
    # so the connection must be usable across threads. WAL mode handles
    # concurrent access safely.
    db = OrchestrationDB(db_path, check_same_thread=False)
    registry = create_default_registry()
    return db, ConvoyService(db), MailboxService(db), registry, TeamService(db)


_db, _convoy_svc, _mailbox_svc, _executor_registry, _team_svc = _get_services()


def _require_subtask_in_convoy(convoy_id: int, subtask_id: int, workspace_id: int):
    # Tenant Isolation v0 (B4): resolve the subtask through the WORKSPACE-scoped
    # read so a cross-tenant subtask_id surfaces as 404 (not found in caller's
    # workspace), not as a leaked row from another tenant's convoy.
    subtask = _convoy_svc.get_subtask(subtask_id, workspace_id=workspace_id)
    if not subtask or subtask.convoy_id != convoy_id:
        raise HTTPException(
            status_code=404,
            detail=f"Subtask {subtask_id} not found in convoy {convoy_id}",
        )
    return subtask


def _require_live_agent_action(action: str, allow_live_agent_run: bool) -> None:
    try:
        require_live_agent_run(action, explicit_opt_in=allow_live_agent_run)
    except LiveExecutionRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# ── Pydantic request models ──────────────────────────────────────────────


class CreateSubtaskBody(BaseModel):
    title: str
    description: str | None = None
    assigned_agent_id: str | None = None
    assigned_agent_name: str | None = None
    depends_on_subtask_indexes: list[int] = Field(default_factory=list)
    metadata: str | None = None


class CreateConvoyBody(BaseModel):
    title: str
    description: str | None = None
    created_by: str = ""
    base_branch: str = "main"
    repo_path: str | None = None
    merge_strategy: str = "squash"
    decomposition_mode: str = "manual"
    subtasks: list[CreateSubtaskBody] = Field(default_factory=list)


class UpdateStatusBody(BaseModel):
    status: str


class AddSubtaskBody(BaseModel):
    title: str
    description: str | None = None
    assigned_agent_id: str | None = None
    assigned_agent_name: str | None = None
    depends_on_subtask_ids: list[int] = Field(default_factory=list)
    metadata: str | None = None


class AddSubtasksBody(BaseModel):
    subtasks: list[AddSubtaskBody]


class DispatchBody(BaseModel):
    paperclip_issue_id: str | None = None
    executor_name: str | None = None
    team_id: int | None = None
    allow_live_agent_run: bool = False


class ProgressBody(BaseModel):
    progress_pct: float = 0.0
    message: str = ""
    executor_name: str = "local"


class FailBody(BaseModel):
    error_message: str | None = None


class SendMessageBody(BaseModel):
    from_agent: str
    recipients: list[str]
    body: str
    convoy_id: int | None = None
    thread_id: int | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to_message_id: int | None = None
    message_type: str = "message"
    subject: str | None = None
    artifact_refs: dict[str, Any] | None = None
    dedupe_key: str | None = None
    msg_type: str | None = None


class TransitionBody(BaseModel):
    status: str


class UpdateFieldsBody(BaseModel):
    assigned_agent_id: str | None = None
    assigned_agent_name: str | None = None
    error_message: str | None = None
    worktree_branch: str | None = None
    merge_commit: str | None = None


class AckBody(BaseModel):
    recipient_agent: str
    claim_token: str


class ExecutorCallbackBody(BaseModel):
    event_type: str
    convoy_id: int
    subtask_id: int
    idempotency_key: str
    payload: dict = {}


class CreateTeamBody(BaseModel):
    team_name: str
    lead_agent_id: str
    lead_agent_name: str | None = None
    convoy_id: int | None = None
    backend_type: str = "local"
    metadata: str | None = None


class AddTeamMemberBody(BaseModel):
    agent_id: str
    agent_name: str | None = None
    role: str = "worker"
    subtask_id: int | None = None


class TeamPingBody(BaseModel):
    agent_id: str | None = None


class TeamRoomRunBody(BaseModel):
    goal: str
    workflow_id: str = "growth_boardroom"
    context: str | None = None
    use_runtime: bool = False
    runtime_lane: str | None = None
    max_rounds: int | None = None
    meeting_mode: str | None = None
    v2: bool = False
    allow_live_agent_run: bool = False


class OperatingRoomRunBody(BaseModel):
    goal: str
    workflow_id: str = "growth_boardroom"
    context: str | None = None
    use_runtime: bool = False
    runtime_lane: str | None = None
    max_rounds: int | None = 2
    meeting_mode: str | None = "facilitated_boardroom"
    run_tick: bool = True
    tick_agent_id: str | None = None
    tick_complete_running: bool = False
    tick_execute_running: bool = False
    tick_executor_command: str = "git_status"
    tick_executor_cwd: str | None = None
    tick_complete_on_executor_success: bool = False
    allow_live_agent_run: bool = False


class TeamLoopStepBody(BaseModel):
    agent_id: str
    subtask_id: int | None = None
    reply_body: str | None = None
    use_runtime: bool = False
    runtime_lane: str | None = None
    complete: bool = False
    allow_live_agent_run: bool = False


class TeamTickBody(BaseModel):
    agent_id: str | None = None
    use_runtime: bool = False
    runtime_lane: str | None = None
    complete_running: bool = False
    execute_running: bool = False
    executor_command: str = "git_status"
    executor_cwd: str | None = None
    complete_on_executor_success: bool = False
    allow_live_agent_run: bool = False


class TeamExecutorStepBody(BaseModel):
    agent_id: str
    subtask_id: int | None = None
    command_key: str = "git_status"
    cwd: str | None = None
    timeout_seconds: int | None = None
    complete_on_success: bool = False
    allow_live_agent_run: bool = False


# ── FastAPI app ───────────────────────────────────────────────────────────

app = FastAPI(title="Orchestration Control API", version="0.1.0")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Bearer enforcement + tenant auth TRUTH TABLE + route-policy deny-by-default.

    Two modes, selected by physical state (Rule 2 — the presence of an active
    non-admin ``tenant_tokens`` row), NOT by config:

    SINGLE-TENANT (zero active non-admin tenant rows — the DEFAULT):
        Behavior is BYTE-UNCHANGED from before Phase A. ``ORCHESTRATION_API_TOKEN``
        bearer-equality is the only gate (when set); every request binds to
        ``DEFAULT_WORKSPACE_ID`` / ``persona_scope=None`` / ``is_admin=True``.
        The ``/api/cabinet/voice/*`` blanket query-token exemption is preserved
        byte-unchanged in this mode (no policy layer — there are no tenants to
        deny).

    MULTI-TENANT (≥1 active non-admin tenant row AND enforcement opted in):
        The bearer is resolved by HASHED row lookup (``resolve_tenant_binding``),
        NOT by equality to the global token. The resolved
        ``workspace_id`` / ``persona_scope`` / ``is_admin`` are set on
        ``request.state``. AFTER binding, ``route_policy.enforce_policy`` runs
        REAL deny-by-default (B2): a valid bound tenant token hitting a route
        with NO declared policy fails CLOSED (403), and a tenant token on an
        ``admin`` / ``voice_query`` route is 403'd. The threaded convoy/mailbox/
        team handlers + the WS3 dashboard handlers do the row-level id gate.

    NB2 (R2) — voice routes are NOT a blanket exemption in MT mode. The route
        template is resolved (``resolve_route_template``) and classified:
        static JS is ``public``; UI/session/control routes are ``voice_query``
        (a tenant HEADER token 403s; the admin query-token path still works);
        the per-persona ``avatars/{persona_id}.png`` route reads persona config
        so it is ``admin`` (a tenant token 403s — it does NOT bypass the policy
        layer via the prefix).

    ``/api/health`` and ``/api/info`` are ``public``; ``/api/audit-log`` enforces
    its own ``DASHBOARD_ADMIN_TOKEN`` internally and is ``admin`` here. In MT
    mode these all route through ``enforce_policy`` like everything else.

    M2 (PRP) — ``request.state`` defaults are set at the TOP, BEFORE any
    exemption returns, so an exempt voice/cabinet handler that reads
    ``request.state.workspace_id`` can never ``AttributeError``/500.
    """
    # M2 — request.state defaults FIRST, before any exemption returns.
    request.state.workspace_id = DEFAULT_WORKSPACE_ID
    request.state.persona_scope = None
    request.state.is_admin = True

    # Resolve the auth truth table against the CURRENT module-level DB (the test
    # fixtures swap ``_db``, so reference the module attr, not a closure bind).
    db = _db
    path = request.url.path

    # Phase-A/B gate (Rule 1, call-time, default OFF): MT enforcement engages
    # ONLY on explicit opt-in AND when an active non-admin tenant row exists.
    # OFF (default) → tenant rows are IGNORED, the legacy single-tenant path runs
    # byte-identically, and there is NO half-locked leak surface. Phase B ships
    # all-route deny-by-default; an operator flips the flag once that lands.
    multi_tenant = _tenant_enforcement_enabled() and is_multi_tenant_mode(db)

    if not multi_tenant:
        # ── SINGLE-TENANT (or enforcement OFF) — preserve TODAY exactly. ─────
        # These three exemptions are byte-unchanged from the pre-Phase-B path.
        if path == "/api/health":
            return await call_next(request)
        if path == "/api/audit-log":
            return await call_next(request)
        if path.startswith("/api/cabinet/voice/"):
            if ORCHESTRATION_API_TOKEN is None:
                return await call_next(request)
            query_token = request.query_params.get("token", "")
            if query_token == ORCHESTRATION_API_TOKEN:
                return await call_next(request)
            return JSONResponse(
                {"detail": "Invalid or missing query-param token for cabinet voice"},
                status_code=401,
            )
        if ORCHESTRATION_API_TOKEN is not None:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != ORCHESTRATION_API_TOKEN:
                return JSONResponse(
                    {"detail": "Invalid or missing bearer token"}, status_code=401
                )
        return await call_next(request)

    # ── MULTI-TENANT — resolve binding, then enforce the route policy. ──────
    # Resolve the LITERAL route template (NB1: scope["route"] is unset in HTTP
    # middleware; this replays Starlette matching across the live route table).
    resolved = resolve_route_template(request)
    route_tpl = resolved[1] if resolved is not None else None
    policy = resolve_policy(request.method, route_tpl)

    # voice_query routes admit the admin/global query-param token path (browsers
    # cannot send the Authorization header reliably on cross-iframe GETs). A
    # valid query token is an ADMIN reach — NOT a tenant token (NB2). A tenant
    # HEADER token on a voice_query route falls through to enforce_policy → 403.
    if policy == "voice_query" and ORCHESTRATION_API_TOKEN is not None:
        if request.query_params.get("token", "") == ORCHESTRATION_API_TOKEN:
            request.state.is_admin = True
            return await call_next(request)

    auth = request.headers.get("Authorization", "")
    bearer = auth[7:] if auth.startswith("Bearer ") else ""
    binding = resolve_tenant_binding(db, bearer)
    if binding is None:
        # public routes (health/info/static/templates/openapi) need no token.
        if policy == "public":
            return await call_next(request)
        return JSONResponse(
            {"detail": "Invalid or missing bearer token"}, status_code=401
        )
    request.state.workspace_id = binding.workspace_id
    request.state.persona_scope = binding.persona_scope
    request.state.is_admin = binding.is_admin

    # B2 — REAL deny-by-default: unregistered route → 403; tenant token on
    # admin/voice_query → 403; admin binding reaches everything.
    deny = enforce_policy(request.method, route_tpl, binding)
    if deny is not None:
        return deny
    return await call_next(request)


# ── Convoy endpoints ─────────────────────────────────────────────────────


@app.post("/api/convoy")
def create_convoy(request: Request, body: CreateConvoyBody):
    ws = request.state.workspace_id
    subtask_inputs = [
        CreateSubtaskInput(
            title=s.title,
            description=s.description,
            assigned_agent_id=s.assigned_agent_id,
            assigned_agent_name=s.assigned_agent_name,
            depends_on_subtask_indexes=s.depends_on_subtask_indexes,
            metadata=s.metadata,
        )
        for s in body.subtasks
    ]
    inp = CreateConvoyInput(
        title=body.title,
        description=body.description,
        created_by=body.created_by,
        base_branch=body.base_branch,
        repo_path=body.repo_path,
        merge_strategy=body.merge_strategy,
        decomposition_mode=body.decomposition_mode,
        subtasks=subtask_inputs,
    )
    try:
        result = _convoy_svc.create_convoy(inp, workspace_id=ws)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return dataclasses.asdict(result)


@app.get("/api/convoy")
def list_convoys(request: Request, status: str | None = None):
    ws = request.state.workspace_id
    convoys = _convoy_svc.list_convoys(workspace_id=ws, status=status)
    return [dataclasses.asdict(c) for c in convoys]


@app.get("/api/convoy/{convoy_id}")
def get_convoy(convoy_id: int, request: Request):
    ws = request.state.workspace_id
    result = _convoy_svc.get_convoy(convoy_id, workspace_id=ws)
    if not result:
        raise HTTPException(status_code=404, detail=f"Convoy {convoy_id} not found")
    return dataclasses.asdict(result)


@app.delete("/api/convoy/{convoy_id}")
def delete_convoy(convoy_id: int, request: Request):
    ws = request.state.workspace_id
    try:
        _convoy_svc.delete_convoy(convoy_id, workspace_id=ws)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.post("/api/convoy/{convoy_id}/status")
def update_convoy_status(convoy_id: int, body: UpdateStatusBody, request: Request):
    ws = request.state.workspace_id
    try:
        result = _convoy_svc.update_convoy_status(convoy_id, body.status, workspace_id=ws)
    except ValueError as e:
        # "not found" (cross-tenant / stale id) -> 404; an invalid transition on
        # an OWNED convoy stays 400. Same discrimination as add_subtasks.
        status = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status, detail=str(e))
    return dataclasses.asdict(result)


@app.post("/api/convoy/{convoy_id}/subtasks")
def add_subtasks(convoy_id: int, body: AddSubtasksBody, request: Request):
    ws = request.state.workspace_id
    inputs = [
        AddSubtaskInput(
            title=s.title,
            description=s.description,
            assigned_agent_id=s.assigned_agent_id,
            assigned_agent_name=s.assigned_agent_name,
            depends_on_subtask_ids=s.depends_on_subtask_ids,
            metadata=s.metadata,
        )
        for s in body.subtasks
    ]
    try:
        result = _convoy_svc.add_subtasks(convoy_id, inputs, workspace_id=ws)
    except ValueError as e:
        status = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status, detail=str(e))
    return [dataclasses.asdict(s) for s in result]


@app.get("/api/convoy/{convoy_id}/ready")
def get_ready_subtasks(convoy_id: int, request: Request):
    ws = request.state.workspace_id
    # B4: the ONLY no-workspace service method. Gate on a ws-scoped parent read
    # so a cross-tenant convoy_id is 404, THEN call the (convoy-id-only) reader.
    if not _convoy_svc.get_convoy(convoy_id, workspace_id=ws):
        raise HTTPException(status_code=404, detail=f"Convoy {convoy_id} not found")
    subtasks = _convoy_svc.get_ready_subtasks(convoy_id)
    return [dataclasses.asdict(s) for s in subtasks]


@app.post("/api/convoy/{convoy_id}/subtask/{subtask_id}/dispatch")
def dispatch_subtask(
    convoy_id: int,
    subtask_id: int,
    request: Request,
    body: DispatchBody | None = None,
):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.dispatch_subtask",
        metadata={
            "surface": surface,
            "convoy_id": convoy_id,
            "subtask_id": subtask_id,
            "team_id": body.team_id if body else None,
            "requested_backend": body.executor_name if body else None,
            "live_agent_opt_in": body.allow_live_agent_run if body else False,
        },
        trace_metadata={"surface": surface, "feature_phase": 6},
        expected_exceptions=(HTTPException,),
    ):
        ws = request.state.workspace_id
        _require_live_agent_action(
            "convoy subtask dispatch",
            body.allow_live_agent_run if body else False,
        )
        _require_subtask_in_convoy(convoy_id, subtask_id, ws)
        executor_name = body.executor_name if body else None
        team_id = body.team_id if body else None

        if team_id is not None:
            try:
                receipt, actual_backend = _team_svc.dispatch_to_executor(
                    team_id, subtask_id, workspace_id=ws,
                )
            except ValueError as e:
                update_observation(
                    metadata={"error_type": "dispatch_validation", "team_id": team_id},
                    level="WARNING",
                    status_message=str(e),
                )
                raise HTTPException(status_code=404, detail=str(e))
            result = dataclasses.asdict(receipt)
            result["actual_backend"] = actual_backend
            _team_for_obs = _team_svc.get_team_session(team_id, workspace_id=ws)
            update_observation(
                metadata={
                    "team_id": team_id,
                    "requested_backend": "team_backend",
                    "actual_backend": actual_backend,
                    "fallback_used": (
                        actual_backend != _team_for_obs.session.backend_type
                        if _team_for_obs
                        else False
                    ),
                },
                output={"status": receipt.status},
            )
            return result

        if executor_name and not _executor_registry.has(executor_name):
            raise HTTPException(status_code=400, detail=f"Unknown executor '{executor_name}'")
        executor = _executor_registry.resolve(executor_name)
        try:
            receipt = _convoy_svc.dispatch_subtask(
                subtask_id,
                workspace_id=ws,
                paperclip_issue_id=body.paperclip_issue_id if body else None,
                executor=executor,
            )
        except ValueError as e:
            update_observation(
                metadata={"error_type": "dispatch_validation"},
                level="WARNING",
                status_message=str(e),
            )
            raise HTTPException(status_code=400, detail=str(e))
        update_observation(
            metadata={
                "requested_backend": executor_name or "local",
                "actual_backend": receipt.executor_name,
                "fallback_used": bool(executor_name and receipt.executor_name != executor_name),
            },
            output={"status": receipt.status},
        )
        return dataclasses.asdict(receipt)


@app.post("/api/convoy/{convoy_id}/subtask/{subtask_id}/complete")
def complete_subtask(convoy_id: int, subtask_id: int, request: Request):
    ws = request.state.workspace_id
    _require_subtask_in_convoy(convoy_id, subtask_id, ws)
    try:
        newly_ready, convoy_completed = _convoy_svc.handle_subtask_completion(
            subtask_id, workspace_id=ws
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "newly_ready": [dataclasses.asdict(s) for s in newly_ready],
        "convoy_completed": convoy_completed,
    }


@app.post("/api/convoy/{convoy_id}/subtask/{subtask_id}/fail")
def fail_subtask(
    convoy_id: int, subtask_id: int, request: Request, body: FailBody | None = None
):
    ws = request.state.workspace_id
    _require_subtask_in_convoy(convoy_id, subtask_id, ws)
    try:
        convoy_failed = _convoy_svc.handle_subtask_failure(
            subtask_id,
            workspace_id=ws,
            error_message=body.error_message if body else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"convoy_failed": convoy_failed}


@app.post("/api/convoy/{convoy_id}/subtask/{subtask_id}/progress")
def report_progress(convoy_id: int, subtask_id: int, body: ProgressBody, request: Request):
    ws = request.state.workspace_id
    _require_subtask_in_convoy(convoy_id, subtask_id, ws)
    import time

    progress = ProgressReport(
        subtask_id=subtask_id,
        convoy_id=convoy_id,
        executor_name=body.executor_name,
        progress_pct=body.progress_pct,
        message=body.message,
        timestamp=int(time.time()),
    )
    try:
        _convoy_svc.report_progress(subtask_id, progress, workspace_id=ws)
    except ValueError as e:
        status = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status, detail=str(e))
    return {"ok": True}


@app.post("/api/convoy/{convoy_id}/subtask/{subtask_id}/transition")
def transition_subtask(
    convoy_id: int, subtask_id: int, body: TransitionBody, request: Request
):
    ws = request.state.workspace_id
    _require_subtask_in_convoy(convoy_id, subtask_id, ws)
    try:
        result = _convoy_svc.transition_subtask(subtask_id, body.status, workspace_id=ws)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return dataclasses.asdict(result)


@app.patch("/api/convoy/{convoy_id}/subtask/{subtask_id}")
def update_subtask_fields(
    convoy_id: int, subtask_id: int, body: UpdateFieldsBody, request: Request
):
    ws = request.state.workspace_id
    _require_subtask_in_convoy(convoy_id, subtask_id, ws)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        result = _convoy_svc.update_subtask_fields(subtask_id, fields, workspace_id=ws)
    except ValueError as e:
        status = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status, detail=str(e))
    return dataclasses.asdict(result)


# ── Executor endpoints ──────────────────────────────────────────────────


@app.get("/api/executors")
def list_executors():
    return _executor_registry.list_capabilities()


@app.post("/api/executor/callback")
def executor_callback(body: ExecutorCallbackBody):
    """Receive an executor lifecycle event with idempotency guarantee.

    Thin adapter — all logic (receipt dedup, state transitions, auto-dispatch)
    lives in convoy_service.handle_executor_callback().
    """
    try:
        status, newly_dispatched = _convoy_svc.handle_executor_callback(
            event_type=body.event_type,
            convoy_id=body.convoy_id,
            subtask_id=body.subtask_id,
            idempotency_key=body.idempotency_key,
            payload=body.payload,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": status, "newly_dispatched": newly_dispatched}


# ── Mailbox endpoints ────────────────────────────────────────────────────


@app.post("/api/mailbox/send")
def send_message(body: SendMessageBody, request: Request):
    ws = request.state.workspace_id
    inp = SendMessageInput(
        from_agent=body.from_agent,
        recipients=body.recipients,
        body=body.body,
        convoy_id=body.convoy_id,
        thread_id=body.thread_id,
        correlation_id=body.correlation_id,
        causation_id=body.causation_id,
        reply_to_message_id=body.reply_to_message_id,
        message_type=body.message_type,
        subject=body.subject,
        artifact_refs=body.artifact_refs,
        dedupe_key=body.dedupe_key,
        msg_type=body.msg_type,
    )
    try:
        result = _mailbox_svc.send_message(inp, workspace_id=ws)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return dataclasses.asdict(result)


@app.get("/api/mailbox/inbox/{agent_id}")
def get_inbox(
    agent_id: str,
    request: Request,
    convoy_id: int | None = None,
    msg_type: str | None = None,
):
    ws = request.state.workspace_id
    messages = _mailbox_svc.get_inbox(
        agent_id, workspace_id=ws, convoy_id=convoy_id, msg_type=msg_type,
    )
    return [dataclasses.asdict(m) for m in messages]


@app.post("/api/mailbox/claim/{agent_id}")
def claim_deliveries(
    agent_id: str, request: Request, convoy_id: int | None = None, limit: int = 10
):
    ws = request.state.workspace_id
    claimed = _mailbox_svc.claim_deliveries(
        agent_id, workspace_id=ws, convoy_id=convoy_id, limit=limit
    )
    return [dataclasses.asdict(m) for m in claimed]


@app.post("/api/mailbox/ack/{delivery_id}")
def ack_delivery(delivery_id: int, body: AckBody, request: Request):
    ws = request.state.workspace_id
    try:
        _mailbox_svc.ack_delivery(
            delivery_id,
            recipient_agent=body.recipient_agent,
            claim_token=body.claim_token,
            workspace_id=ws,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.get("/api/mailbox/convoy/{convoy_id}")
def get_convoy_messages(convoy_id: int, request: Request):
    ws = request.state.workspace_id
    messages = _mailbox_svc.get_convoy_messages(convoy_id, workspace_id=ws)
    return [dataclasses.asdict(m) for m in messages]


# ── Team session endpoints ──────────────────────────────────────────────


@app.post("/api/team")
def create_team(request: Request, body: CreateTeamBody):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.create_team",
        metadata={
            "surface": surface,
            "team_name": body.team_name,
            "lead_agent_id": body.lead_agent_id,
            "convoy_id": body.convoy_id,
            "requested_backend": body.backend_type,
        },
        trace_metadata={"surface": surface, "feature_phase": 4},
        expected_exceptions=(HTTPException,),
    ):
        inp = CreateTeamSessionInput(
            team_name=body.team_name,
            lead_agent_id=body.lead_agent_id,
            lead_agent_name=body.lead_agent_name,
            convoy_id=body.convoy_id,
            backend_type=body.backend_type,
            metadata=body.metadata,
        )
        try:
            result = _team_svc.create_team_session(
                inp, workspace_id=request.state.workspace_id
            )
        except ValueError as e:
            update_observation(
                metadata={"error_type": "team_validation"},
                level="WARNING",
                status_message=str(e),
            )
            raise HTTPException(status_code=400, detail=str(e))
        update_observation(
            metadata={
                "team_id": result.session.id,
                "team_name": result.session.team_name,
                "convoy_id": result.session.convoy_id,
                "actual_backend": result.session.backend_type,
            }
        )
        return dataclasses.asdict(result)


@app.get("/api/team")
def list_teams(request: Request, status: str | None = None):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.list_teams",
        metadata={"surface": surface, "status_filter": status},
        trace_metadata={"surface": surface, "feature_phase": 5},
    ):
        teams = _team_svc.list_team_sessions(
            status=status, workspace_id=request.state.workspace_id
        )
        update_observation(metadata={"team_count": len(teams)})
        return [dataclasses.asdict(t) for t in teams]


@app.post("/api/team/room/run")
def run_team_room(request: Request, body: TeamRoomRunBody):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.team_room_run",
        metadata={
            "surface": surface,
            "workflow_id": body.workflow_id,
            "meeting_mode": body.meeting_mode,
            "v2": body.v2,
            "use_runtime": body.use_runtime,
            "runtime_lane": body.runtime_lane,
            "max_rounds": body.max_rounds,
            "live_agent_opt_in": body.allow_live_agent_run,
        },
        trace_metadata={"surface": surface, "feature_phase": 12},
        expected_exceptions=(HTTPException,),
    ):
        _require_live_agent_action("team room run", body.allow_live_agent_run)
        try:
            result = TeamRoomWorkflowService(_db).run_team_room(
                goal=body.goal,
                workflow_id=body.workflow_id,
                context=body.context,
                use_runtime=body.use_runtime,
                runtime_lane=body.runtime_lane,
                max_rounds=body.max_rounds,
                meeting_mode=(
                    "facilitated_boardroom"
                    if body.v2 and not body.meeting_mode
                    else body.meeting_mode
                ),
                # B4: the boardroom CREATES a team + convoy — they must land in
                # the caller's workspace, not the default ws 1 (self-isolation).
                workspace_id=request.state.workspace_id,
            )
        except ValueError as e:
            update_observation(
                level="WARNING",
                status_message=str(e),
                metadata={"error_type": "team_room_validation"},
            )
            raise HTTPException(status_code=400, detail=str(e))
        payload = team_room_workflow_result_to_dict(result)
        update_observation(
            metadata={
                "team_id": payload["team_id"],
                "convoy_id": payload["convoy_id"],
                "workflow_id": payload["workflow_id"],
                "progress": (
                    f"{payload['progress']['completed']}/"
                    f"{payload['progress']['total']}"
                ),
            },
            output={"final_brief_chars": len(payload["final_brief"])},
        )
        return payload


@app.post("/api/team/operating-room/run")
def run_operating_room(request: Request, body: OperatingRoomRunBody):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.operating_room_run",
        metadata={
            "surface": surface,
            "workflow_id": body.workflow_id,
            "meeting_mode": body.meeting_mode,
            "use_runtime": body.use_runtime,
            "runtime_lane": body.runtime_lane,
            "run_tick": body.run_tick,
            "live_agent_opt_in": body.allow_live_agent_run,
        },
        trace_metadata={"surface": surface, "feature_phase": 13},
        expected_exceptions=(HTTPException,),
    ):
        _require_live_agent_action("operating room run", body.allow_live_agent_run)
        try:
            result = OperatingRoomService(_db).run_operating_room(
                goal=body.goal,
                workflow_id=body.workflow_id,
                context=body.context,
                use_runtime=body.use_runtime,
                runtime_lane=body.runtime_lane,
                max_rounds=body.max_rounds,
                meeting_mode=body.meeting_mode,
                run_tick=body.run_tick,
                tick_agent_id=body.tick_agent_id,
                tick_complete_running=body.tick_complete_running,
                tick_execute_running=body.tick_execute_running,
                tick_executor_command=body.tick_executor_command,
                tick_executor_cwd=body.tick_executor_cwd,
                tick_complete_on_executor_success=body.tick_complete_on_executor_success,
                # B4: the operating room CREATES a team + convoy and ticks it —
                # all of it must be scoped to the caller's workspace.
                workspace_id=request.state.workspace_id,
            )
        except ValueError as e:
            update_observation(
                level="WARNING",
                status_message=str(e),
                metadata={"error_type": "operating_room_validation"},
            )
            raise HTTPException(status_code=400, detail=str(e))
        payload = operating_room_result_to_dict(result)
        proof = payload["proof_packet"]
        update_observation(
            metadata={
                "run_id": payload["run_id"],
                "team_id": proof["team_id"],
                "convoy_id": proof["convoy_id"],
                "workflow_id": proof["workflow_id"],
                "sanitized": proof["sanitized"],
            },
            output={"final_brief_chars": len(proof["final_brief"] or "")},
        )
        return payload


@app.get("/api/capabilities/status")
def get_capability_gateway_status(request: Request):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.capabilities_status",
        metadata={"surface": surface},
        trace_metadata={"surface": surface, "feature_phase": 13},
        expected_exceptions=(HTTPException,),
    ):
        payload = collect_capability_gateway_status()
        update_observation(
            metadata={
                "enabled_capabilities": payload["capabilities"]["enabled_count"],
                "total_capabilities": payload["capabilities"]["total_count"],
                "enabled_integrations": payload["integrations"]["enabled_count"],
                "toolset_count": len(payload["toolsets"]),
            }
        )
        return payload


@app.get("/api/team/{team_id}")
def get_team(team_id: int, request: Request):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.get_team",
        metadata={"surface": surface, "team_id": team_id},
        trace_metadata={"surface": surface, "feature_phase": 5, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        result = _team_svc.get_team_session(
            team_id, workspace_id=request.state.workspace_id
        )
        if not result:
            raise HTTPException(status_code=404, detail=f"Team session {team_id} not found")
        update_observation(
            metadata={
                "team_id": result.session.id,
                "team_name": result.session.team_name,
                "convoy_id": result.session.convoy_id,
                "backend_type": result.session.backend_type,
                "member_count": len(result.members),
            }
        )
        return dataclasses.asdict(result)


@app.delete("/api/team/{team_id}")
def close_team(team_id: int, request: Request):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.close_team",
        metadata={"surface": surface, "team_id": team_id},
        trace_metadata={"surface": surface, "feature_phase": 5, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        try:
            # B4: scope the mutation to the caller's workspace — a cross-tenant
            # team_id raises ValueError("...not found") → 404 (the team is not in
            # B's workspace), never a silent close of A's team in workspace 1.
            result = _team_svc.close_team_session(
                team_id, workspace_id=request.state.workspace_id
            )
        except ValueError as e:
            update_observation(level="WARNING", status_message=str(e), metadata={"error_type": "team_validation"})
            raise HTTPException(status_code=404, detail=str(e))
        update_observation(metadata={"team_id": team_id, "final_status": result.status})
        return dataclasses.asdict(result)


@app.post("/api/team/{team_id}/members")
def add_team_member(team_id: int, request: Request, body: AddTeamMemberBody):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.add_team_member",
        metadata={
            "surface": surface,
            "team_id": team_id,
            "agent_id": body.agent_id,
            "role": body.role,
            "subtask_id": body.subtask_id,
        },
        trace_metadata={"surface": surface, "feature_phase": 4, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        inp = AddTeamMemberInput(
            agent_id=body.agent_id,
            agent_name=body.agent_name,
            role=body.role,
            subtask_id=body.subtask_id,
        )
        try:
            result = _team_svc.add_member(
                team_id, inp, workspace_id=request.state.workspace_id
            )
        except ValueError as e:
            status = 404 if "not found" in str(e).lower() else 400
            update_observation(level="WARNING", status_message=str(e), metadata={"error_type": "team_validation"})
            raise HTTPException(status_code=status, detail=str(e))
        update_observation(metadata={"team_id": team_id, "agent_id": result.agent_id, "member_role": result.role})
        return dataclasses.asdict(result)


@app.post("/api/team/{team_id}/shutdown")
def request_team_shutdown(team_id: int, request: Request):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.request_team_shutdown",
        metadata={"surface": surface, "team_id": team_id, "msg_type": "shutdown_request"},
        trace_metadata={"surface": surface, "feature_phase": 5, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        try:
            result = _team_svc.request_shutdown(
                team_id, workspace_id=request.state.workspace_id
            )
        except ValueError as e:
            update_observation(level="WARNING", status_message=str(e), metadata={"error_type": "team_validation"})
            raise HTTPException(status_code=404, detail=str(e))
        update_observation(metadata={"team_id": team_id, "final_status": result.status})
        return dataclasses.asdict(result)


@app.post("/api/team/{team_id}/ping")
def ping_team(team_id: int, request: Request, body: TeamPingBody | None = None):
    surface = _operator_surface(request)
    agent_id = body.agent_id if body else None
    with orchestration_span(
        "orchestration.api.ping_team",
        metadata={"surface": surface, "team_id": team_id, "agent_id": agent_id},
        trace_metadata={"surface": surface, "feature_phase": 4, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        try:
            _team_svc.ping_activity(
                team_id, agent_id=agent_id, workspace_id=request.state.workspace_id
            )
        except ValueError as e:
            update_observation(level="WARNING", status_message=str(e), metadata={"error_type": "team_validation"})
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}


@app.post("/api/team/{team_id}/loop-step")
def run_team_loop_step(team_id: int, request: Request, body: TeamLoopStepBody):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.team_loop_step",
        metadata={
            "surface": surface,
            "team_id": team_id,
            "agent_id": body.agent_id,
            "subtask_id": body.subtask_id,
            "use_runtime": body.use_runtime,
            "runtime_lane": body.runtime_lane,
            "complete": body.complete,
            "live_agent_opt_in": body.allow_live_agent_run,
        },
        trace_metadata={"surface": surface, "feature_phase": 8, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        _require_live_agent_action("team loop step", body.allow_live_agent_run)
        try:
            result = TeamLoopService(_db).run_member_step(
                team_id,
                body.agent_id,
                subtask_id=body.subtask_id,
                reply_body=body.reply_body,
                use_runtime=body.use_runtime,
                runtime_lane=body.runtime_lane,
                complete=body.complete,
                workspace_id=request.state.workspace_id,  # B4: cross-tenant team_id → 404
            )
        except ValueError as e:
            update_observation(
                level="WARNING",
                status_message=str(e),
                metadata={"error_type": "team_loop_validation"},
            )
            status = 404 if "not found" in str(e).lower() else 400
            raise HTTPException(status_code=status, detail=str(e))
        payload = result_to_dict(result)
        update_observation(
            metadata={
                "team_id": team_id,
                "agent_id": body.agent_id,
                "action": payload["action"],
                "claimed_count": payload["claimed_count"],
                "subtask_status": payload["subtask_after"]["status"]
                if payload["subtask_after"]
                else None,
            }
        )
        return payload


@app.post("/api/team/{team_id}/tick")
def run_team_tick(team_id: int, request: Request, body: TeamTickBody | None = None):
    surface = _operator_surface(request)
    payload_body = body or TeamTickBody()
    with orchestration_span(
        "orchestration.api.team_tick",
        metadata={
            "surface": surface,
            "team_id": team_id,
            "agent_id": payload_body.agent_id,
            "use_runtime": payload_body.use_runtime,
            "runtime_lane": payload_body.runtime_lane,
            "complete_running": payload_body.complete_running,
            "execute_running": payload_body.execute_running,
            "executor_command": payload_body.executor_command,
            "complete_on_executor_success": payload_body.complete_on_executor_success,
            "live_agent_opt_in": payload_body.allow_live_agent_run,
        },
        trace_metadata={"surface": surface, "feature_phase": 9, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        _require_live_agent_action("team tick", payload_body.allow_live_agent_run)
        try:
            result = TeamTickService(_db).run_team_tick(
                team_id,
                agent_id=payload_body.agent_id,
                use_runtime=payload_body.use_runtime,
                runtime_lane=payload_body.runtime_lane,
                complete_running=payload_body.complete_running,
                execute_running=payload_body.execute_running,
                executor_command=payload_body.executor_command,
                executor_cwd=payload_body.executor_cwd,
                complete_on_executor_success=payload_body.complete_on_executor_success,
                workspace_id=request.state.workspace_id,  # B4: cross-tenant team_id → 404
            )
        except ValueError as e:
            update_observation(
                level="WARNING",
                status_message=str(e),
                metadata={"error_type": "team_tick_validation"},
            )
            status = 404 if "not found" in str(e).lower() else 400
            raise HTTPException(status_code=status, detail=str(e))
        payload = tick_result_to_dict(result)
        update_observation(
            metadata={
                "team_id": team_id,
                "agent_id": payload["agent_id"],
                "selected_action": payload["selected_action"],
                "waited": payload["waited"],
                "has_error": bool(payload["error"]),
            }
        )
        return payload


@app.post("/api/team/{team_id}/executor-step")
def run_team_executor_step(team_id: int, request: Request, body: TeamExecutorStepBody):
    surface = _operator_surface(request)
    with orchestration_span(
        "orchestration.api.team_executor_step",
        metadata={
            "surface": surface,
            "team_id": team_id,
            "agent_id": body.agent_id,
            "subtask_id": body.subtask_id,
            "command_key": body.command_key,
            "complete_on_success": body.complete_on_success,
            "live_agent_opt_in": body.allow_live_agent_run,
        },
        trace_metadata={"surface": surface, "feature_phase": 10, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        _require_live_agent_action("team executor step", body.allow_live_agent_run)
        try:
            result = TeamExecutorService(_db).run_executor_step(
                team_id,
                agent_id=body.agent_id,
                subtask_id=body.subtask_id,
                command_key=body.command_key,
                cwd=body.cwd,
                timeout_seconds=body.timeout_seconds,
                complete_on_success=body.complete_on_success,
                workspace_id=request.state.workspace_id,  # B4: cross-tenant team_id → 404
            )
        except ValueError as e:
            update_observation(
                level="WARNING",
                status_message=str(e),
                metadata={"error_type": "team_executor_validation"},
            )
            status = 404 if "not found" in str(e).lower() else 400
            raise HTTPException(status_code=status, detail=str(e))
        payload = executor_result_to_dict(result)
        update_observation(
            metadata={
                "team_id": team_id,
                "agent_id": payload["agent_id"],
                "command_key": payload["command_key"],
                "success": payload["success"],
                "exit_code": payload["exit_code"],
            }
        )
        return payload


# ── Team Memory (Phase 7) ─────────────────────────────────────────────────


class WriteMemoryBody(BaseModel):
    content: str
    overwrite: bool = False


def _require_team(team_id: int, workspace_id: int):
    """Return the TeamSessionWithMembers or raise 404.

    Tenant Isolation v0 (B4): the WORKSPACE-scoped read is the parent gate for
    the entire team-memory CRUD family — a cross-tenant team_id surfaces as 404
    BEFORE any memory file is read, written, or deleted.
    """
    session = _team_svc.get_team_session(team_id, workspace_id=workspace_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Team session {team_id} not found")
    return session


@app.get("/api/team/{team_id}/memory")
def list_team_memory_endpoint(team_id: int, request: Request):
    from orchestration.team_memory import list_team_memory

    _require_team(team_id, request.state.workspace_id)
    with orchestration_span(
        "orchestration.api.team_memory_list",
        metadata={"team_id": team_id, "memory_scope": "team"},
        trace_metadata={"feature_phase": 7, "team_id": team_id},
    ):
        files = list_team_memory(team_id)
        update_observation(metadata={"team_id": team_id, "memory_scope": "team", "memory_file_count": len(files)})
        return {"team_id": team_id, "files": files}


@app.get("/api/team/{team_id}/memory/{filename}")
def read_team_memory_endpoint(team_id: int, filename: str, request: Request):
    from orchestration.team_memory import read_team_memory

    _require_team(team_id, request.state.workspace_id)
    with orchestration_span(
        "orchestration.api.team_memory_read",
        metadata={"team_id": team_id, "memory_scope": "team", "memory_filename": filename},
        trace_metadata={"feature_phase": 7, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        try:
            content = read_team_memory(team_id, filename)
        except FileNotFoundError as e:
            update_observation(level="WARNING", status_message=str(e), metadata={"error_type": "team_memory_not_found"})
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            update_observation(level="WARNING", status_message=str(e), metadata={"error_type": "team_memory_validation"})
            raise HTTPException(status_code=400, detail=str(e))
        update_observation(metadata={"team_id": team_id, "memory_filename": filename})
        return {"team_id": team_id, "filename": filename, "content": content}


@app.post("/api/team/{team_id}/memory/{filename}")
def write_team_memory_endpoint(
    team_id: int, filename: str, body: WriteMemoryBody, request: Request
):
    from orchestration.team_memory import write_team_memory

    _require_team(team_id, request.state.workspace_id)
    with orchestration_span(
        "orchestration.api.team_memory_write",
        metadata={
            "team_id": team_id,
            "memory_scope": "team",
            "memory_filename": filename,
            "overwrite": body.overwrite,
        },
        trace_metadata={"feature_phase": 7, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        try:
            path = write_team_memory(team_id, filename, body.content, overwrite=body.overwrite)
        except ValueError as e:
            update_observation(
                metadata={"memory_write_allowed": False, "error_type": "team_memory_validation"},
                level="WARNING",
                status_message=str(e),
            )
            raise HTTPException(status_code=422, detail=str(e))
        except FileExistsError as e:
            update_observation(
                metadata={"memory_write_allowed": False, "error_type": "team_memory_conflict"},
                level="WARNING",
                status_message=str(e),
            )
            raise HTTPException(status_code=409, detail=str(e))
        update_observation(
            metadata={"memory_write_allowed": True, "team_id": team_id, "memory_filename": filename},
            output={"status": "written"},
        )
        return {"team_id": team_id, "filename": filename, "path": str(path), "status": "written"}


@app.delete("/api/team/{team_id}/memory/{filename}")
def delete_team_memory_endpoint(team_id: int, filename: str, request: Request):
    from orchestration.team_memory import delete_team_memory

    _require_team(team_id, request.state.workspace_id)
    with orchestration_span(
        "orchestration.api.team_memory_delete",
        metadata={"team_id": team_id, "memory_scope": "team", "memory_filename": filename},
        trace_metadata={"feature_phase": 7, "team_id": team_id},
        expected_exceptions=(HTTPException,),
    ):
        try:
            removed = delete_team_memory(team_id, filename)
        except ValueError as e:
            update_observation(level="WARNING", status_message=str(e), metadata={"error_type": "team_memory_validation"})
            raise HTTPException(status_code=400, detail=str(e))
        if not removed:
            raise HTTPException(status_code=404, detail=f"Team memory file not found: {filename}")
        update_observation(metadata={"team_id": team_id, "memory_filename": filename}, output={"status": "deleted"})
        return {"team_id": team_id, "filename": filename, "status": "deleted"}


# ── PRD-8 Phase 3 / WS2 — dashboard router mount ─────────────────────────
#
# Slice ownership: orchestration/api.py contains ZERO dashboard logic. The
# dashboard router lives in its own module (.claude/scripts/dashboard_api.py)
# owned by dashboard-owner. The two-touch is just (a) the /api/health path
# exemption in auth_middleware above (R1 B3 + owner Decision 3) and (b)
# the one-line include_router below.
#
# Lazy import inside a try/except so a partial dashboard install (e.g.
# missing dashboard_db.py during a fresh checkout) does NOT prevent the
# orchestration API from booting. Existing convoy/mailbox/team endpoints
# stay reachable; the dashboard surface goes dark with a logged warning.
try:
    from dashboard_api import router as _dashboard_router
    app.include_router(_dashboard_router)
except Exception as _exc:  # noqa: BLE001
    logger.warning("dashboard_api router not mounted: %s", _exc)
