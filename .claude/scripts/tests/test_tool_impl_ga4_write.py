"""GA4 fleet write tools (epic #465 1a PR 2) — the gated GA4 hands.

Same discipline as the X write tool tests: the REAL dispatch path
(``build_persona_tool_payload`` -> ``dispatch``) with a synthetic toolset
registry, plus one real-wiring anchor over the SHIPPED registry. Google,
urllib, and the vercel subprocess are faked at their module attrs (Rule 3);
the fleet artifacts, the edit-token file, the repo root, and the persona
profile tree are all physical tmp state. Token files here contain only fake
strings — never a real credential.

The fleet artifacts use the CANONICAL ga4-ops shapes (Codex R1): the desired
config keys brand rows by ``id`` with no resource ids; the live state keys
rows by ``brand_id`` and carries the property/stream/measurement ids.

Codex R1 additions: snapshot-bound payloads (the stored target IS the
approval), drift refusal at execute time, token negative tests for BOTH
executors (tokenless / replay / payload mismatch / cross-action /
cross-persona — each asserting zero provider calls), the failing-stream
partial, exact-match verification, and app_dir confinement.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
CHAT_DIR = SCRIPTS_DIR.parent / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

import config  # noqa: E402
from personas import action_proposals  # noqa: E402
from runtime import persona_tools, tool_impl_ga4_write, tool_registry  # noqa: E402

PERSONA = "sales"

EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

# CANONICAL shapes: desired config rows keyed `id` (intent, no resource ids);
# live-state rows keyed `brand_id` (fact, with the reconciled ids).
FLEET_CONFIG = {
    "account": "accounts/401450559",
    "time_zone": "America/Los_Angeles",
    "currency_code": "USD",
    "brands": [
        {
            "id": "new-brand",
            "domain": "newbrand.com",
            "locale": "en",
            "app_dir": "new-brand",
            "vercel_project": "new-brand",
        },
        {
            "id": "wayward-insurance",
            "domain": "waywardinsurance.com",
            "locale": "en",
            "app_dir": "wayward-insurance",
            "vercel_project": "wayward-insurance",
        },
    ],
}

LIVE_STATE = {
    "account": "accounts/401450559",
    "brands": [
        {
            "brand_id": "new-brand",
            "account": "accounts/401450559",
            "domain": "newbrand.com",
            "app_dir": "new-brand",
            "vercel_project": "new-brand",
            "property": "properties/555000111",
            "stream": "properties/555000111/dataStreams/999000111",
            "measurement_id": "G-NEWBRAND99",
        },
        {
            "brand_id": "wayward-insurance",
            "account": "accounts/401450559",
            "domain": "waywardinsurance.com",
            "app_dir": "wayward-insurance",
            "vercel_project": "wayward-insurance",
            "property": "properties/546004532",
            "stream": "properties/546004532/dataStreams/15271890364",
            "measurement_id": "G-TEST12345",
        },
    ],
}

_MINIMAL_TOOLSETS = {
    "safe_core": {"description": "d", "tools": [], "includes": []},
    "seo_geo_read": {"description": "d", "tools": ["ga4_overview"], "includes": ["safe_core"]},
    "browser": {"description": "d", "tools": ["page_read"], "includes": []},
    "crypto": {"description": "d", "tools": ["chart_read"], "includes": []},
    "social": {"description": "d", "tools": [], "includes": ["browser"]},
    "ga4_fleet_write": {
        "description": "d",
        "tools": ["ga4_provision_site", "ga4_deploy_tag"],
        "includes": ["seo_geo_read"],
    },
}


def _token_payload(scopes: list[str]) -> str:
    """A structurally valid OAuth token file containing only fake strings."""
    return json.dumps(
        {
            "token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            # Far-future expiry: without one the loader treats the token as
            # expired and attempts a REAL network refresh inside a hermetic test.
            "expiry": "2099-01-01T00:00:00Z",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake-client-id",
            "client_secret": "fake-client-secret",
            "scopes": scopes,
        }
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    saved_registry = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    saved_executors = dict(action_proposals._EXECUTORS)
    action_proposals._EXECUTORS.clear()

    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / PERSONA
    (profile_dir / "data").mkdir(parents=True)
    (profile_dir / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "ambient-data", raising=False)
    for var in (
        "HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS",
        "HOMIE_KILLSWITCH_PERSONA_TOOLS",
        "HOMIE_KILLSWITCH_PERSONA_ELEVATION",
        "HOMIE_VAULT_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("runtime.toolsets.TOOLSETS", _MINIMAL_TOOLSETS, raising=False)
    monkeypatch.setattr(
        "personas.experience._reindex_note", lambda *a, **k: None, raising=False
    )

    # Physical fleet artifacts + repo root with the app dirs on disk. The
    # live state is SIGNED — the deploy path refuses unsigned state (R2 B3).
    from integrations import ga4_admin_api

    fleet_path = tmp_path / "ga4-fleet.json"
    fleet_path.write_text(json.dumps(FLEET_CONFIG), encoding="utf-8")
    monkeypatch.setenv("GA4_FLEET_CONFIG", str(fleet_path))
    live_path = tmp_path / "ga4-fleet-live.json"
    signed_live = dict(LIVE_STATE)
    signed_live["sha256"] = ga4_admin_api.canonical_state_sha(LIVE_STATE)
    live_path.write_text(json.dumps(signed_live), encoding="utf-8")
    monkeypatch.setenv("GA4_FLEET_LIVE", str(live_path))
    repo_root = tmp_path / "repo"
    for brand in FLEET_CONFIG["brands"]:
        (repo_root / "apps" / brand["app_dir"]).mkdir(parents=True)
    monkeypatch.setenv("GA4_FLEET_REPO_ROOT", str(repo_root))
    monkeypatch.delenv("GA4_VERCEL_SCOPE", raising=False)

    # A valid edit-scoped token file (fake contents) by default.
    token_path = tmp_path / "ga4-edit-token.json"
    token_path.write_text(_token_payload([EDIT_SCOPE, READONLY_SCOPE]), encoding="utf-8")
    monkeypatch.setenv("GA4_EDIT_TOKEN_FILE", str(token_path))

    yield tmp_path
    action_proposals._EXECUTORS.clear()
    action_proposals._EXECUTORS.update(saved_executors)
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved_registry)


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    return tmp_path / ".homie" / "profiles" / PERSONA


@pytest.fixture
def fleet_files(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=tmp_path / "ga4-fleet.json", live=tmp_path / "ga4-fleet-live.json"
    )


# ── Fakes at the module attrs the client reads (Rule 3) ────────────────────


class _FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


class _FakeAdmin:
    """A stateful analyticsadmin double: list reflects what create added.

    ``fail_stream_create`` simulates Google accepting the property and then
    5xx-ing the stream create — the mid-reconcile partial (Codex R1).
    """

    def __init__(self) -> None:
        self.properties_rows: list[dict] = []
        self.streams_rows: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail_stream_create = False

    def properties(self) -> Any:
        admin = self

        class _Properties:
            def list(self, **kw):
                return _FakeRequest({"properties": list(admin.properties_rows)})

            def create(self, body=None):
                admin.calls.append(("create_property", dict(body or {})))
                row = {
                    "name": f"properties/{900000 + len(admin.properties_rows)}",
                    "displayName": body["displayName"],
                }
                admin.properties_rows.append(row)
                return _FakeRequest(row)

            def dataStreams(self):  # noqa: N802 — mirrors the Google API surface
                class _Streams:
                    def list(self, **kw):
                        return _FakeRequest({"dataStreams": list(admin.streams_rows)})

                    def create(self, parent=None, body=None):
                        admin.calls.append(("create_stream", dict(body or {})))
                        if admin.fail_stream_create:
                            raise RuntimeError("backend 5xx on stream create")
                        row = {
                            "name": f"{parent}/dataStreams/777",
                            "webStreamData": {
                                "defaultUri": body["webStreamData"]["defaultUri"],
                                "measurementId": "G-NEWBRAND99",
                            },
                        }
                        admin.streams_rows.append(row)
                        return _FakeRequest(row)

                return _Streams()

        return _Properties()


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> _FakeAdmin:
    fake = _FakeAdmin()
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: fake)
    return fake


@pytest.fixture
def vercel(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    argv_log: list[list[str]] = []

    def fake_run(argv, **kw):
        argv_log.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return argv_log


def _vercel_argv(argv_log: list[list[str]]) -> list[list[str]]:
    """Only vercel invocations — other machinery (git) may legitimately run."""
    return [a for a in argv_log if "vercel" in Path(a[0]).name]


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], status=200, body="")

    class _Response:
        def __init__(self):
            self.status = state.status

        def read(self):
            body = state.body() if callable(state.body) else state.body
            return body.encode()

        def geturl(self):
            return "https://example/"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=0, **kw):
        state.calls.append((request.full_url, timeout))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return state


# ── Helpers ────────────────────────────────────────────────────────────────


def _register() -> None:
    assert tool_impl_ga4_write.register_tools() == 2
    tool_registry.register_tool(
        "ga4_overview", "read GA4 overview", toolset="seo_geo_read",
        handler=lambda **kw: "overview",
    )
    tool_registry.register_tool(
        "page_read", "read a page", toolset="browser", handler=lambda **kw: "page"
    )
    tool_registry.register_tool(
        "chart_read", "read a chart", toolset="crypto", handler=lambda **kw: "chart"
    )


def _payload(toolsets: list[str]):
    result = persona_tools.build_persona_tool_payload(PERSONA, {"toolsets": toolsets})
    assert result is not None
    return result


def _propose(tool: str, arguments: dict) -> str:
    _register()
    _defs, dispatch = _payload(["ga4_fleet_write"])
    return dispatch(tool, arguments)


def _code_from_card(card: str) -> str:
    match = re.search(r"\*\*Action `([A-Z0-9]{6})`\*\*", card)
    assert match, f"card carries no approval code: {card!r}"
    return match.group(1)


def _approve(code: str, **overrides):
    fields = {
        "user_role": "admin",
        "source": "interactive",
        "actor": "owner",
        "surface": "cli",
        "channel_id": "1",
    }
    fields.update(overrides)
    return action_proposals.decide_action(PERSONA, code, True, **fields)


def _store_rows(profile_dir: Path) -> list[dict]:
    store = profile_dir / "data" / action_proposals.STORE_FILENAME
    if not store.exists():
        return []
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM persona_action_proposals")]
    finally:
        conn.close()


def _ledger_rows(profile_dir: Path) -> list[dict]:
    ledger = profile_dir / "data" / action_proposals.LEDGER_FILENAME
    if not ledger.exists():
        return []
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _experience_body(profile_dir: Path) -> str:
    notes = list((profile_dir / "memory" / "experience").glob("*.md"))
    assert notes, "no experience note written"
    return notes[0].read_text(encoding="utf-8")


def _mint_bundle(tool: str, slug: str) -> dict:
    """A REAL gate-minted bundle, captured before the executor consumes it.

    Proposes through dispatch, swaps in a capture executor, approves — the
    token is minted by the winning CAS and never consumed. The caller then
    re-registers the real tools and fires the bundle at the REAL executor.
    """
    card = _propose(tool, {"brand_slug": slug})
    code = _code_from_card(card)
    captured: dict = {}
    action_proposals.register_action_executor(
        tool, lambda **kw: captured.update(kw) or {"ok": True, "results": []}
    )
    decision = _approve(code)
    assert decision.outcome == action_proposals.DECISION_EXECUTED
    tool_impl_ga4_write.register_tools()  # restore the real executors
    return captured


# ── Propose path: card out, nothing executed ────────────────────────────────


def test_granted_call_proposes_and_calls_nothing(
    profile_dir: Path, google: _FakeAdmin, vercel: list, http: SimpleNamespace
):
    card = _propose("ga4_provision_site", {"brand_slug": "new-brand"})

    assert f"/act approve {PERSONA}" in card
    assert "newbrand.com" in card, "the card names the exact domain"
    assert "accounts/401450559" in card, "the card names the exact account"
    assert google.calls == [] and google.properties_rows == []
    # Other machinery may legitimately shell out (git); VERCEL and HTTP must
    # not be touched at propose time.
    assert _vercel_argv(vercel) == []
    assert http.calls == []
    rows = _store_rows(profile_dir)
    assert len(rows) == 1
    assert rows[0]["status"] == action_proposals.STATUS_PENDING
    stored = json.loads(rows[0]["arguments_json"])
    assert stored["brand_slug"] == "new-brand"
    # The stored target snapshot IS the approval (Codex R1): every externally
    # relevant field is bound by the payload hash.
    assert stored["target"] == {
        "account": "accounts/401450559",
        "domain": "newbrand.com",
        "property_resource": "",
        "time_zone": "America/Los_Angeles",
        "currency_code": "USD",
    }


def test_deploy_card_names_every_target(profile_dir: Path):
    card = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    for needle in (
        "G-TEST12345",
        "waywardinsurance.com",
        "wayward-insurance",
        "apps/wayward-insurance",
        "scope",
        "PRODUCTION DEPLOY",
    ):
        assert needle in card, f"deploy card hides {needle!r}"
    stored = json.loads(_store_rows(profile_dir)[0]["arguments_json"])
    assert stored["target"]["measurement_id"] == "G-TEST12345"
    assert stored["target"]["vercel_project"] == "wayward-insurance"
    assert stored["target"]["app_dir"] == "wayward-insurance"
    assert "vercel_scope" in stored["target"]


def test_unknown_brand_slug_is_an_error_and_leaves_no_row(profile_dir: Path):
    result = _propose("ga4_provision_site", {"brand_slug": "not-a-brand"})
    assert "not in the GA4 fleet config" in result
    assert _store_rows(profile_dir) == []


def test_non_string_slug_is_an_error(profile_dir: Path):
    result = _propose("ga4_provision_site", {"brand_slug": 123})
    assert result.startswith("error: brand_slug must be a string")
    assert _store_rows(profile_dir) == []


def test_deploy_without_live_state_points_at_provision(profile_dir: Path, fleet_files):
    from integrations import ga4_admin_api

    state = json.loads(fleet_files.live.read_text(encoding="utf-8"))
    state.pop("sha256", None)
    state["brands"] = [
        row for row in state["brands"] if row["brand_id"] != "new-brand"
    ]
    state["sha256"] = ga4_admin_api.canonical_state_sha(state)
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")

    result = _propose("ga4_deploy_tag", {"brand_slug": "new-brand"})
    assert "live-state" in result
    assert "ga4_provision_site" in result
    assert _store_rows(profile_dir) == []


def test_deploy_requires_an_explicit_vercel_project(
    profile_dir: Path, fleet_files: SimpleNamespace
):
    """A brand without vercel_project is a malformed fleet row: whole-fleet
    validation refuses it and names the offender — never ambient linkage."""
    config_data = json.loads(fleet_files.config.read_text(encoding="utf-8"))
    config_data["brands"].append(
        {
            "id": "no-project-brand",
            "domain": "noproject.com",
            "locale": "en",
            "app_dir": "no-project-brand",
        }
    )
    fleet_files.config.write_text(json.dumps(config_data), encoding="utf-8")

    result = _propose("ga4_deploy_tag", {"brand_slug": "no-project-brand"})

    assert "no-project-brand" in result
    assert "vercel_project" in result
    assert _store_rows(profile_dir) == []


def test_tampered_live_state_checksum_fails_closed(
    profile_dir: Path, fleet_files: SimpleNamespace
):
    from integrations import ga4_admin_api

    state = json.loads(fleet_files.live.read_text(encoding="utf-8"))
    state["sha256"] = "0" * 64  # a present checksum must verify
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")
    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "checksum" in result
    assert _store_rows(profile_dir) == []

    # A MISSING checksum refuses too (R2 B3): unsigned state never drives a
    # Vercel write.
    state.pop("sha256")
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")
    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "no checksum" in result
    assert _store_rows(profile_dir) == []

    # A correctly checksummed file is accepted.
    state["sha256"] = ga4_admin_api.canonical_state_sha(state)
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")
    card = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "**Action `" in card


def test_live_state_domain_disagreement_is_called_out(profile_dir: Path, fleet_files):
    from integrations import ga4_admin_api

    state = json.loads(fleet_files.live.read_text(encoding="utf-8"))
    state.pop("sha256", None)
    row = next(r for r in state["brands"] if r["brand_id"] == "wayward-insurance")
    row["domain"] = "different-domain.com"
    state["sha256"] = ga4_admin_api.canonical_state_sha(state)  # signed, but wrong
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")
    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "disagrees" in result
    assert _store_rows(profile_dir) == []


# ── The edit-scope gate: fail CLOSED, no proposal, no Google calls ──────────


def test_missing_edit_scope_fails_closed_at_propose(
    tmp_path: Path, profile_dir: Path, google: _FakeAdmin, monkeypatch: pytest.MonkeyPatch
):
    readonly_token = tmp_path / "readonly-token.json"
    readonly_token.write_text(_token_payload([READONLY_SCOPE]), encoding="utf-8")
    monkeypatch.setenv("GA4_EDIT_TOKEN_FILE", str(readonly_token))

    result = _propose("ga4_provision_site", {"brand_slug": "new-brand"})

    assert "analytics.edit" in result
    assert result.startswith("error:")
    assert _store_rows(profile_dir) == []
    assert google.calls == [], "a readonly token must never reach a create call"


# ── Approve: the executor runs the stored payload, receipts everywhere ──────


def test_approve_provision_executes_and_leaves_receipts(
    profile_dir: Path, google: _FakeAdmin
):
    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert [name for name, _ in google.calls] == ["create_property", "create_stream"]
    row = _store_rows(profile_dir)[0]
    assert row["status"] == action_proposals.STATUS_APPROVED
    assert row["status_detail"] == "executed"
    assert "G-NEWBRAND99" in row["outcome_json"]
    body = _experience_body(profile_dir)
    assert "ga4_provision_site" in body
    assert "operator-approved -> executed" in body
    assert "G-NEWBRAND99" in body
    outcomes = [(r["operation"], r["outcome"]) for r in _ledger_rows(profile_dir)]
    assert ("propose", "proposed") in outcomes
    assert ("decide", "approved") in outcomes
    assert ("execute", "executed") in outcomes


def test_reconcile_converges_instead_of_duplicating(
    profile_dir: Path, google: _FakeAdmin
):
    """The second approval finds what the first created: existed, not created."""
    first = _approve(_code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"})))
    assert first.outcome == action_proposals.DECISION_EXECUTED
    assert [name for name, _ in google.calls] == ["create_property", "create_stream"]

    second = _approve(
        _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))
    )

    assert second.outcome == action_proposals.DECISION_EXECUTED
    assert [name for name, _ in google.calls] == ["create_property", "create_stream"], (
        "a retry must converge on the existing resources, not mint siblings"
    )
    row = second.result["results"][0]
    assert "existed" in row["property_detail"]
    assert "existed" in row["stream_detail"]


def test_mid_reconcile_partial_is_recorded_honestly(
    profile_dir: Path, google: _FakeAdmin
):
    """Property created, stream create 5xx: a real property EXISTS, so the
    verdict is partial and the receipt names the created resource."""
    google.fail_stream_create = True
    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_PARTIAL
    row = _store_rows(profile_dir)[0]
    assert row["status_detail"] == "partial"
    outcomes = [(r["operation"], r["outcome"]) for r in _ledger_rows(profile_dir)]
    assert ("execute", "partial") in outcomes
    assert ("execute", "failed") not in outcomes
    body = _experience_body(profile_dir)
    assert "operator-approved -> partial" in body
    assert "stream: failed" in body
    assert "properties/900000" in body, "the created property resource must be named"


def test_approve_deploy_syncs_deploys_and_verifies(
    profile_dir: Path, vercel: list, http: SimpleNamespace
):
    """Deploy causality (R2 B1): the fake site serves the tag ONLY after the
    production deploy call — without one, verification can never pass."""
    def site_body() -> str:
        deployed = any(
            "vercel" in Path(a[0]).name and a[1:2] == ["deploy"] for a in vercel
        )
        return "<html><script>G-TEST12345</script></html>" if deployed else "<html>no tag</html>"

    http.body = site_body
    code = _code_from_card(_propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    vercel_calls = _vercel_argv(vercel)
    links = [a for a in vercel_calls if a[1:2] == ["link"]]
    env_adds = [a for a in vercel_calls if a[1:3] == ["env", "add"]]
    deploys = [a for a in vercel_calls if a[1:2] == ["deploy"]]
    assert links and "--project" in links[0], "an explicit project link is required"
    assert links[0][links[0].index("--project") + 1] == "wayward-insurance"
    assert env_adds, "vercel env add never ran"
    argv = env_adds[0]
    assert "NEXT_PUBLIC_GA_MEASUREMENT_ID" in argv
    assert "production" in argv
    assert argv[argv.index("--value") + 1] == "G-TEST12345"
    assert deploys and "--prod" in deploys[0], "approval must trigger the production deploy"
    assert http.calls and http.calls[0][0] == "https://waywardinsurance.com/"
    body = _experience_body(profile_dir)
    assert "verification: verified" in body


def test_deploy_in_flight_at_deadline_is_partial_not_failed(
    profile_dir: Path, vercel: list, http: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    """Env synced + deploy triggered + tag not yet live = honest partial."""
    monkeypatch.setenv("GA4_VERIFY_DEADLINE_S", "1")
    monkeypatch.setenv("GA4_VERIFY_INTERVAL_S", "0.05")
    http.body = "<html>no tag here</html>"  # the build never serves it in time
    code = _code_from_card(_propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_PARTIAL
    row = decision.result["results"][0]
    assert row["status"] == "deployed"
    assert row["verification"] == "failed"
    assert "not yet live" in row["verification_detail"]
    assert any(a[1:2] == ["deploy"] for a in _vercel_argv(vercel)), (
        "the production deploy must have been triggered before the poll"
    )
    body = _experience_body(profile_dir)
    assert "deployed" in body and "verification: failed" in body


def test_failed_vercel_sync_is_a_failed_execution(
    profile_dir: Path, http: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    def failing_run(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="not linked")

    monkeypatch.setattr(subprocess, "run", failing_run)
    code = _code_from_card(_propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    assert _store_rows(profile_dir)[0]["status_detail"] == "failed"
    assert http.calls == [], "verification must not run when the sync failed"


# ── Snapshot drift: approved target vs physical artifacts (BLOCKER) ─────────


def test_config_drift_between_propose_and_approve_refuses_without_calls(
    profile_dir: Path, fleet_files: SimpleNamespace, vercel: list, http: SimpleNamespace
):
    """Approve a card for project wayward-insurance, then the fleet row says
    victim-production: the executor must refuse, touching nothing."""
    code = _code_from_card(_propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"}))
    config_data = json.loads(fleet_files.config.read_text(encoding="utf-8"))
    config_data["brands"][1]["vercel_project"] = "victim-production"
    fleet_files.config.write_text(json.dumps(config_data), encoding="utf-8")

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    result_row = decision.result["results"][0]
    assert result_row["status"] == "refused"
    assert "mismatch" in result_row["detail"]
    assert "victim-production" in result_row["detail"]
    assert _vercel_argv(vercel) == [] and http.calls == []


# ── Token boundary negatives, BOTH executors (Codex R1 MAJOR) ───────────────
#
# Every case asserts the provider layers recorded ZERO calls. The replay case
# executes once legitimately, so its assertion is on the delta.


@pytest.mark.parametrize(
    ("tool", "slug"),
    [("ga4_provision_site", "new-brand"), ("ga4_deploy_tag", "wayward-insurance")],
)
def test_executor_refuses_without_a_token(
    tool: str, slug: str, google: _FakeAdmin, vercel: list, http: SimpleNamespace
):
    bundle = _mint_bundle(tool, slug)
    executor = action_proposals.get_action_executor(tool)
    receipt = executor(
        persona_id=PERSONA,
        action_id=bundle["action_id"],
        execution_token="",
        arguments=bundle["arguments"],
    )
    assert receipt["ok"] is False
    assert receipt["results"][0]["status"] == "refused"
    assert google.calls == [] and _vercel_argv(vercel) == [] and http.calls == []


@pytest.mark.parametrize(
    ("tool", "slug"),
    [("ga4_provision_site", "new-brand"), ("ga4_deploy_tag", "wayward-insurance")],
)
def test_executor_refuses_a_replayed_token(
    tool: str, slug: str, google: _FakeAdmin, vercel: list, http: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    http.body = "G-TEST12345"  # let the first (legitimate) execution succeed
    bundle = _mint_bundle(tool, slug)
    executor = action_proposals.get_action_executor(tool)
    first = executor(
        persona_id=PERSONA,
        action_id=bundle["action_id"],
        execution_token=bundle["execution_token"],
        arguments=bundle["arguments"],
    )
    assert first["results"][0]["status"] != "refused"
    google_calls = len(google.calls)
    vercel_calls = len(_vercel_argv(vercel))

    second = executor(
        persona_id=PERSONA,
        action_id=bundle["action_id"],
        execution_token=bundle["execution_token"],
        arguments=bundle["arguments"],
    )

    assert second["results"][0]["status"] == "refused"
    assert len(google.calls) == google_calls
    assert len(_vercel_argv(vercel)) == vercel_calls


@pytest.mark.parametrize(
    ("tool", "slug"),
    [("ga4_provision_site", "new-brand"), ("ga4_deploy_tag", "wayward-insurance")],
)
def test_executor_refuses_a_payload_mismatch(
    tool: str, slug: str, google: _FakeAdmin, vercel: list, http: SimpleNamespace
):
    """Same token, arguments that are NOT the stored payload (an extra key
    keeps the drift check passing so only the token hash can catch this)."""
    bundle = _mint_bundle(tool, slug)
    executor = action_proposals.get_action_executor(tool)
    tampered_args = {**bundle["arguments"], "smuggled": True}
    receipt = executor(
        persona_id=PERSONA,
        action_id=bundle["action_id"],
        execution_token=bundle["execution_token"],
        arguments=tampered_args,
    )
    assert receipt["results"][0]["status"] == "refused"
    assert google.calls == [] and _vercel_argv(vercel) == [] and http.calls == []


def test_deploy_executor_refuses_a_provision_actions_token(
    google: _FakeAdmin, vercel: list, http: SimpleNamespace
):
    """Cross-action: a token minted for one action authorizes no other."""
    provision_bundle = _mint_bundle("ga4_provision_site", "new-brand")
    # A deploy-shaped payload that MATCHES the physical artifacts (so the
    # drift check passes) paired with the provision action's id+token.
    deploy_executor = action_proposals.get_action_executor("ga4_deploy_tag")
    deploy_args = {
        "brand_slug": "wayward-insurance",
        "target": tool_impl_ga4_write._deploy_target("wayward-insurance")[0],
    }
    receipt = deploy_executor(
        persona_id=PERSONA,
        action_id=provision_bundle["action_id"],
        execution_token=provision_bundle["execution_token"],
        arguments=deploy_args,
    )
    assert receipt["results"][0]["status"] == "refused"
    assert google.calls == [] and _vercel_argv(vercel) == [] and http.calls == []


@pytest.mark.parametrize(
    ("tool", "slug"),
    [("ga4_provision_site", "new-brand"), ("ga4_deploy_tag", "wayward-insurance")],
)
def test_executor_refuses_a_cross_persona_token(
    tool: str, slug: str, google: _FakeAdmin, vercel: list, http: SimpleNamespace
):
    """A token lives in the persona's own store; another persona id misses."""
    bundle = _mint_bundle(tool, slug)
    executor = action_proposals.get_action_executor(tool)
    receipt = executor(
        persona_id="another-persona",
        action_id=bundle["action_id"],
        execution_token=bundle["execution_token"],
        arguments=bundle["arguments"],
    )
    assert receipt["results"][0]["status"] == "refused"
    assert google.calls == [] and _vercel_argv(vercel) == [] and http.calls == []


# ── Verification exactness + totality (Codex R1 MAJOR) ──────────────────────


def test_verify_tag_live_is_boundary_exact(http: SimpleNamespace):
    from integrations import ga4_admin_api

    http.body = "<html>G-ABCD only</html>"
    result = ga4_admin_api.verify_tag_live("example.com", "G-ABC")
    assert result["ok"] is False and result["tag_present"] is False, (
        "G-ABC must NOT match a page containing only G-ABCD"
    )
    http.body = '<html><script src=".../G-ABC/collect"></html>'
    result = ga4_admin_api.verify_tag_live("example.com", "G-ABC")
    assert result["ok"] is True and result["tag_present"] is True


def test_verify_tag_live_never_raises_on_garbage(http: SimpleNamespace):
    from integrations import ga4_admin_api

    bad_domain = ga4_admin_api.verify_tag_live("evil\n.com", "G-ABC")
    assert bad_domain["ok"] is False and "invalid domain" in bad_domain["detail"]
    assert http.calls == [], "an invalid domain never reaches the network"

    bad_id = ga4_admin_api.verify_tag_live("example.com", "not-a-mid")
    assert bad_id["ok"] is False and "invalid measurement id" in bad_id["detail"]

    # A garbage timeout coerces instead of raising.
    http.body = "G-ABC"
    result = ga4_admin_api.verify_tag_live("example.com", "G-ABC", timeout="not-an-int")
    assert result["ok"] is True


def test_verify_tag_live_converts_transport_failures(http: SimpleNamespace):
    from integrations import ga4_admin_api

    def boom(request, timeout=0, **kw):
        raise urllib.error.URLError("connection refused")

    http_real = urllib.request.urlopen
    try:
        import urllib.request as rq

        rq.urlopen = boom
        result = ga4_admin_api.verify_tag_live("example.com", "G-ABC")
    finally:
        import urllib.request as rq

        rq.urlopen = http_real
    assert result["ok"] is False
    assert "URLError" in result["detail"]


# ── app_dir confinement (Codex R1 MAJOR) ─────────────────────────────────────


def test_resolve_app_path_confines_to_the_apps_boundary(tmp_path: Path):
    from integrations import ga4_admin_api

    for bad in ("../../docs", "..", "/abs/path", "a/b", "a\\b", ""):
        with pytest.raises(ga4_admin_api.FleetConfigError):
            ga4_admin_api.resolve_app_path(bad)
    resolved = ga4_admin_api.resolve_app_path("new-brand")
    assert resolved.is_dir()
    assert resolved.parent.name == "apps"


# ── Scope discipline ─────────────────────────────────────────────────────────


def test_ungranted_persona_is_out_of_scope(profile_dir: Path, google: _FakeAdmin):
    _register()
    _defs, dispatch = _payload(["seo_geo_read"])

    result = json.loads(dispatch("ga4_provision_site", {"brand_slug": "new-brand"}))

    assert "not in this persona's granted scope" in result["error"]
    assert _store_rows(profile_dir) == []
    assert google.calls == []


def test_request_tool_cannot_elevate_a_ga4_write_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from runtime import persona_elevation

    _register()
    persona_elevation.register_tools()
    monkeypatch.setattr(
        persona_tools, "PERSONA_CHAT_BASE_TOOLS", ("request_tool",), raising=False
    )
    context = {
        "persona_id": PERSONA,
        "platform": "cli",
        "channel_id": "chan-1",
        "thread_id": "chan-1",
        "session_key": "cli:test:test",
        "turn_id": "turn-1",
        "original_user_id": "operator-1",
        "original_user_name": "Operator",
        "original_user_role": "admin",
        "original_text": "provision the new brand",
        "has_attachments": False,
        "project_root": str(tmp_path),
    }
    payload = persona_tools.build_persona_tool_payload(
        PERSONA, {"toolsets": ["safe_core"]}, request_context=context
    )
    assert payload is not None

    result = json.loads(
        payload[1](
            "request_tool",
            {
                "tool": "ga4_provision_site",
                "reason": "need one provision",
                "arguments": {"brand_slug": "new-brand"},
            },
        )
    )

    assert result["status"] == "refused"


@pytest.mark.parametrize("toolset", ["seo_geo_read", "browser", "crypto", "social"])
def test_sibling_toolsets_never_surface_the_ga4_writes(toolset: str):
    _register()
    defs, _dispatch = _payload([toolset])
    names = {row["function"]["name"] for row in defs}
    assert "ga4_provision_site" not in names
    assert "ga4_deploy_tag" not in names


def test_ga4_toolset_surfaces_exactly_the_write_tools():
    _register()
    defs, _dispatch = _payload(["ga4_fleet_write"])
    names = {row["function"]["name"] for row in defs}
    assert {"ga4_provision_site", "ga4_deploy_tag"} <= names


# ── Registration shape ───────────────────────────────────────────────────────


def test_tools_register_as_dedicated_gate_persona_scoped_writes():
    assert tool_impl_ga4_write.register_tools() == 2
    for name in ("ga4_provision_site", "ga4_deploy_tag"):
        entry = tool_registry.get_entry(name)
        assert entry is not None
        assert entry.toolset == "ga4_fleet_write"
        assert entry.effect == "write"
        assert entry.dedicated_gate is True
        assert entry.elevatable is False
        assert entry.persona_scoped is True
        assert action_proposals.get_action_executor(name) is not None


# ── The REAL wiring, end to end (anti-vacuity anchor) ───────────────────────
#
# Everything above isolates with a synthetic toolset registry — exactly how a
# broken real wiring hides. This anchor loads the SHIPPED registry via the
# REAL bootstrap; reverting either the toolsets.py entry or the tool_impl.py
# import block fails it.


def test_real_wiring_full_bootstrap_proposes(profile_dir: Path, google: _FakeAdmin):
    import importlib

    import runtime.toolsets as real_toolsets

    importlib.reload(real_toolsets)  # rebind the SHIPPED registry

    payload = persona_tools.build_persona_tool_payload(
        PERSONA, {"toolsets": ["ga4_fleet_write"]}
    )
    assert payload is not None, "real ga4_fleet_write scope assembled to nothing"
    defs, dispatch = payload
    names = {(d.get("function") or {}).get("name") for d in defs}
    assert {"ga4_provision_site", "ga4_deploy_tag"} <= names

    card = dispatch("ga4_provision_site", {"brand_slug": "new-brand"})
    assert "**Action `" in card, f"expected a proposal card, got: {card[:200]!r}"
    assert "not in this persona's granted scope" not in card
    assert len(_store_rows(profile_dir)) == 1
    assert google.calls == []


# ── Kill-switch-first + credential scoping (R2 M5) ──────────────────────────


def test_kill_switch_fires_before_any_credential_or_config_io(
    profile_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """OFF is side-effect-free: with the switch disabled AND a credential
    file that does not exist AND a config path that does not exist, the error
    must be the kill switch — proving no I/O happened before it."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS", "disabled")
    monkeypatch.setenv("GA4_EDIT_TOKEN_FILE", "/nonexistent/token.json")
    monkeypatch.setenv("GA4_FLEET_CONFIG", "/nonexistent/fleet.json")

    _register()
    _defs, dispatch = _payload(["ga4_fleet_write"])
    result = json.loads(dispatch("ga4_provision_site", {"brand_slug": "new-brand"}))

    assert "KillSwitchDisabled" in result["error"]
    assert "nonexistent" not in result["error"], "credential/config I/O ran first"
    assert _store_rows(profile_dir) == []


def test_deploy_needs_no_google_credential(
    tmp_path: Path, profile_dir: Path, google: _FakeAdmin, vercel: list,
    http: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
):
    """Deploy is Vercel + urllib only — it must work with NO edit token."""
    monkeypatch.setenv("GA4_EDIT_TOKEN_FILE", str(tmp_path / "missing-token.json"))
    http.body = "<html>G-TEST12345</html>"
    code = _code_from_card(_propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert google.calls == [], "deploy must never call the Google API"


# ── Canonical convergence (R2 B2) ────────────────────────────────────────────


def test_provision_converges_on_the_canonical_display_name(
    profile_dir: Path, google: _FakeAdmin
):
    """A property the ga4-ops skill created (`YourBusiness Fleet | <domain>`)
    must be REUSED, never duplicated."""
    google.properties_rows.append(
        {"name": "properties/481148336", "displayName": "YourBusiness Fleet | newbrand.com"}
    )
    google.streams_rows.append(
        {
            "name": "properties/481148336/dataStreams/10350277981",
            "webStreamData": {
                "defaultUri": "https://newbrand.com",
                "measurementId": "G-EXISTING1",
            },
        }
    )
    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert google.calls == [], "an existing canonical property must never be re-created"
    row = decision.result["results"][0]
    assert "existed" in row["property_detail"]
    assert "481148336" in row["property_detail"]
    assert "G-EXISTING1" in row["detail"]


def test_declared_property_resource_is_matched_exactly(
    profile_dir: Path, google: _FakeAdmin, fleet_files: SimpleNamespace
):
    config_data = json.loads(fleet_files.config.read_text(encoding="utf-8"))
    config_data["brands"][0]["property_resource"] = "properties/481148336"
    fleet_files.config.write_text(json.dumps(config_data), encoding="utf-8")
    google.properties_rows.append(
        {"name": "properties/481148336", "displayName": "Legacy Name"}
    )
    google.streams_rows.append(
        {
            "name": "properties/481148336/dataStreams/10350277981",
            "webStreamData": {
                "defaultUri": "https://newbrand.com",
                "measurementId": "G-EXISTING1",
            },
        }
    )
    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert google.calls == []
    assert "481148336" in decision.result["results"][0]["property_detail"]


def test_declared_property_resource_missing_fails_without_creating(
    profile_dir: Path, google: _FakeAdmin, fleet_files: SimpleNamespace
):
    config_data = json.loads(fleet_files.config.read_text(encoding="utf-8"))
    config_data["brands"][0]["property_resource"] = "properties/999999999"
    fleet_files.config.write_text(json.dumps(config_data), encoding="utf-8")
    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    assert google.calls == [], "a missing declared resource must not mint a sibling"
    assert "999999999" in decision.result["results"][0]["property_detail"]


def test_ambiguous_display_name_matches_are_refused(
    profile_dir: Path, google: _FakeAdmin
):
    for name in ("properties/111", "properties/222"):
        google.properties_rows.append(
            {"name": name, "displayName": "YourBusiness Fleet | newbrand.com"}
        )
    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    assert google.calls == [], "ambiguity must never resolve to a guess"
    assert "ambiguous" in decision.result["results"][0]["property_detail"]


# ── Whole-fleet validation (R2 B3) ───────────────────────────────────────────


def test_duplicate_domain_in_desired_config_refuses_everything(
    profile_dir: Path, fleet_files: SimpleNamespace
):
    config_data = json.loads(fleet_files.config.read_text(encoding="utf-8"))
    config_data["brands"][1]["domain"] = "newbrand.com"  # collision
    fleet_files.config.write_text(json.dumps(config_data), encoding="utf-8")

    result = _propose("ga4_provision_site", {"brand_slug": "new-brand"})

    assert "duplicate domain" in result
    assert "newbrand.com" in result
    assert _store_rows(profile_dir) == []


def test_duplicate_measurement_id_in_live_state_refuses_deploy(
    profile_dir: Path, fleet_files: SimpleNamespace
):
    from integrations import ga4_admin_api

    state = json.loads(fleet_files.live.read_text(encoding="utf-8"))
    state.pop("sha256", None)
    second = dict(next(r for r in state["brands"] if r["brand_id"] == "wayward-insurance"))
    second["brand_id"] = "other-brand"
    state["brands"].append(second)  # same measurement id twice
    state["sha256"] = ga4_admin_api.canonical_state_sha(state)
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")

    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})

    assert "duplicate measurement_id" in result
    assert _store_rows(profile_dir) == []


# ── Boundary-exact verification (R2 B4) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<html>xG-ABC</html>", False),
        ("<html>G-ABCD</html>", False),
        ("<html>G-ABC-EXTRA</html>", False),
        ("<html>9G-ABC</html>", False),
        ("<html>/G-ABC/</html>", True),
        ('<html>src="https://www.googletagmanager.com/gtag/js?id=G-ABC"</html>', True),
    ],
)
def test_verify_tag_live_rejects_decoy_ids(http: SimpleNamespace, body, expected):
    from integrations import ga4_admin_api

    http.body = body
    result = ga4_admin_api.verify_tag_live("example.com", "G-ABC")
    assert result["ok"] is expected and result["tag_present"] is expected, body


def test_verify_tag_live_survives_a_hostile_str(http: SimpleNamespace):
    """An object whose __str__ raises still yields ok=False (R2 M7)."""
    from integrations import ga4_admin_api

    class Hostile:
        def __str__(self):
            raise RuntimeError("boom-str")

        def __repr__(self):
            raise RuntimeError("boom-repr")

    result = ga4_admin_api.verify_tag_live(Hostile(), "G-ABC")
    assert result["ok"] is False
    assert "invalid domain" in result["detail"]
    result = ga4_admin_api.verify_tag_live("example.com", Hostile())
    assert result["ok"] is False
    assert "invalid measurement id" in result["detail"]


# ── Vercel substep honesty (R2 M6) ───────────────────────────────────────────


def test_landed_link_plus_failed_env_is_partial_with_receipt(
    profile_dir: Path, http: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    def fail_env_only(argv, **kw):
        if "vercel" in Path(argv[0]).name and argv[1:2] == ["env"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="env denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fail_env_only)
    code = _code_from_card(_propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_PARTIAL
    row = decision.result["results"][0]
    assert row["status"] == "linked"
    assert row["env_sync"] == "failed"
    assert "env denied" in row["env_detail"]
    assert http.calls == [], "verification must not run when the env write failed"
    body = _experience_body(profile_dir)
    assert "linked; env_sync: failed" in body


# ── Codex R3 B1 — the Vercel team is bound, named, and explicit ─────────────
#
# A bare vercel command searches ACROSS teams and auto-selects the CLI's
# current-team match: approving project `foo` could deploy another org's `foo`.
# The snapshot binds the resolved team, the card names it, and every substep
# argv carries --scope explicitly.


def test_deploy_snapshot_binds_the_resolved_vercel_scope(profile_dir: Path):
    # GA4_VERCEL_SCOPE unset in the fixture -> the canonical ga4-ops team.
    card = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    stored = json.loads(_store_rows(profile_dir)[0]["arguments_json"])
    scope = stored["target"]["vercel_scope"]
    assert scope == "your-github-users-projects"
    assert scope in card


def test_every_vercel_substep_carries_the_approved_scope(
    profile_dir: Path, vercel: list[list[str]], http, monkeypatch
):
    monkeypatch.setenv("GA4_VERCEL_SCOPE", "fleet-team")
    card = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    code = re.search(r"\*\*Action `([A-Z0-9]{6})`\*\*", card).group(1)
    http.body = '<script src="https://www.googletagmanager.com/gtag/js?id=G-TEST12345"></script>'

    decision = action_proposals.decide_action(
        PERSONA, code, True,
        user_role="admin", source="interactive", actor="owner",
    )
    assert decision.outcome == action_proposals.DECISION_EXECUTED
    argv = _vercel_argv(vercel)
    assert len(argv) == 3, f"expected link+env+deploy, got {argv}"
    for call in argv:
        assert "--scope" in call, f"substep missing --scope: {call}"
        assert call[call.index("--scope") + 1] == "fleet-team"


def test_garbage_scope_is_refused_at_propose(profile_dir: Path, monkeypatch):
    monkeypatch.setenv("GA4_VERCEL_SCOPE", "Not A Team!")
    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "not a team slug" in result
    assert _store_rows(profile_dir) == []


# ── Codex R3 B2 — canonical fleet validation: aliases and partial state ─────
#
# Uniqueness compares canonical identities (casefolded, www-stripped domains):
# apps/Brand and apps/brand are one directory on Windows; www.example.com and
# example.com are one site. And a signed but PARTIAL live state (brand_id +
# measurement_id only) can never drive a deploy.


def _write_config(fleet_files, config):
    fleet_files.config.write_text(json.dumps(config), encoding="utf-8")


def test_case_variant_app_dir_is_a_duplicate(profile_dir: Path, fleet_files):
    config = json.loads(fleet_files.config.read_text(encoding="utf-8"))
    twin = dict(config["brands"][1])
    twin["id"] = "wayward-twin"
    twin["domain"] = "wayward-twin.com"
    twin["app_dir"] = "WAYWARD-INSURANCE"  # same physical dir on this host
    twin["vercel_project"] = "wayward-twin"
    config["brands"].append(twin)
    _write_config(fleet_files, config)

    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "duplicate app_dir" in result
    assert _store_rows(profile_dir) == []


def test_www_and_bare_domain_are_one_site(profile_dir: Path, fleet_files):
    config = json.loads(fleet_files.config.read_text(encoding="utf-8"))
    twin = dict(config["brands"][1])
    twin["id"] = "wayward-twin"
    twin["domain"] = "www.waywardinsurance.com"  # canonical form collides
    twin["app_dir"] = "wayward-twin"
    twin["vercel_project"] = "wayward-twin"
    config["brands"].append(twin)
    _write_config(fleet_files, config)

    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "duplicate domain" in result
    assert _store_rows(profile_dir) == []


def test_signed_but_partial_live_state_cannot_drive_deploy(
    profile_dir: Path, fleet_files
):
    from integrations import ga4_admin_api

    state = json.loads(fleet_files.live.read_text(encoding="utf-8"))
    state.pop("sha256", None)
    for row in state["brands"]:
        # Strip everything but identity + an arbitrary measurement id.
        for field in ("account", "domain", "app_dir", "vercel_project", "property", "stream"):
            row.pop(field, None)
    state["sha256"] = ga4_admin_api.canonical_state_sha(state)  # correctly signed
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")

    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "fails fleet validation" in result or "missing" in result
    assert _store_rows(profile_dir) == []


def test_live_state_must_cover_the_whole_fleet(profile_dir: Path, fleet_files):
    from integrations import ga4_admin_api

    state = json.loads(fleet_files.live.read_text(encoding="utf-8"))
    state.pop("sha256", None)
    state["brands"] = [
        row for row in state["brands"] if row["brand_id"] == "wayward-insurance"
    ]
    state["sha256"] = ga4_admin_api.canonical_state_sha(state)
    fleet_files.live.write_text(json.dumps(state), encoding="utf-8")

    result = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    assert "covers 1 brand" in result
    assert _store_rows(profile_dir) == []


# ── Codex R3 B3 — reconcile is serialized per target; duplicates refuse ─────


def test_reconcile_runs_under_a_per_target_lock(
    profile_dir: Path, google: _FakeAdmin, monkeypatch
):
    import shared
    from integrations import ga4_admin_api

    locks: list[str] = []
    real_lock = shared.file_lock

    def recording_lock(path, timeout=30.0):
        locks.append(Path(path).name)
        return real_lock(path, timeout=timeout)

    monkeypatch.setattr(shared, "file_lock", recording_lock)
    monkeypatch.setattr(
        ga4_admin_api, "file_lock", recording_lock, raising=False
    )

    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))
    decision = _approve(code)
    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert locks, "reconcile ran without the cross-process lock"
    assert any("accounts-401450559-newbrand-com" in name for name in locks)


def test_reconcile_lock_held_elsewhere_times_out_without_creating(
    profile_dir: Path, google: _FakeAdmin, monkeypatch
):
    import shared
    from integrations import ga4_admin_api

    monkeypatch.setattr(ga4_admin_api, "_RECONCILE_LOCK_TIMEOUT_S", 0.2)
    lock_path = Path(str(config.DATA_DIR)) / "ga4-reconcile-accounts-401450559-newbrand-com"
    with shared.file_lock(lock_path, timeout=5):
        code = _code_from_card(
            _propose("ga4_provision_site", {"brand_slug": "new-brand"})
        )
        decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    assert [c for c in google.calls if "create" in str(c)] == []


def test_ambiguous_existing_streams_are_refused(
    profile_dir: Path, google: _FakeAdmin, monkeypatch
):
    # The fleet row already exists AND has two streams for the same URI.
    google.properties_rows.append(
        {
            "name": "properties/555000111",
            "displayName": "YourBusiness Fleet | newbrand.com",
        }
    )
    dup = {
        "name": "properties/555000111/dataStreams/111",
        "webStreamData": {"defaultUri": "https://newbrand.com"},
    }
    dup2 = dict(dup)
    dup2["name"] = "properties/555000111/dataStreams/222"
    google.streams_rows.extend([dup, dup2])

    code = _code_from_card(_propose("ga4_provision_site", {"brand_slug": "new-brand"}))
    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_PARTIAL
    row = decision.result["results"][0]
    assert "ambiguous" in row["detail"]
    creates = [c for c in google.calls if "dataStreams" in str(c) and "create" in str(c)]
    assert creates == []


# ── Codex R3 M1 — a FAILED vercel link can still change physical linkage ────
#
# vercel link deletes .vercel/project.json before its network work. A nonzero
# exit after that point leaves the app UNLINKED — the receipt must say state
# changed (partial), never "nothing happened".


def _approve_deploy_with_link_file(profile_dir, fleet_files, http, monkeypatch, fake_run):
    app_dir = Path(str(fleet_files.config)).parent / "repo" / "apps" / "wayward-insurance"
    (app_dir / ".vercel").mkdir()
    (app_dir / ".vercel" / "project.json").write_text('{"projectId":"old"}', encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", fake_run)
    code = _code_from_card(_propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"}))
    return _approve(code)


def test_failed_link_that_unlinks_the_app_is_a_partial(
    profile_dir: Path, fleet_files, http, monkeypatch
):
    def fake_run(argv, **kw):
        if "link" in argv:
            # the real CLI deletes the link file before failing
            link_file = Path(argv[-1]) / ".vercel" / "project.json"
            link_file.unlink(missing_ok=True)
            return SimpleNamespace(returncode=1, stdout="", stderr="auth denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ran: list[list[str]] = []

    def recording_run(argv, **kw):
        ran.append(list(argv))
        return fake_run(argv, **kw)

    decision = _approve_deploy_with_link_file(
        profile_dir, fleet_files, http, monkeypatch, recording_run
    )
    assert decision.outcome == action_proposals.DECISION_PARTIAL
    row = decision.result["results"][0]
    assert row["status"] == "link_mutated"
    assert "linkage changed" in row["detail"]
    # only the link attempt ran — env/deploy never fired
    assert len(_vercel_argv(ran)) == 1


def test_failed_link_without_mutation_is_a_clean_failure(
    profile_dir: Path, fleet_files, http, monkeypatch
):
    def fake_run(argv, **kw):
        if "link" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="auth denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    decision = _approve_deploy_with_link_file(
        profile_dir, fleet_files, http, monkeypatch, fake_run
    )
    assert decision.outcome == action_proposals.DECISION_FAILED


# ── Codex R3 M2 — /act converges the proposal's OWN tool, not a fixed set ───
#
# The chat handler used to converge {x_follow_accounts, x_enable_notifications}
# only; ensure_tools_registered short-circuits on a merely non-empty registry,
# so a chat process with X loaded and GA4 not loaded recorded an approved GA4
# action as failed ("no executor"). This test reproduces exactly that world:
# registry holds ONLY the X tools, the proposal is GA4, approval comes through
# the REAL /act handler.


class _ActIncoming:
    """The bounded slice of IncomingMessage handle_act reads."""

    def __init__(self):
        self.user_role = "admin"
        self.user = SimpleNamespace(platform_id="owner")
        self.channel = SimpleNamespace(platform_id="1")
        self.platform = SimpleNamespace(value="test")
        self.source = "interactive"


def test_act_approve_converges_the_proposals_own_tool(
    profile_dir: Path, google: _FakeAdmin, vercel: list, http: SimpleNamespace
):
    import asyncio

    import core_handlers

    from runtime import tool_impl_x_write

    # The R3 world, reproduced faithfully: the PROPOSAL was created by another
    # process (dashboard); THIS process (chat) has X registered and GA4 not.
    tool_impl_x_write.register_tools()

    http.body = '<script src="https://www.googletagmanager.com/gtag/js?id=G-TEST12345"></script>'
    card = _propose("ga4_deploy_tag", {"brand_slug": "wayward-insurance"})
    code = _code_from_card(card)

    # The propose above ran the full bootstrap in-process; unwind the GA4 half
    # to simulate the separate chat process where GA4 never loaded.
    for name in ("ga4_provision_site", "ga4_deploy_tag"):
        tool_registry.unregister_tool(name)
    action_proposals._EXECUTORS.pop("ga4_provision_site", None)
    action_proposals._EXECUTORS.pop("ga4_deploy_tag", None)

    reply = asyncio.run(
        core_handlers.handle_act(None, _ActIncoming(), f"approve {PERSONA} {code}")
    )

    assert "no executor" not in reply.lower()
    rows = _store_rows(profile_dir)
    assert rows[0]["status"] == "approved"
    assert len(_vercel_argv(vercel)) == 3, "deploy substeps must actually run"
