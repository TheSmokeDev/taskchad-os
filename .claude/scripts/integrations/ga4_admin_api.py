"""GA4 Admin API client — the substrate behind the persona fleet-write tools.

Epic #465 ticket 1a PR 2. This is the minimal client for the two gated write
verbs (``ga4_provision_site`` / ``ga4_deploy_tag``) — deliberately NOT a port
of the ga4-ops skill's 783-line CLI. The skill owns the checksummed
plan/account-ticket flows and the full property configuration (custom
dimensions, key events, enhanced measurement); this module owns exactly the
operations the action-gate executors need.

**The canonical artifacts, and who writes them (Codex R1).** Two files, two
shapes, exactly as the skill defines them:

* ``GA4_FLEET_CONFIG`` — the DESIRED config (``config/ga4-fleet.json``):
  brand rows keyed ``id`` with ``domain``/``app_dir``/``vercel_project`` (+
  optional per-brand ``account``), top-level ``account``/``time_zone``/
  ``currency_code``.
* ``GA4_FLEET_LIVE`` — the checksummed LIVE state
  (``config/ga4-fleet-live.json``): rows keyed ``brand_id`` carrying
  ``property``/``stream``/``measurement_id``. When the file carries its
  ``sha256`` field it is VERIFIED on read (canonical JSON, sorted keys,
  compact separators, the sha256 key excluded — the skill's
  ``canonical_sha``): a state file that fails its own checksum never drives
  a deploy.

This module never WRITES the live-state file. Provisioning through the gate
returns the reconciled ids in its receipt; promoting them into the
checksummed state belongs to the skill's apply flow (``ga4_ops.py apply``),
which owns the checksum discipline. Documenting that split is the contract.
Provision reads the desired config only (consistent with the skill, whose
plans are built from config); the live-state checksum gate applies to deploy
proposals, because a Vercel write is where tampered state does real damage.

**Snapshots, not lookups.** The tool handlers resolve the complete target at
propose time and store it in the proposal payload; the executors re-read the
physical files and refuse on drift. The functions here take resolved values
(domain, account, measurement id, app_dir) — never a brand slug to look up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_VERCEL_ENV_VAR = "NEXT_PUBLIC_GA_MEASUREMENT_ID"

# Bounded waits: a hung Google/vercel/HTTP call must not hold the decision thread.
_HTTP_TIMEOUT_S = 30
_HTTP_TIMEOUT_MAX_S = 120
_VERCEL_TIMEOUT_S = 60

# Cross-process reconcile serialization (Codex R3 B3). Module-level so tests
# can shrink the wait at the module attribute (Rule 3).
_RECONCILE_LOCK_TIMEOUT_S = 120

_USER_AGENT = "Homie-GA4-Fleet/1.0"

# Shapes, mirroring the skill's contract (ga4_ops.py MEASUREMENT_RE /
# RESOURCE_RE / resolve_brand_account). Validation results are DATA for
# verify_tag_live and errors for the config readers — never guesses.
_MEASUREMENT_RE = re.compile(r"^G-[A-Z0-9]+$")
_ACCOUNT_RE = re.compile(r"^accounts/\d+$")
_RESOURCE_RE = re.compile(r"^properties/\d+$")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")
_BRAND_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# The canonical ga4-ops team (ga4_ops.py's vercel-sync --scope default). An
# empty scope NEVER reaches the Vercel CLI: a bare command searches across
# teams and auto-selects the current-team match, so the approved card could
# deploy a different organization's same-named project (#465 1a codex R3).
_DEFAULT_VERCEL_SCOPE = "your-github-users-projects"


def resolve_vercel_scope(scope: str | None = None) -> str:
    """Resolve the Vercel team identity. Never returns an empty string.

    Explicit argument, else ``GA4_VERCEL_SCOPE``, else the canonical ga4-ops
    team. Propose-time snapshots call this so the approval binds the RESOLVED
    team; executors pass the snapshot value straight through.
    """
    if scope is not None:
        resolved = str(scope).strip()
    else:
        resolved = os.getenv("GA4_VERCEL_SCOPE", "").strip()
    return resolved or _DEFAULT_VERCEL_SCOPE


def is_valid_vercel_scope(scope: Any) -> bool:
    """Shape check for a Vercel team slug (lowercase alnum + hyphens)."""
    return isinstance(scope, str) and bool(_SCOPE_RE.match(scope.strip()))

# The canonical property display name (ga4_ops.py property_display_name):
# resources created by the skill's apply flow carry it, so convergence MUST
# match it — anything else duplicates an existing fleet property.
def canonical_property_display_name(domain: str) -> str:
    return f"YourBusiness Fleet | {domain}"


class FleetConfigError(RuntimeError):
    """The fleet config or live state is unset, missing, or malformed. Fail-closed."""


# ── Desired config + live state (physical reads) ────────────────────────────


def fleet_config_path() -> Path | None:
    """The desired-config file, or None when unset (Rule 1 — call-time env)."""
    raw = os.getenv("GA4_FLEET_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else None


def live_state_path() -> Path | None:
    """The checksummed live-state file, or None when unset."""
    raw = os.getenv("GA4_FLEET_LIVE", "").strip()
    return Path(raw).expanduser() if raw else None


def _read_brands_file(path: Path | None, *, label: str, key: str) -> dict[str, Any]:
    if path is None:
        raise FleetConfigError(f"{label} is not configured (env var unset)")
    if not path.is_file():
        raise FleetConfigError(f"{label} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise FleetConfigError(f"{label} unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("brands"), list):
        raise FleetConfigError(f"{label} at {path} has no brands list")
    for index, row in enumerate(data["brands"]):
        if not isinstance(row, dict) or not str(row.get(key) or "").strip():
            raise FleetConfigError(
                f"{label} brands[{index}] is not an object with a {key} field"
            )
    return data


def _canon_domain(value: Any) -> str:
    """The skill's ``normalize_domain``, verbatim semantics (ga4_ops.py):
    urlparse-tolerant, lowercased, trailing dot and leading ``www.`` stripped.
    ``www.example.com`` and ``example.com`` are ONE site; treating them as two
    is how one site gets two properties.
    """
    text = str(value or "").strip()
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _check_unique(rows: list[dict[str, Any]], field: str, *, label: str) -> None:
    """Whole-fleet uniqueness (Codex R2 B3) — duplicates name the offenders.

    Comparison is casefolded: ``apps/Brand`` and ``apps/brand`` are the same
    physical directory on this Windows host, and Vercel project names are
    case-insensitive slugs — a case-variant duplicate is a duplicate
    (#465 1a codex R3).
    """
    seen: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field) or "").strip().casefold()
        if not value:
            continue
        seen[value] = seen.get(value, 0) + 1
    duplicates = sorted(value for value, count in seen.items() if count > 1)
    if duplicates:
        raise FleetConfigError(
            f"{label} has duplicate {field} value(s): {', '.join(duplicates)}"
        )


def load_fleet_config(path: Path | str | None = None) -> dict[str, Any]:
    """Read and validate the DESIRED config — ALL rows, not just the selected.

    The ga4-ops contract (ga4_ops.py validate_config) holds the whole fleet to
    unique ids/domains/app_dirs/projects and well-formed fields, because one
    duplicated domain silently becomes two properties for one site. A
    malformed row anywhere refuses every proposal and names the offender.
    """
    resolved = Path(path) if path is not None else fleet_config_path()
    data = _read_brands_file(resolved, label="GA4_FLEET_CONFIG", key="id")
    rows = data["brands"]
    for row in rows:
        brand_id = str(row.get("id") or "")
        if not _BRAND_ID_RE.match(brand_id):
            raise FleetConfigError(f"fleet config: invalid brand id {brand_id[:64]!r}")
        domain = _canon_domain(row.get("domain"))
        if not _DOMAIN_RE.match(domain):
            raise FleetConfigError(
                f"fleet config: brand {brand_id!r} has invalid domain {domain[:64]!r}"
            )
        row["domain"] = domain
        for field in ("app_dir", "vercel_project"):
            if not str(row.get(field) or "").strip():
                raise FleetConfigError(
                    f"fleet config: brand {brand_id!r} is missing {field}"
                )
        account = str(row.get("account") or "").strip()
        if account and not _ACCOUNT_RE.match(account):
            raise FleetConfigError(
                f"fleet config: brand {brand_id!r} has invalid account {account!r}"
            )
        resource = str(row.get("property_resource") or "").strip()
        if resource and not _RESOURCE_RE.match(resource):
            raise FleetConfigError(
                f"fleet config: brand {brand_id!r} has invalid property_resource "
                f"{resource!r}"
            )
    for field in ("id", "domain", "app_dir", "vercel_project", "property_resource"):
        _check_unique(rows, field, label="fleet config")
    account = str(data.get("account") or "").strip()
    if account and not _ACCOUNT_RE.match(account):
        raise FleetConfigError(f"fleet config: invalid default account {account!r}")
    return data


def canonical_state_sha(state: dict[str, Any]) -> str:
    """The skill's ``canonical_sha``, verbatim: sorted keys, compact
    separators, the sha256 field itself excluded."""
    clean = {key: value for key, value in state.items() if key != "sha256"}
    raw = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_live_state(path: Path | str | None = None) -> dict[str, Any]:
    """Read and validate the LIVE state — checksum REQUIRED (Codex R2 B3).

    The skill's contract is an exact checksummed state for Vercel writes: a
    state file with no sha256, or one that fails it, is tampered or torn and
    must never drive a deploy. Every row is validated too: a duplicated
    measurement id would contaminate cross-domain analytics before
    verification could object.
    """
    resolved = Path(path) if path is not None else live_state_path()
    data = _read_brands_file(resolved, label="GA4_FLEET_LIVE", key="brand_id")
    expected = str(data.get("sha256") or "")
    if not expected:
        raise FleetConfigError(
            f"GA4 live state at {resolved} carries no checksum — refusing to "
            "trust unsigned state for a deploy (re-run the ga4-ops apply flow)"
        )
    if expected != canonical_state_sha(data):
        raise FleetConfigError(
            f"GA4 live state at {resolved} fails its checksum — refusing to "
            "trust it (re-run the ga4-ops apply flow to regenerate)"
        )
    rows = data["brands"]
    for row in rows:
        brand_id = str(row.get("brand_id") or "")
        measurement = str(row.get("measurement_id") or "").strip()
        if not _MEASUREMENT_RE.match(measurement):
            raise FleetConfigError(
                f"live state: brand {brand_id!r} has invalid measurement id "
                f"{measurement[:40]!r}"
            )
        prop = str(row.get("property") or "").strip()
        if prop and not _RESOURCE_RE.match(prop):
            raise FleetConfigError(
                f"live state: brand {brand_id!r} has invalid property {prop!r}"
            )
    for field in ("brand_id", "measurement_id", "property", "domain"):
        _check_unique(rows, field, label="live state")
    return data


def validate_live_state_against_config(
    config: dict[str, Any], state: dict[str, Any]
) -> None:
    """The skill's ``validate_state`` contract (ga4_ops.py:489-526), mirrored.

    The checksum proves the state file wasn't tampered; THIS proves it belongs
    to THIS fleet: same brand set, and every row agreeing with the desired
    config on account/domain/app_dir/vercel_project, with domain, property,
    stream, and measurement id all present. Without it, a correctly
    re-checksummed PARTIAL state (``{brand_id, measurement_id}`` and nothing
    else) could push an arbitrary measurement id to production
    (#465 1a codex R3 B2).
    """
    brands = config.get("brands") or []
    rows = state.get("brands") or []
    if len(rows) != len(brands):
        raise FleetConfigError(
            f"live state covers {len(rows)} brand(s) but the desired config "
            f"declares {len(brands)} — re-run the ga4-ops apply flow"
        )
    by_id = {
        str(row.get("brand_id") or ""): row for row in rows if isinstance(row, dict)
    }
    default_account = str(config.get("account") or "").strip()
    for brand in brands:
        brand_id = str(brand.get("id") or "")
        row = by_id.get(brand_id)
        if row is None:
            raise FleetConfigError(f"live state is missing brand {brand_id!r}")
        expected_account = str(brand.get("account") or "").strip() or default_account
        if expected_account and str(row.get("account") or "").strip() not in {
            "",
            expected_account,
        }:
            raise FleetConfigError(
                f"live state account mismatch for {brand_id!r}"
            )
        for field in ("app_dir", "vercel_project"):
            state_value = str(row.get(field) or "").strip()
            config_value = str(brand.get(field) or "").strip()
            if state_value.casefold() != config_value.casefold():
                raise FleetConfigError(
                    f"live state {field} mismatch for {brand_id!r}: "
                    f"state {state_value!r} vs config {config_value!r}"
                )
        if _canon_domain(row.get("domain")) != _canon_domain(brand.get("domain")):
            raise FleetConfigError(
                f"live state domain mismatch for {brand_id!r}: "
                f"state {row.get('domain')!r} vs config {brand.get('domain')!r}"
            )
        for field in ("property", "stream", "measurement_id"):
            if not str(row.get(field) or "").strip():
                raise FleetConfigError(
                    f"live state row for {brand_id!r} is missing {field} — "
                    "partial state cannot drive a deploy"
                )
    stream_values = [str(r.get("stream") or "").strip() for r in rows]
    if len({s.casefold() for s in stream_values}) != len(stream_values):
        raise FleetConfigError("live state has duplicate stream values")


def get_brand(slug: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """One DESIRED-config brand row by ``id``. None on miss."""
    wanted = str(slug or "").strip()
    for brand in config.get("brands") or []:
        if isinstance(brand, dict) and str(brand.get("id") or "") == wanted:
            return brand
    return None


def get_live_brand(slug: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """One LIVE-state brand row by ``brand_id``. None on miss."""
    wanted = str(slug or "").strip()
    for row in state.get("brands") or []:
        if isinstance(row, dict) and str(row.get("brand_id") or "") == wanted:
            return row
    return None


def resolve_app_path(app_dir: Any) -> Path:
    """The brand's app directory on disk — confined, physical, or an error.

    Confinement (Codex R1): ``app_dir`` must be ONE path segment (no
    separators, no ``..``, never absolute), and the resolved candidate must be
    a DIRECT child of the resolved ``<root>/apps``. The old join+is_dir
    accepted ``../../docs`` as a real directory outside the boundary.
    """
    segment = str(app_dir or "").strip()
    if (
        not segment
        or segment in {".", ".."}
        or "/" in segment
        or "\\" in segment
        or Path(segment).is_absolute()
    ):
        raise FleetConfigError(f"app_dir {segment[:64]!r} is not a single safe path segment")
    raw = os.getenv("GA4_FLEET_REPO_ROOT", "").strip()
    if not raw:
        raise FleetConfigError(
            "GA4_FLEET_REPO_ROOT is not set — cannot locate the brand's app "
            "directory for the Vercel sync"
        )
    apps_root = (Path(raw).expanduser() / "apps").resolve()
    candidate = (apps_root / segment).resolve()
    if candidate.parent != apps_root:
        raise FleetConfigError(
            f"app_dir {segment!r} escapes the fleet apps boundary ({apps_root})"
        )
    if not candidate.is_dir():
        raise FleetConfigError(f"app directory missing: {candidate}")
    return candidate


# ── Admin API ───────────────────────────────────────────────────────────────


def _admin_service() -> Any:
    """Build the analyticsadmin v1beta client. Late imports (Rule 3): tests
    monkeypatch ``googleapiclient.discovery.build`` at its module attr."""
    from googleapiclient.discovery import build  # noqa: PLC0415

    from integrations import auth  # noqa: PLC0415 — Rule 3 module attr

    return build(
        "analyticsadmin",
        "v1beta",
        credentials=auth.get_ga4_admin_credentials(),
        cache_discovery=False,
    )


def _paged(request_factory: Any, result_key: str) -> list[dict[str, Any]]:
    """Drain one list endpoint. Same shape as the skill's pager."""
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        response = request_factory(token).execute()
        items.extend(response.get(result_key, []) or [])
        token = response.get("nextPageToken")
        if not token:
            return items


def reconcile_site(target: dict[str, Any]) -> dict[str, Any]:
    """Create-or-get the target's property and web stream. Never duplicates.

    ``target`` is the RESOLVED snapshot: ``id``, ``domain``, ``account``,
    ``time_zone``, ``currency_code``. Matching precedes creating, so a retry
    converges: the second call reports ``existed`` instead of minting
    siblings.

    Concurrency (Codex R3 B3): list-then-create is only convergent if the
    list still holds at create time. Two approvals for the same target
    executing together both saw "missing" and both created. The whole
    reconcile runs under a cross-process file lock keyed by account+domain,
    so the second caller lists the FIRST caller's created resources and
    reports ``existed``.

    Per-subaction outcomes are STRUCTURED (Codex R1): a property that was
    created before the stream step failed is a ``failed`` stream beside a
    ``created`` property — with the resource NAME — so the gate can record a
    partial instead of claiming total failure over a real created resource.
    True precondition failures (auth, transport on the LIST calls, where no
    honest state is known) still raise.

    Scope note: custom dimensions, key events, and enhanced-measurement
    settings stay with the ga4-ops skill's checksummed plan flow.
    """
    account = str(target.get("account") or "").strip()
    domain = str(target.get("domain") or "").strip()
    if not _ACCOUNT_RE.match(account):
        raise FleetConfigError(
            f"invalid Analytics account for {str(target.get('id') or '')!r}: {account!r}"
        )
    if not domain:
        raise FleetConfigError(
            f"target for {str(target.get('id') or '')!r} has no domain"
        )

    import config as _config  # noqa: PLC0415 — Rule 3 module attr
    from shared import file_lock  # noqa: PLC0415

    lock_name = re.sub(r"[^a-z0-9]+", "-", f"{account}-{domain}".lower()).strip("-")
    with file_lock(
        Path(_config.DATA_DIR) / f"ga4-reconcile-{lock_name}",
        timeout=float(_RECONCILE_LOCK_TIMEOUT_S),
    ):
        return _reconcile_site_locked(target)


def _reconcile_site_locked(target: dict[str, Any]) -> dict[str, Any]:
    """The reconcile body — only ever entered holding the per-target lock."""
    account = str(target.get("account") or "").strip()
    domain = str(target.get("domain") or "").strip()
    slug = str(target.get("id") or "").strip()
    result: dict[str, Any] = {
        "property_status": "failed",
        "property": "",
        "property_detail": "",
        "stream_status": "skipped",
        "stream": "",
        "stream_detail": "",
        "measurement_id": "",
    }
    if not _ACCOUNT_RE.match(account):
        raise FleetConfigError(f"invalid Analytics account for {slug!r}: {account!r}")
    if not domain:
        raise FleetConfigError(f"target for {slug!r} has no domain")

    beta = _admin_service()
    # Canonical convergence (Codex R2 B2): a declared property_resource is
    # matched EXACTLY (and its absence is a failure, never a create); without
    # one, the match key is the skill's canonical display name
    # `YourBusiness Fleet | <domain>`. Multiple display-name matches are
    # ambiguous and refused — picking the first is how fleets get silently
    # cross-wired.
    display = canonical_property_display_name(domain)
    declared_resource = str(target.get("property_resource") or "").strip()

    properties = _paged(
        lambda token: beta.properties().list(
            filter=f"parent:{account}",
            pageSize=200,
            pageToken=token,
            showDeleted=False,
        ),
        "properties",
    )
    if declared_resource:
        prop = next((p for p in properties if p.get("name") == declared_resource), None)
        if prop is None:
            result["property_status"] = "failed"
            result["property_detail"] = (
                f"declared property_resource {declared_resource} not found under "
                f"{account} — refusing to create a sibling"
            )
            return result
    else:
        matches = [p for p in properties if p.get("displayName") == display]
        if len(matches) > 1:
            result["property_status"] = "failed"
            result["property_detail"] = (
                f"ambiguous: {len(matches)} properties named {display!r} under "
                f"{account} — refusing to pick one"
            )
            return result
        prop = matches[0] if matches else None
    if prop is None:
        try:
            prop = (
                beta.properties()
                .create(
                    body={
                        "parent": account,
                        "displayName": display,
                        "industryCategory": "FINANCE",
                        "timeZone": target.get("time_zone") or "America/Los_Angeles",
                        "currencyCode": target.get("currency_code") or "USD",
                    }
                )
                .execute()
            )
            result["property_status"] = "created"
        except Exception as exc:  # noqa: BLE001 — structured, named, honest
            result["property_detail"] = f"create failed: {type(exc).__name__}: {exc}"
            return result
    else:
        result["property_status"] = "existed"
    property_name = str(prop.get("name") or "")
    result["property"] = property_name

    try:
        streams = _paged(
            lambda token: beta.properties()
            .dataStreams()
            .list(parent=property_name, pageSize=200, pageToken=token),
            "dataStreams",
        )
        want_uri = f"https://{domain}"
        stream_matches = [
            s
            for s in streams
            if (s.get("webStreamData") or {}).get("defaultUri") == want_uri
        ]
        if len(stream_matches) > 1:
            # Codex R3 B3: picking the first of duplicates silently cross-wires
            # measurement. Refuse and name the collision.
            result["stream_status"] = "failed"
            result["stream_detail"] = (
                f"ambiguous: {len(stream_matches)} streams for {want_uri} under "
                f"{property_name} — refusing to pick one"
            )
            return result
        stream = stream_matches[0] if stream_matches else None
        if stream is None:
            stream = (
                beta.properties()
                .dataStreams()
                .create(
                    parent=property_name,
                    body={
                        "type": "WEB_DATA_STREAM",
                        "displayName": domain,
                        "webStreamData": {"defaultUri": want_uri},
                    },
                )
                .execute()
            )
            result["stream_status"] = "created"
        else:
            result["stream_status"] = "existed"
        result["stream"] = str(stream.get("name") or "")
        measurement_id = str(
            (stream.get("webStreamData") or {}).get("measurementId") or ""
        )
        if not _MEASUREMENT_RE.match(measurement_id):
            result["stream_status"] = "failed"
            result["stream_detail"] = (
                f"no valid measurement id on the stream: {measurement_id[:32]!r}"
            )
            return result
        result["measurement_id"] = measurement_id
    except Exception as exc:  # noqa: BLE001 — partial state must survive
        result["stream_status"] = "failed"
        result["stream_detail"] = f"{type(exc).__name__}: {exc}"
    return result


# ── Production verification (result as data) ────────────────────────────────


def _safe_str(value: Any, *, limit: int = 200) -> str:
    """str() that cannot raise (Codex R2 M7): str -> repr -> type name."""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — the whole point of this helper
        try:
            text = repr(value)
        except Exception:  # noqa: BLE001
            text = ""
    if not text:
        text = f"<unprintable {type(value).__name__}>"
    return text[:limit]


def verify_tag_live(
    domain: Any, measurement_id: Any, *, timeout: Any = None
) -> dict[str, Any]:
    """GET the canonical origin: HTTP 200 AND the exact measurement id present.

    NEVER raises (Codex R1 + R2): every input is coerced through
    :func:`_safe_str` INSIDE the guard, every expected failure family
    (invalid shapes, invalid URLs, HTTP errors, transport errors, timeout
    garbage, hostile objects whose ``__str__`` raises) comes back as a failed
    verification result — this function's caller records whatever it returns
    as the post-deploy truth.

    Matching is boundary-safe on BOTH sides: ASCII letters, digits, and
    hyphen adjacent to the id all disqualify the match, so ``xG-ABC``,
    ``G-ABCD``, and ``G-ABC-EXTRA`` are misses while ``/G-ABC/`` is a hit.
    """
    try:
        domain_text = _safe_str(domain).strip()
        if not _DOMAIN_RE.match(domain_text):
            return {
                "ok": False, "status": None, "tag_present": False,
                "detail": f"invalid domain shape: {domain_text[:64]!r}",
            }
        measurement = _safe_str(measurement_id).strip()
        if not _MEASUREMENT_RE.match(measurement):
            return {
                "ok": False, "status": None, "tag_present": False,
                "detail": f"invalid measurement id shape: {measurement[:40]!r}",
            }
        if timeout is None:
            seconds = _HTTP_TIMEOUT_S
        else:
            try:
                seconds = max(1, min(_HTTP_TIMEOUT_MAX_S, int(timeout)))
            except (TypeError, ValueError):
                seconds = _HTTP_TIMEOUT_S

        tag_re = re.compile(
            r"(?<![A-Za-z0-9-])" + re.escape(measurement) + r"(?![A-Za-z0-9-])"
        )
        url = f"https://{domain_text}/"
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=seconds) as response:  # noqa: S310 — the fleet's own domains
            body = response.read().decode("utf-8", errors="replace")
            found = bool(tag_re.search(body))
            return {
                "ok": response.status == 200 and found,
                "status": response.status,
                "tag_present": found,
                "detail": ""
                if response.status == 200 and found
                else f"HTTP {response.status}, tag {'present' if found else 'absent'}",
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False, "status": exc.code, "tag_present": False,
            "detail": f"HTTP {exc.code}",
        }
    except Exception as exc:  # noqa: BLE001 — URLError/InvalidURL/OSError/ValueError all land here
        return {
            "ok": False, "status": None, "tag_present": False,
            "detail": f"{type(exc).__name__}: {_safe_str(exc, limit=180)}",
        }


def _verify_deadline_s() -> float:
    """Poll deadline, env-tunable at call time (Rule 1), clamped."""
    raw = os.getenv("GA4_VERIFY_DEADLINE_S", "").strip()
    try:
        value = float(raw) if raw else 600.0
    except ValueError:
        value = 600.0
    return max(1.0, min(3600.0, value))


def _verify_interval_s() -> float:
    raw = os.getenv("GA4_VERIFY_INTERVAL_S", "").strip()
    try:
        value = float(raw) if raw else 30.0
    except ValueError:
        value = 30.0
    return max(0.05, min(300.0, value))


def verify_tag_live_until(
    domain: Any, measurement_id: Any, *, timeout: Any = None
) -> dict[str, Any]:
    """Poll :func:`verify_tag_live` until the tag is live or the deadline.

    Fresh production builds take minutes; a single immediate fetch after a
    deploy call would report a failure that is really just latency. On
    deadline the result stays honest: ok=False with a detail that says the
    deploy is in flight, not that the tag is absent.
    """
    import time

    deadline = time.monotonic() + _verify_deadline_s()
    interval = _verify_interval_s()
    while True:
        result = verify_tag_live(domain, measurement_id, timeout=timeout)
        if result["ok"]:
            return result
        if time.monotonic() >= deadline:
            result["detail"] = (
                f"deploy in flight, tag not yet live at the "
                f"{int(_verify_deadline_s())}s deadline ({result['detail']})"
            )
            return result
        time.sleep(interval)


# ── Vercel sync + production deploy (bounded argv subprocesses) ─────────────


def _vercel_deploy_timeout_s() -> int:
    """Deploy-step bound, env-tunable at call time (Rule 1). Builds take minutes."""
    raw = os.getenv("GA4_DEPLOY_TIMEOUT_S", "").strip()
    try:
        value = int(raw) if raw else 600
    except ValueError:
        value = 600
    return max(30, min(1800, value))



def _link_state(path: Path) -> str:
    """Physical ``.vercel/project.json`` contents; '' when absent/unreadable.

    ``vercel link`` deletes this file before its network work, so a FAILED
    link can still change physical state — callers compare before/after
    instead of trusting the exit code to mean "nothing happened" (R3).
    """
    try:
        link_file = Path(path) / ".vercel" / "project.json"
        if not link_file.is_file():
            return ""
        return link_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

def sync_vercel_env(
    app_path: Path | str,
    measurement_id: str,
    *,
    project: str,
    scope: str | None = None,
) -> dict[str, Any]:
    """link -> env add -> production deploy, with PER-SUBSTEP outcomes.

    ``project`` is REQUIRED: without an explicit link target, ``env add``
    would land on whatever project the directory happens to be linked to —
    ambient linkage is not an approval target. ``scope`` is the Vercel team
    identity; ``None`` resolves ``GA4_VERCEL_SCOPE`` at call time, falling back
    to the canonical ga4-ops team. The resolved scope is ALWAYS passed
    explicitly: a bare command lets the Vercel CLI search across teams and
    auto-select its current-team match, so the approved card could deploy a
    different organization's same-named project (#465 1a codex R3). The
    executor passes the SNAPSHOT values, so the approved team is the team
    written to.

    Every substep reports independently (Codex R2): ``steps`` maps
    link/env/deploy to ok|failed|skipped, ``ran`` lists only SUCCESSFUL
    substeps (appended after the return-code check — a failed step never
    wears a success receipt), and the first failure stops the chain. A landed
    link beside a failed env is real state change and must be visible as one.

    The deploy step is the production trigger for the approved app target:
    ``vercel deploy --prod`` against the confined app path (operator ruling,
    epic #465: an approved deploy action finishes the job). Argv lists only —
    no shell, no string interpolation of anything that started life as tool
    input.
    """
    project_name = str(project or "").strip()
    if not project_name:
        return {
            "ok": False,
            "detail": "vercel project is required — refusing ambient linkage",
            "ran": [],
            "steps": {"link": "skipped", "env": "skipped", "deploy": "skipped"},
        }
    path = Path(app_path)
    if not path.is_dir():
        return {
            "ok": False,
            "detail": f"app directory missing: {path}",
            "ran": [],
            "steps": {"link": "skipped", "env": "skipped", "deploy": "skipped"},
        }
    link_before = _link_state(path)
    vercel = shutil.which("vercel.cmd") or shutil.which("vercel") or "vercel"
    resolved_scope = resolve_vercel_scope(scope)
    if not _SCOPE_RE.match(resolved_scope):
        return {
            "ok": False,
            "detail": f"vercel scope {resolved_scope!r} is not a team slug",
            "ran": [],
            "steps": {"link": "skipped", "env": "skipped", "deploy": "skipped"},
        }
    scope_args = ["--scope", resolved_scope]

    steps: list[tuple[str, list[str], int]] = [
        (
            "link",
            [vercel, "link", "--yes", "--project", project_name, *scope_args,
             "--cwd", str(path)],
            _VERCEL_TIMEOUT_S,
        ),
        (
            "env",
            [
                vercel, "env", "add", _VERCEL_ENV_VAR, "production",
                "--value", str(measurement_id), "--force", "--no-sensitive", "--yes",
                *scope_args, "--cwd", str(path),
            ],
            _VERCEL_TIMEOUT_S,
        ),
        (
            "deploy",
            [vercel, "deploy", "--prod", "--yes", *scope_args, "--cwd", str(path)],
            _vercel_deploy_timeout_s(),
        ),
    ]
    outcomes = {"link": "skipped", "env": "skipped", "deploy": "skipped"}
    ran: list[str] = []
    for name, argv, timeout_s in steps:
        try:
            result = subprocess.run(  # noqa: S603 — fixed argv list, no shell
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            outcomes[name] = "failed"
            return {
                "ok": False,
                "detail": f"vercel {name} failed to run: {type(exc).__name__}: {exc}",
                "ran": ran,
                "steps": outcomes,
                "link_changed": _link_state(path) != link_before,
            }
        if result.returncode != 0:
            outcomes[name] = "failed"
            detail = (result.stderr or result.stdout or "").strip()[:300]
            return {
                "ok": False,
                "detail": f"vercel {name} exited {result.returncode}: {detail}",
                "ran": ran,
                "steps": outcomes,
                "link_changed": _link_state(path) != link_before,
            }
        outcomes[name] = "ok"
        ran.append(name)
    return {
        "ok": True,
        "detail": "",
        "ran": ran,
        "steps": outcomes,
        "link_changed": _link_state(path) != link_before,
    }


__all__ = [
    "FleetConfigError",
    "canonical_property_display_name",
    "canonical_state_sha",
    "fleet_config_path",
    "get_brand",
    "get_live_brand",
    "live_state_path",
    "load_fleet_config",
    "load_live_state",
    "validate_live_state_against_config",
    "reconcile_site",
    "resolve_app_path",
    "resolve_vercel_scope",
    "is_valid_vercel_scope",
    "sync_vercel_env",
    "verify_tag_live",
    "verify_tag_live_until",
]
