"""Tests for the persona tool registry (#237).

The tests that matter here are the INVARIANT proofs, not the happy path:

* default-deny by construction — a tool outside every toolset is unreachable
* fail-closed scoping — no declared toolsets means no tools, not all tools
* stateless catalog — a registry mutation is visible on the very next assembly

Each of those is a real, documented failure class rather than a hypothetical.
The third one in particular (openclaw#84141) shipped as *silent tool dropouts*:
no error, no log, the model simply stopped being offered tools it still had.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime import tool_registry as tr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate every test from the module-level registry.

    Snapshot/restore rather than clear/rebuild so a real tool registered by an
    imported module is not destroyed for the rest of the session.
    """
    saved = dict(tr._REGISTRY)
    saved_gen = tr._GENERATION
    tr._REGISTRY.clear()
    yield
    tr._REGISTRY.clear()
    tr._REGISTRY.update(saved)
    tr._GENERATION = saved_gen


def _toolset(tools=None, includes=None, **extra):
    return {
        "description": "test toolset",
        "tools": list(tools or []),
        "includes": list(includes or []),
        **extra,
    }


def _register(name, toolset, **kw):
    return tr.register_tool(name, f"{name} description", toolset=toolset, **kw)


# ---------------------------------------------------------------------------
# The core security invariant
# ---------------------------------------------------------------------------


def test_tool_outside_every_toolset_is_unreachable():
    """"All tools must be part of a toolset to be accessible."

    A tool registered into a toolset that nothing enables must not appear in
    ANY scope. This is the whole security model — if it can leak into a scope
    the persona did not ask for, default-deny is decorative.
    """
    registry = {"granted": _toolset(tools=["visible_tool"])}
    _register("visible_tool", "granted")
    _register("orphan_tool", "never_enabled")

    defs = tr.get_tool_definitions(["granted"], registry=registry)
    names = [d["function"]["name"] for d in defs]

    assert names == ["visible_tool"]
    assert "orphan_tool" not in names


def test_toolset_listing_a_name_cannot_grant_a_tool_owned_elsewhere():
    """Ownership mismatch — the two registries must AGREE, not just one of them.

    Found by adversarial review (Codex). `runtime.toolsets` owns STRUCTURE
    (which names a toolset claims); this module owns SUBSTANCE (which toolset a
    tool was registered under). Resolving names from the structural side and
    emitting the schema without re-checking the substantive side trusts one
    registry blindly.

    Reachable without malice: a typo in a `tools:` list, a live-source name
    collision, or a custom registry passed by a caller.
    """
    registry = {"granted": _toolset(tools=["secret_tool", "legit_tool"])}
    _register("secret_tool", "secret")      # owner is NOT granted
    _register("legit_tool", "granted")      # owner IS granted

    names = [d["function"]["name"] for d in tr.get_tool_definitions(["granted"], registry=registry)]

    assert names == ["legit_tool"]
    assert "secret_tool" not in names, (
        "a toolset merely LISTING a name handed over a tool owned by another scope"
    )


def test_ownership_is_satisfied_through_the_includes_closure():
    """A tool owned by an INCLUDED toolset is legitimately reachable.

    The ownership check must not break composition — granting `browser` (which
    includes `research`, which includes `core`) grants all three, so a tool
    registered under `core` is in scope. Without the transitive closure this
    check would be a correctness regression dressed as a security fix.
    """
    registry = {
        "core": _toolset(tools=["core_tool"]),
        "research": _toolset(tools=["research_tool"], includes=["core"]),
        "browser": _toolset(tools=["browser_tool"], includes=["research"]),
    }
    _register("core_tool", "core")
    _register("research_tool", "research")
    _register("browser_tool", "browser")

    names = [d["function"]["name"] for d in tr.get_tool_definitions(["browser"], registry=registry)]
    assert names == ["browser_tool", "core_tool", "research_tool"]


def test_disabling_a_toolset_also_disowns_tools_registered_under_it():
    registry = {
        "wide": _toolset(tools=["shared_name"], includes=["banned"]),
        "banned": _toolset(tools=["banned_tool"]),
    }
    _register("shared_name", "wide")
    _register("banned_tool", "banned")

    names = [
        d["function"]["name"]
        for d in tr.get_tool_definitions(["wide"], ["banned"], registry=registry)
    ]
    assert names == ["shared_name"]


def test_resolve_toolset_closure_fails_closed():
    registry = {"a": _toolset(includes=["b"]), "b": _toolset()}
    assert tr.resolve_toolset_closure(None, registry=registry) == frozenset()
    assert tr.resolve_toolset_closure([], registry=registry) == frozenset()
    assert tr.resolve_toolset_closure(["a"], registry=registry) == frozenset({"a", "b"})


def test_registration_requires_a_toolset():
    """A tool cannot be registered without declaring an owner.

    Registration is where the invariant becomes structural. If ``toolset``
    could be omitted, an unowned tool would exist and the model would depend on
    every future read path remembering to filter it out.
    """
    with pytest.raises(tr.ToolRegistryError, match="must declare a non-empty toolset"):
        tr.register_tool("floating", "desc", toolset="")

    with pytest.raises(tr.ToolRegistryError, match="must declare a non-empty toolset"):
        tr.register_tool("floating", "desc", toolset="   ")


def test_no_enabled_toolsets_resolves_to_nothing_not_everything():
    """Fail CLOSED — the deliberate deviation from Hermes.

    Hermes treats ``enabled_toolsets=None`` as "start with everything" because
    it is a single-user CLI. Here that would hand the full catalog to any
    persona whose config forgot to declare toolsets.
    """
    registry = {"some_toolset": _toolset(tools=["a_tool"])}
    _register("a_tool", "some_toolset")

    assert tr.get_tool_definitions(None, registry=registry) == []
    assert tr.get_tool_definitions([], registry=registry) == []
    assert tr.resolve_tool_names(None, registry=registry) == []


def test_unknown_toolset_name_yields_no_tools():
    """A typo must not silently widen scope (Hermes silent-on-missing)."""
    registry = {"real": _toolset(tools=["real_tool"])}
    _register("real_tool", "real")

    assert tr.get_tool_definitions(["taypo"], registry=registry) == []


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_includes_compose_without_duplication():
    """``crypto`` includes ``research`` — the composition case from the epic."""
    registry = {
        "research": _toolset(tools=["web_search", "shared_tool"]),
        "crypto": _toolset(tools=["chart_read", "shared_tool"], includes=["research"]),
    }
    for name in ("web_search", "shared_tool", "chart_read"):
        _register(name, "research" if name != "chart_read" else "crypto")

    names = [d["function"]["name"] for d in tr.get_tool_definitions(["crypto"], registry=registry)]

    assert names == ["chart_read", "shared_tool", "web_search"]
    assert len(names) == len(set(names)), "composition must not duplicate a shared tool"


def test_toolset_cycle_terminates():
    """A ↔ B composition must not recurse forever.

    Cycles are not bugs — they are the cost of allowing diamond composition,
    so the resolver absorbs them silently rather than raising.
    """
    registry = {
        "a": _toolset(tools=["tool_a"], includes=["b"]),
        "b": _toolset(tools=["tool_b"], includes=["a"]),
    }
    _register("tool_a", "a")
    _register("tool_b", "b")

    names = [d["function"]["name"] for d in tr.get_tool_definitions(["a"], registry=registry)]
    assert names == ["tool_a", "tool_b"]


def test_disabled_toolset_subtracts_after_the_union():
    registry = {
        "wide": _toolset(tools=["keep_me", "drop_me"]),
        "banned": _toolset(tools=["drop_me"]),
    }
    _register("keep_me", "wide")
    _register("drop_me", "wide")

    names = [
        d["function"]["name"]
        for d in tr.get_tool_definitions(["wide"], ["banned"], registry=registry)
    ]
    assert names == ["keep_me"]


# ---------------------------------------------------------------------------
# Statelessness (openclaw#84141)
# ---------------------------------------------------------------------------


def test_registry_mutation_is_visible_on_the_very_next_assembly():
    """No cross-turn cache. The silent-dropout regression, asserted.

    OpenClaw's session-keyed catalog drifted from the live registry and the
    model quietly stopped being offered tools it still had — no error anywhere.
    Assembly N+1 must reflect a mutation made after assembly N.
    """
    registry = {"live": _toolset(tools=["first", "second"])}
    _register("first", "live")

    before = [d["function"]["name"] for d in tr.get_tool_definitions(["live"], registry=registry)]
    assert before == ["first"]

    _register("second", "live")
    after = [d["function"]["name"] for d in tr.get_tool_definitions(["live"], registry=registry)]
    assert after == ["first", "second"], "catalog was cached across assemblies"

    tr.unregister_tool("second")
    after_removal = [
        d["function"]["name"] for d in tr.get_tool_definitions(["live"], registry=registry)
    ]
    assert after_removal == ["first"], "removal was not visible on the next assembly"


def test_generation_increments_on_every_mutation():
    start = tr.get_generation()
    _register("gen_tool", "ts")
    assert tr.get_generation() == start + 1

    tr.unregister_tool("gen_tool")
    assert tr.get_generation() == start + 2

    assert tr.unregister_tool("never_existed") is False
    assert tr.get_generation() == start + 2, "no-op removal must not bump generation"


# ---------------------------------------------------------------------------
# Wire format + registration guards
# ---------------------------------------------------------------------------


def test_emitted_schema_is_openai_format():
    """The format IS the portability mechanism.

    This exact shape produced a structured tool call on Kimi K3 during the
    epic's spike. Portability comes from the format, not a vendor SDK.
    """
    registry = {"ts": _toolset(tools=["shaped"])}
    _register(
        "shaped",
        "ts",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )

    (definition,) = tr.get_tool_definitions(["ts"], registry=registry)
    assert definition["type"] == "function"
    fn = definition["function"]
    assert fn["name"] == "shaped"
    assert fn["description"] == "shaped description"
    assert fn["parameters"]["required"] == ["city"]


def test_parameterless_tool_still_emits_a_parameters_object():
    """Some providers reject a function entry with no ``parameters`` key."""
    registry = {"ts": _toolset(tools=["bare"])}
    _register("bare", "ts")

    (definition,) = tr.get_tool_definitions(["ts"], registry=registry)
    assert definition["function"]["parameters"] == {"type": "object", "properties": {}}


def test_schema_name_mismatch_is_rejected():
    """The model would be told to call a name dispatch cannot resolve."""
    bad = tr.build_tool_schema("actual_name", "desc")
    with pytest.raises(tr.ToolRegistryError, match="does not match registered name"):
        tr.register_tool("declared_name", "desc", toolset="ts", schema=bad)


def test_mutating_the_schema_after_registration_cannot_rewrite_the_tool():
    """`frozen=True` freezes the BINDING, not the nested dict.

    Found by adversarial review (Codex). Without a defensive copy, a caller who
    mutates the dict they passed in silently rewrites what the registry emits —
    including the tool name that was just validated. Validation would describe
    the object only at the instant it ran.
    """
    registry = {"ts": _toolset(tools=["safe_name"])}
    schema = tr.build_tool_schema("safe_name", "desc")
    tr.register_tool("safe_name", "desc", toolset="ts", schema=schema)

    schema["function"]["name"] = "unregistered_after_validation"

    names = [d["function"]["name"] for d in tr.get_tool_definitions(["ts"], registry=registry)]
    assert names == ["safe_name"], "post-registration mutation rewrote the emitted tool"
    assert tr.get_entry("safe_name") is not None


def test_mutating_a_returned_definition_cannot_poison_later_assemblies():
    """The catalog is rebuilt every assembly — but from these stored dicts.

    Returning the stored object by reference means one careless consumer
    corrupts every future assembly for every persona.
    """
    registry = {"ts": _toolset(tools=["stable"])}
    _register("stable", "ts")

    first = tr.get_tool_definitions(["ts"], registry=registry)
    first[0]["function"]["name"] = "hijacked"
    first[0]["function"]["parameters"]["properties"]["injected"] = {"type": "string"}

    second = tr.get_tool_definitions(["ts"], registry=registry)
    assert second[0]["function"]["name"] == "stable"
    assert "injected" not in second[0]["function"]["parameters"]["properties"]


@pytest.mark.parametrize(
    "bad_schema,match",
    [
        ("not-a-dict", "must be a dict"),
        ({"type": "tool", "function": {"name": "x", "description": "d",
                                       "parameters": {"type": "object"}}}, "type='function'"),
        ({"type": "function"}, "missing a 'function' object"),
        ({"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}},
         "string description"),
        ({"type": "function", "function": {"name": "x", "description": "d"}}, "'parameters' object"),
        ({"type": "function", "function": {"name": "x", "description": "d",
                                           "parameters": {"type": "array"}}}, "JSON Schema object"),
    ],
)
def test_malformed_schemas_are_rejected_at_registration(bad_schema, match):
    """Shape validation, not just name matching.

    A schema that passes a name check but is otherwise malformed reaches the
    provider and fails there — as an opaque 400, at request time, on whichever
    lane happened to be selected. Catch it at import instead.
    """
    with pytest.raises(tr.ToolRegistryError, match=match):
        tr.register_tool("x", "d", toolset="ts", schema=bad_schema)


def test_bridge_names_are_reserved():
    """A plugin must not shadow the disclosure bridge and intercept calls."""
    for reserved in ("tool_search", "tool_describe", "tool_call"):
        with pytest.raises(tr.ToolRegistryError, match="reserved"):
            tr.register_tool(reserved, "desc", toolset="ts")


def test_moving_a_tool_between_toolsets_is_refused():
    """Silently changing a tool's reachability is a scoping bug."""
    _register("settled", "original")
    _register("settled", "original")  # same-toolset reload is legal

    with pytest.raises(tr.ToolRegistryError, match="refusing to move"):
        _register("settled", "somewhere_else")


def test_unknown_effect_is_rejected():
    with pytest.raises(tr.ToolRegistryError, match="unknown effect"):
        tr.register_tool("odd", "desc", toolset="ts", effect="maybe")


def test_declared_but_unregistered_name_is_skipped_not_fatal():
    """Toolsets legitimately name tools whose module has not loaded yet."""
    registry = {"partial": _toolset(tools=["present", "absent"])}
    _register("present", "partial")

    names = [d["function"]["name"] for d in tr.get_tool_definitions(["partial"], registry=registry)]
    assert names == ["present"]


def test_definitions_are_name_ordered_for_stable_prompts():
    """An unstable tools array churns prompt-cache prefixes every turn."""
    registry = {"ts": _toolset(tools=["zebra", "alpha", "middle"])}
    for name in ("zebra", "alpha", "middle"):
        _register(name, "ts")

    names = [d["function"]["name"] for d in tr.get_tool_definitions(["ts"], registry=registry)]
    assert names == ["alpha", "middle", "zebra"]


def test_list_registered_is_not_an_authorization_path():
    """Diagnostics bypass scoping; the model-facing path must not.

    Guards the shape of the mistake rather than the wording: if someone ever
    builds a tools array from ``list_registered()``, an orphan tool ships.
    """
    registry = {"granted": _toolset(tools=["in_scope"])}
    _register("in_scope", "granted")
    _register("out_of_scope", "ungranted")

    assert {e.name for e in tr.list_registered()} == {"in_scope", "out_of_scope"}
    scoped = [d["function"]["name"] for d in tr.get_tool_definitions(["granted"], registry=registry)]
    assert scoped == ["in_scope"]


# ---------------------------------------------------------------------------
# The shipped toolset declarations
# ---------------------------------------------------------------------------


def test_core_carries_execution_and_write_verbs_by_operator_decision():
    """Core is wide on purpose — ``terminal`` is core, Hermes-faithful.

    The architecture doc reserved this decision for the operator (Hermes ships
    ``terminal`` core with a human watching the CLI; these personas run
    unattended). The call was made on 2026-07-27: ship it core.

    This test pins the decision so it cannot be quietly narrowed later by
    someone reading the old "unattended = dangerous" reasoning and assuming it
    was an oversight. Narrowing this is an operator decision too.
    """
    from runtime import capabilities as caps
    from runtime.toolsets import TOOLSETS, _HOMIE_CORE_TOOLS

    resolved = caps.resolve_toolset("core", TOOLSETS)
    assert resolved == sorted(_HOMIE_CORE_TOOLS)

    for verb in ("terminal", "process", "write_file", "patch", "skill_manage"):
        assert verb in resolved, (
            f"{verb!r} left core — that is an operator decision, not a cleanup"
        )


def test_core_is_a_floor_not_the_whole_catalog():
    """Per-persona scoping (epic non-negotiable #4) survives a wide core.

    "Crypto's set is not Browser Homie's set." A wide core is only acceptable
    while DOMAIN capability still differentiates personas — if domain verbs
    leaked into core, every persona would be identical and the epic's success
    criterion ("adding a capability to one persona does not add it to the other
    twenty-four") would be silently false.
    """
    from runtime.toolsets import _HOMIE_CORE_TOOLS

    core = set(_HOMIE_CORE_TOOLS)
    for domain_verb in (
        "browser_navigate",
        "browser_snapshot",
        "web_search",
        "web_extract",
    ):
        assert domain_verb not in core, (
            f"{domain_verb!r} is a DOMAIN verb; putting it in core erases the "
            "difference between personas"
        )


def test_launch_toolsets_compose_onto_core():
    """research -> core, browser -> research, crypto -> research."""
    from runtime import capabilities as caps
    from runtime.toolsets import TOOLSETS, _HOMIE_CORE_TOOLS

    core = set(_HOMIE_CORE_TOOLS)
    for name in ("research", "browser", "crypto"):
        resolved = set(caps.resolve_toolset(name, TOOLSETS))
        assert core.issubset(resolved), f"{name} lost the core floor"

    browser = set(caps.resolve_toolset("browser", TOOLSETS))
    assert "browser_navigate" in browser
    for write_verb in ("browser_click", "browser_type", "browser_press"):
        assert write_verb not in browser, (
            "browser writes stay behind their own operator-approval gates"
        )


def test_a_persona_scope_differs_from_another_persona_scope():
    """The epic's success criterion, asserted end to end.

    Two personas with different declared toolsets must resolve to different
    tool sets. If this ever passes trivially (both empty, both identical), the
    scoping mechanism is decorative.
    """
    from runtime.toolsets import TOOLSETS

    browser_scope = set(tr.resolve_tool_names(["browser"], registry=TOOLSETS))
    research_scope = set(tr.resolve_tool_names(["research"], registry=TOOLSETS))

    assert browser_scope, "browser persona resolved to nothing"
    assert research_scope, "research persona resolved to nothing"
    assert browser_scope != research_scope, (
        "two differently-scoped personas resolved to the same tools — "
        "per-persona capability is not actually scoped"
    )
    assert browser_scope > research_scope, (
        "browser includes research, so it must be a strict superset"
    )


def test_two_personas_get_different_MODEL_FACING_arrays():
    """The same criterion through the real join, with real registrations.

    The test above compares `resolve_tool_names()` only — it registers nothing
    and never calls the model-facing path, so it could pass while
    `get_tool_definitions()` was broken (Codex flagged exactly that gap). This
    one registers real tools against the shipped toolsets and compares the
    arrays a model would actually receive.
    """
    from runtime.toolsets import (
        TOOLSETS,
        _HOMIE_OPERATOR_EXEC_TOOLS,
        _HOMIE_SAFE_CORE_TOOLS,
    )

    # ``core`` is now a compatibility wrapper over two explicit capability
    # classes. Register against the owning classes so this model-facing test
    # exercises the same ownership check as production.
    for tool_name in _HOMIE_SAFE_CORE_TOOLS:
        _register(tool_name, "safe_core")
    for tool_name in _HOMIE_OPERATOR_EXEC_TOOLS:
        _register(tool_name, "operator_exec")
    _register("web_search", "research_read")
    _register("web_extract", "research_read")
    _register("browser_navigate", "browser_read")

    def names_for(toolset):
        return {
            d["function"]["name"]
            for d in tr.get_tool_definitions([toolset], registry=TOOLSETS)
        }

    browser = names_for("browser")
    research = names_for("research")
    core = names_for("core")

    assert core, "core persona received an empty tools array"
    assert core < research < browser, (
        f"scopes must strictly nest core < research < browser; "
        f"got core={len(core)} research={len(research)} browser={len(browser)}"
    )
    assert "browser_navigate" in browser
    assert "browser_navigate" not in research, (
        "a browser-only verb leaked into the research persona's array"
    )
    # Core is the floor: every persona stands on it (operator decision — see
    # test_core_carries_execution_and_write_verbs_by_operator_decision).
    assert "terminal" in core and "terminal" in browser
