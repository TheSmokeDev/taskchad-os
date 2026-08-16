"""Issue #466 — apartments completion: main reads every persona vault, read-only.

Every named persona already owns an isolated vault-tree + recall index
(`~/.homie/profiles/<id>/memory` + `<id>/data/memory.db`). These tests lock
the missing direction that #466 adds — the main homie addressing each
apartment by its PLAIN profile id on the same shelf as `thehomie` /
`coding-vault`, plus the `all` fan-out — while proving the read path is
ONE-WAY: not a single byte lands in a persona's tree from the main side.

The load-bearing test asserts on the FILESYSTEM (bytes + mtime + sidecars +
non-creation), never on "we didn't call a write function".
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Unique tokens that cannot appear in the real main vault — keeps every
# cross-vault assertion deterministic (same convention as
# test_persona_recall_isolation.py).
_FACT_A = "The persona crypto codeword is Zqxwvblorptium held at block 840000."
_TOKEN_A = "Zqxwvblorptium"
_SHARED_TOKEN = "Xqvortelmiraz"


@pytest.fixture()
def profiles_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake ~/.homie root with an empty profiles/ dir; active profile = default.

    ``get_default_homie_root`` is monkeypatched at its defining module so both
    the enumerator (personas.core) and the path resolver (get_persona_paths)
    see the same fake root — HOMIE_HOME stays unset so the process reads as
    the default (main) profile.
    """
    import personas.core as personas_core

    monkeypatch.delenv("HOMIE_HOME", raising=False)
    root = tmp_path / "homie-root"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setattr(personas_core, "get_default_homie_root", lambda: root)
    return root / "profiles"


def _make_profile_vault(root: Path, fact: str) -> Path:
    """Create a profile-shaped vault (<root>/memory + <root>/data), write a
    fact note into memory/, and index it into the resolver-chosen DB — the
    persona's OWN write path (same helper pattern as
    test_persona_recall_isolation.py).
    """
    import config as _cfg
    from db import get_memory_db
    from memory_index import index_file

    memory_dir = root / "memory"
    data_dir = root / "data"
    memory_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    note = memory_dir / "MEMORY.md"
    note.write_text(f"# Codeword\n\n{fact}\n", encoding="utf-8")

    db_path = _cfg.resolve_db_path(memory_dir)
    db = get_memory_db(db_path=db_path)
    db.init_schema()
    index_file(db, note, memory_dir, generate_embeddings=False)
    db.close()
    return memory_dir


# ---------------------------------------------------------------------------
# 1. Registry — live persona ids on the shelf, physical, uncached.
# ---------------------------------------------------------------------------


def test_persona_vault_registered_live_and_uncached(profiles_root: Path) -> None:
    import config as _cfg

    (profiles_root / "sales" / "memory").mkdir(parents=True)
    (profiles_root / "sales" / "data").mkdir(parents=True)

    assert "sales" in _cfg.list_vault_names()
    resolved_mem, resolved_db = _cfg.resolve_vault("sales")
    assert resolved_mem == profiles_root / "sales" / "memory"
    assert resolved_db == profiles_root / "sales" / "data" / "memory.db"
    assert _cfg.is_readonly_vault("sales") is True
    assert _cfg.is_readonly_vault(resolved_mem) is True

    # Half-provisioned profile (no data/) drops out — the deleted-profile case.
    (profiles_root / "geo" / "memory").mkdir(parents=True)
    assert "geo" not in _cfg.list_vault_names()

    # Deleted profile drops out on the very next call — proves no cache.
    shutil.rmtree(profiles_root / "sales")
    assert "sales" not in _cfg.list_vault_names()
    assert _cfg.resolve_vault("sales")[0] is None
    assert _cfg.is_readonly_vault("sales") is False


def test_persona_id_colliding_with_static_vault_is_shadowed(profiles_root: Path) -> None:
    import config as _cfg

    (profiles_root / "coding-vault" / "memory").mkdir(parents=True)
    (profiles_root / "coding-vault" / "data").mkdir(parents=True)

    resolved_mem, resolved_db = _cfg.resolve_vault("coding-vault")
    # The static registry entry wins — configured or not — never the persona's.
    assert resolved_mem == _cfg._VAULT_MEMORY_DIRS["coding-vault"]
    assert resolved_db == _cfg._VAULT_DB_PATHS["coding-vault"]
    assert resolved_mem != profiles_root / "coding-vault" / "memory"
    assert resolved_db != profiles_root / "coding-vault" / "data" / "memory.db"
    assert _cfg.is_readonly_vault("coding-vault") is False
    assert _cfg.list_vault_names().count("coding-vault") == 1


# ---------------------------------------------------------------------------
# 2. Main reads A — by plain name and via the `all` fan-out.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_reads_persona_vault_by_name(
    profiles_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recall_service import SearchMode, recall

    import config as _cfg

    monkeypatch.setattr(_cfg, "RECALL_ENABLED", True, raising=False)
    _make_profile_vault(profiles_root / "crypto", _FACT_A)

    resolved_mem, _db = _cfg.resolve_vault("crypto")
    resp = await recall(
        _TOKEN_A,
        memory_dir=Path(resolved_mem),
        search_mode=SearchMode.KEYWORD,
        max_results=5,
    )
    assert _TOKEN_A in resp.formatted_text


@pytest.mark.asyncio
async def test_fanout_merges_vaults_with_attribution(
    profiles_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recall_service import recall_all

    import config as _cfg

    monkeypatch.setattr(_cfg, "RECALL_ENABLED", True, raising=False)

    # Hermetic static registry: the sweep's "thehomie" is a tmp vault, not
    # the real one (functions read these dicts as module globals at call time).
    main_root = tmp_path / "main-vault"
    _make_profile_vault(main_root, f"The main estate codeword is {_SHARED_TOKEN} for Q4.")
    monkeypatch.setattr(
        _cfg,
        "_VAULT_MEMORY_DIRS",
        {"thehomie": main_root / "memory", "coding-vault": None},
    )
    monkeypatch.setattr(
        _cfg,
        "_VAULT_DB_PATHS",
        {
            "thehomie": main_root / "data" / "memory.db",
            "coding-vault": tmp_path / "memory.coding-vault.db",
        },
    )

    _make_profile_vault(
        profiles_root / "crypto", f"The crypto persona codeword is {_SHARED_TOKEN} at block 9."
    )
    _make_profile_vault(
        profiles_root / "sales", f"The sales persona codeword is {_SHARED_TOKEN} in the pipeline."
    )

    resp = await recall_all(_SHARED_TOKEN, max_results=5)

    # Every result carries its vault tag; >=2 vaults merged into ONE response.
    assert all(getattr(r, "vault", "") for r in resp.results)
    vault_tags = {r.vault for r in resp.results}
    assert {"thehomie", "crypto", "sales"} <= vault_tags
    assert "[vault:crypto]" in resp.formatted_text
    assert "[vault:sales]" in resp.formatted_text


# ---------------------------------------------------------------------------
# 3. THE LOAD-BEARING TEST — the read path never writes a byte.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_side_recall_leaves_persona_db_untouched(
    profiles_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recall_service import SearchMode, recall

    import config as _cfg

    monkeypatch.setattr(_cfg, "RECALL_ENABLED", True, raising=False)
    a_mem = _make_profile_vault(profiles_root / "crypto", _FACT_A)
    db_path = profiles_root / "crypto" / "data" / "memory.db"
    assert db_path.exists()
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    resp = await recall(
        _TOKEN_A, memory_dir=a_mem, search_mode=SearchMode.KEYWORD, max_results=5
    )
    assert _TOKEN_A in resp.formatted_text, "read-only recall must still return the fact"

    # Filesystem truth, not call-graph truth: bytes AND mtime unchanged, and
    # no WAL sidecars appeared next to the persona's DB.
    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime_ns == before_mtime
    assert not (db_path.parent / "memory.db-wal").exists()
    assert not (db_path.parent / "memory.db-shm").exists()


@pytest.mark.asyncio
async def test_unbuilt_persona_db_returns_empty_and_is_not_created(
    profiles_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recall_service import SearchMode, recall

    import config as _cfg
    from memory_search import search_keyword

    monkeypatch.setattr(_cfg, "RECALL_ENABLED", True, raising=False)
    mem = profiles_root / "sales" / "memory"
    data = profiles_root / "sales" / "data"
    mem.mkdir(parents=True)
    data.mkdir(parents=True)

    assert search_keyword("anything", limit=5, memory_dir=mem) == []
    resp = await recall(
        "anything at all here", memory_dir=mem, search_mode=SearchMode.KEYWORD, max_results=5
    )
    assert resp.results == []
    # Rule 2: the persona index that was never built must NOT come into being.
    assert not (data / "memory.db").exists()


# ---------------------------------------------------------------------------
# 4. DATABASE_URL — a persona read never touches the shared Postgres.
# ---------------------------------------------------------------------------


def test_database_url_persona_read_stays_sqlite(
    profiles_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import db as db_mod
    from memory_search import search_keyword

    a_mem = _make_profile_vault(profiles_root / "crypto", _FACT_A)
    monkeypatch.setattr(
        db_mod, "DATABASE_URL", "postgresql://nope:nope@127.0.0.1:9/nope"
    )

    mdb = db_mod.get_memory_db(
        db_path=profiles_root / "crypto" / "data" / "memory.db", read_only=True
    )
    assert isinstance(mdb, db_mod.SQLiteMemoryDB)
    mdb.close()

    # End-to-end: the search still reads the per-persona SQLite file.
    hits = search_keyword(_TOKEN_A, limit=5, memory_dir=a_mem)
    assert any(_TOKEN_A in r.text for r in hits)


# ---------------------------------------------------------------------------
# 5. The widened resolver must not hand out a WRITE. Indexing is the one
#    write path that reaches a persona vault by name for free now that
#    resolve_vault answers for persona ids.
# ---------------------------------------------------------------------------


def test_main_process_refuses_to_index_a_persona_vault(
    profiles_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import memory_index

    (profiles_root / "sales" / "memory").mkdir(parents=True)
    (profiles_root / "sales" / "data").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["memory_index.py", "--vault", "sales"])

    with pytest.raises(SystemExit) as exc:
        memory_index.main()

    assert exc.value.code == 1
    assert "read-only" in capsys.readouterr().out
    # Refused BEFORE any index came into being in the persona's tree.
    assert not (profiles_root / "sales" / "data" / "memory.db").exists()


# ---------------------------------------------------------------------------
# 6. Default recall unchanged — the engine stays pinned to the main vault.
# ---------------------------------------------------------------------------


def test_engine_default_recall_pinned_to_main_vault() -> None:
    engine_src = (_CHAT_DIR / "engine.py").read_text(encoding="utf-8")
    call = engine_src.split("recall_response = await recall_memory_service(", 1)[1]
    call = call.split(")", 1)[0]
    assert "memory_dir=MEMORY_DIR" in call
    assert "vault" not in call
    assert "recall_all" not in engine_src
