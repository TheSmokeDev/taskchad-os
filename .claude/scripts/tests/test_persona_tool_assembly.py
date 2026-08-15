"""Persona turn assembly — where scoping becomes real (#244 + #242).

The epic's success criteria, asserted:

* a persona answers by acting, on any lane, without a refusal
* adding a capability to ONE persona does not add it to the other twenty-four
* a persona that declares nothing behaves EXACTLY as it did before this epic

The dispatcher built here is the chokepoint: kill switch, scope re-check, and
audit row all live inside it, so a caller cannot assemble tools without also
getting the guardrails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime import persona_tools, tool_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    yield
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved)


@pytest.fixture
def _registered(monkeypatch):
    """Two tools in two different toolsets — the scoping fixture."""
    calls = []
    tool_registry.register_tool(
        "chart_read", "read a chart", toolset="crypto",
        handler=lambda **kw: calls.append(("chart_read", kw)) or "chart",
    )
    tool_registry.register_tool(
        "page_read", "read a page", toolset="browser",
        handler=lambda **kw: calls.append(("page_read", kw)) or "page",
    )
    monkeypatch.setattr(
        "runtime.toolsets.TOOLSETS",
        {
            "crypto": {"description": "d", "tools": ["chart_read"], "includes": []},
            "browser": {"description": "d", "tools": ["page_read"], "includes": []},
        },
        raising=False,
    )
    return calls


# ---------------------------------------------------------------------------
# Default-deny: no declaration, no tools
# ---------------------------------------------------------------------------


def test_a_persona_that_declares_nothing_gets_no_tools():
    """None is the DEFAULT-DENY answer, not an error.

    Every persona that has not opted in behaves exactly as it did before the
    epic — which is what makes this safe to ship to 25 live profiles at once.
    """
    assert persona_tools.build_persona_tool_payload("sales", {}) is None
    assert persona_tools.build_persona_tool_payload("sales", {"persona": {"name": "x"}}) is None
    assert persona_tools.build_persona_tool_payload("sales", None) is None


def test_an_empty_or_unknown_toolset_yields_no_payload():
    """Unknown names resolve to nothing — fail-closed, never a blanket grant."""
    assert persona_tools.build_persona_tool_payload("x", {"toolsets": []}) is None
    assert persona_tools.build_persona_tool_payload("x", {"toolsets": ["nope"]}) is None


def test_registration_retries_until_required_registry_entries_exist(
    monkeypatch,
):
    calls = []

    def register_incrementally():
        calls.append(len(calls) + 1)
        name = "first" if len(calls) == 1 else "second"
        tool_registry.register_tool(
            name,
            name,
            toolset="safe_core",
            handler=lambda: name,
        )
        return 1

    monkeypatch.setattr(
        "runtime.tool_impl.register_tools",
        register_incrementally,
    )

    persona_tools.ensure_tools_registered({"first", "second"})
    assert calls == [1]
    persona_tools.ensure_tools_registered({"first", "second"})
    assert calls == [1, 2]
    persona_tools.ensure_tools_registered({"first", "second"})
    assert calls == [1, 2]


# ---------------------------------------------------------------------------
# The epic's headline criterion
# ---------------------------------------------------------------------------


def test_one_persona_gaining_a_capability_does_not_grant_it_to_another(_registered):
    """"Adding a capability to one persona does not add it to the other 24."

    If this ever passes trivially (both empty, both identical), scoping is
    decorative and the epic did not happen.
    """
    crypto = persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]})
    browser = persona_tools.build_persona_tool_payload("browser_ops", {"toolsets": ["browser"]})

    assert crypto is not None and browser is not None
    crypto_names = {d["function"]["name"] for d in crypto[0]}
    browser_names = {d["function"]["name"] for d in browser[0]}

    assert crypto_names == {"chart_read"}
    assert browser_names == {"page_read"}
    assert crypto_names.isdisjoint(browser_names), "scopes bled into each other"


def test_individual_grants_are_additive_and_still_registry_gated(_registered):
    payload = persona_tools.build_persona_tool_payload(
        "crypto", {"toolsets": ["crypto"], "tools": ["page_read", "never_registered"]}
    )
    names = {d["function"]["name"] for d in payload[0]}
    assert names == {"chart_read", "page_read"}, (
        "an individual grant must add a REGISTERED tool and skip an unknown one"
    )


def test_individual_grant_schema_is_a_defensive_copy(_registered):
    first = persona_tools.build_persona_tool_payload(
        "crypto",
        {"toolsets": ["crypto"], "tools": ["page_read"]},
    )
    page_schema = next(
        row for row in first[0] if row["function"]["name"] == "page_read"
    )
    page_schema["function"]["name"] = "poisoned"

    stored = tool_registry.get_entry("page_read")
    assert stored.schema["function"]["name"] == "page_read"
    second = persona_tools.build_persona_tool_payload(
        "crypto",
        {"toolsets": ["crypto"], "tools": ["page_read"]},
    )
    assert {row["function"]["name"] for row in second[0]} == {
        "chart_read",
        "page_read",
    }


# ---------------------------------------------------------------------------
# The dispatch chokepoint
# ---------------------------------------------------------------------------


def test_dispatch_executes_the_registered_handler(_registered):
    _defs, dispatch = persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]})
    assert dispatch("chart_read", {"symbol": "BTC"}) == "chart"
    assert _registered == [("chart_read", {"symbol": "BTC"})]


def test_dispatch_injects_identity_for_persona_scoped_tools(monkeypatch):
    observed = []
    tool_registry.register_tool(
        "whoami",
        "show the bound persona",
        toolset="identity",
        persona_scoped=True,
        handler=lambda _persona_id=None: (
            observed.append(_persona_id) or _persona_id
        ),
    )
    monkeypatch.setattr(
        "runtime.toolsets.TOOLSETS",
        {
            "identity": {
                "description": "d",
                "tools": ["whoami"],
                "includes": [],
            }
        },
        raising=False,
    )

    _defs, dispatch = persona_tools.build_persona_tool_payload(
        "ai-engineer",
        {"toolsets": ["identity"]},
    )

    assert dispatch("whoami", None) == "ai-engineer"
    assert observed == ["ai-engineer"]


def test_safe_core_memory_tools_use_each_personas_private_index(
    tmp_path,
    monkeypatch,
):
    import memory_search
    from runtime import tool_impl

    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / "homie"))
    calls = []

    def fake_search(*_args, **kwargs):
        calls.append(("memory_search", kwargs["memory_dir"]))
        return []

    def fake_keyword(*_args, **kwargs):
        calls.append(("search_files", kwargs["memory_dir"]))
        return []

    monkeypatch.setattr(memory_search, "search", fake_search)
    monkeypatch.setattr(memory_search, "search_keyword", fake_keyword)
    tool_impl.register_tools()

    for persona_id in ("ai-engineer", "founder-operator"):
        _defs, dispatch = persona_tools.build_persona_tool_payload(
            persona_id,
            {"toolsets": ["safe_core"]},
        )
        dispatch("memory_search", {"query": "private doctrine"})
        dispatch("search_files", {"pattern": "private doctrine"})

    expected_root = tmp_path / "homie" / "profiles"
    assert calls == [
        ("memory_search", expected_root / "ai-engineer" / "memory"),
        ("search_files", expected_root / "ai-engineer" / "memory"),
        ("memory_search", expected_root / "founder-operator" / "memory"),
        ("search_files", expected_root / "founder-operator" / "memory"),
    ]


def test_safe_core_memory_search_dispatches_to_the_private_index_without_type_error(
    tmp_path,
    monkeypatch,
):
    import memory_search
    from runtime import tool_impl

    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / "homie"))
    observed = []

    def fake_keyword(query, limit, *, path_prefix="", memory_dir=None):
        observed.append((query, limit, path_prefix, memory_dir))
        return []

    monkeypatch.setattr(memory_search, "search_keyword", fake_keyword)
    tool_impl.register_tools()
    _defs, dispatch = persona_tools.build_persona_tool_payload(
        "seo_geo",
        {"toolsets": ["safe_core"]},
    )

    result = dispatch("memory_search", {"query": "fleet measurement", "mode": "keyword"})

    assert result == "No vault results for 'fleet measurement'."
    assert observed == [
        ("fleet measurement", 5, "", tmp_path / "homie" / "profiles" / "seo_geo" / "memory")
    ]


def test_persona_scoped_handler_that_omits_identity_fails_loudly(monkeypatch):
    tool_registry.register_tool(
        "broken_private_tool",
        "a misconfigured private tool",
        toolset="private",
        persona_scoped=True,
        handler=lambda: "should not execute",
    )
    monkeypatch.setattr(
        "runtime.toolsets.TOOLSETS",
        {
            "private": {
                "description": "d",
                "tools": ["broken_private_tool"],
                "includes": [],
            }
        },
        raising=False,
    )
    _defs, dispatch = persona_tools.build_persona_tool_payload(
        "ai-engineer",
        {"toolsets": ["private"]},
    )

    result = dispatch("broken_private_tool", {})

    assert "_persona_id" in result
    assert "TypeError" in result


def test_out_of_scope_calls_are_refused_at_dispatch(_registered):
    """Scope is re-checked at CALL time, not only at assembly.

    Distinct from the adapters' guard: those check what the model was OFFERED
    this turn; this checks what the PERSONA was granted. A bridge call or a
    replayed name from history could present a tool the model was never offered
    directly, and the persona's grant must hold regardless of how it arrived.
    """
    _defs, dispatch = persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]})
    out = dispatch("page_read", {})
    assert "not in this persona's granted scope" in out
    assert _registered == [], "an out-of-scope tool reached its handler"


def test_a_tool_with_no_handler_fails_loudly_not_silently(monkeypatch):
    """Declared in a toolset but never wired — the config LOOKS correct."""
    rows = _capture_audit(monkeypatch)
    tool_registry.register_tool("ghost", "declared, unwired", toolset="crypto")
    monkeypatch.setattr(
        "runtime.toolsets.TOOLSETS",
        {"crypto": {"description": "d", "tools": ["ghost"], "includes": []}},
        raising=False,
    )
    _defs, dispatch = persona_tools.build_persona_tool_payload("c", {"toolsets": ["crypto"]})
    assert "no handler registered" in dispatch("ghost", {})
    assert rows[0]["outcome"] == "no_handler"
    assert rows[0]["blocked"] is True


def test_a_raising_tool_is_returned_to_the_model_not_raised(monkeypatch):
    def boom(**kw):
        raise ValueError("nope")

    tool_registry.register_tool("boom", "d", toolset="crypto", handler=boom)
    monkeypatch.setattr(
        "runtime.toolsets.TOOLSETS",
        {"crypto": {"description": "d", "tools": ["boom"], "includes": []}},
        raising=False,
    )
    _defs, dispatch = persona_tools.build_persona_tool_payload("c", {"toolsets": ["crypto"]})
    out = dispatch("boom", {})
    assert "ValueError" in out and "nope" in out


# ---------------------------------------------------------------------------
# Kill switch (#242) — ships ON
# ---------------------------------------------------------------------------


def test_kill_switch_off_disables_assembly_entirely(_registered, monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_TOOLS", "disabled")
    assert persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]}) is None


def test_kill_switch_takes_effect_MID_TURN(_registered, monkeypatch):
    """An operator control that only applies at assembly is not a kill switch.

    A long turn assembled before the flip would keep executing tools after the
    operator pulled the cord — the exact moment the switch matters most.
    """
    _defs, dispatch = persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]})
    assert dispatch("chart_read", {}) == "chart"

    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_TOOLS", "disabled")
    out = dispatch("chart_read", {})
    assert "kill switch" in out
    assert len(_registered) == 1, "a tool executed after the kill switch was pulled"


def test_ships_ON_by_default(_registered):
    """A kill switch turns a feature OFF; it does not birth it dark."""
    assert persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]}) is not None


# ---------------------------------------------------------------------------
# Audit (#242) — one row per invocation, persona-attributed
# ---------------------------------------------------------------------------


def _capture_audit(monkeypatch):
    rows = []
    import types

    fake = types.ModuleType("dashboard_api")
    fake._audit_write = lambda **kw: rows.append(kw)
    monkeypatch.setitem(sys.modules, "dashboard_api", fake)
    return rows


def test_every_invocation_writes_one_persona_attributed_row(_registered, monkeypatch):
    """An audit trail that says "a tool ran" without saying WHOSE turn ran it
    cannot answer the only question worth asking after an incident."""
    rows = _capture_audit(monkeypatch)
    _defs, dispatch = persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]})
    dispatch("chart_read", {"symbol": "BTC"})

    assert len(rows) == 1
    row = rows[0]
    assert row["target_persona_id"] == "crypto"
    assert row["operator_id"] == "persona:crypto"
    assert row["action"] == "persona_tool_call"
    assert row["outcome"] == "completed"
    assert row["detail"]["tool"] == "chart_read"
    assert row["blocked"] is False


def test_refusals_are_audited_as_blocked(_registered, monkeypatch):
    rows = _capture_audit(monkeypatch)
    _defs, dispatch = persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]})
    dispatch("page_read", {})

    assert rows[0]["outcome"] == "out_of_scope"
    assert rows[0]["blocked"] is True, "a refused call must be greppable as blocked"


def test_a_failing_audit_write_never_breaks_the_tool(_registered, monkeypatch):
    """Losing the trail is bad; losing the TURN because of it is worse."""
    import types

    fake = types.ModuleType("dashboard_api")

    def explode(**kw):
        raise RuntimeError("audit store down")

    fake._audit_write = explode
    monkeypatch.setitem(sys.modules, "dashboard_api", fake)

    _defs, dispatch = persona_tools.build_persona_tool_payload("crypto", {"toolsets": ["crypto"]})
    assert dispatch("chart_read", {}) == "chart"


def test_surface_allowlist_is_subtractive_and_dispatch_enforced(_registered):
    payload = persona_tools.build_persona_tool_payload(
        "crypto",
        {"toolsets": ["crypto"], "tools": ["page_read"]},
        allowed_tool_names={"chart_read"},
    )
    assert payload is not None
    definitions, dispatch = payload
    assert {item["function"]["name"] for item in definitions} == {"chart_read"}
    assert dispatch("chart_read", {}) == "chart"
    assert "not in this persona's granted scope" in dispatch("page_read", {})


def test_empty_surface_allowlist_fails_closed(_registered):
    assert persona_tools.build_persona_tool_payload(
        "crypto",
        {"toolsets": ["crypto"]},
        allowed_tool_names=set(),
    ) is None
