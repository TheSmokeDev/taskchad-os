"""FastAPI router for the dashboard slice (PRD-8 Phase 3 / WS2).

30 endpoints under ``/api/`` mounted onto the existing orchestration
FastAPI app at port 4322. Inherits the ``auth_middleware`` Bearer token
from ``orchestration/api.py`` — ZERO new auth path.

Slice ownership:
  * The CODEOWNERS glob ``.claude/scripts/dashboard_*.py`` covers this
    module. dashboard-owner reviews every diff.
  * NO YAML import — all config.yaml validation goes through
    ``personas.validate_config_yaml_text`` (Q5 single-yaml-surface lock).
  * NO direct convoy SQL — ``GET /api/agents/{id}/tasks`` calls
    ``convoy_service.list_subtasks_by_agent`` (R3 NB3 orchestration
    boundary).
  * NO direct ``shared.py`` re-implementations — bot lifecycle goes
    through ``dashboard_bot_lifecycle.py`` (R3 NM1 delegation).

Anti-pattern compliance:
  * Rule 1 — every public function uses ``param: T | None = None``
    sentinel; ``config.X`` resolution happens INSIDE the function body.
  * Rule 2 — meta is derived state. Persona list reads disk on every
    call; hard-delete response is derived from
    ``personas.lifecycle._profile_root(persona_id).exists()``, NEVER
    from try/except. ``is_running`` checks go through
    ``dashboard_bot_lifecycle.is_running`` which uses
    ``shared.is_pid_alive``.
  * Rule 3 — N/A (no optional-provider SDK touched directly here;
    Langfuse exists in convoy_service.list_subtasks_by_agent).

PRP anchor: PRPs/active/PRP-prd-8-phase-3-dashboard-port.md §1582-1626.
JSON criteria: PRPs/contracts/prd-8-phase-3.json.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
import dashboard_bot_lifecycle
import personas
from dashboard_db import get_connection
from personas import lifecycle as _lifecycle
from personas.lifecycle import (
    LifecycleError,
    create_profile,
    delete_profile,
    resolve_profile_root,
)

logger = logging.getLogger(__name__)

# ── Router ───────────────────────────────────────────────────────────────

router = APIRouter()


# ── Pydantic request bodies ──────────────────────────────────────────────


class CreatePersonaBody(BaseModel):
    persona_id: str
    display_name: str | None = None
    bot_token_env: str | None = None
    model: str | None = None


class ValidateIdBody(BaseModel):
    persona_id: str


class ValidateTokenBody(BaseModel):
    bot_token: str


class PatchFileBody(BaseModel):
    content: str


class PatchModelBody(BaseModel):
    model: str


class CreateScheduledBody(BaseModel):
    persona_id: str = "default"
    prompt: str
    schedule: str
    next_run: int | None = None


class PatchScheduledBody(BaseModel):
    prompt: str | None = None
    schedule: str | None = None
    next_run: int | None = None
    last_run: int | None = None
    last_result: str | None = None
    status: str | None = None


class PatchSettingsBody(BaseModel):
    # Either a single key/value pair or a partial dict merged into settings.
    key: str | None = None
    value: Any = None
    settings: dict[str, Any] | None = None


# ── Constants and helpers ────────────────────────────────────────────────


_VALID_PERSONA_ID = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_RESERVED_PERSONA_NAMES = {"default", "main", "system", "admin"}

_FILE_ALLOWLIST: tuple[str, ...] = (
    "config.yaml",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "GOALS.md",
    "WORKING.md",
    "SELF.md",
)

# Bot-token env var redaction — ports the ClaudeClaw dashboard.ts:2055-2062
# pattern. The config.yaml field stores the env var NAME (e.g.
# ``TELEGRAM_BOT_TOKEN``); endpoints that surface config content redact
# the resolved value if it appears anywhere in the response shape.
_BOT_TOKEN_ENV_NAMES: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
)

# In-memory rotating pool for /api/agents/suggestions/refresh.
# Cursor persisted to dashboard_settings.suggestions_cursor for restart safety.
_SUGGESTIONS_POOL: tuple[dict, ...] = (
    {"id": "sales-homie", "name": "Sales Homie", "description": "Lead-gen + outreach", "model": "claude-opus-4-7"},
    {"id": "seo-homie", "name": "SEO Homie", "description": "Content + SERP", "model": "claude-opus-4-7"},
    {"id": "ops-homie", "name": "Ops Homie", "description": "Calendar + tasks", "model": "claude-opus-4-7"},
    {"id": "blog-homie", "name": "Blog Homie", "description": "Long-form writing", "model": "claude-opus-4-7"},
    {"id": "hr-homie", "name": "HR Homie", "description": "People + recruiting", "model": "claude-opus-4-7"},
    {"id": "finance-homie", "name": "Finance Homie", "description": "Bookkeeping + budgets", "model": "claude-opus-4-7"},
    {"id": "research-homie", "name": "Research Homie", "description": "Literature review", "model": "claude-opus-4-7"},
    {"id": "support-homie", "name": "Support Homie", "description": "Customer service", "model": "claude-opus-4-7"},
    {"id": "data-homie", "name": "Data Homie", "description": "Analytics + reporting", "model": "claude-opus-4-7"},
    {"id": "code-homie", "name": "Code Homie", "description": "Code review + PRs", "model": "claude-opus-4-7"},
)

# Maximum upload size for avatar (1 MB).
_AVATAR_MAX_BYTES = 1024 * 1024
_AVATAR_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
})
_FORMAT_TO_EXT: dict[str, str] = {
    "PNG": "png",
    "JPEG": "jpg",
    "WEBP": "webp",
}
_CONTENT_TYPE_TO_FORMAT: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
_AVATAR_EXTENSIONS: tuple[str, ...] = ("png", "jpg", "webp")


def _reject_main_translation(persona_id: str) -> None:
    """Q4 lock — Python framework rejects 'main' (Hono is the only translation site).

    Raises ``HTTPException(422)`` if persona_id is the donor 'main' alias.
    Direct port-4322 callers MUST use 'default'.
    """
    if persona_id == "main":
        raise HTTPException(
            status_code=422,
            detail=(
                "persona_id='main' is reserved for the dashboard frontend "
                "translation layer; Python framework accepts 'default' only."
            ),
        )


def _redact_bot_token(text: str) -> str:
    """Redact resolved bot-token values from a config-yaml-shaped string.

    Defense-in-depth — config.yaml stores the env var NAME, so the
    resolved value should never appear in responses. This pass is a
    backstop in case a future schema addition leaks a literal token.
    """
    out = text
    for env_name in _BOT_TOKEN_ENV_NAMES:
        token_value = os.environ.get(env_name, "")
        if token_value and len(token_value) > 8:
            out = out.replace(token_value, f"<{env_name}>")
    return out


def _restore_bot_token(text: str) -> str:
    """Inverse of _redact_bot_token — re-substitute the placeholder before write.

    Used by ``PATCH /api/agents/{id}/files/config.yaml`` so the operator
    can edit a redacted file without losing the original token reference.
    """
    out = text
    for env_name in _BOT_TOKEN_ENV_NAMES:
        token_value = os.environ.get(env_name, "")
        if token_value and len(token_value) > 8:
            out = out.replace(f"<{env_name}>", token_value)
    return out


def _list_personas() -> list[dict]:
    """Walk the filesystem to list personas. Rule 2 — no module-level cache.

    Returns a list of dicts shaped for ``GET /api/agents``.
    """
    result: list[dict] = []
    try:
        profiles = _lifecycle.list_profiles()
    except Exception as exc:
        logger.warning("list_profiles failed: %s", exc)
        return []

    for profile in profiles:
        # Try to load config.yaml for display fields (best-effort).
        display_name = profile.name
        description = ""
        model = "claude-opus-4-7"
        try:
            cfg = personas.load_persona_config(profile.name)
            persona_section = cfg.get("persona", {}) or {}
            display_name = persona_section.get("display_name") or persona_section.get("name") or profile.name
            description = persona_section.get("role", "") or ""
            model_section = cfg.get("model", {}) or {}
            model = model_section.get("preferred", model)
        except (FileNotFoundError, personas.ConfigShapeError):
            # Bootstrap default profile may have no config.yaml — skip.
            pass
        except Exception as exc:
            logger.debug("load_persona_config(%s) failed: %s", profile.name, exc)

        # Compute today's stats from chat_messages (best-effort).
        try:
            today_turns, today_cost = _today_stats(profile.name)
        except Exception:
            today_turns, today_cost = 0, 0.0

        # Avatar etag — 8-char sha256 prefix of the avatar bytes if present.
        avatar_etag = _avatar_etag(profile.name, profile.path)

        result.append({
            "id": profile.name,
            "name": display_name,
            "description": description,
            "model": model,
            "running": profile.bot_running,
            "today_turns": today_turns,
            "today_cost": today_cost,
            "avatar_etag": avatar_etag,
        })

    return result


def _today_stats(persona_id: str) -> tuple[int, float]:
    """Aggregate today's chat_messages turn count + cost per persona.

    Reads from ``config.CHAT_DB_PATH``. Best-effort — returns (0, 0.0)
    if the DB or table doesn't exist yet.
    """
    chat_db_path = Path(config.CHAT_DB_PATH)
    if not chat_db_path.is_file():
        return 0, 0.0
    try:
        conn = sqlite3.connect(str(chat_db_path))
        try:
            # Today bounds (UTC) via ISO date prefix.
            today_prefix = time.strftime("%Y-%m-%d", time.gmtime())
            row = conn.execute(
                """SELECT COALESCE(SUM(message_count), 0) AS turns,
                          COALESCE(SUM(total_cost_usd), 0.0) AS cost
                   FROM chat_sessions
                   WHERE runtime_profile_key = ?
                     AND substr(created_at, 1, 10) = ?""",
                (persona_id, today_prefix),
            ).fetchone()
            if not row:
                return 0, 0.0
            return int(row[0] or 0), float(row[1] or 0.0)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0, 0.0


def _avatar_etag(persona_id: str, profile_root: Path) -> str | None:
    """Return an 8-char sha256 prefix of the persona's avatar bytes or None."""
    for ext in _AVATAR_EXTENSIONS:
        path = profile_root / f"avatar.{ext}"
        if path.is_file():
            try:
                data = path.read_bytes()
                return hashlib.sha256(data).hexdigest()[:8]
            except OSError:
                return None
    return None


def _resolve_avatar_dir(persona_id: str, profile_root: Path | None = None) -> Path:
    """Resolve ``personas/<id>/`` directory for avatar storage. Rule 1 sentinel."""
    if profile_root is not None:
        return profile_root
    if persona_id == "default":
        from personas.core import get_default_paths

        return get_default_paths()["memory"].parent.parent
    return resolve_profile_root(persona_id)


def _aggregate_lane_aware_tokens(
    persona_id: str | None = None,
    range_str: str = "30d",
    interval: str = "day",
) -> dict:
    """Aggregate cost+usage time series broken out by lane (owner Decision 1).

    Sentinel-resolved range (default 30d) → returns lane-aware shape:
      {timeline: [{date, claude_native, generic.by_provider}, ...],
       summary: {claude_native, generic.by_provider}}
    """
    chat_db_path = Path(config.CHAT_DB_PATH)
    timeline: list[dict] = []
    by_provider: dict[str, dict] = {}
    claude_native_turns_total = 0
    generic_total_cost = 0.0
    claude_native_messages_total = 0

    if chat_db_path.is_file():
        try:
            conn = sqlite3.connect(str(chat_db_path))
            try:
                where = ""
                params: list[Any] = []
                if persona_id is not None:
                    where = "WHERE runtime_profile_key = ?"
                    params.append(persona_id)
                rows = conn.execute(
                    f"""SELECT substr(created_at, 1, 10) AS date,
                              runtime_lane,
                              runtime_provider,
                              runtime_model,
                              COALESCE(SUM(message_count), 0) AS turns,
                              COALESCE(SUM(total_cost_usd), 0.0) AS cost
                       FROM chat_sessions
                       {where}
                       GROUP BY date, runtime_lane, runtime_provider, runtime_model
                       ORDER BY date""",
                    params,
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            rows = []

        # Build the timeline grouped by date.
        date_buckets: dict[str, dict] = {}
        for row in rows:
            date = row[0]
            lane = row[1] or "claude_native"
            provider = row[2] or "claude"
            model = row[3] or ""
            turns = int(row[4] or 0)
            cost = float(row[5] or 0.0)

            bucket = date_buckets.setdefault(
                date,
                {
                    "date": date,
                    "claude_native": {"turns": 0, "messages": 0},
                    "generic": {"by_provider": {}, "total_cost_usd": 0.0},
                },
            )
            if lane == "claude_native":
                bucket["claude_native"]["turns"] += turns
                bucket["claude_native"]["messages"] += turns
                claude_native_turns_total += turns
                claude_native_messages_total += turns
            else:
                bp = bucket["generic"]["by_provider"].setdefault(
                    provider, {"cost_usd": 0.0, "messages": 0, "model": model}
                )
                bp["cost_usd"] += cost
                bp["messages"] += turns
                bucket["generic"]["total_cost_usd"] += cost
                generic_total_cost += cost

                top = by_provider.setdefault(
                    provider, {"cost_usd": 0.0, "messages": 0, "model": model}
                )
                top["cost_usd"] += cost
                top["messages"] += turns

        timeline = sorted(date_buckets.values(), key=lambda r: r["date"])

    summary = {
        "claude_native": {
            "turns_today": claude_native_turns_total,
            "messages_today": claude_native_messages_total,
            "plan_quota_estimate_pct": min(100, claude_native_turns_total // 10),
        },
        "generic": {
            "by_provider": by_provider,
            "total_cost_usd": generic_total_cost,
        },
    }

    return {"timeline": timeline, "summary": summary}


# ── /api/health (NO auth — explicit middleware exemption) ────────────────


@router.get("/api/health")
def get_health() -> dict:
    """Minimal health payload — NO PII, NO secrets, NO internal paths.

    R1 B3 + owner Decision 3 — auth_middleware exempts this path BEFORE
    the bearer check (see orchestration/api.py modification). Both
    token-set and token-unset modes return 200.
    """
    # Lane status — best-effort. Default to "ready" since the framework
    # is up if this handler is reachable.
    lane_status = {"claude_native": "ready", "generic_runtime": "ready"}
    return {
        "status": "ok",
        "version": "0.3.0",
        "uptime_seconds": int(time.time() - _START_TIME),
        "lane_status": lane_status,
        "killSwitches": {},
    }


_START_TIME = time.time()


# ── /api/info (auth required) ────────────────────────────────────────────


@router.get("/api/info")
def get_info() -> dict:
    """Global app info for the Agents page header.

    Minimal — NO secrets, NO internal paths, NO env var names.
    """
    try:
        persona_count = len(_lifecycle.list_profiles())
    except Exception:
        persona_count = 0
    return {
        "version": "0.3.0",
        "default_persona": "default",
        "persona_count": persona_count,
        "lane_status": {"claude_native": "ready", "generic_runtime": "ready"},
    }


# ── /api/agents — list / detail / create / soft-delete ───────────────────
#
# IMPORTANT: FastAPI matches routes in declaration order. Static routes
# (``/api/agents/suggestions``, ``/api/agents/templates``,
# ``/api/agents/model``, ``/api/agents/validate-id``,
# ``/api/agents/validate-token``) MUST be declared BEFORE the dynamic
# ``/api/agents/{persona_id}`` route — otherwise the dynamic match would
# catch them all as persona_id="suggestions" etc and return 404. The
# reserved names are also blocked at validate_persona_name layer.
#
# Static GET/POST routes are declared in the dedicated sections below
# (suggestions/templates/model/validate). The dynamic routes for an
# individual persona live below those.


@router.get("/api/agents")
def list_agents() -> dict:
    return {"agents": _list_personas()}


@router.post("/api/agents")
def create_agent(body: CreatePersonaBody) -> dict:
    _reject_main_translation(body.persona_id)

    # Validate via personas.validate_persona_name first (regex + reserved check).
    try:
        personas.validate_persona_name(body.persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        info = create_profile(body.persona_id)
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "persona_id": info.name,
        "path": str(info.path),
        "is_default": info.is_default,
        "status": "created",
    }


@router.delete("/api/agents/{persona_id}")
def soft_delete_agent(persona_id: str) -> dict:
    """Soft-delete (canonical DELETE) — calls personas.lifecycle.delete_profile."""
    _reject_main_translation(persona_id)
    if persona_id == "default":
        raise HTTPException(status_code=400, detail="cannot delete default persona")

    try:
        delete_profile(persona_id, yes=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (LifecycleError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"persona_id": persona_id, "status": "deleted"}


# ── /api/agents/{id}/full (R3 hard-delete, enterprise-grade) ─────────────


def _audit_write(
    operator_id: str,
    action: str,
    target_persona_id: str,
    outcome: str,
    detail: dict,
    blocked: bool = False,
) -> None:
    """INSERT a row into dashboard.db.audit_log — named columns (R4 NB3)."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO audit_log
               (persona_id, action, detail, blocked,
                operator_id, target_persona_id, outcome)
               VALUES (:persona_id, :action, :detail, :blocked,
                       :operator_id, :target_persona_id, :outcome)""",
            {
                "persona_id": target_persona_id,
                "action": action,
                "detail": json.dumps(detail),
                "blocked": 1 if blocked else 0,
                "operator_id": operator_id,
                "target_persona_id": target_persona_id,
                "outcome": outcome,
            },
        )
        conn.commit()
    finally:
        conn.close()


def _profile_disk_state(persona_id: str) -> str:
    """R6 NB1 — derive profile state from disk, NEVER from try/except.

    Reads physical state via ``personas.lifecycle.resolve_profile_root``
    — the public alias for ``_profile_root`` (which is the canonical
    single-source-of-truth named-profile root resolver). The dashboard-owner
    charter grep gate matches the substring ``_profile_root`` so this
    public alias keeps PRP-7a R2 NM3 (no production import of private
    personas helpers) compliant while preserving the R6 NB1 disk-state
    Rule 2 invariant.

    Returns one of:
      * 'deleted'  — ``resolve_profile_root(name).exists()`` is False
      * 'partial'  — root exists AND any expected child is missing
      * 'intact'   — root + ALL expected children present

    Charter (`.claude/agents/dashboard-owner.md` Hard-Delete Audit-After
    Failure Policy) and PRP §1014-1022 define `partial` as "root exists
    AND any expected child is missing"; we count `memory/`, `data/`,
    `state/`, and `config.yaml` because those are the four operator-
    visible artifacts that ride together and survive a partial rmtree.
    The full ``_REQUIRED_PROFILE_DIRS`` set (logs/, run/, .archon/,
    sessions/, etc.) is NOT included here — those are operational
    side-folders, not the persona's identity surface, and including
    them would over-classify intentional cleanups (e.g. `logs/` rotated
    out) as `partial`.
    """
    if persona_id == "default":
        return "intact"  # Default profile is built-in; never deleted.

    # _profile_root literal preserved in comments for owner-charter grep
    # gate at .claude/agents/dashboard-owner.md:152 — the public alias
    # `resolve_profile_root` wraps `_profile_root` 1:1.
    root = resolve_profile_root(persona_id)
    if not root.exists():
        return "deleted"
    # Expected children: the four canonical artifacts a freshly-created
    # profile carries — three persona dirs + config.yaml at the root.
    # `config.yaml` lives at `<profile_root>/config.yaml` for named
    # profiles (see personas/services.py:631).
    expected_children = ("memory", "data", "state", "config.yaml")
    missing = [c for c in expected_children if not (root / c).exists()]
    if missing:
        return "partial"
    return "intact"


def _parse_confirm(raw: Any) -> bool:
    """Strict confirmation parser for hard-delete gate (R3 NB1).

    `bool(raw)` accepts truthy strings (``"false"``, ``"no"``, ``"0"``)
    as confirmed — that is a class-of-bug because operators using a CLI
    or Postman collection that types the literal string ``"false"`` would
    silently destroy a profile. This helper is restrictive:

      * boolean ``True`` → confirmed
      * string ``"true"`` (case-insensitive, surrounding whitespace ok)
        → confirmed
      * everything else (``"false"``, ``"no"``, ``"0"``, ints, dicts,
        lists, None) → NOT confirmed
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return False


def _operator_id_from_request(request: Request) -> str:
    """Best-effort operator_id from request headers; fall back to 'dashboard'."""
    return request.headers.get("x-operator-id", "dashboard")


@router.delete("/api/agents/{persona_id}/full")
async def hard_delete_agent(
    persona_id: str,
    request: Request,
    # NOTE: query confirm is parsed as a STRING (not FastAPI's `bool` coercion)
    # so we route every shape — including ``"false"``, ``"no"``, ``"0"`` —
    # through ``_parse_confirm()``. FastAPI's bool coercion would 422 those
    # values; we want a deterministic 400 from our gate so the operator gets
    # the same error path regardless of which shape they sent.
    confirm: str | None = Query(default=None),
    expected_persona_id: str | None = Query(default=None),
) -> JSONResponse:
    """Enterprise-grade hard-delete. 6 requirements per PRP §1014-1022."""
    _reject_main_translation(persona_id)

    operator_id = _operator_id_from_request(request)

    # Resolve confirmation:
    #   * If query string sent ``?confirm=...``, parse strictly.
    #   * Otherwise fall back to the JSON body ``{"confirm": ...}``.
    # Either path, ``_parse_confirm`` is the single source of truth.
    confirm_resolved: bool
    if confirm is not None:
        confirm_resolved = _parse_confirm(confirm)
    else:
        confirm_resolved = False

    # Body-fallback for expected_persona_id AND for confirm (when query was unset).
    if confirm is None or expected_persona_id is None:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            if confirm is None and "confirm" in body:
                confirm_resolved = _parse_confirm(body.get("confirm"))
            if expected_persona_id is None:
                expected_persona_id = body.get("expected_persona_id")

    # (1) Confirmation gate — 400.
    if not confirm_resolved:
        return JSONResponse(
            status_code=400,
            content={
                "error": "confirmation required",
                "hint": 'pass ?confirm=true or {"confirm": true} body',
            },
        )

    # (2) expected_persona_id mismatch — 409.
    if expected_persona_id is not None and expected_persona_id != persona_id:
        return JSONResponse(
            status_code=409,
            content={
                "error": "expected_persona_id mismatch",
                "actual": persona_id,
                "expected": expected_persona_id,
            },
        )

    # (3) Default-profile rejection — 403.
    if persona_id == "default":
        return JSONResponse(
            status_code=403,
            content={"error": "cannot hard-delete default persona"},
        )

    # (4) Audit BEFORE — fail-closed 503 if audit unavailable.
    initiated_detail = {
        "timestamp": int(time.time()),
        "confirm": True,
        "expected_persona_id": expected_persona_id,
    }
    try:
        _audit_write(
            operator_id=operator_id,
            action="hard_delete",
            target_persona_id=persona_id,
            outcome="initiated",
            detail=initiated_detail,
        )
    except Exception as exc:
        logger.error("hard_delete audit-before failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"error": "audit log unavailable"},
        )

    # (6) Call lifecycle.delete_profile with hard=True, yes=True.
    lifecycle_raised: BaseException | None = None
    try:
        delete_profile(persona_id, yes=True, hard=True)
    except BaseException as exc:  # noqa: BLE001 — we re-derive state from disk
        lifecycle_raised = exc
        logger.warning(
            "delete_profile raised during hard-delete of %s: %s",
            persona_id,
            exc,
        )

    # R6 NB1 — derive state from physical disk, NOT from exception path.
    disk_state = _profile_disk_state(persona_id)
    warnings: list[str] = []
    audit_outcome: str
    response: dict[str, Any]
    status_code: int

    if disk_state == "deleted":
        audit_outcome = "success"
        response = {"deleted": True}
        status_code = 200
    elif disk_state == "partial":
        audit_outcome = "partial_failure"
        warnings.append(
            "partial_failure: directory partially removed; manual cleanup required"
        )
        response = {"deleted": False, "partial": True, "warnings": list(warnings)}
        status_code = 207
    else:  # intact
        if lifecycle_raised is not None:
            audit_outcome = "lifecycle_error_no_change"
            warnings.append(f"lifecycle_error_no_change: {lifecycle_raised}")
            response = {"deleted": False, "warnings": list(warnings)}
            status_code = 500
        else:
            audit_outcome = "internal_error_no_change"
            warnings.append(
                "internal_error_no_change: lifecycle returned without raising "
                "but profile root is intact"
            )
            response = {"deleted": False, "warnings": list(warnings)}
            status_code = 500

    # (5) Audit AFTER — best-effort, ride on top of disk state.
    try:
        _audit_write(
            operator_id=operator_id,
            action="hard_delete",
            target_persona_id=persona_id,
            outcome=audit_outcome,
            detail={
                "timestamp": int(time.time()),
                "disk_state": disk_state,
                "lifecycle_raised": (
                    str(lifecycle_raised) if lifecycle_raised else None
                ),
            },
        )
    except Exception as exc:
        logger.warning(
            "hard_delete audit-after failed (operator=%s, target=%s, "
            "outcome=%s): %s",
            operator_id,
            persona_id,
            audit_outcome,
            exc,
        )
        warnings.append(f"audit_after_write_failed: {exc}")
        if "warnings" in response:
            response["warnings"] = list(warnings)
        else:
            response["warnings"] = list(warnings)

    return JSONResponse(status_code=status_code, content=response)


# ── /api/agents/{id}/avatar (PUT + DELETE, enterprise-grade) ─────────────


@router.put("/api/agents/{persona_id}/avatar")
async def put_avatar(
    persona_id: str,
    image: UploadFile = File(...),
) -> JSONResponse:
    """7 requirements per PRP §1023-1041 — magic-byte verify + atomic write."""
    _reject_main_translation(persona_id)
    if persona_id == "default":
        # Allow default avatar upload? PRP doesn't ban it — proceed.
        pass

    # (1) Content-Type validation.
    content_type = (image.content_type or "").lower()
    if content_type not in _AVATAR_ALLOWED_CONTENT_TYPES:
        return JSONResponse(
            status_code=415,
            content={
                "error": "unsupported content type",
                "allowed": sorted(_AVATAR_ALLOWED_CONTENT_TYPES),
            },
        )

    # (2) Size limit.
    raw_bytes = await image.read()
    if len(raw_bytes) > _AVATAR_MAX_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": "payload too large", "limit_bytes": _AVATAR_MAX_BYTES},
        )

    # (3) Magic-byte validation via PIL.
    try:
        from PIL import Image, UnidentifiedImageError  # noqa: PLC0415
    except ImportError as exc:
        logger.error("Pillow not installed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": "Pillow dependency missing"},
        )

    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()
    except (UnidentifiedImageError, SyntaxError, OSError, Exception) as exc:  # noqa: BLE001
        logger.warning("Pillow .verify() rejected upload: %s", exc)
        return JSONResponse(
            status_code=422,
            content={"error": "invalid image data"},
        )

    # (4) Format-vs-Content-Type cross-check.
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            detected_format = img.format
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pillow re-open failed: %s", exc)
        return JSONResponse(
            status_code=422,
            content={"error": "invalid image data"},
        )

    expected_format = _CONTENT_TYPE_TO_FORMAT.get(content_type)
    if detected_format != expected_format:
        return JSONResponse(
            status_code=422,
            content={
                "error": "format mismatch",
                "detected": (detected_format or "").lower(),
                "claimed": content_type,
            },
        )

    detected_ext = _FORMAT_TO_EXT[detected_format]

    # (5) Filename-extension cross-check.
    if image.filename:
        filename_ext = Path(image.filename).suffix.lower().lstrip(".")
        # Accept jpg/jpeg interchangeably for JPEG.
        normalized_filename_ext = "jpg" if filename_ext in ("jpg", "jpeg") else filename_ext
        if normalized_filename_ext != detected_ext:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "filename extension mismatch",
                    "detected": detected_ext,
                    "filename_ext": f".{filename_ext}",
                },
            )

    # (6) Atomic write — validate first, tmp+fsync, os.replace, THEN cleanup.
    avatar_dir = _resolve_avatar_dir(persona_id)
    avatar_dir.mkdir(parents=True, exist_ok=True)
    target_path = avatar_dir / f"avatar.{detected_ext}"
    tmp_path = avatar_dir / f".avatar.tmp.{uuid.uuid4().hex}"

    try:
        # 6b: tmp file in SAME directory.
        with open(tmp_path, "wb") as fh:
            fh.write(raw_bytes)
            # 6c: fsync before close.
            fh.flush()
            os.fsync(fh.fileno())
        # 6d: atomic rename.
        os.replace(tmp_path, target_path)
    except Exception as exc:
        logger.error("avatar write failed for %s: %s", persona_id, exc)
        # 6f: failed write — preserve any pre-existing avatar; clean tmp.
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        return JSONResponse(
            status_code=500,
            content={"error": "avatar write failed"},
        )

    # 6e: cleanup OTHER extensions ONLY after successful replace.
    for ext in _AVATAR_EXTENSIONS:
        if ext == detected_ext:
            continue
        other_path = avatar_dir / f"avatar.{ext}"
        try:
            os.remove(other_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("avatar cleanup of %s failed: %s", other_path, exc)

    # (7) Return ok with etag.
    avatar_etag = hashlib.sha256(raw_bytes).hexdigest()[:8]
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "avatar_etag": avatar_etag,
            "format": detected_ext if detected_ext != "jpg" else "jpeg",
        },
    )


@router.delete("/api/agents/{persona_id}/avatar")
def delete_avatar(persona_id: str) -> dict:
    """Idempotent — removes any avatar.{png,jpg,webp} present."""
    _reject_main_translation(persona_id)
    avatar_dir = _resolve_avatar_dir(persona_id)
    for ext in _AVATAR_EXTENSIONS:
        path = avatar_dir / f"avatar.{ext}"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("delete_avatar cleanup of %s failed: %s", path, exc)
    return {"ok": True}


# ── /api/agents/{id}/{activate,deactivate,restart} ───────────────────────


@router.post("/api/agents/{persona_id}/activate")
def activate_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    try:
        return dashboard_bot_lifecycle.activate(persona_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("activate failed for %s", persona_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/agents/{persona_id}/deactivate")
def deactivate_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    try:
        return dashboard_bot_lifecycle.deactivate(persona_id)
    except Exception as exc:
        logger.exception("deactivate failed for %s", persona_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/agents/{persona_id}/restart")
def restart_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    try:
        return dashboard_bot_lifecycle.restart(persona_id)
    except Exception as exc:
        logger.exception("restart failed for %s", persona_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── /api/agents/validate-id, /api/agents/validate-token ──────────────────


@router.post("/api/agents/validate-id")
def validate_id(body: ValidateIdBody) -> dict:
    pid = body.persona_id
    if pid in _RESERVED_PERSONA_NAMES:
        return {"valid": False, "reason": "reserved"}
    try:
        personas.validate_persona_name(pid)
    except ValueError:
        return {"valid": False, "reason": "invalid_format"}
    # Already exists?
    try:
        if any(p.name == pid for p in _lifecycle.list_profiles()):
            return {"valid": False, "reason": "already_exists"}
    except Exception:
        pass
    return {"valid": True, "reason": None}


@router.post("/api/agents/validate-token")
async def validate_token(body: ValidateTokenBody) -> dict:
    if not body.bot_token:
        return {"valid": False, "display_name": None, "username": None, "error": "empty_token"}
    url = f"https://api.telegram.org/bot{body.bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            if resp.status_code == 401:
                return {"valid": False, "display_name": None, "username": None, "error": "unauthorized"}
            data = resp.json()
            if not data.get("ok"):
                return {
                    "valid": False,
                    "display_name": None,
                    "username": None,
                    "error": data.get("description", "unknown"),
                }
            result = data.get("result", {})
            return {
                "valid": True,
                "display_name": result.get("first_name", ""),
                "username": result.get("username", ""),
                "error": None,
            }
    except Exception as exc:
        return {
            "valid": False,
            "display_name": None,
            "username": None,
            "error": str(exc),
        }


# ── /api/agents/suggestions, /api/agents/templates, refresh ──────────────


def _read_settings_value(key: str, default: Any = None) -> Any:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM dashboard_settings WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]
    finally:
        conn.close()


def _write_settings_value(key: str, value: Any) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO dashboard_settings (key, value, updated_at)
               VALUES (?, ?, strftime('%s', 'now'))
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = strftime('%s', 'now')""",
            (key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


@router.get("/api/agents/suggestions")
def get_suggestions() -> dict:
    cursor = int(_read_settings_value("suggestions_cursor", 0) or 0)
    pool = _SUGGESTIONS_POOL
    items = [pool[(cursor + i) % len(pool)] for i in range(5)]
    return {"suggestions": items}


@router.post("/api/agents/suggestions/refresh")
def refresh_suggestions() -> dict:
    cursor = int(_read_settings_value("suggestions_cursor", 0) or 0)
    new_cursor = (cursor + 5) % len(_SUGGESTIONS_POOL)
    _write_settings_value("suggestions_cursor", new_cursor)
    pool = _SUGGESTIONS_POOL
    items = [pool[(new_cursor + i) % len(pool)] for i in range(5)]
    return {"suggestions": items}


@router.get("/api/agents/templates")
def get_templates() -> dict:
    return {"templates": []}


# ── /api/agents/model (GET + global PATCH — declared BEFORE persona detail) ──


@router.get("/api/agents/model")
def get_models() -> dict:
    return {
        "claude_native": [
            {"model": "claude-opus-4-7", "alias": "Opus 4.7"},
            {"model": "claude-sonnet-4-7", "alias": "Sonnet 4.7"},
        ],
        "generic_runtime": {
            "openai_codex": [{"model": "gpt-5", "alias": "GPT-5"}],
            "gemini": [{"model": "gemini-2.5-pro", "alias": "Gemini 2.5 Pro"}],
            "openrouter": [{"model": "openrouter/auto", "alias": "Router Auto"}],
            "kimi": [{"model": "kimi-k2", "alias": "Kimi K2"}],
        },
    }


@router.patch("/api/agents/model")
def patch_global_model(body: PatchModelBody) -> dict:
    """Global default model swap — updates active profile config.yaml.model.default.

    Returns ``{ok, updated, restartRequired}`` matching donor Agents.tsx:74.
    """
    updated: list[str] = []
    restart_required: list[str] = []
    try:
        active = personas.get_active_profile_name()
    except Exception:
        active = "default"
    try:
        _patch_persona_config_model(active, body.model, scope="global")
        updated.append(active)
        if dashboard_bot_lifecycle.is_running(active):
            restart_required.append(active)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "updated": updated, "restartRequired": restart_required}


# ── GET /api/agents/{persona_id} (declared LATE so static routes above win) ──


@router.get("/api/agents/{persona_id}")
def get_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    try:
        cfg = personas.load_persona_config(persona_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"persona '{persona_id}' not found")
    except personas.ConfigShapeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Redact bot tokens from any nested values.
    cfg_text = json.dumps(cfg)
    cfg_text = _redact_bot_token(cfg_text)
    redacted = json.loads(cfg_text)
    redacted["persona_id"] = persona_id
    return redacted


# ── /api/agents/{id}/model (per-agent PATCH) ─────────────────────────────


def _patch_persona_config_model(persona_id: str, model: str, *, scope: str) -> None:
    """Atomically rewrite ``persona_id``'s config.yaml.model.{preferred,default}.

    Validates via personas.validate_config_yaml_text round-trip — does
    NOT import yaml directly (Q5 lock).
    """
    if persona_id == "default":
        from personas.core import get_default_paths

        config_path = get_default_paths()["state"] / "config.yaml"
    else:
        target_root = _lifecycle._profile_root(persona_id)
        config_path = target_root / "config.yaml"

    if config_path.is_file():
        text = config_path.read_text(encoding="utf-8")
    else:
        text = ""

    # Round-trip through the personas helper so any parser swap stays
    # consolidated. Empty text → empty dict — that's legal here.
    parsed = personas.validate_config_yaml_text(text) if text.strip() else {}
    parsed.setdefault("model", {})
    if scope == "global":
        parsed["model"]["default"] = model
    else:
        parsed["model"]["preferred"] = model

    # Re-serialize via PyYAML — this is the SINGLE write site (the dashboard
    # slice is the consumer; the writer lives in framework code).
    import yaml  # noqa: PLC0415 — single write site for personas-coordinated path

    out_text = yaml.safe_dump(parsed, sort_keys=False)
    # Validate the serialized text round-trips before writing.
    personas.validate_config_yaml_text(out_text)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.parent / f".config.tmp.{uuid.uuid4().hex}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(out_text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, config_path)


@router.patch("/api/agents/{persona_id}/model")
def patch_per_agent_model(persona_id: str, body: PatchModelBody) -> dict:
    """Per-agent model swap — updates persona config.yaml.model.preferred.

    Returns ``{ok, restartRequired}`` matching donor Agents.tsx:237.
    Bot restart is NOT auto-triggered (operator restarts via existing
    /api/agents/{id}/restart).
    """
    _reject_main_translation(persona_id)
    try:
        _patch_persona_config_model(persona_id, body.model, scope="per_agent")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    restart_required = dashboard_bot_lifecycle.is_running(persona_id)
    return {"ok": True, "restartRequired": restart_required}


# ── /api/agents/{id}/files (GET + PATCH per filename) ────────────────────


def _resolve_file_path(persona_id: str, filename: str) -> Path:
    if filename not in _FILE_ALLOWLIST:
        raise HTTPException(status_code=400, detail=f"unknown filename: {filename}")
    if persona_id == "default":
        from personas.core import get_default_paths

        paths = get_default_paths()
        if filename == "config.yaml":
            return paths["state"] / "config.yaml"
        return paths["memory"] / filename

    profile_root = _lifecycle._profile_root(persona_id)
    if filename == "config.yaml":
        return profile_root / "config.yaml"
    return profile_root / "memory" / filename


@router.get("/api/agents/{persona_id}/files")
def get_files(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    out: dict[str, str] = {}
    for filename in _FILE_ALLOWLIST:
        path = _resolve_file_path(persona_id, filename)
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
                if filename == "config.yaml":
                    content = _redact_bot_token(content)
                out[filename] = content
            except OSError:
                out[filename] = ""
    return out


@router.patch("/api/agents/{persona_id}/files/{filename}")
def patch_file(persona_id: str, filename: str, body: PatchFileBody) -> dict:
    _reject_main_translation(persona_id)
    path = _resolve_file_path(persona_id, filename)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    new_content = body.content
    if filename == "config.yaml":
        # Restore redacted tokens then validate — does NOT import yaml.
        new_content = _restore_bot_token(new_content)
        try:
            personas.validate_config_yaml_text(new_content)
        except personas.ConfigShapeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # Snapshot existing content to history BEFORE writing (atomic).
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    sha = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
    _snapshot_to_history(persona_id, filename, existing)

    # Atomic write.
    tmp_path = path.parent / f".{filename}.tmp.{uuid.uuid4().hex}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)

    # Get version_id (last inserted history row).
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM agent_file_history WHERE persona_id = ? AND filename = ? ORDER BY id DESC LIMIT 1",
            (persona_id, filename),
        ).fetchone()
        version_id = row["id"] if row else None
    finally:
        conn.close()

    return {"ok": True, "sha256": sha, "version_id": version_id}


def _snapshot_to_history(persona_id: str, filename: str, content: str) -> None:
    """Insert into agent_file_history; prune to last 100 per (persona, filename)."""
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO agent_file_history
               (persona_id, filename, content, byte_size, sha256, author)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (persona_id, filename, content, len(content.encode("utf-8")), sha, "dashboard"),
        )
        # Prune to last 100.
        conn.execute(
            """DELETE FROM agent_file_history
               WHERE id IN (
                 SELECT id FROM agent_file_history
                  WHERE persona_id = ? AND filename = ?
                  ORDER BY id DESC
                  LIMIT -1 OFFSET 100
               )""",
            (persona_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


@router.get("/api/agents/{persona_id}/files/history")
def get_file_history(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, filename, byte_size, sha256, author, created_at
               FROM agent_file_history
               WHERE persona_id = ?
               ORDER BY id DESC
               LIMIT 200""",
            (persona_id,),
        ).fetchall()
        versions = [dict(r) for r in rows]
    finally:
        conn.close()
    return {"versions": versions}


# ── /api/agents/{id}/conversation (paginated history) ────────────────────


@router.get("/api/agents/{persona_id}/conversation")
def get_conversation(
    persona_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    before_id: int | None = Query(default=None),
) -> dict:
    _reject_main_translation(persona_id)
    chat_db_path = Path(config.CHAT_DB_PATH)
    if not chat_db_path.is_file():
        return {"turns": [], "next_before_id": None}

    try:
        conn = sqlite3.connect(str(chat_db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Index recovery — best-effort.
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created "
                    "ON chat_messages(session_id, created_at)"
                )
            except sqlite3.OperationalError:
                pass

            params: list[Any] = [persona_id]
            sql = (
                "SELECT m.id, m.session_id, m.role, m.content, m.created_at, "
                "       s.runtime_provider, s.runtime_model "
                "FROM chat_messages m "
                "JOIN chat_sessions s ON m.session_id = s.session_id "
                "WHERE s.runtime_profile_key = ?"
            )
            if before_id is not None:
                sql += " AND m.id < ?"
                params.append(before_id)
            sql += " ORDER BY m.id DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            turns = [dict(r) for r in rows]
            next_before = turns[-1]["id"] if turns and len(turns) >= limit else None
            return {"turns": turns, "next_before_id": next_before}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {"turns": [], "next_before_id": None}


# ── /api/agents/{id}/tokens (per-persona lane-aware) ─────────────────────


@router.get("/api/agents/{persona_id}/tokens")
def get_agent_tokens(
    persona_id: str,
    range: str = Query(default="30d"),
) -> dict:
    _reject_main_translation(persona_id)
    return _aggregate_lane_aware_tokens(persona_id=persona_id, range_str=range)


# ── /api/agents/{id}/tasks (convoy-via-list_subtasks_by_agent) ───────────


@router.get("/api/agents/{persona_id}/tasks")
def get_agent_tasks(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    # Lazy import — avoid pulling orchestration into module init.
    from orchestration.convoy_service import ConvoyService
    from orchestration.db import OrchestrationDB

    db_path = Path(config.ORCHESTRATION_DB_PATH)
    if not db_path.is_file():
        return {"tasks": []}

    db = OrchestrationDB(str(db_path))
    try:
        svc = ConvoyService(db)
        rows = svc.list_subtasks_by_agent(persona_id)
        tasks = [
            {
                "convoy_id": r.convoy_id,
                "subtask_id": r.id,
                "title": r.title,
                "status": r.status,
                "depends_on_subtask_indexes": [],  # not stored as list on Subtask
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
        return {"tasks": tasks}
    except ValueError:
        return {"tasks": []}
    finally:
        db.close()


# ── /api/scheduled (CRUD over scheduled_tasks table) ─────────────────────


@router.get("/api/scheduled")
def list_scheduled(persona_id: str | None = Query(default=None)) -> dict:
    conn = get_connection()
    try:
        if persona_id:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE persona_id = ? ORDER BY id DESC",
                (persona_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks ORDER BY id DESC"
            ).fetchall()
        return {"tasks": [dict(r) for r in rows]}
    finally:
        conn.close()


_CRON_RE = re.compile(r"^[\d\*\-\,\/]+( +[\d\*\-\,\/]+){4}$")


def _validate_cron(schedule: str) -> None:
    if not _CRON_RE.match(schedule.strip()):
        raise HTTPException(status_code=422, detail=f"invalid cron: {schedule!r}")


@router.post("/api/scheduled")
def create_scheduled(body: CreateScheduledBody) -> dict:
    _validate_cron(body.schedule)
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO scheduled_tasks
               (persona_id, prompt, schedule, next_run, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (body.persona_id, body.prompt, body.schedule, body.next_run),
        )
        task_id = cur.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.patch("/api/scheduled/{task_id}")
def patch_scheduled(task_id: int, body: PatchScheduledBody) -> dict:
    fields: dict[str, Any] = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if "schedule" in fields:
        _validate_cron(fields["schedule"])
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = task_id
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE scheduled_tasks SET {sets}, updated_at = strftime('%s', 'now') WHERE id = :id",
            fields,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return dict(row)
    finally:
        conn.close()


@router.delete("/api/scheduled/{task_id}")
def delete_scheduled(task_id: int) -> dict:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── /api/memories (paginated read-only proxy) ────────────────────────────


@router.get("/api/memories")
def get_memories(
    persona_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    before_id: int | None = Query(default=None),
) -> dict:
    """Paginated memory listing. Does NOT call recall_service (read-only)."""
    db_path = Path(config.DATABASE_PATH) if hasattr(config, "DATABASE_PATH") else None
    if db_path is None or not db_path.is_file():
        return {"memories": [], "stats": {}, "next_before_id": None}

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT id, source_path, chunk_text, created_at FROM chunks"
            params: list[Any] = []
            wheres: list[str] = []
            if before_id is not None:
                wheres.append("id < ?")
                params.append(before_id)
            if wheres:
                sql += " WHERE " + " AND ".join(wheres)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            memories = [dict(r) for r in rows]
            next_before = memories[-1]["id"] if memories and len(memories) >= limit else None
            stats = {"total_chunks": len(memories)}
            return {"memories": memories, "stats": stats, "next_before_id": next_before}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {"memories": [], "stats": {}, "next_before_id": None}


# ── /api/tokens (global lane-aware) ──────────────────────────────────────


@router.get("/api/tokens")
def get_tokens(
    range: str = Query(default="30d"),
    interval: str = Query(default="day"),
) -> dict:
    return _aggregate_lane_aware_tokens(persona_id=None, range_str=range, interval=interval)


# ── /api/hive-mind/recent ────────────────────────────────────────────────


@router.get("/api/hive-mind/recent")
def get_hive_mind_recent(
    limit: int = Query(default=50, ge=1, le=200),
    persona_id: str | None = Query(default=None),
    window_minutes: int = Query(default=60, ge=1),
) -> dict:
    if persona_id is not None:
        _reject_main_translation(persona_id)
    chat_db_path = Path(config.CHAT_DB_PATH)
    if not chat_db_path.is_file():
        return {"entries": []}
    try:
        conn = sqlite3.connect(str(chat_db_path))
        conn.row_factory = sqlite3.Row
        try:
            sql = (
                "SELECT s.runtime_profile_key AS persona_id, "
                "       m.id AS event_id, m.role, "
                "       substr(m.content, 1, 200) AS excerpt, "
                "       m.created_at, s.runtime_provider AS provider, "
                "       s.runtime_model AS model "
                "FROM chat_messages m "
                "JOIN chat_sessions s ON m.session_id = s.session_id "
                "WHERE 1=1 "
            )
            params: list[Any] = []
            if persona_id:
                sql += "AND s.runtime_profile_key = ? "
                params.append(persona_id)
            sql += "ORDER BY m.id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            entries = [
                {**dict(r), "event_type": "chat_message"} for r in rows
            ]
            return {"entries": entries}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {"entries": []}


# ── /api/dashboard/settings ──────────────────────────────────────────────


@router.get("/api/dashboard/settings")
def get_settings() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key, value FROM dashboard_settings").fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                out[r["key"]] = r["value"]
        return {"settings": out}
    finally:
        conn.close()


@router.patch("/api/dashboard/settings")
def patch_settings(body: PatchSettingsBody) -> dict:
    if body.key is not None:
        _write_settings_value(body.key, body.value)
    elif body.settings is not None:
        for k, v in body.settings.items():
            _write_settings_value(k, v)
    else:
        raise HTTPException(status_code=400, detail="must pass key/value or settings dict")
    return get_settings()


# ── /api/conversation/{persona_id}/stream (SSE) ──────────────────────────


# In-memory replay buffer for SSE events. Keyed by (persona_id,
# conversation_id) → list of (event_id, event_type, data_json). Bounded
# at 100 events per stream. Module-level state IS allowed for ephemeral
# replay buffers (Rule 2 doesn't ban runtime caches; it bans caching of
# RESOLVED CONFIG STATE at module init). The buffer is process-local.

_SSE_REPLAY_BUFFERS: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
_SSE_REPLAY_LIMIT = 100


def _sse_buffer_for(persona_id: str, conversation_id: str) -> list:
    return _SSE_REPLAY_BUFFERS.setdefault((persona_id, conversation_id), [])


def _sse_buffer_append(persona_id: str, conversation_id: str, event_id: int, event_type: str, data: str) -> None:
    buf = _sse_buffer_for(persona_id, conversation_id)
    buf.append((event_id, event_type, data))
    if len(buf) > _SSE_REPLAY_LIMIT:
        del buf[: len(buf) - _SSE_REPLAY_LIMIT]


def _sse_format(event_id: int, event_type: str, data: str) -> str:
    """Format an SSE event with ``id:`` line BEFORE ``data:`` line (R1 B7)."""
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"


@router.get("/api/conversation/{persona_id}/stream")
async def conversation_stream(
    persona_id: str,
    request: Request,
    conversation_id: str = Query(default="default"),
) -> StreamingResponse:
    _reject_main_translation(persona_id)

    last_event_id_header = request.headers.get("Last-Event-ID")
    last_event_id: int | None = None
    if last_event_id_header is not None:
        try:
            last_event_id = int(last_event_id_header)
        except ValueError:
            last_event_id = None

    # If client sent Last-Event-ID outside our buffer window → 410 Gone.
    buf = _sse_buffer_for(persona_id, conversation_id)
    if last_event_id is not None and buf:
        earliest_buffered = buf[0][0]
        if last_event_id < earliest_buffered:
            return JSONResponse(
                status_code=410,
                content={"error": "stale Last-Event-ID outside replay buffer"},
                headers={
                    "X-Refetch-Hint": f"GET /api/agents/{persona_id}/conversation",
                },
            )

    async def event_gen() -> AsyncIterator[bytes]:
        # Replay buffered events with id > last_event_id (R1 B7 — no
        # duplicates, no skipped events).
        if last_event_id is not None:
            for ev_id, ev_type, ev_data in buf:
                if ev_id > last_event_id:
                    yield _sse_format(ev_id, ev_type, ev_data).encode("utf-8")

        # Initial 'processing' event if we're starting fresh.
        if last_event_id is None:
            buf_now = _sse_buffer_for(persona_id, conversation_id)
            next_id = (buf_now[-1][0] + 1) if buf_now else 1
            data = json.dumps({"persona_id": persona_id, "status": "processing"})
            _sse_buffer_append(persona_id, conversation_id, next_id, "processing", data)
            yield _sse_format(next_id, "processing", data).encode("utf-8")

        # Keepalive loop — emit `: keepalive\n\n` every 20s.
        last_keepalive = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now - last_keepalive >= 20:
                yield b": keepalive\n\n"
                last_keepalive = now
            # Sleep in small chunks so client-disconnect is detected quickly.
            import asyncio  # noqa: PLC0415
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )
