"""Thin operator routes, mounted under the existing dashboard auth and scope."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from personas.learning import operator

router = APIRouter()
logger = logging.getLogger(__name__)


def _operator(persona_id: str, request: Request) -> operator.LearningOperator:
    # Lazy module-attribute access preserves the canonical scope checks and
    # avoids a second main/default translation site or auth implementation.
    import dashboard_api

    dashboard_api._reject_main_translation(persona_id)
    dashboard_api._require_persona_in_scope(request, persona_id)
    dashboard_api._require_target_persona_physical(request, persona_id)
    return operator.get_learning_operator(persona_id)


def _call(action: Callable[[], Any]) -> Any:
    from personas.learning.models import LearningError, LearningNotFound, LearningValidationError

    try:
        return action()
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Persona profile not found") from None
    except LookupError:
        raise HTTPException(status_code=404, detail="Learning record not found") from None
    except LearningNotFound:
        raise HTTPException(status_code=404, detail="Persona profile not found") from None
    except LearningValidationError as exc:
        raise HTTPException(status_code=422, detail=operator.safe_text(str(exc))) from None
    except LearningError as exc:
        raise HTTPException(status_code=409, detail=operator.safe_text(str(exc))) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=operator.safe_text(str(exc))) from None
    except Exception as exc:
        logger.error("Learning operator request failed: %s", operator.safe_text(str(exc)))
        raise HTTPException(
            status_code=503, detail="Learning service unavailable; retry after checking its status"
        ) from None


@router.get("/api/agents/{persona_id}/learning")
def learning_summary(persona_id: str, request: Request) -> dict:
    return _call(lambda: _operator(persona_id, request).summary())


@router.get("/api/agents/{persona_id}/learning/records")
def learning_records(
    persona_id: str,
    request: Request,
    kind: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None, max_length=256),
    status: str | None = Query(None, max_length=64),
) -> dict:
    return _call(
        lambda: _operator(persona_id, request).list_records(
            kind, limit=limit, cursor=cursor, status=status
        )
    )


@router.get("/api/agents/{persona_id}/learning/records/{record_id}")
def learning_record(persona_id: str, record_id: str, request: Request) -> dict:
    return _call(lambda: _operator(persona_id, request).get_record(record_id))


@router.post("/api/agents/{persona_id}/learning/pause")
def pause_learning(persona_id: str, request: Request) -> dict:
    return _call(lambda: _operator(persona_id, request).set_paused(True))


@router.post("/api/agents/{persona_id}/learning/resume")
def resume_learning(persona_id: str, request: Request) -> dict:
    return _call(lambda: _operator(persona_id, request).set_paused(False))


@router.post("/api/agents/{persona_id}/learning/activations/{activation_id}/rollback")
def rollback_learning(persona_id: str, activation_id: str, request: Request) -> dict:
    return _call(lambda: _operator(persona_id, request).rollback(activation_id))
