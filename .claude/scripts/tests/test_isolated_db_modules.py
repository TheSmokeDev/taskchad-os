"""Proof tests for tests/module_isolation.py (issue #27).

One test per distinct code path in ``isolated_db_modules_ctx``:

P1  patched config flows into REAL module behavior (actual SQLite DDL),
    not just attribute theater — 512 != the real default 768, so this
    cannot pass unless the override flowed through the fresh module body.
P2  restore with a prior import: originals are never mutated during the
    ctx (the property importlib.reload can't give) and are byte-identical
    after exit.
P3  restore with NO prior import: names absent from sys.modules before
    the ctx are absent again after (the stashed-is-None branch).
P4  exception safety: a raise inside the ctx still restores sys.modules
    entries and config attributes (the finally branch).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tests.module_isolation import ISOLATED_MODULES, isolated_db_modules_ctx


def test_patched_config_flows_into_real_ddl(tmp_path: Path):
    """P1 — the override must reach actual backend behavior.

    get_actual_embedding_dim() reads the vector dim from sqlite_master DDL
    (physical schema, Rule 2). It can only return 512 if the patched
    EMBEDDING_DIMENSIONS flowed through db.py's module body into the
    CREATE VIRTUAL TABLE statement. 512 != the real config default (768),
    so a silently-broken fixture cannot pass this by coincidence.
    """
    db_path = tmp_path / "p1.db"
    with isolated_db_modules_ctx(
        DATABASE_PATH=db_path,
        DATABASE_URL="",  # force SQLite
        EMBEDDING_DIMENSIONS=512,
    ) as iso:
        db = iso.db.SQLiteMemoryDB(db_path=str(db_path))
        try:
            db.init_schema()
            assert db.get_actual_embedding_dim() == 512
        finally:
            db.close()


def test_restore_with_prior_import(tmp_path: Path):
    """P2 — originals never mutated; restored identically after exit."""
    import db as original_db
    import memory_index as original_mi

    orig_dim = original_db.EMBEDDING_DIMENSIONS
    orig_model = original_db.EMBEDDING_MODEL
    orig_path = original_db.DATABASE_PATH
    orig_mi_dim = original_mi.EMBEDDING_DIMENSIONS

    with isolated_db_modules_ctx(
        DATABASE_PATH=tmp_path / "p2.db",
        DATABASE_URL="",
        EMBEDDING_DIMENSIONS=512,
        EMBEDDING_MODEL="proof-model",
    ) as iso:
        # The fresh modules are NEW objects carrying the patched values...
        assert iso.db is not original_db
        assert iso.memory_index is not original_mi
        assert iso.db.EMBEDDING_DIMENSIONS == 512
        assert iso.db.EMBEDDING_MODEL == "proof-model"
        assert iso.memory_index.EMBEDDING_DIMENSIONS == 512
        # ...while the ORIGINAL module objects stay pristine — the property
        # importlib.reload (which mutates in place) cannot provide.
        assert original_db.EMBEDDING_DIMENSIONS == orig_dim
        assert original_db.EMBEDDING_MODEL == orig_model
        assert original_mi.EMBEDDING_DIMENSIONS == orig_mi_dim

    # After exit: sys.modules holds the exact original objects again.
    assert sys.modules["db"] is original_db
    assert sys.modules["memory_index"] is original_mi
    assert original_db.EMBEDDING_DIMENSIONS == orig_dim
    assert original_db.EMBEDDING_MODEL == orig_model
    assert original_db.DATABASE_PATH == orig_path

    # A fresh `import` after exit resolves to the restored originals.
    import memory_index as mi_after
    assert mi_after is original_mi
    assert mi_after.EMBEDDING_DIMENSIONS == orig_mi_dim


def test_restore_with_no_prior_import():
    """P3 — names absent before the ctx must be absent after it."""
    # Stash + remove any current entries; this test restores them itself.
    stash = {name: sys.modules.pop(name, None) for name in ISOLATED_MODULES}
    try:
        with isolated_db_modules_ctx(DATABASE_URL="") as iso:
            assert sys.modules["db"] is iso.db
            assert sys.modules["memory_index"] is iso.memory_index
        assert "db" not in sys.modules
        assert "memory_index" not in sys.modules
    finally:
        for name, mod in stash.items():
            if mod is not None:
                sys.modules[name] = mod
            else:
                sys.modules.pop(name, None)


def test_exception_inside_ctx_restores_state(tmp_path: Path):
    """P4 — a raise inside the ctx still restores modules + config."""
    import config as config_mod
    import db as original_db
    import memory_index as original_mi

    orig_config_dim = config_mod.EMBEDDING_DIMENSIONS
    orig_config_path = config_mod.DATABASE_PATH

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with isolated_db_modules_ctx(
            DATABASE_PATH=tmp_path / "p4.db",
            DATABASE_URL="",
            EMBEDDING_DIMENSIONS=512,
        ):
            # Patch is live inside the ctx...
            assert config_mod.EMBEDDING_DIMENSIONS == 512
            raise _Boom("boom")

    # ...and fully unwound despite the exception.
    assert sys.modules["db"] is original_db
    assert sys.modules["memory_index"] is original_mi
    assert config_mod.EMBEDDING_DIMENSIONS == orig_config_dim
    assert config_mod.DATABASE_PATH == orig_config_path
