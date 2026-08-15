"""Persona toolset self-provisioning executor (issue #426, epic #419).

``add_persona_toolset`` / ``remove_persona_toolset`` are the ONLY mutation
path for a persona's ``toolsets:`` list. The tests below map one case per
distinct code path through that executor:

* the two happy paths (grant, revoke) and the two already-true paths
* every refusal branch (missing operator turn, non-admin role, kill switch,
  default profile, invalid/unknown persona, unknown toolset)
* the two clobber-refusal branches a strict-read RMW exists for (unparseable
  file, non-list / blank-entry ``toolsets`` value)
* the ledger contract (schema, hostile trigger text, best-effort write)
* liveness by construction — a written grant resolves on the next read with
  no cache to invalidate, and the registry it is checked against is read
  live rather than snapshotted
* the Q6 spike receipt — the default-profile refusal AND the engine gate
  that justifies it, so the refusal fails loudly the day that gate moves

Physical state is asserted throughout: a refusal must leave the config file
byte-identical (or absent), never merely return an error.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from personas import toolset_grants as grants  # noqa: E402
from personas.services import (  # noqa: E402
    ConfigShapeError,
    add_persona_toolset,
    read_profile_config,
    remove_persona_toolset,
    resolve_persona_tool_scope,
)

CLAUDE_DIR = SCRIPTS_DIR.parent

# A real registered toolset (runtime/toolsets.py) — grants must name one.
KNOWN_TOOLSET = "research_read"
OTHER_TOOLSET = "repo_read"

OPERATOR = {
    "actor": "owner",
    "actor_role": "admin",
    "trigger_text": "give sales the research toolset",
    "surface": "telegram",
    "channel_id": "42",
}


# ── Fixtures / helpers ───────────────────────────────────────────────────


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A physical named-profile tree at ``<tmp>/.homie/profiles/sales``.

    ``HOMIE_HOME`` points at the fake root (not at a profile), so
    ``get_default_homie_root()`` returns it and named-profile resolution
    lands under ``<root>/profiles/<name>/``.
    """
    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / "sales"
    (profile_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    return profile_dir


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


def rows(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    return [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def outcome_rows(ledger_path: Path) -> list[dict]:
    """Ledger rows minus the pre-mutation ``intent`` rows.

    A mutating attempt writes a correlated PAIR — ``intent`` before the
    atomic replace, ``granted``/``revoked`` after it. Tests that care about
    what the executor DID read through this; tests that care about the
    two-row contract itself read ``rows()`` directly.
    """
    return [row for row in rows(ledger_path) if row["outcome"] != grants.OUTCOME_INTENT]


def config_file(profile_dir: Path) -> Path:
    return profile_dir / "config.yaml"


def write_config(profile_dir: Path, text: str) -> Path:
    path = config_file(profile_dir)
    path.write_text(text, encoding="utf-8")
    return path


# ── Happy paths ──────────────────────────────────────────────────────────


def test_grant_writes_the_toolset_and_a_correlated_intent_outcome_pair(
    profile: Path, ledger: Path
):
    result = add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert result.changed is True
    assert result.outcome == grants.OUTCOME_GRANTED
    assert result.toolsets == (KNOWN_TOOLSET,)
    assert read_profile_config("sales")["toolsets"] == [KNOWN_TOOLSET]

    # A mutating exit writes TWO correlated rows, in this order: `intent`
    # before the config was touched, `granted` only after the atomic replace
    # returned. Order is the whole point — it is what stops a failed write
    # from ever leaving a `granted` row behind.
    intent, row = rows(ledger)
    assert intent["outcome"] == grants.OUTCOME_INTENT
    assert row["outcome"] == grants.OUTCOME_GRANTED
    assert intent["correlation_id"] == row["correlation_id"] != ""

    for entry in (intent, row):
        assert entry["action"] == "toolset_grant"
        assert entry["operation"] == grants.OPERATION_GRANT
        assert entry["persona_id"] == "sales"
        assert entry["toolset"] == KNOWN_TOOLSET
        assert entry["actor"] == "owner"
        assert entry["actor_role"] == "admin"
        assert entry["trigger_text"] == OPERATOR["trigger_text"]
        assert entry["toolsets_after"] == [KNOWN_TOOLSET]
        assert entry["config_path"] == str(config_file(profile))
    assert result.audit_id.startswith(row["timestamp"])


def test_audit_id_is_unique_even_within_the_same_wall_clock_second(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """#435 convention: the returned audit_id must be unique PER ROW.

    The id used to be ``f"{timestamp}:{persona_id}:{operation}:{outcome}"`` —
    no toolset, no nonce. Two DIFFERENT toolsets granted to the same persona
    inside the same wall-clock second (the timestamp has only second
    precision) collapsed onto the identical string despite being two
    distinct ledger rows. A caller keying off this id (a dashboard receipt,
    a sibling #428/#429 surface) would silently read the wrong row.
    """
    import datetime as datetime_module

    class _FrozenDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(grants, "datetime", _FrozenDatetime)

    first = add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)
    second = add_persona_toolset("sales", OTHER_TOOLSET, audit_path=ledger, **OPERATOR)

    assert first.audit_id.startswith("2026-01-01T12:00:00")
    assert second.audit_id.startswith("2026-01-01T12:00:00")
    assert first.audit_id != second.audit_id


def test_grant_appends_without_touching_other_sections(profile: Path, ledger: Path):
    write_config(
        profile,
        "persona:\n  id: sales\n  name: Sales\ntoolsets:\n  - safe_core\n"
        "learning:\n  enabled: true\n",
    )

    add_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor=OPERATOR["actor"],
        actor_role=OPERATOR["actor_role"],
        trigger_text=OPERATOR["trigger_text"],
        surface="telegram",
        channel_id="4242",
    )

    data = read_profile_config("sales")
    assert data["toolsets"] == ["safe_core", KNOWN_TOOLSET]
    assert data["persona"] == {"id": "sales", "name": "Sales"}
    assert data["learning"] == {"enabled": True}
    assert rows(ledger)[0]["surface"] == "telegram"
    assert rows(ledger)[0]["channel_id"] == "4242"


def test_revoke_removes_every_occurrence_of_the_name(profile: Path, ledger: Path):
    write_config(
        profile,
        f"toolsets:\n  - safe_core\n  - {KNOWN_TOOLSET}\n  - {OTHER_TOOLSET}\n"
        f"  - {KNOWN_TOOLSET}\n",
    )

    result = remove_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="take research off sales",
        surface="telegram",
        channel_id="42",
    )

    assert result.changed is True
    assert result.outcome == grants.OUTCOME_REVOKED
    # A duplicate left behind would be a revoke that did not revoke.
    assert read_profile_config("sales")["toolsets"] == ["safe_core", OTHER_TOOLSET]
    assert rows(ledger)[0]["action"] == "toolset_revoke"
    assert rows(ledger)[0]["toolsets_after"] == ["safe_core", OTHER_TOOLSET]


def test_grant_then_revoke_round_trips_to_the_original_config(
    profile: Path, ledger: Path
):
    original = write_config(profile, "toolsets:\n- safe_core\n").read_text(
        encoding="utf-8"
    )

    add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)
    assert read_profile_config("sales")["toolsets"] == ["safe_core", KNOWN_TOOLSET]

    remove_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="undo that",
        surface="telegram",
        channel_id="42",
    )

    assert read_profile_config("sales")["toolsets"] == ["safe_core"]
    # Round-tripped through pyyaml, so compare parsed state, not bytes.
    assert read_profile_config("sales") == {"toolsets": ["safe_core"]}
    assert "safe_core" in original
    assert [r["outcome"] for r in outcome_rows(ledger)] == [
        grants.OUTCOME_GRANTED,
        grants.OUTCOME_REVOKED,
    ]
    # Each mutation carried its own intent row, and every intent found its
    # outcome — no orphan, which is what a torn write would look like.
    correlations = [r["correlation_id"] for r in rows(ledger)]
    assert len(correlations) == 4
    assert len(set(correlations)) == 2
    assert all(correlations.count(cid) == 2 for cid in set(correlations))


# ── Already-true paths (real answers, not errors, and no write) ──────────


def test_granting_what_the_persona_already_has_leaves_the_file_untouched(
    profile: Path, ledger: Path
):
    path = write_config(profile, f"toolsets:\n  - {KNOWN_TOOLSET}   # authored\n")
    before = path.read_bytes()

    result = add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert result.changed is False
    assert result.outcome == grants.OUTCOME_ALREADY_GRANTED
    assert path.read_bytes() == before  # comment and formatting survive
    assert rows(ledger)[0]["outcome"] == grants.OUTCOME_ALREADY_GRANTED


def test_revoking_what_the_persona_never_had_reports_what_it_holds(
    profile: Path, ledger: Path
):
    path = write_config(profile, f"toolsets:\n  - safe_core\n  - {OTHER_TOOLSET}\n")
    before = path.read_bytes()

    result = remove_persona_toolset(
        "sales",
        "reserch_raed",
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="drop reserch_raed",
        surface="telegram",
        channel_id="42",
    )

    assert result.changed is False
    assert result.outcome == grants.OUTCOME_NOT_GRANTED
    assert result.suggestions == ("safe_core", OTHER_TOOLSET)
    assert path.read_bytes() == before
    assert rows(ledger)[0]["suggestions"] == ["safe_core", OTHER_TOOLSET]


# ── Unknown-toolset refusal ──────────────────────────────────────────────


def test_unknown_toolset_refuses_with_nearest_matches_and_writes_nothing(
    profile: Path, ledger: Path
):
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("sales", "reserch_raed", audit_path=ledger, **OPERATOR)

    refusal = excinfo.value
    assert refusal.reason == grants.REASON_UNKNOWN_TOOLSET
    assert KNOWN_TOOLSET in refusal.suggestions
    assert "reserch_raed" in str(refusal)
    assert KNOWN_TOOLSET in str(refusal)
    assert not config_file(profile).exists()

    (row,) = rows(ledger)
    assert row["outcome"] == grants.OUTCOME_REFUSED
    assert row["reason"] == grants.REASON_UNKNOWN_TOOLSET
    assert KNOWN_TOOLSET in row["suggestions"]


def test_unknown_toolset_with_no_near_match_still_names_the_registry_miss(
    profile: Path, ledger: Path
):
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("sales", "zzqqxx", audit_path=ledger, **OPERATOR)

    assert excinfo.value.suggestions == ()
    assert "not in the live toolset registry" in str(excinfo.value)
    assert not config_file(profile).exists()


def test_revoke_does_not_consult_the_registry(profile: Path, ledger: Path):
    """An unregistered name that IS granted must still be removable.

    Otherwise a toolset that shipped, was granted, and was later retired
    from the registry would be stranded in the config forever.
    """
    write_config(profile, "toolsets:\n  - retired_pack\n  - safe_core\n")

    result = remove_persona_toolset(
        "sales",
        "retired_pack",
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="drop the retired pack",
        surface="telegram",
        channel_id="42",
    )

    assert "retired_pack" not in grants.known_toolset_names()
    assert result.changed is True
    assert read_profile_config("sales")["toolsets"] == ["safe_core"]


# ── Registry outage: an unreadable registry is not a bad name ────────────


def test_a_registry_outage_refuses_honestly_instead_of_blaming_the_name(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """The ordered #435 refusal-cause-honesty item — design gate, #427.

    ``known_toolset_names()`` fails closed to ``()``, and EVERY name misses
    an empty registry. Without an outage branch, a registry that would not
    load refused a perfectly valid toolset as "'research_read' is not in the
    live toolset registry (0 registered)" — blaming the operator for a broken
    import and sending them to fix a name that was never wrong.

    The outage is produced through the REAL fail-closed path (a non-mapping
    ``TOOLSETS``) rather than by stubbing ``known_toolset_names``, so this
    also pins that that path still yields an empty registry.
    """
    from runtime import toolsets as runtime_toolsets

    monkeypatch.setattr(runtime_toolsets, "TOOLSETS", None)
    assert grants.known_toolset_names() == ()

    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    refusal = excinfo.value
    assert refusal.reason == grants.REASON_REGISTRY_UNAVAILABLE
    assert "registry is unavailable" in str(refusal)
    # The name is exonerated by name, and the old lie is gone.
    assert KNOWN_TOOLSET in str(refusal)
    assert "not in the live toolset registry" not in str(refusal)
    assert not config_file(profile).exists()

    (row,) = rows(ledger)
    assert row["outcome"] == grants.OUTCOME_REFUSED
    assert row["reason"] == grants.REASON_REGISTRY_UNAVAILABLE


def test_a_genuine_unknown_name_is_still_reported_as_a_name_problem(
    profile: Path, ledger: Path
):
    """Non-vacuity contrast for the outage branch.

    A branch that answered ``registry_unavailable`` for every miss would pass
    the outage test while destroying the nearest-match refusal, so the
    live-registry miss is pinned to the OTHER reason right beside it.
    """
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("sales", "reserch_raed", audit_path=ledger, **OPERATOR)

    assert excinfo.value.reason == grants.REASON_UNKNOWN_TOOLSET
    assert KNOWN_TOOLSET in excinfo.value.suggestions


def test_revoke_still_works_while_the_registry_is_down(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """Taking reach BACK must not depend on the registry being readable.

    The outage refusal is scoped to GRANT deliberately: an incident is
    exactly when the registry is least likely to load and when an operator
    most needs to pull a toolset, and revoke is the direction that REDUCES
    blast radius. Revoke already skips the registry for retired names; this
    pins that the new branch did not quietly change that.
    """
    from runtime import toolsets as runtime_toolsets

    write_config(profile, f"toolsets:\n  - {KNOWN_TOOLSET}\n  - safe_core\n")
    monkeypatch.setattr(runtime_toolsets, "TOOLSETS", None)
    assert grants.known_toolset_names() == ()

    result = remove_persona_toolset(
        "sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR
    )

    assert result.changed is True
    assert read_profile_config("sales")["toolsets"] == ["safe_core"]


# ── Strict-read RMW: never clobber a file we cannot parse ────────────────


def test_malformed_config_raises_config_shape_error_and_leaves_the_file_alone(
    profile: Path, ledger: Path
):
    path = write_config(profile, "voice: [\npersona:\n  id: sales\n")
    before = path.read_bytes()

    with pytest.raises(ConfigShapeError):
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert path.read_bytes() == before
    (row,) = rows(ledger)
    assert row["outcome"] == grants.OUTCOME_ERROR
    assert row["reason"] == grants.REASON_CONFIG_SHAPE


def test_non_list_toolsets_value_refuses_to_clobber(profile: Path, ledger: Path):
    path = write_config(profile, "toolsets: safe_core\n")
    before = path.read_bytes()

    with pytest.raises(ConfigShapeError) as excinfo:
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert "toolsets" in str(excinfo.value)
    assert path.read_bytes() == before
    assert rows(ledger)[0]["reason"] == grants.REASON_CONFIG_SHAPE


def test_blank_toolset_entry_refuses_to_clobber(profile: Path, ledger: Path):
    path = write_config(profile, "toolsets:\n  - safe_core\n  - '   '\n")
    before = path.read_bytes()

    with pytest.raises(ConfigShapeError):
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert path.read_bytes() == before


def test_explicit_null_toolsets_raises_instead_of_being_silently_healed(
    profile: Path, ledger: Path
):
    """BLOCKER fix (issue #426 round 2): an explicit ``toolsets: null`` is a

    malformed declaration, not an absent key. Collapsing both to ``[]``
    before validation let a bad file be silently overwritten with whatever
    the caller happened to grant, instead of raising ``ConfigShapeError``
    and leaving the bytes untouched like every other clobber-refusal case.
    """
    path = write_config(
        profile, "toolsets: null\nlearning:\n  enabled: true\n"
    )
    before = path.read_bytes()

    with pytest.raises(ConfigShapeError):
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert path.read_bytes() == before
    assert rows(ledger)[0]["reason"] == grants.REASON_CONFIG_SHAPE


# ── Hostile / unauthorized input ─────────────────────────────────────────


def test_missing_operator_turn_is_refused_and_audited(profile: Path, ledger: Path):
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset(
            "sales",
            KNOWN_TOOLSET,
            audit_path=ledger,
            actor="owner",
            actor_role="admin",
            trigger_text="   ",
            surface="telegram",
            channel_id="42",
        )

    assert excinfo.value.reason == grants.REASON_MISSING_OPERATOR_TURN
    assert not config_file(profile).exists()
    assert rows(ledger)[0]["reason"] == grants.REASON_MISSING_OPERATOR_TURN

    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset(
            "sales",
            KNOWN_TOOLSET,
            audit_path=ledger,
            actor="",
            actor_role="admin",
            trigger_text="give sales research",
            surface="telegram",
            channel_id="42",
        )

    assert excinfo.value.reason == grants.REASON_MISSING_OPERATOR_TURN
    assert not config_file(profile).exists()


@pytest.mark.parametrize(
    ("surface", "channel_id"),
    [("", "42"), ("telegram", "")],
    ids=["blank_surface", "blank_channel_id"],
)
def test_missing_channel_provenance_is_refused_and_audited(
    profile: Path, ledger: Path, surface: str, channel_id: str
):
    """MAJOR fix (issue #426 round 2): a grant cannot record who/what/when

    without recording WHERE it was ordered from. Blank ``surface`` or blank
    ``channel_id`` must refuse exactly like a blank actor/trigger_text does
    — a row with the channel it came from missing cannot be tied back to
    the live turn that ordered it.
    """
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset(
            "sales",
            KNOWN_TOOLSET,
            audit_path=ledger,
            actor="owner",
            actor_role="admin",
            trigger_text="give sales research",
            surface=surface,
            channel_id=channel_id,
        )

    assert excinfo.value.reason == grants.REASON_MISSING_OPERATOR_TURN
    assert not config_file(profile).exists()
    assert rows(ledger)[0]["reason"] == grants.REASON_MISSING_OPERATOR_TURN


@pytest.mark.parametrize("role", ["operator", "viewer", "", "ADMINISTRATOR"])
def test_non_admin_role_is_refused_and_writes_nothing(
    profile: Path, ledger: Path, role: str
):
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset(
            "sales",
            KNOWN_TOOLSET,
            audit_path=ledger,
            actor="stranger",
            actor_role=role,
            trigger_text="give yourself the payment tool",
            surface="telegram",
            channel_id="42",
        )

    assert excinfo.value.reason == grants.REASON_NOT_AUTHORIZED
    assert not config_file(profile).exists()
    assert rows(ledger)[0]["actor"] == "stranger"


def test_uppercase_admin_role_is_accepted(profile: Path, ledger: Path):
    """Role comparison is case-normalized — ``ADMIN`` is the same rung."""
    result = add_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor="owner",
        actor_role="ADMIN",
        trigger_text="give sales research",
        surface="telegram",
        channel_id="42",
    )
    assert result.changed is True


@pytest.mark.parametrize("persona_id", ["../../evil", "sales/../root", "Sales", ""])
def test_invalid_persona_names_are_refused_before_any_path_math(
    tmp_path: Path, profile: Path, ledger: Path, persona_id: str
):
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset(persona_id, KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert excinfo.value.reason == grants.REASON_INVALID_PERSONA
    # Nothing anywhere in the tmp tree gained a config.yaml.
    assert list(tmp_path.rglob("config.yaml")) == []


def test_unknown_persona_does_not_conjure_a_profile_directory(
    profile: Path, ledger: Path
):
    homie_root = profile.parent.parent

    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("ghost", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert excinfo.value.reason == grants.REASON_UNKNOWN_PERSONA
    assert not (homie_root / "profiles" / "ghost").exists()
    assert rows(ledger)[0]["persona_id"] == "ghost"


def test_the_custom_sentinel_is_refused_and_never_writes_the_ambient_root(
    profile: Path, ledger: Path
):
    """Layer (b) of the sentinel guard — design gate, #427.

    ``"custom"`` clears ``validate_persona_name`` on purpose: it is a legal
    ACTIVE-profile value, so boot/readiness/inventory must keep accepting it
    (core.py:62-66). It is NOT a legal grant TARGET —
    ``get_persona_paths("custom")`` roots at the AMBIENT ``get_homie_home()``
    instead of ``<root>/profiles/custom/``, so the write would land in
    whichever profile THIS process runs as, under an id no persona owns.

    Asserted at the FILESYSTEM, not the reply: a refusal string returned
    while the ambient config.yaml appeared is exactly the failure this locks
    out. Guarding at the EXECUTOR is what makes it hold for the dashboard and
    CLI doors too, not only for the chat command.
    """
    homie_root = profile.parent.parent
    assert not (homie_root / "config.yaml").exists()

    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("custom", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    refusal = excinfo.value
    assert refusal.reason == grants.REASON_INVALID_PERSONA
    assert "sentinel" in str(refusal)
    assert not (homie_root / "config.yaml").exists()

    (row,) = rows(ledger)
    assert row["outcome"] == grants.OUTCOME_REFUSED
    assert row["reason"] == grants.REASON_INVALID_PERSONA


def test_the_sentinel_guard_covers_revoke_as_well_as_grant(
    profile: Path, ledger: Path
):
    """The gate sits ABOVE the grant/revoke branch, so both operations hit it.

    Revoke skips the registry check by design, so if the sentinel guard had
    been placed inside the grant branch this direction would still have
    reached the ambient root.
    """
    homie_root = profile.parent.parent

    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        remove_persona_toolset("custom", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert excinfo.value.reason == grants.REASON_INVALID_PERSONA
    assert not (homie_root / "config.yaml").exists()


def test_a_real_persona_still_passes_the_sentinel_guard(
    profile: Path, ledger: Path
):
    """Non-vacuity contrast: the guard rejects sentinels, not every name."""
    result = add_persona_toolset(
        "sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR
    )

    assert result.changed is True
    assert read_profile_config("sales")["toolsets"] == [KNOWN_TOOLSET]


def test_two_refused_grants_can_never_provision_a_ghost_persona(
    profile: Path, isolated_operator_sinks: Path
):
    """Codex R3 MAJOR 2 — the refusal used to CREATE the persona it refused.

    Deliberately runs the PRODUCTION shape with NO ``audit_path``: the ledger
    is target-keyed, so the unknown-persona refusal resolved
    ``profiles/ghost/data`` and ``append_audit_record`` mkdir'd it. The
    executor's own existence gate (``config_path.parent.is_dir()``) then saw a
    real directory, so the SECOND identical command wrote ``config.yaml`` —
    persona creation through a grant command, outside the lifecycle
    provisioner, from two refused turns.

    Every sibling test injects ``audit_path=ledger``, which is exactly what
    masked this: the injected path never lands under ``profiles/ghost/``, so
    no directory ever appeared. This one must NOT inject.

    ``isolated_operator_sinks`` is REQUIRED here, not decoration (Codex R4):
    an unknown persona has no profile ledger, so the refusal row resolves the
    process-AMBIENT ``config.DATA_DIR`` — and the ``profile`` fixture
    redirects only ``HOMIE_HOME``. Without this second fixture every run
    appended a fake ``ghost`` refusal to the CHECKOUT's own operational
    ledger while all the assertions below still passed. That is the same
    target-vs-ambient class the executor fix addresses, in fixture form, which
    is exactly what the #422 R4 fixture exists for — composed, not duplicated.
    """
    homie_root = profile.parent.parent
    ghost = homie_root / "profiles" / "ghost"

    for attempt in (1, 2):
        with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
            add_persona_toolset("ghost", KNOWN_TOOLSET, **OPERATOR)
        assert excinfo.value.reason == grants.REASON_UNKNOWN_PERSONA, (
            f"attempt {attempt} stopped refusing — the first refusal "
            f"provisioned the profile it refused"
        )

    assert not ghost.exists()


def test_the_refusal_for_an_unknown_persona_is_still_audited_somewhere(
    profile: Path, isolated_operator_sinks: Path
):
    """The ghost fix RELOCATES the row; it must never DROP it.

    With no profile to key on, ``resolve_ledger_path`` falls through to the
    ambient ledger — the same fallback that already existed for the "no target
    persona" case. Losing the audit would trade one defect for a worse one.

    The ambient sink is the fixture's redirected dir rather than a hand-rolled
    ``monkeypatch.setattr(config, "DATA_DIR", ...)``: one definition of "where
    the ambient sinks go" for the whole suite.
    """
    with pytest.raises(grants.ToolsetGrantRefusedError):
        add_persona_toolset("ghost", KNOWN_TOOLSET, **OPERATOR)

    row = rows(isolated_operator_sinks / grants.LEDGER_FILENAME)[-1]
    assert row["persona_id"] == "ghost"
    assert row["outcome"] == grants.OUTCOME_REFUSED
    assert row["reason"] == grants.REASON_UNKNOWN_PERSONA


def test_the_ghost_refusal_leaves_the_checkouts_own_ledger_byte_identical(
    profile: Path, isolated_operator_sinks: Path
):
    """The guard for the fixture class itself (Codex R4).

    Resolves the checkout's real ledger from the REPO TREE, not from
    ``config.DATA_DIR`` — the fixture has already redirected that attribute,
    so reading it back would follow the redirect and prove nothing. Byte
    comparison, so a test that starts writing there fails here instead of
    silently corrupting operator state that no assertion looks at.
    """
    checkout_ledger = CLAUDE_DIR / "data" / grants.LEDGER_FILENAME
    before = checkout_ledger.read_bytes() if checkout_ledger.is_file() else None

    with pytest.raises(grants.ToolsetGrantRefusedError):
        add_persona_toolset("ghost", KNOWN_TOOLSET, **OPERATOR)

    after = checkout_ledger.read_bytes() if checkout_ledger.is_file() else None
    assert after == before, (
        f"{checkout_ledger} changed during the test run — an ambient sink "
        f"escaped the fixture"
    )


def test_an_existing_persona_still_gets_its_own_target_keyed_ledger(profile: Path):
    """Non-vacuity contrast: the ghost fix must not break target-keying.

    A guard that always fell back to the ambient ledger would pass both cases
    above while destroying the Rule 4 invariant the ledger exists to hold, so
    a real persona's row is pinned to its OWN profile data dir.
    """
    add_persona_toolset("sales", KNOWN_TOOLSET, **OPERATOR)

    own = profile / "data" / grants.LEDGER_FILENAME
    assert own.is_file()
    assert rows(own)[-1]["persona_id"] == "sales"


def test_kill_switch_refuses_the_mutation(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    from security import kill_switches

    # Stub the module kill_switches lazily imports for its own audit row so
    # the refusal never reaches the real dashboard.db.
    stub = types.ModuleType("dashboard_api")
    stub._audit_write = lambda **_kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dashboard_api", stub)
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")

    with pytest.raises(kill_switches.KillSwitchDisabled):
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert not config_file(profile).exists()
    (row,) = rows(ledger)
    assert row["outcome"] == grants.OUTCOME_REFUSED
    assert row["reason"] == grants.REASON_KILL_SWITCH


# ── Q6 spike: the default-profile grant path ─────────────────────────────


def test_default_profile_grant_is_refused_with_the_spike_verdict(
    profile: Path, ledger: Path
):
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("default", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    refusal = excinfo.value
    assert refusal.reason == grants.REASON_DEFAULT_PROFILE_UNSUPPORTED
    assert "DEFAULT_AGENT_TOOLSET" in str(refusal)
    assert rows(ledger)[0]["reason"] == grants.REASON_DEFAULT_PROFILE_UNSUPPORTED


_SENTINEL_TOOL_DEFS = [{"name": "spike_probe_tool"}]


async def _drive_engine_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_profile: str,
    payload: tuple | None = None,
) -> tuple[list[str], list]:
    """Run ONE real ``ConversationEngine`` turn under *active_profile*.

    Returns ``(payload_calls, runtime_requests)`` — the profile names the
    engine handed to ``build_persona_tool_payload``, and the actual
    ``RuntimeRequest`` objects it built. The request is what makes these
    tests behavioral rather than structural: ``tool_defs`` is set ONLY by
    the persona-scope branch, so it reports whether the scope was applied
    no matter how the source is arranged.

    An exception anywhere in the engine's persona block does NOT quietly
    reach its ``except Exception`` — the handler references an undefined
    ``logger`` (chat/engine.py:1573) and raises ``NameError`` out of the
    turn. So "this turn completed" is itself proof the block ran clean, and
    a broken harness fails loudly instead of faking a not-called result.
    """
    import engine as engine_module
    from engine import ConversationEngine
    from models import Channel, IncomingMessage, Platform, Thread, User
    from session import SQLiteSessionStore

    import personas as personas_module
    from runtime import persona_elevation, persona_tools
    from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult

    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(parents=True)

    runtime_requests: list = []

    async def fake_run(request):
        runtime_requests.append(request)
        return RuntimeResult(
            text="runtime replied",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude",
            model="claude-sonnet-5",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    # Module-attribute patches (Rule 3): engine.py does `import personas as
    # _personas` and a call-time `from runtime.persona_tools import ...`, so
    # every one of these propagates into the live turn.
    monkeypatch.setattr(
        personas_module, "get_active_profile_name", lambda: active_profile
    )
    monkeypatch.setattr(
        personas_module,
        "load_persona_config",
        lambda name: {"persona": {"id": name}},
    )
    monkeypatch.setattr(persona_elevation, "build_turn_context", lambda *a, **k: None)
    monkeypatch.setattr(
        persona_tools, "persona_tool_scope_version", lambda *a, **k: "scope-v1"
    )

    payload_calls: list[str] = []

    def recording_payload(profile_name, _config, **_kwargs):
        payload_calls.append(profile_name)
        return payload

    monkeypatch.setattr(persona_tools, "build_persona_tool_payload", recording_payload)

    message = IncomingMessage(
        text="Need a summary of where the pipeline stands",
        user=User(platform=Platform.TELEGRAM, platform_id="user-1", display_name="YourUser"),
        channel=Channel(platform=Platform.TELEGRAM, platform_id="chat-1", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id="thread-1"),
    )
    engine = ConversationEngine(SQLiteSessionStore(tmp_path / "chat.db"), project_root)
    outputs = [out async for out in engine.handle_message(message)]

    assert outputs, "engine produced no output — the harness never reached the turn"
    assert len(runtime_requests) == 1, runtime_requests
    return payload_calls, runtime_requests


@pytest.mark.asyncio
async def test_engine_never_resolves_persona_tools_for_the_default_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The behavioral evidence behind the refusal above.

    This is the entire justification for refusing a default-profile grant:
    the main homie's chat turn never reads config ``toolsets:``, so writing
    one would change a file nothing reads. Asserted by BEHAVIOR, not by
    source order — a refactor that dedents the payload build out of the
    gate would make default-profile grants effective while a source-order
    receipt (gate text appears before the call) kept passing right through
    it. This fails there, which is exactly when the refusal should be
    revisited rather than silently kept.
    """
    payload_calls, requests = await _drive_engine_turn(
        tmp_path, monkeypatch, active_profile="default", payload=(_SENTINEL_TOOL_DEFS, {})
    )

    assert payload_calls == [], (
        "the default profile resolved persona tools — the engine now reads "
        "config `toolsets:` for the main homie, so the default-profile "
        "grant refusal is stale and must be revisited"
    )
    # The gate's product effect, independent of how the source is arranged:
    # no persona scope ever reaches the runtime for the main homie. Note the
    # payload builder above was armed with a REAL payload, so a call would
    # have shown up here.
    assert requests[0].tool_defs is None
    assert requests[0].tool_dispatch is None


@pytest.mark.asyncio
async def test_engine_resolves_persona_tools_for_a_named_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The inverse: the gate lets NAMED personas through.

    Without this, the default-profile test above could pass for the trivial
    wrong reason — a harness that never reaches the payload builder at all.
    """
    payload_calls, requests = await _drive_engine_turn(
        tmp_path, monkeypatch, active_profile="sales", payload=(_SENTINEL_TOOL_DEFS, {})
    )

    assert payload_calls == ["sales"]
    assert requests[0].tool_defs == _SENTINEL_TOOL_DEFS
    # Scoped personas do NOT also get the built-in surface.
    assert requests[0].allowed_tools == []


# ── Liveness + registry freshness ────────────────────────────────────────


def test_grant_is_live_on_the_next_resolution_with_no_cache_to_flush(
    profile: Path, ledger: Path
):
    from runtime import capabilities as runtime_capabilities
    from runtime import toolsets as runtime_toolsets

    write_config(profile, "toolsets:\n  - safe_core\n")
    before = resolve_persona_tool_scope(read_profile_config("sales"))
    assert KNOWN_TOOLSET not in before.toolsets

    add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    # Same reads a persona turn makes: config off disk -> declared scope ->
    # registry resolution. No invalidation call in between.
    after = resolve_persona_tool_scope(read_profile_config("sales"))
    assert KNOWN_TOOLSET in after.toolsets

    resolved = runtime_capabilities.resolve_toolset(
        KNOWN_TOOLSET, registry=runtime_toolsets.TOOLSETS
    )
    assert "web_search" in resolved


def test_registry_check_reads_the_live_registry_not_a_snapshot(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """A plugin-registered toolset must be grantable without a restart."""
    from runtime import toolsets as runtime_toolsets

    extended = dict(runtime_toolsets.TOOLSETS)
    extended["plugin_pack"] = {
        "description": "registered after import",
        "tools": ["plugin_read"],
        "includes": [],
    }
    monkeypatch.setattr(runtime_toolsets, "TOOLSETS", extended)

    assert "plugin_pack" in grants.known_toolset_names()
    result = add_persona_toolset(
        "sales",
        "plugin_pack",
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="give sales the plugin pack",
        surface="telegram",
        channel_id="42",
    )
    assert result.changed is True
    assert read_profile_config("sales")["toolsets"] == ["plugin_pack"]


# ── Concurrency: the read-modify-write is serialized per persona ─────────


def test_concurrent_grants_to_one_persona_keep_both_and_audit_truthfully(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two grants racing the same config must both survive, both truthfully.

    ``os.replace`` makes one WRITE atomic; it does nothing for the
    read -> decide -> append -> write sequence. Without a lock held across
    that whole sequence, both callers read the same base list, both append a
    ``granted`` row, and the second replace drops the first grant: an
    accepted grant that is not live next turn, with a ledger swearing it is.

    The interleave is forced, not slept for. Thread ``A`` is held with its
    read ALREADY DONE -- holding the base list in hand -- until thread ``B``
    reaches the lock. That is precisely the window a lost update needs: park
    A before its read instead and A simply re-reads B's finished work, which
    passes with or without a lock. With the lock, B blocks at acquisition
    and reads fresh afterwards; without it, B never calls the lock at all,
    A's wait expires holding a stale base, and A's write clobbers B's grant.
    """
    import threading

    import shared
    from personas import services as persona_services

    write_config(profile, "toolsets:\n  - safe_core\n")

    b_reached_lock = threading.Event()
    real_read = persona_services._read_yaml_strict
    real_file_lock = shared.file_lock

    def hooked_read(path):
        data = real_read(path)
        # Park AFTER the read, so A is sitting on a base list it has already
        # committed to -- the only state from which a second writer can be
        # clobbered. Only A parks, and only inside the critical section.
        if threading.current_thread().name == "grant-A":
            # Bounded: in the FIXED path this returns instantly because B
            # sets the event before it blocks, so a green run never sleeps.
            # It only expires in the unlocked path, where B never reaches a
            # lock to announce -- and the collision assertions below fail.
            b_reached_lock.wait(timeout=10.0)
        return data

    def watched_file_lock(lock_path, timeout=30.0):
        if threading.current_thread().name == "grant-B":
            # Announce BEFORE acquiring, so A is released by a thread that
            # is about to block on A's own lock. Signalling after acquiring
            # would deadlock the pair.
            b_reached_lock.set()
        return real_file_lock(lock_path, timeout=timeout)

    monkeypatch.setattr(persona_services, "_read_yaml_strict", hooked_read)
    monkeypatch.setattr(shared, "file_lock", watched_file_lock)

    failures: list[BaseException] = []

    def grant(toolset: str):
        try:
            add_persona_toolset(
                "sales",
                toolset,
                audit_path=ledger,
                actor="owner",
                actor_role="admin",
                trigger_text=f"give sales {toolset}",
                surface="telegram",
                channel_id="42",
            )
        except BaseException as exc:  # noqa: BLE001 — re-raised in the main thread
            failures.append(exc)

    threads = [
        threading.Thread(target=grant, args=(KNOWN_TOOLSET,), name="grant-A"),
        threading.Thread(target=grant, args=(OTHER_TOOLSET,), name="grant-B"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
        assert not thread.is_alive(), "grant thread hung — the lock deadlocked"
    assert not failures, f"a grant raised: {failures!r}"

    # 1. Physical state — neither accepted grant was clobbered.
    final = read_profile_config("sales")["toolsets"]
    assert sorted(final) == sorted(["safe_core", KNOWN_TOOLSET, OTHER_TOOLSET])
    assert len(final) == len(set(final)), f"duplicate entries: {final}"

    # 2. Both outcome rows landed, both successes, one per toolset.
    #    Serializing the appends inside the lock is what keeps the two
    #    intent/outcome pairs from interleaving into each other.
    ledger_rows = outcome_rows(ledger)
    assert len(ledger_rows) == 2, ledger_rows
    assert {row["outcome"] for row in ledger_rows} == {grants.OUTCOME_GRANTED}
    assert {row["toolset"] for row in ledger_rows} == {KNOWN_TOOLSET, OTHER_TOOLSET}
    assert len(rows(ledger)) == 4, "each grant writes one intent + one outcome"

    # 3. Truthfulness — a row must never claim a toolset the live config
    #    lacks (the lost-update signature: the loser's row claims its own
    #    grant is live while the file says otherwise), and whichever grant
    #    ran second must report the full final state.
    for row in ledger_rows:
        assert set(row["toolsets_after"]) <= set(final), (
            f"row claims state the config does not have: {row}"
        )
    assert max((row["toolsets_after"] for row in ledger_rows), key=len) == final


# ── Peer writers: a blueprint reconcile must not erase a grant ───────────


def _provision_paths(tmp_path: Path):
    """Minimal physical provisioning environment (shape from
    ``tests/test_persona_provisioning.py::_paths``)."""
    import yaml

    from personas.provisioning import ProvisionPaths

    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "env_groups": {
                    "runtime_core": ["OPENAI_API_KEY"],
                    "vault_memory": ["HOMIE_VAULT_DIR"],
                },
                "skill_groups": {},
                "profile_defaults": {
                    "env_groups": ["runtime_core", "vault_memory"],
                    "skill_groups": [],
                    "skills": [],
                },
                "profiles": {},
            }
        ),
        encoding="utf-8",
    )
    master_env = tmp_path / "master.env"
    master_env.write_text(
        "OPENAI_API_KEY=top-secret\nHOMIE_VAULT_DIR=C:/vault\n", encoding="utf-8"
    )
    bindings = tmp_path / "discord-channel-bindings.json"
    bindings.write_text(
        json.dumps({"guild_id": "g1", "channels": {}}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ProvisionPaths(
        homie_root=tmp_path / "homie",
        bindings_file=bindings,
        capability_matrix_file=matrix,
        master_env_file=master_env,
    )


def test_blueprint_reconcile_preserves_an_audited_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A normal reconcile must not silently erase an executor-owned grant.

    The executor is not the only writer of ``toolsets:`` — blueprint
    provisioning renders the whole config from a template and the patch
    REPLACES the list. Before the fix, granting ``research_read`` and then
    running a plain reconcile left the persona without it while the
    append-only ledger still carried a ``granted`` row and no ``revoked``
    row: the ledger described reach the persona did not have.

    Full delta routing (blueprint ADDS also carrying operator-turn
    provenance) is issue #435; this pins the erasure half.
    """
    import yaml

    import config as config_module
    from personas.blueprints import ProvisionMode, build_builtin_blueprint
    from personas.provisioning import apply_provision, preview_provision

    paths = _provision_paths(tmp_path)
    # Both the executor and provisioning must resolve the SAME profile tree
    # and the SAME default ledger — that shared physical state is the fix.
    monkeypatch.setenv("HOMIE_HOME", str(paths.homie_root))
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    # Default resolution, keyed to the TARGET persona (no audit_path= here:
    # these are the end-to-end tests that exercise real path resolution).
    ledger_path = grants.resolve_ledger_path(None, "ai-engineer")

    audit_calls: list[dict] = []
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda _actor, _persona, _outcome, receipt: audit_calls.append(receipt),
    )

    def provision(mode, *, approved=False):
        blueprint = build_builtin_blueprint("ai-engineer", channel_id="123456789012345678")
        preview = preview_provision(blueprint, mode=mode, paths=paths)
        result = apply_provision(
            blueprint,
            mode=mode,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test-operator",
            paths=paths,
            reconcile_approved=approved,
        )
        return preview, result

    def live_toolsets():
        text = (paths.profiles_root / "ai-engineer" / "config.yaml").read_text(
            encoding="utf-8"
        )
        return yaml.safe_load(text)["toolsets"]

    provision(ProvisionMode.CREATE)
    assert live_toolsets() == ["safe_core", "ai_engineering"]

    add_persona_toolset(
        "ai-engineer",
        KNOWN_TOOLSET,
        actor="owner",
        actor_role="admin",
        trigger_text="give the ai engineer the research toolset",
        surface="telegram",
        channel_id="42",
    )
    assert KNOWN_TOOLSET in live_toolsets()

    preview, _result = provision(ProvisionMode.RECONCILE, approved=True)

    # 1. The grant survived the template rewrite.
    assert KNOWN_TOOLSET in live_toolsets(), (
        "a plain blueprint reconcile erased an audited grant — the ledger now "
        "claims reach the persona does not have"
    )
    # The blueprint still owns its own scope; preserving is additive.
    assert {"safe_core", "ai_engineering"} <= set(live_toolsets())

    # 2. The ledger stayed truthful: one grant, no phantom revoke, and the
    #    replay still matches what the persona physically holds.
    ledger_outcomes = [row["outcome"] for row in rows(ledger_path)]
    assert ledger_outcomes.count(grants.OUTCOME_GRANTED) == 1
    assert grants.OUTCOME_REVOKED not in ledger_outcomes
    assert grants.active_grants("ai-engineer") == (KNOWN_TOOLSET,)
    assert set(grants.active_grants("ai-engineer")) <= set(live_toolsets())

    # 3. The preservation is on the receipt, not silent.
    assert preview.preserved_grants == (KNOWN_TOOLSET,)
    assert audit_calls, "provisioning audit never fired"
    assert audit_calls[-1]["preserved_grants"] == [KNOWN_TOOLSET]


def test_a_half_completed_revoke_never_resurrects_the_toolset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A revoke that moved the config but lost its outcome row must stay revoked.

    The torn state is reachable: the executor writes intent, mutates the
    config, then appends the outcome — and that last append can fail, which
    raises WITHOUT rolling the config back. Round 4's preserve mechanism then
    turned that into a REGRESSION: the replay ignored intent rows, so it
    still counted the old grant, and a blueprint reconcile treated the replay
    as authority and RE-ADDED the revoked toolset to physical config.

    Both seams are pinned here, and the ordering matters — the reconcile
    check runs BEFORE the retry, because a working repair would otherwise
    heal the ledger and hide a broken replay.
    """
    import yaml

    import config as config_module
    from personas.blueprints import ProvisionMode, build_builtin_blueprint
    from personas.provisioning import apply_provision, preview_provision

    paths = _provision_paths(tmp_path)
    monkeypatch.setenv("HOMIE_HOME", str(paths.homie_root))
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit", lambda *_a, **_k: None
    )
    # Default resolution, keyed to the TARGET persona (no audit_path= here:
    # these are the end-to-end tests that exercise real path resolution).
    ledger_path = grants.resolve_ledger_path(None, "ai-engineer")
    revoked_toolset = "operator_exec"

    def blueprint():
        return build_builtin_blueprint("ai-engineer", channel_id="123456789012345678")

    def reconcile_preview():
        return preview_provision(blueprint(), mode=ProvisionMode.RECONCILE, paths=paths)

    def provision(mode, *, approved=False):
        preview = preview_provision(blueprint(), mode=mode, paths=paths)
        return apply_provision(
            blueprint(),
            mode=mode,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test-operator",
            paths=paths,
            reconcile_approved=approved,
        )

    def live_toolsets():
        text = (paths.profiles_root / "ai-engineer" / "config.yaml").read_text(
            encoding="utf-8"
        )
        return yaml.safe_load(text)["toolsets"]

    turn = {
        "actor": "owner",
        "actor_role": "admin",
        "surface": "telegram",
        "channel_id": "42",
    }

    provision(ProvisionMode.CREATE)
    add_persona_toolset(
        "ai-engineer",
        revoked_toolset,
        trigger_text="give the ai engineer operator exec",
        **turn,
    )
    assert revoked_toolset in live_toolsets()

    # ── The torn revoke: config mutation lands, the outcome row does not.
    real_append = grants.append_audit_record

    def fail_the_effective_revoke(**fields):
        if fields.get("outcome") == grants.OUTCOME_REVOKED:
            raise OSError("ledger volume vanished mid-revoke")
        return real_append(**fields)

    monkeypatch.setattr(grants, "append_audit_record", fail_the_effective_revoke)
    with pytest.raises(grants.ToolsetGrantAuditError) as excinfo:
        remove_persona_toolset(
            "ai-engineer",
            revoked_toolset,
            trigger_text="take operator exec back off",
            **turn,
        )

    # Physical state moved; the ledger never recorded it. A caller MUST be
    # able to tell this apart from an unaudited refusal (#427 R2 gate).
    assert revoked_toolset not in live_toolsets()
    assert excinfo.value.applied is True
    assert grants.OUTCOME_REVOKED not in {r["outcome"] for r in rows(ledger_path)}

    # ── SEAM 1 (checked with the ledger still torn, BEFORE any retry).
    assert grants.active_grants("ai-engineer") == (), (
        "the replay still counts a revoked toolset as granted — a reconcile "
        "will hand the persona back reach an operator took away"
    )
    assert reconcile_preview().preserved_grants == ()

    # ── Storage recovers. SEAM 2: the retry heals the ledger.
    monkeypatch.setattr(grants, "append_audit_record", real_append)
    result = remove_persona_toolset(
        "ai-engineer",
        revoked_toolset,
        trigger_text="make sure operator exec is off",
        **turn,
    )
    assert result.outcome == grants.OUTCOME_NOT_GRANTED
    assert result.changed is False

    repairs = [
        r for r in rows(ledger_path)
        if r["reason"] == grants.REASON_REPAIR_CONFIG_ABSENT
    ]
    assert len(repairs) == 1, (
        "the retry observed config-absent while the ledger still claimed the "
        "grant, and did not record the revoke that actually happened"
    )
    (repair,) = repairs
    assert repair["outcome"] == grants.OUTCOME_REVOKED
    assert repair["operation"] == grants.OPERATION_REVOKE
    # Correlated to the TORN attempt's intent, not to the retry that noticed.
    torn_intent = next(
        r for r in rows(ledger_path)
        if r["outcome"] == grants.OUTCOME_INTENT
        and r["operation"] == grants.OPERATION_REVOKE
    )
    assert repair["correlation_id"] == torn_intent["correlation_id"]
    # Healed once, not on every retry.
    remove_persona_toolset(
        "ai-engineer", revoked_toolset, trigger_text="and again", **turn
    )
    assert len(
        [r for r in rows(ledger_path) if r["reason"] == grants.REASON_REPAIR_CONFIG_ABSENT]
    ) == 1

    # ── End state: a real reconcile leaves it gone, and the ledger agrees.
    provision(ProvisionMode.RECONCILE, approved=True)
    assert revoked_toolset not in live_toolsets()
    assert grants.active_grants("ai-engineer") == ()
    effective = [
        r for r in rows(ledger_path)
        if r["outcome"] in (grants.OUTCOME_GRANTED, grants.OUTCOME_REVOKED)
    ]
    assert [r["outcome"] for r in effective] == [
        grants.OUTCOME_GRANTED,
        grants.OUTCOME_REVOKED,
    ]


def test_a_half_completed_grant_is_healed_and_then_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The mirror of the revoke heal: a torn GRANT must not go invisible.

    A grant tears exactly like a revoke — intent row, config write lands,
    outcome append fails and raises without rolling back. The stake is
    inverted: the toolset IS in config, but with no effective ``granted``
    row the replay cannot see it, so a blueprint reconcile does not preserve
    it and the operator's grant quietly disappears at the next template
    rewrite. The retry, which finds the name PRESENT, is where physical state
    proves the grant landed.
    """
    import yaml

    import config as config_module
    from personas.blueprints import ProvisionMode, build_builtin_blueprint
    from personas.provisioning import apply_provision, preview_provision

    paths = _provision_paths(tmp_path)
    monkeypatch.setenv("HOMIE_HOME", str(paths.homie_root))
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit", lambda *_a, **_k: None
    )
    # Default resolution, keyed to the TARGET persona (no audit_path= here:
    # these are the end-to-end tests that exercise real path resolution).
    ledger_path = grants.resolve_ledger_path(None, "ai-engineer")

    def provision(mode, *, approved=False):
        blueprint = build_builtin_blueprint("ai-engineer", channel_id="123456789012345678")
        preview = preview_provision(blueprint, mode=mode, paths=paths)
        return apply_provision(
            blueprint,
            mode=mode,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test-operator",
            paths=paths,
            reconcile_approved=approved,
        )

    def live_toolsets():
        text = (paths.profiles_root / "ai-engineer" / "config.yaml").read_text(
            encoding="utf-8"
        )
        return yaml.safe_load(text)["toolsets"]

    turn = {
        "actor": "owner",
        "actor_role": "admin",
        "surface": "telegram",
        "channel_id": "42",
    }
    provision(ProvisionMode.CREATE)

    # The torn grant: config write lands, effective row does not.
    real_append = grants.append_audit_record

    def fail_the_effective_grant(**fields):
        if fields.get("outcome") == grants.OUTCOME_GRANTED:
            raise OSError("ledger volume vanished mid-grant")
        return real_append(**fields)

    monkeypatch.setattr(grants, "append_audit_record", fail_the_effective_grant)
    with pytest.raises(grants.ToolsetGrantAuditError) as excinfo:
        add_persona_toolset(
            "ai-engineer", KNOWN_TOOLSET, trigger_text="give it research", **turn
        )

    assert excinfo.value.applied is True
    assert KNOWN_TOOLSET in live_toolsets()
    # Invisible to the replay while torn — which is why a reconcile would
    # drop it, and why the retry has to heal.
    assert grants.active_grants("ai-engineer") == ()

    # Storage recovers; the retry observes config-present and heals.
    monkeypatch.setattr(grants, "append_audit_record", real_append)
    result = add_persona_toolset(
        "ai-engineer", KNOWN_TOOLSET, trigger_text="make sure research is on", **turn
    )
    assert result.outcome == grants.OUTCOME_ALREADY_GRANTED
    assert result.changed is False

    repairs = [
        r for r in rows(ledger_path)
        if r["reason"] == grants.REASON_REPAIR_CONFIG_PRESENT
    ]
    assert len(repairs) == 1, (
        "the retry saw the toolset in config with only a dangling grant "
        "intent behind it, and did not record the grant that really landed"
    )
    (repair,) = repairs
    assert repair["outcome"] == grants.OUTCOME_GRANTED
    torn_intent = next(
        r for r in rows(ledger_path)
        if r["outcome"] == grants.OUTCOME_INTENT
        and r["operation"] == grants.OPERATION_GRANT
    )
    assert repair["correlation_id"] == torn_intent["correlation_id"]
    # Healed once, not on every retry.
    add_persona_toolset(
        "ai-engineer", KNOWN_TOOLSET, trigger_text="and again", **turn
    )
    assert len(
        [r for r in rows(ledger_path) if r["reason"] == grants.REASON_REPAIR_CONFIG_PRESENT]
    ) == 1

    # The replay counts the healed grant, so the reconcile preserves it.
    assert grants.active_grants("ai-engineer") == (KNOWN_TOOLSET,)
    provision(ProvisionMode.RECONCILE, approved=True)
    assert KNOWN_TOOLSET in live_toolsets()


def test_grant_from_a_persona_bot_process_is_seen_by_a_default_process_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The ledger follows the TARGET persona, not the process's own profile.

    Persona bots run as separate processes with ``HOMIE_HOME`` forced to
    their own profile root, so ``config.DATA_DIR`` — computed once at import
    from the AMBIENT profile — differs per process. Keying the ledger off it
    meant a grant made inside one bot landed in that bot's profile while
    provisioning, running as the default profile, read a different file
    entirely: saw no grants, and erased them on the next reconcile. Same
    authorization grain, two different files.

    This drives the real path resolution — no ``audit_path=`` anywhere — with
    the ambient profile deliberately set to something OTHER than the target.
    """
    import yaml

    import config as config_module
    from personas.blueprints import ProvisionMode, build_builtin_blueprint
    from personas.provisioning import apply_provision, preview_provision

    paths = _provision_paths(tmp_path)
    target = "ai-engineer"
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit", lambda *_a, **_k: None
    )

    def become_process(profile_name: str) -> None:
        """Enter the ambient env a bot/dashboard process would import under.

        A persona bot gets HOMIE_HOME = its own profile root, and its
        ``config.DATA_DIR`` resolves from that at import time.
        """
        if profile_name == "default":
            monkeypatch.setenv("HOMIE_HOME", str(paths.homie_root))
            monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "install-data")
            return
        root = paths.profiles_root / profile_name
        monkeypatch.setenv("HOMIE_HOME", str(root))
        monkeypatch.setattr(config_module, "DATA_DIR", root / "data")

    def provision(mode, *, approved=False):
        blueprint = build_builtin_blueprint(target, channel_id="123456789012345678")
        preview = preview_provision(blueprint, mode=mode, paths=paths)
        apply_provision(
            blueprint,
            mode=mode,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test-operator",
            paths=paths,
            reconcile_approved=approved,
        )
        return preview

    def live_toolsets():
        text = (paths.profiles_root / target / "config.yaml").read_text(encoding="utf-8")
        return yaml.safe_load(text)["toolsets"]

    become_process("default")
    provision(ProvisionMode.CREATE)

    # ── Now we are the SALES bot: a different profile entirely, granting to
    # ai-engineer. Nothing here injects a ledger path.
    become_process("sales")
    ambient_ledger = paths.profiles_root / "sales" / "data" / grants.LEDGER_FILENAME
    target_ledger = paths.profiles_root / target / "data" / grants.LEDGER_FILENAME

    add_persona_toolset(
        target,
        KNOWN_TOOLSET,
        actor="owner",
        actor_role="admin",
        trigger_text="grant from the sales bot process",
        surface="telegram",
        channel_id="42",
    )

    assert target_ledger.is_file(), (
        "the grant did not land in the TARGET persona's ledger — it followed "
        "the ambient profile of the process that happened to execute it"
    )
    assert not ambient_ledger.exists(), (
        "the grant landed in the EXECUTING process's own profile ledger"
    )
    assert [r["outcome"] for r in rows(target_ledger)] == [
        grants.OUTCOME_INTENT,
        grants.OUTCOME_GRANTED,
    ]

    # ── And back in the DEFAULT process, where reconciles run.
    become_process("default")
    assert grants.active_grants(target) == (KNOWN_TOOLSET,), (
        "a default-profile process cannot see the grant a persona bot wrote"
    )

    preview = provision(ProvisionMode.RECONCILE, approved=True)
    assert preview.preserved_grants == (KNOWN_TOOLSET,)
    assert KNOWN_TOOLSET in live_toolsets(), (
        "the reconcile erased a grant it could not see"
    )

    # The mirror: a revoke from the bot process must tombstone for the
    # default process too, or round 7's resurrection comes straight back.
    become_process("sales")
    remove_persona_toolset(
        target,
        KNOWN_TOOLSET,
        actor="owner",
        actor_role="admin",
        trigger_text="revoke from the sales bot process",
        surface="telegram",
        channel_id="42",
    )
    become_process("default")
    assert grants.ledger_scope(target).tombstoned == (KNOWN_TOOLSET,)
    provision(ProvisionMode.RECONCILE, approved=True)
    assert KNOWN_TOOLSET not in live_toolsets()


def test_reconcile_keeps_a_revoked_blueprint_bundle_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A successful revoke must survive a reconcile even when the TEMPLATE wants it.

    The preserve mechanism only ever unioned the POSITIVE replay, so it could
    say "this was granted, keep it" but had no way to say "this was revoked,
    keep it OFF". A blueprint-recommended bundle therefore came straight back
    on the next reconcile — the ledger read `revoked` while live reach
    returned. The existing torn-revoke test misses this because it revokes
    `operator_exec`, which this blueprint does not recommend.

    Event order decides, so a later re-grant clears the tombstone.
    """
    import yaml

    import config as config_module
    from personas.blueprints import ProvisionMode, build_builtin_blueprint
    from personas.provisioning import apply_provision, preview_provision

    # A bundle the ai-engineer blueprint RECOMMENDS — that is the whole point.
    recommended = "ai_engineering"

    paths = _provision_paths(tmp_path)
    monkeypatch.setenv("HOMIE_HOME", str(paths.homie_root))
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    audit_receipts: list[dict] = []
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda _a, _p, _o, receipt: audit_receipts.append(receipt),
    )

    def provision(mode, *, approved=False):
        blueprint = build_builtin_blueprint("ai-engineer", channel_id="123456789012345678")
        preview = preview_provision(blueprint, mode=mode, paths=paths)
        apply_provision(
            blueprint,
            mode=mode,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test-operator",
            paths=paths,
            reconcile_approved=approved,
        )
        return preview

    def live_toolsets():
        text = (paths.profiles_root / "ai-engineer" / "config.yaml").read_text(
            encoding="utf-8"
        )
        return yaml.safe_load(text)["toolsets"]

    turn = {
        "actor": "owner",
        "actor_role": "admin",
        "surface": "telegram",
        "channel_id": "42",
    }

    provision(ProvisionMode.CREATE)
    assert recommended in live_toolsets(), "fixture assumption: blueprint recommends it"

    # A successful, fully-audited revoke of the blueprint's own bundle.
    result = remove_persona_toolset(
        "ai-engineer", recommended, trigger_text="drop ai engineering", **turn
    )
    assert result.outcome == grants.OUTCOME_REVOKED
    assert recommended not in live_toolsets()
    assert grants.ledger_scope("ai-engineer").tombstoned == (recommended,)

    preview = provision(ProvisionMode.RECONCILE, approved=True)
    assert recommended not in live_toolsets(), (
        "the reconcile re-added a successfully revoked bundle — the ledger "
        "says revoked while live reach came back"
    )
    # The removal is on the receipt, not silent.
    assert preview.revoked_grants == (recommended,)
    assert audit_receipts[-1]["revoked_grants"] == [recommended]
    # Everything the operator never touched still belongs to the template.
    assert "safe_core" in live_toolsets()

    # ── Event order wins: a re-grant clears the tombstone and holds.
    add_persona_toolset(
        "ai-engineer", recommended, trigger_text="put ai engineering back", **turn
    )
    assert grants.ledger_scope("ai-engineer").tombstoned == ()
    assert grants.active_grants("ai-engineer") == (recommended,)

    provision(ProvisionMode.RECONCILE, approved=True)
    assert recommended in live_toolsets(), (
        "a re-grant after a revoke must survive the next reconcile"
    )


def test_reconcile_holds_the_executors_config_lock_while_committing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Provisioning and the executor must serialize on the SAME lock.

    Provisioning locks ``run/persona-provisioning/locks/<persona>``; the
    executor locks ``<profile>/config.yaml.lock``. Nothing serialized the
    two, so a reconcile could hash-check config.yaml, lose the CPU to a
    concurrent grant committing under the OTHER lock, and then atomically
    replace the file with its own stale render — erasing an audited grant.

    Proven by holding the reconcile INSIDE its config write and showing the
    canonical lock is genuinely unavailable to anyone else, plus a real
    concurrent grant that ends up in the file rather than under it.
    """
    import threading

    import yaml

    import config as config_module
    import shared
    from personas import provisioning
    from personas.blueprints import ProvisionMode, build_builtin_blueprint
    from personas.provisioning import apply_provision, preview_provision

    paths = _provision_paths(tmp_path)
    monkeypatch.setenv("HOMIE_HOME", str(paths.homie_root))
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit", lambda *_a, **_k: None
    )
    config_path = paths.profiles_root / "ai-engineer" / "config.yaml"

    def blueprint():
        return build_builtin_blueprint("ai-engineer", channel_id="123456789012345678")

    def provision(mode, *, approved=False):
        preview = preview_provision(blueprint(), mode=mode, paths=paths)
        return apply_provision(
            blueprint(),
            mode=mode,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test-operator",
            paths=paths,
            reconcile_approved=approved,
        )

    provision(ProvisionMode.CREATE)

    # Give the reconcile real work on config.yaml: `tools:` is a key the
    # blueprint owns and rewrites, so this guarantees a config commit.
    stale = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    stale["tools"] = ["stale_tool"]
    config_path.write_text(yaml.safe_dump(stale, sort_keys=False), encoding="utf-8")

    committing = threading.Event()
    release = threading.Event()
    real_atomic_write = provisioning.atomic_write_text

    def paused_write(target, content):
        if Path(target).name == "config.yaml":
            committing.set()
            # Bounded: the probe below releases this as soon as it has its
            # answer. Never a synchronization sleep.
            release.wait(timeout=30.0)
        return real_atomic_write(target, content)

    monkeypatch.setattr(provisioning, "atomic_write_text", paused_write)

    failures: list[BaseException] = []

    def run(fn):
        def wrapped():
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 — surfaced below
                failures.append(exc)
        return wrapped

    reconcile = threading.Thread(
        target=run(lambda: provision(ProvisionMode.RECONCILE, approved=True)),
        name="reconcile",
    )
    reconcile.start()
    assert committing.wait(timeout=30.0), "reconcile never reached its config write"

    # THE ASSERTION: while the reconcile is mid-commit, the executor's
    # canonical lock is held, so no grant can slip in underneath it.
    lock_was_held = False
    try:
        with shared.file_lock(config_path, timeout=0.3):
            lock_was_held = False
    except TimeoutError:
        lock_was_held = True

    # A real grant, racing the same window.
    granter = threading.Thread(
        target=run(
            lambda: add_persona_toolset(
                "ai-engineer",
                KNOWN_TOOLSET,
                actor="owner",
                actor_role="admin",
                trigger_text="grant during the reconcile",
                surface="telegram",
                channel_id="42",
            )
        ),
        name="granter",
    )
    granter.start()

    release.set()
    for thread in (reconcile, granter):
        thread.join(timeout=60.0)
        assert not thread.is_alive(), f"{thread.name} hung"
    assert not failures, f"a worker raised: {failures!r}"

    assert lock_was_held, (
        "the reconcile committed config.yaml without holding the executor's "
        "canonical lock — a concurrent grant can be erased in that window"
    )

    final = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # The reconcile's own work landed...
    assert final["tools"] == []
    # ...and the concurrent grant is in the file, not under it.
    assert KNOWN_TOOLSET in final["toolsets"]
    assert grants.active_grants("ai-engineer") == (KNOWN_TOOLSET,)


@pytest.mark.parametrize(
    ("root_text", "root_type"),
    [("[]\n", "list"), ("false\n", "bool"), ("0\n", "int")],
)
def test_falsey_yaml_root_refuses_instead_of_being_clobbered(
    profile: Path, ledger: Path, root_text: str, root_type: str
):
    """A non-mapping config root must raise, not be silently overwritten.

    The strict reader collapsed ``yaml.safe_load(text) or {}``, so every
    FALSEY root — ``[]``, ``false``, ``0``, ``''`` — became an empty mapping
    that passed the ``isinstance(result, dict)`` guard directly beneath it.
    The executor then wrote its own shape over a file it had never
    understood, which is precisely the clobber the strict reader exists to
    prevent.
    """
    config = write_config(profile, root_text)
    before = config.read_bytes()

    with pytest.raises(ConfigShapeError) as excinfo:
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert "top-level must be mapping" in str(excinfo.value)
    assert root_type in str(excinfo.value)
    # Byte-identity: the operator's file is exactly as they left it.
    assert config.read_bytes() == before
    assert rows(ledger)[0]["reason"] == grants.REASON_CONFIG_SHAPE


def test_invalid_utf8_config_raises_config_shape_and_is_audited(
    profile: Path, ledger: Path
):
    """A corrupt config byte is a malformed config: raise, audit, touch nothing.

    ``_read_yaml_strict`` caught ``OSError`` and ``yaml.YAMLError`` only, but
    ``UnicodeDecodeError`` is a ``ValueError`` — so an invalid byte escaped
    both, never became ``ConfigShapeError``, and therefore never reached the
    executor's refusal audit, which keys on ``ConfigShapeError``. The exit was
    entirely unrecorded.
    """
    config = config_file(profile)
    config.write_bytes(b"toolsets:\n  - safe_core\n# caf\xff\xfe\n")
    before = config.read_bytes()

    with pytest.raises(ConfigShapeError) as excinfo:
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert "encoding" in str(excinfo.value)
    assert config.read_bytes() == before
    (row,) = rows(ledger)
    assert row["outcome"] == grants.OUTCOME_ERROR
    assert row["reason"] == grants.REASON_CONFIG_SHAPE


def test_hostile_but_valid_json_rows_cannot_break_the_replay(
    profile: Path, ledger: Path
):
    """Schema-valid JSON is still untrusted input.

    Every field in a ledger row is JSON, so a corrupt or hostile line can put
    any value anywhere. The replay used to call ``str(...)`` on whatever it
    found; ``str()`` of a deeply nested list recurses per level and raised
    ``RecursionError`` straight out of a function that promises to raise
    nothing — one bad line blocking every persona create and reconcile.
    ``json.loads`` itself recurses too, so deep enough nesting blew up before
    the replay even saw the row.

    Fields are now type-CHECKED and length-bounded, never coerced.
    """
    add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    deep_open, deep_close = b"[" * 20000, b"]" * 20000
    with open(ledger, "ab") as handle:
        # (a) so deep that `json.loads` itself blows the stack
        handle.write(
            b'{"persona_id":"sales","outcome":"granted","toolset":'
            + deep_open + b"0" + deep_close + b"}\n"
        )
        # (b) valid JSON, wrong TYPES in every replay field
        handle.write(
            json.dumps(
                {
                    "persona_id": "sales",
                    "outcome": ["granted"],
                    "toolset": {"nested": "object"},
                    "operation": 17,
                    "correlation_id": None,
                }
            ).encode("utf-8") + b"\n"
        )
        # (c) valid types, absurd length
        handle.write(
            json.dumps(
                {
                    "persona_id": "sales",
                    "outcome": grants.OUTCOME_GRANTED,
                    "toolset": "x" * 50000,
                    "operation": grants.OPERATION_GRANT,
                }
            ).encode("utf-8") + b"\n"
        )

    # Every reader still answers, and none of the junk became reach.
    assert grants.active_grants("sales", ledger) == (KNOWN_TOOLSET,)
    assert grants.ledger_scope("sales", ledger).tombstoned == ()
    assert grants.orphan_intent_correlation(
        "sales", KNOWN_TOOLSET, grants.OPERATION_GRANT, ledger
    ) == ""

    # And the executor still works over the poisoned ledger.
    result = remove_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="take it off",
        surface="telegram",
        channel_id="42",
    )
    assert result.outcome == grants.OUTCOME_REVOKED
    assert grants.ledger_scope("sales", ledger).tombstoned == (KNOWN_TOOLSET,)


def test_an_empty_config_file_is_still_treated_as_empty(profile: Path, ledger: Path):
    """The `None` parse — and only it — still means "no config yet"."""
    write_config(profile, "")
    result = add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert result.changed is True
    assert read_profile_config("sales")["toolsets"] == [KNOWN_TOOLSET]


def test_ledger_survives_invalid_utf8_and_malformed_json_lines(
    profile: Path, ledger: Path
):
    """One corrupt line costs one row — never the whole reader.

    Both readers took ``path.read_text(encoding="utf-8")`` inside an
    ``except OSError``. A single invalid byte raises ``UnicodeDecodeError``
    (a ``ValueError``), which sailed past that handler and out through
    ``active_grants`` into provisioning — turning a damaged log line into a
    blocked persona create/reconcile. Fail-open has to mean BOTH halves: no
    phantom grants AND no blocked reconcile.
    """
    add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)
    assert grants.active_grants("sales", ledger) == (KNOWN_TOOLSET,)

    with open(ledger, "ab") as handle:
        handle.write(b'{"persona_id": "sales", "outcome": "granted", "toolset": "\xff\xfe"}\n')
        handle.write(b"{not json at all\n")
        handle.write(b'"a bare json string, not an object"\n')
        handle.write(b"\n")

    # Reads through, skipping exactly the damaged lines.
    assert grants.active_grants("sales", ledger) == (KNOWN_TOOLSET,)
    assert grants.orphan_intent_correlation(
        "sales", KNOWN_TOOLSET, grants.OPERATION_GRANT, ledger
    ) == ""

    # And the executor still works over the damaged ledger.
    result = remove_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="take it off",
        surface="telegram",
        channel_id="42",
    )
    assert result.outcome == grants.OUTCOME_REVOKED
    assert grants.active_grants("sales", ledger) == ()


def _write_attempt(
    ledger_path: Path,
    outcome: str,
    toolset: str,
    *,
    operation: str = grants.OPERATION_GRANT,
    persona: str = "sales",
    with_intent: bool = True,
    correlation: str = "",
    **overrides,
) -> str:
    """Append a REAL-shaped attempt: an intent row, then its outcome row.

    The executor never writes an effective row without a preceding intent
    carrying the same correlation id, and never writes either without the
    full operator turn. A test that fabricates rows any other way is
    describing a ledger the executor cannot produce.
    """
    correlation = correlation or grants.new_correlation_id()
    fields = {
        "operation": operation,
        "persona_id": persona,
        "toolset": toolset,
        "actor": "owner",
        "actor_role": "admin",
        "trigger_text": f"{operation} {toolset} for {persona}",
        "surface": "telegram",
        "channel_id": "42",
        "correlation_id": correlation,
        "audit_path": ledger_path,
        **overrides,
    }
    if with_intent and outcome != grants.OUTCOME_INTENT:
        grants.append_audit_record(outcome=grants.OUTCOME_INTENT, **fields)
    grants.append_audit_record(outcome=outcome, **fields)
    return correlation


def test_active_grants_replays_grants_minus_revokes(ledger: Path):
    """Only rows where physical state MOVED count. Intent is not a grant."""
    assert grants.active_grants("sales", ledger) == ()

    # An intent whose outcome never landed must NOT resurrect a grant.
    _write_attempt(ledger, grants.OUTCOME_INTENT, KNOWN_TOOLSET)
    assert grants.active_grants("sales", ledger) == ()

    _write_attempt(ledger, grants.OUTCOME_GRANTED, KNOWN_TOOLSET)
    _write_attempt(ledger, grants.OUTCOME_GRANTED, OTHER_TOOLSET)
    _write_attempt(ledger, grants.OUTCOME_REFUSED, "never_granted")
    _write_attempt(
        ledger, grants.OUTCOME_GRANTED, "other_persona_toolset", persona="marketing"
    )
    assert grants.active_grants("sales", ledger) == (KNOWN_TOOLSET, OTHER_TOOLSET)

    _write_attempt(
        ledger,
        grants.OUTCOME_REVOKED,
        KNOWN_TOOLSET,
        operation=grants.OPERATION_REVOKE,
    )
    assert grants.active_grants("sales", ledger) == (OTHER_TOOLSET,)
    assert grants.ledger_scope("sales", ledger).tombstoned == (KNOWN_TOOLSET,)

    # Re-granting after a revoke brings it back, at the end, and clears the
    # tombstone — event order wins.
    _write_attempt(ledger, grants.OUTCOME_GRANTED, KNOWN_TOOLSET)
    assert grants.active_grants("sales", ledger) == (OTHER_TOOLSET, KNOWN_TOOLSET)
    assert grants.ledger_scope("sales", ledger).tombstoned == ()

    # Fail-open: a truncated line yields no phantom grants and no exception.
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write('{"persona_id": "sales", "outcome": "gran\n')
    assert grants.active_grants("sales", ledger) == (OTHER_TOOLSET, KNOWN_TOOLSET)


def test_replay_ignores_rows_the_executor_could_not_have_written(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """A forged effective row contributes nothing — to scope or to config.

    The replay used to accept any correctly-typed row, so the cheapest
    possible forgery — a short object with the right keys and blank
    provenance — granted (or tombstoned) real reach. Now a row must look
    like something the executor actually wrote: complete provenance AND a
    preceding intent for the same attempt.
    """
    forged = {
        "persona_id": "sales",
        "toolset": KNOWN_TOOLSET,
        "outcome": grants.OUTCOME_GRANTED,
    }
    ledger.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    assert grants.active_grants("sales", ledger) == ()

    # Full provenance but NO intent — still not a record of anything.
    ledger.write_text("", encoding="utf-8")
    _write_attempt(ledger, grants.OUTCOME_GRANTED, KNOWN_TOOLSET, with_intent=False)
    assert grants.active_grants("sales", ledger) == ()

    # Intent present but provenance blanked — the executor refuses to act
    # without the operator turn, so a row missing it cannot be its output.
    ledger.write_text("", encoding="utf-8")
    _write_attempt(ledger, grants.OUTCOME_GRANTED, KNOWN_TOOLSET, actor="")
    assert grants.active_grants("sales", ledger) == ()

    # A forged REVOKE cannot strip reach either, in either direction.
    ledger.write_text("", encoding="utf-8")
    _write_attempt(ledger, grants.OUTCOME_GRANTED, KNOWN_TOOLSET)
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + json.dumps(
            {
                "persona_id": "sales",
                "toolset": KNOWN_TOOLSET,
                "outcome": grants.OUTCOME_REVOKED,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    scope = grants.ledger_scope("sales", ledger)
    assert scope.active == (KNOWN_TOOLSET,)
    assert scope.tombstoned == ()

    # A correlated intent from a DIFFERENT attempt does not vouch for it.
    ledger.write_text("", encoding="utf-8")
    _write_attempt(ledger, grants.OUTCOME_INTENT, OTHER_TOOLSET, correlation="shared")
    _write_attempt(
        ledger,
        grants.OUTCOME_GRANTED,
        KNOWN_TOOLSET,
        with_intent=False,
        correlation="shared",
    )
    assert grants.active_grants("sales", ledger) == ()


def test_a_forged_grant_row_cannot_reach_physical_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The end of the chain: a forged row must not survive into config.yaml."""
    import yaml

    import config as config_module
    from personas.blueprints import ProvisionMode, build_builtin_blueprint
    from personas.provisioning import apply_provision, preview_provision

    paths = _provision_paths(tmp_path)
    monkeypatch.setenv("HOMIE_HOME", str(paths.homie_root))
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit", lambda *_a, **_k: None
    )

    def provision(mode, *, approved=False):
        blueprint = build_builtin_blueprint("ai-engineer", channel_id="123456789012345678")
        preview = preview_provision(blueprint, mode=mode, paths=paths)
        apply_provision(
            blueprint,
            mode=mode,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test-operator",
            paths=paths,
            reconcile_approved=approved,
        )
        return preview

    config_path = paths.profiles_root / "ai-engineer" / "config.yaml"
    provision(ProvisionMode.CREATE)
    before = config_path.read_bytes()

    # Default resolution, keyed to the TARGET persona (no audit_path= here:
    # these are the end-to-end tests that exercise real path resolution).
    ledger_path = grants.resolve_ledger_path(None, "ai-engineer")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        # Forged grant of a toolset the blueprint does not give...
        handle.write(
            json.dumps(
                {
                    "persona_id": "ai-engineer",
                    "toolset": KNOWN_TOOLSET,
                    "outcome": grants.OUTCOME_GRANTED,
                }
            ) + "\n"
        )
        # ...and a forged revoke of one it does.
        handle.write(
            json.dumps(
                {
                    "persona_id": "ai-engineer",
                    "toolset": "ai_engineering",
                    "outcome": grants.OUTCOME_REVOKED,
                }
            ) + "\n"
        )

    preview = provision(ProvisionMode.RECONCILE, approved=True)

    assert preview.preserved_grants == ()
    assert preview.revoked_grants == ()
    final = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert KNOWN_TOOLSET not in final["toolsets"], "a forged grant reached config"
    assert "ai_engineering" in final["toolsets"], "a forged revoke stripped config"
    assert config_path.read_bytes() == before


# ── Ledger contract ──────────────────────────────────────────────────────


def test_ledger_path_resolves_at_call_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Rule 1 — the default path is a None sentinel, not a def-time bind."""
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    assert grants.resolve_ledger_path() == tmp_path / "data" / grants.LEDGER_FILENAME

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "moved")
    assert grants.resolve_ledger_path() == tmp_path / "moved" / grants.LEDGER_FILENAME
    assert grants.resolve_ledger_path(tmp_path / "explicit.jsonl") == (
        tmp_path / "explicit.jsonl"
    )


def test_trigger_text_is_collapsed_capped_and_secret_scrubbed(
    profile: Path, ledger: Path
):
    secret = "sk-ant-api03-" + "A" * 40
    add_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text=f"give sales research\nkey {secret}\n" + "tail " * 200,
        surface="telegram",
        channel_id="42",
    )

    recorded = rows(ledger)[0]["trigger_text"]
    assert "\n" not in recorded  # one event stays one greppable row
    assert len(recorded) <= 400
    assert secret not in recorded
    assert recorded.startswith("give sales research key sk-ant")


def test_audit_write_failure_blocks_the_grant(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """BLOCKER fix (issue #426 round 2): the ledger row is a precondition.

    A grant that changes config.yaml must never exist without a matching
    audit row (epic metric 5 — "zero grants without a matching live
    operator turn" — must be greppable by construction). Previously the
    success audit was best-effort and swallowed a ledger failure, so the
    grant still landed with ``audit_id == ""``. Now the durable append runs
    BEFORE the config write, so a ledger failure aborts the whole operation
    and config.yaml is never touched.
    """

    def boom(**_kwargs):
        raise OSError("ledger volume is gone")

    monkeypatch.setattr(grants, "append_audit_record", boom)

    with pytest.raises(OSError):
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    assert not config_file(profile).exists()
    assert rows(ledger) == []


def test_failed_config_write_never_leaves_a_success_row(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """A grant that did not land must not be recorded as one.

    The success row used to be appended BEFORE the atomic replace, so a
    failed ``os.replace`` left a ``granted`` row for a grant that never
    happened — an append-only safety ledger labelling a precondition as the
    completed outcome. Now the pre-mutation row is ``intent`` and the
    ``granted`` row is written only after the replace returned.
    """
    from personas import services as persona_services

    write_config(profile, "toolsets:\n  - safe_core\n")
    before = config_file(profile).read_bytes()

    def boom(_path, _data):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(persona_services, "_minimal_yaml_write", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)

    # Physical state never moved.
    assert config_file(profile).read_bytes() == before

    recorded = rows(ledger)
    assert [row["outcome"] for row in recorded] == [
        grants.OUTCOME_INTENT,
        grants.OUTCOME_ERROR,
    ], "a failed write must read as attempted-and-failed, never as granted"
    assert grants.OUTCOME_GRANTED not in {row["outcome"] for row in recorded}
    # Correlated, so the pair is one legible event rather than two loose rows.
    assert recorded[0]["correlation_id"] == recorded[1]["correlation_id"] != ""
    assert recorded[1]["reason"] == grants.REASON_WRITE_FAILED

    # And the replay agrees with the file: nothing was granted.
    assert grants.active_grants("sales", ledger) == ()


def test_refusal_that_cannot_be_audited_raises_a_distinct_audit_failure(
    profile: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unaudited refusal must not come back as a polished refusal.

    The acceptance criterion is "unknown toolset -> refusal audited". The
    refusal row used to be best-effort, so with a dead ledger the caller
    still got ``ToolsetGrantRefusedError`` (reason ``unknown_toolset``,
    nearest matches and all) with no row on disk — indistinguishable from an
    audited refusal. Now that case is its own error type.
    """

    def boom(**_kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(grants, "append_audit_record", boom)

    with pytest.raises(grants.ToolsetGrantAuditError) as excinfo:
        add_persona_toolset("sales", "reserch_raed", audit_path=ledger, **OPERATOR)

    assert excinfo.value.reason == grants.REASON_UNKNOWN_TOOLSET
    assert "could not be audited" in str(excinfo.value)
    # Nothing landed here, unlike the torn-write case — a command-layer
    # caller branches on this bit before it ever says "refused" (#427 R2).
    assert excinfo.value.applied is False
    # Not the refusal type — a caller catching that would read this as an
    # audited "no". (ValueError is its base, so this also proves it escapes
    # the `except ValueError` handlers around persona config writes.)
    assert not isinstance(excinfo.value, grants.ToolsetGrantRefusedError)
    assert not isinstance(excinfo.value, ValueError)
    # Still nothing written, on either side.
    assert not config_file(profile).exists()
    assert rows(ledger) == []


def test_refusal_is_audited_and_still_refuses_on_a_healthy_ledger(
    profile: Path, ledger: Path
):
    """The strict path must not change the normal refusal contract."""
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_toolset("sales", "reserch_raed", audit_path=ledger, **OPERATOR)

    assert excinfo.value.reason == grants.REASON_UNKNOWN_TOOLSET
    assert KNOWN_TOOLSET in excinfo.value.suggestions
    (row,) = rows(ledger)
    assert row["outcome"] == grants.OUTCOME_REFUSED
    assert row["reason"] == grants.REASON_UNKNOWN_TOOLSET
    assert not config_file(profile).exists()


def test_every_mutating_row_carries_the_operator_turn_that_ordered_it(
    profile: Path, ledger: Path
):
    """Epic metric 5 by construction: no grant row without a live turn.

    The executor cannot be called without an actor and a trigger text (see
    the missing-operator-turn refusal), so every row that reports a real
    mutation carries both. Grepping the ledger for a mutating outcome with
    an empty actor is the negative case, and it is empty here by design.
    """
    add_persona_toolset("sales", KNOWN_TOOLSET, audit_path=ledger, **OPERATOR)
    remove_persona_toolset(
        "sales",
        KNOWN_TOOLSET,
        audit_path=ledger,
        actor="owner",
        actor_role="admin",
        trigger_text="take it back off",
        surface="telegram",
        channel_id="42",
    )
    with pytest.raises(grants.ToolsetGrantRefusedError):
        add_persona_toolset("sales", "zzqqxx", audit_path=ledger, **OPERATOR)

    mutating = {grants.OUTCOME_GRANTED, grants.OUTCOME_REVOKED}
    recorded = rows(ledger)
    assert {r["outcome"] for r in recorded} >= mutating
    for row in recorded:
        if row["outcome"] in mutating:
            assert row["actor"], row
            assert row["trigger_text"], row
            assert row["timestamp"].endswith("+00:00")


def test_gates_are_untouched_by_this_slice():
    """Reach is not action — a grant widens the tool surface, nothing else.

    The executor must not reference any per-tool default-deny gate. If a
    future edit reaches for one, this fails and the reviewer sees why.
    """
    source = (SCRIPTS_DIR / "personas" / "services.py").read_text(encoding="utf-8")
    executor = source[source.index("def _mutate_persona_toolset(") :]
    executor = executor[: executor.index("\ndef _read_persisted_port(")]

    for forbidden in (
        "require_integration_action",
        "capability_gateway",
        "IntegrationAction",
        "browser_write",
    ):
        assert forbidden not in executor
