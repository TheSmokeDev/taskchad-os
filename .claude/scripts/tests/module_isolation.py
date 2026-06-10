"""Module-isolation helper for tests that need importlib-reload semantics.

Issue #27 — test(infra): isolation fixture for the ``importlib.reload()``
pattern in ``test_dim_drift_guard.py``.

WHY RELOAD-LIKE ISOLATION IS NEEDED AT ALL
------------------------------------------
``db.py`` and ``memory_index.py`` copy config values into their module
namespaces at import time (``from config import EMBEDDING_DIMENSIONS, ...``).
Monkeypatching ``config.X`` after import never reaches those copies — the
only way a patched config value can flow into db/memory_index behavior is to
re-execute their module bodies under the patched config.

WHY POP + FRESH-IMPORT INSTEAD OF ``importlib.reload()``
--------------------------------------------------------
``importlib.reload()`` mutates the existing module object in place, so there
is no pristine original left to restore. Worse, ``monkeypatch.setattr()``
calls made AFTER a reload record the post-reload (patched) values as
"originals", so monkeypatch teardown "restores" the leaked values for the
remainder of the pytest session — the exact bug #27 describes (e.g.
``test_postgres_dim_migration.py`` reading a stale ``db`` module after
``test_dim_drift_guard.py`` ran).

Stash-pop-fresh-import keeps the original module objects untouched:

1. stash the current ``sys.modules`` entries for the target modules
2. apply config overrides via a private ``pytest.MonkeyPatch`` instance
3. pop the targets from ``sys.modules`` and fresh-import them in dependency
   order (``db`` first; ``memory_index``'s ``from db import ...`` then
   binds the fresh ``db``)
4. yield the fresh modules
5. finally: put the stashed originals back (or remove the fresh entries if
   the names were absent before), then undo the config patches

Every earlier ``from db import X`` reference stays pristine, restore is an
O(1) ``sys.modules`` dict assignment, and the fresh import exercises the
identical import-time config-binding path a real production import uses.

Lives in a helper module (not conftest.py) so proof tests can import it
directly: ``from tests.module_isolation import isolated_db_modules_ctx``.
Test-fixture consumers use the ``isolated_db_modules`` factory fixture in
``conftest.py`` instead.

Scope note: this helper is deliberately limited to ``db`` + ``memory_index``.
Other test files reload different modules (capabilities, orchestration api,
dashboard_api, ...) with different invariants — do not generalize without a
per-module design pass.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

#: Modules that copy config values at import time, in dependency order —
#: ``db`` must be fresh-imported before ``memory_index`` so the latter's
#: ``from db import MemoryDB, get_memory_db`` binds the fresh db module.
ISOLATED_MODULES = ("db", "memory_index")


@contextmanager
def isolated_db_modules_ctx(**config_overrides):
    """Fresh-import ``db`` + ``memory_index`` under patched config values.

    Yields a ``SimpleNamespace`` with ``config``, ``db``, and
    ``memory_index`` attributes (the patched config module object and the
    two freshly imported modules).

    On exit — including when the body raises — restores the original
    ``sys.modules`` entries (or removes the fresh ones if the names were
    absent before entry) and undoes the config patches. Restore covers
    ``sys.modules`` and config attributes ONLY: the fresh import of
    ``memory_index`` re-executes the personas boot shim
    (``apply_persona_override()``), whose ``os.environ`` side effects
    (HOMIE_HOME normalization, HOMIE_NAME setdefault) are not rolled back.

    Uses its own ``pytest.MonkeyPatch`` instance instead of the injected
    ``monkeypatch`` fixture so the context manager is directly testable
    outside fixture injection.
    """
    import config as config_mod

    mp = pytest.MonkeyPatch()
    # Stash (present, value) pairs — sys.modules.get() alone would conflate
    # an explicit ``None`` mapping (the import-blocking convention) with an
    # absent name and drop the sentinel on restore.
    stashed = {
        name: (name in sys.modules, sys.modules.get(name))
        for name in ISOLATED_MODULES
    }
    try:
        for key, value in config_overrides.items():
            mp.setattr(config_mod, key, value)
        for name in ISOLATED_MODULES:
            sys.modules.pop(name, None)
        fresh = {name: importlib.import_module(name) for name in ISOLATED_MODULES}
        yield SimpleNamespace(
            config=config_mod,
            db=fresh["db"],
            memory_index=fresh["memory_index"],
        )
    finally:
        for name in ISOLATED_MODULES:
            was_present, original = stashed[name]
            if was_present:
                sys.modules[name] = original
            else:
                sys.modules.pop(name, None)
        mp.undo()
