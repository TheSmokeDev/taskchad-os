"""Tenant token resolution — the auth truth table's backend (Tenant Isolation v0).

Phase A foundation. This module turns a raw ``Bearer`` token into a
``TenantBinding`` (workspace_id + persona_scope + is_admin) by HASHED row lookup,
and reports whether the deployment is in multi-tenant mode (≥1 active non-admin
``tenant_tokens`` row). The orchestration middleware (``api.py``) consumes both.

PHASE-A SCOPE BOUNDARY (read this before enabling tenant rows in production):
    Phase A lands the auth FOUNDATION + threads ``workspace_id`` through the
    convoy/mailbox/team service path. It does NOT ship the full route-policy
    registry, deny-by-default on all 113 routes, or dashboard persona scoping —
    that is Phase B. Multi-tenant mode is therefore NOT a complete isolation
    boundary yet: an operator must NOT create non-admin tenant rows in a
    production deployment until Phase B ships. Until then, the convoy/mailbox/
    team workspace binding is correct, but unthreaded dashboard routes and
    unregistered routes are not yet deny-by-default.

Anti-pattern compliance:
    - Rule 1: no tunable ``config.X`` bound as a default arg; everything resolves
      from the passed ``db`` + ``bearer`` at call time.
    - Rule 2: ``is_multi_tenant_mode`` and ``resolve_tenant_binding`` read the
      physical ``tenant_tokens`` rows per call (revocation filter in the read) —
      no module-level cache that could survive a revoke.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from orchestration.db import OrchestrationDB


@dataclass(frozen=True)
class TenantBinding:
    """Resolved identity for a request in multi-tenant mode.

    ``workspace_id`` — the tenant's workspace; every convoy/mailbox/team service
        call is scoped to it.
    ``persona_scope`` — frozenset of allowed persona ids, or ``None``. The
        ``None`` value is RESERVED for admin/single-tenant allow-all. A NON-admin
        binding NEVER carries ``None``: the resolver coerces a non-admin row's
        ``None`` (absent/empty stored scope) to an EMPTY frozenset (deny-all
        personas) so the dashboard gate can read ``None`` as "admin allow-all"
        and ``frozenset()`` as "non-admin with no personas — deny" without
        ambiguity (R2 NB3 — WS2 must not hand WS3 a None that reads as allow-all).
    ``is_admin`` — admin/global token (carries the legacy ``ORCHESTRATION_API_TOKEN``);
        excluded from tenant-resource isolation tests.
    """

    workspace_id: int
    persona_scope: frozenset[str] | None
    is_admin: bool


def hash_token(raw_bearer: str) -> str:
    """Return the lowercase hex SHA-256 of *raw_bearer*.

    The single hashing chokepoint — both the CLI (mint) and the resolver
    (lookup) call this so the stored hash and the lookup hash always match.
    """
    return hashlib.sha256(raw_bearer.encode("utf-8")).hexdigest()


def parse_persona_scope(text: str | None) -> frozenset[str] | None:
    """Strictly parse a stored ``persona_scope`` JSON array into a frozenset.

    Contract (PRP "persona_scope is a JSON ARRAY, parsed strictly"):
        - ``None`` / empty / whitespace            -> ``None`` (unscoped)
        - a JSON list of non-empty strings         -> ``frozenset`` of them (dedup)
        - anything else (non-list, non-str entries,
          empty-string entries, invalid JSON)      -> ``frozenset()`` (deny-all)

    Returning an EMPTY frozenset (not ``None``) for malformed input is
    deliberate: a corrupt scope must fail CLOSED (no personas), never silently
    widen to allow-all. ``None`` is reserved strictly for the deliberate
    "unscoped" case (empty/absent text).
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return frozenset()
    if not isinstance(parsed, list):
        return frozenset()
    out: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, str):
            return frozenset()
        e = entry.strip()
        if not e:
            return frozenset()
        out.add(e)
    return frozenset(out)


def is_multi_tenant_mode(db: OrchestrationDB) -> bool:
    """True iff ≥1 active non-admin ``tenant_tokens`` row exists (Rule 2 read).

    When False (the default, zero tenant rows), the middleware preserves the
    legacy ``ORCHESTRATION_API_TOKEN`` behavior EXACTLY — single-tenant
    back-compat is byte-unchanged.
    """
    return db.has_active_tenant_token()


def resolve_tenant_binding(db: OrchestrationDB, raw_bearer: str) -> TenantBinding | None:
    """Resolve *raw_bearer* to a ``TenantBinding`` by HASHED row lookup, or None.

    ``None`` means "not a known active token" — the middleware 401s it in
    multi-tenant mode. An empty bearer never matches (its hash won't be stored).

    The persona scope is parsed strictly. NB3 FAIL-CLOSED (R2): a NON-admin row
    whose parsed scope is ``None`` (absent/empty stored scope) is coerced to an
    EMPTY frozenset (deny-all personas) — a non-admin binding NEVER carries
    ``None``. This is the unambiguous WS2→WS3 contract: ``persona_scope=None``
    means admin allow-all; ``frozenset()`` means non-admin with no personas
    (deny). The dashboard gate must NOT read a non-admin ``None`` as allow-all,
    so we never produce one.
    """
    if not raw_bearer:
        return None
    row = db.get_binding_by_hash(hash_token(raw_bearer))
    if row is None:
        return None
    is_admin = bool(row["is_admin"])
    scope = parse_persona_scope(row["persona_scope"])
    if not is_admin and scope is None:
        # NB3 fail-closed: a non-admin token must never resolve to allow-all.
        scope = frozenset()
    return TenantBinding(
        workspace_id=int(row["workspace_id"]),
        persona_scope=scope,
        is_admin=is_admin,
    )
