"""The CHECKOUT's operator sinks stay clean while pytest runs real creations.

Issue #422 round 4. The born-learning receipt resolves its target from
``config.DATA_DIR`` and the kill-switch refusal row from
``config.DASHBOARD_DB_PATH`` — both process-AMBIENT, neither derived from
``HOMIE_HOME``. Fixtures that redirected only the profile root therefore
isolated everything the tests ASSERTED on while the receipts landed in the
checkout's operational state: 166 synthetic rows had accumulated in
``.claude/data/persona_learning_audit.jsonl`` (60 cofounder, 55
upwork-operator, 33 repo-scout, 16 ops, 2 api-surface) and 80
``killswitch_refusal`` rows in ``.claude/data/dashboard.db``. A green suite
was corrupting the ledger that rollout verification reads.

This is the #426 target-vs-ambient lesson in fixture form: isolating the
TARGET (the profile tree) proves nothing about an AMBIENT sink.

Two rules keep these tests non-vacuous:

1. Each test requests ONLY the fixture whose sink composition it pins, and
   resolves the redirected location from ``config`` at call time. Requesting
   ``isolated_operator_sinks`` alongside would redirect the sinks by itself
   and the test would pass even with the composition removed.
2. Each test asserts a receipt WAS written somewhere. "The checkout did not
   change" is worthless unless something actually tried to write.

The checkout paths come from THIS FILE's location, never from ``config`` —
those globals are exactly what the fixtures patch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# tests/ -> scripts/ -> .claude/
_CLAUDE_DIR = Path(__file__).resolve().parent.parent.parent
CHECKOUT_LEARNING_LEDGER = _CLAUDE_DIR / "data" / "persona_learning_audit.jsonl"
CHECKOUT_DASHBOARD_DB = _CLAUDE_DIR / "data" / "dashboard.db"


def _ledger_bytes() -> bytes | None:
    """Raw bytes of the checkout ledger, or None when it does not exist."""
    if not CHECKOUT_LEARNING_LEDGER.is_file():
        return None
    return CHECKOUT_LEARNING_LEDGER.read_bytes()


def _checkout_audit_rows() -> int:
    """Rows in the checkout audit_log, read WITHOUT touching the file.

    Opened read-only via a URI so the probe itself cannot create the DB,
    apply schema, or rewrite a page — otherwise the measurement would be the
    mutation it is looking for.
    """
    if not CHECKOUT_DASHBOARD_DB.is_file():
        return 0
    conn = sqlite3.connect(f"file:{CHECKOUT_DASHBOARD_DB.as_posix()}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _redirected_learning_rows() -> list[str]:
    """Learning rows wherever ``config.DATA_DIR`` currently points.

    Resolved at CALL time, exactly like the writer — so this reads the
    fixture's redirect rather than assuming one.
    """
    import config as _config

    ledger = Path(_config.DATA_DIR) / "persona_learning_audit.jsonl"
    if not ledger.is_file():
        return []
    return [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def _redirected_refusal_rows() -> int:
    import config as _config

    db = Path(_config.DASHBOARD_DB_PATH)
    if not db.is_file():
        return 0
    conn = sqlite3.connect(db)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'killswitch_refusal'"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def test_seeder_creation_receipt_never_reaches_the_checkout_ledger(
    seeder_homie_root: Path,
) -> None:
    """A real cofounder seed lands its learning receipt in tmp, not the checkout.

    Pins ``seeder_homie_root``'s sink composition — the fixture the
    cofounder / repo-scout / upwork-operator suites all delegate to.
    """
    from cofounder import persona as persona_mod

    before = _ledger_bytes()

    result = persona_mod.seed_cofounder_persona()
    assert result.outcome == persona_mod.OUTCOME_CREATED
    assert result.profile_created is True

    rows = _redirected_learning_rows()
    assert any('"cofounder"' in row for row in rows), (
        "the seed wrote no learning receipt anywhere — an unchanged checkout "
        "would prove nothing"
    )
    assert _ledger_bytes() == before


def test_killswitch_refusal_row_never_reaches_the_checkout_audit_db(
    seeder_homie_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second ambient sink: a refusal row must not reach the checkout DB.

    The seeders create nothing in their refusal cases, so the profile-tree
    isolation they already had was never in question — which is exactly why
    this sink went unnoticed.
    """
    from cofounder import persona as persona_mod

    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    before_rows = _checkout_audit_rows()

    result = persona_mod.seed_cofounder_persona()
    assert result.outcome == persona_mod.OUTCOME_REFUSED

    assert _redirected_refusal_rows() >= 1, "the refusal wrote no audit row anywhere"
    assert _checkout_audit_rows() == before_rows


def test_clone_all_creation_receipt_never_reaches_the_checkout_ledger(
    tmp_homie_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins ``tmp_homie_home``'s sink composition.

    A resolver fixture — but real creations run on top of it
    (``test_create_clone_all_from_named_profile_still_uses_copytree`` clones
    ``sales`` into ``ops``), and that was still appending one ``ops`` row to
    the checkout ledger on every sweep after the four named fixtures were
    fixed. Found by a per-test detector over the whole persona suite.
    """
    from personas.lifecycle import create_profile

    monkeypatch.setenv("HOMIE_HOME", str(tmp_homie_home.parent.parent))
    before = _ledger_bytes()

    info = create_profile("ops", clone_all=True, clone_from="sales", no_alias=True)
    assert info.path.exists()

    rows = _redirected_learning_rows()
    assert any('"ops"' in row for row in rows), (
        "the clone-all create wrote no learning receipt anywhere"
    )
    assert _ledger_bytes() == before


def test_atomic_creation_receipt_never_reaches_the_checkout_ledger(
    isolated_operator_sinks: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guarantee on the atomic blueprint door (CLI + dashboard).

    This one requests ``isolated_operator_sinks`` directly because that IS
    the fixture under test here — the dashboard/creation-surface fixtures
    compose it rather than adding a root of their own.
    """
    import json

    import yaml

    from personas.creation import PersonaCreationSpec, apply_persona_creation
    from personas.provisioning import ProvisionPaths

    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "env_groups": {"runtime_core": [], "vault_memory": []},
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
    master_env.write_text("", encoding="utf-8")
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({"guild_id": "t", "channels": {}}) + "\n", encoding="utf-8")
    paths = ProvisionPaths(
        homie_root=tmp_path / "homie",
        bindings_file=bindings,
        capability_matrix_file=matrix,
        master_env_file=master_env,
    )
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )

    before = _ledger_bytes()

    receipt = apply_persona_creation(
        PersonaCreationSpec(persona_id="sink-probe"),
        actor="test-operator",
        paths=paths,
    )
    assert receipt.outcome == "created"

    rows = _redirected_learning_rows()
    assert any('"sink-probe"' in row for row in rows), (
        "the atomic create wrote no learning receipt anywhere"
    )
    assert _ledger_bytes() == before
