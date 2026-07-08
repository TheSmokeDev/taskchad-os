"""Issue #110 — per-persona semantic recall must stay vault-isolated.

Regression lock for the cross-pollution trap the handoff nearly shipped:
before the fix, ``config.resolve_db_path(<profile>/memory)`` slugged every
persona's ``memory`` dir to the SAME ``DATA_DIR/memory.memory.db`` in the MAIN
vault (name collision + wrong root), so a Discord persona recall would read the
shared main index instead of ``~/.homie/profiles/<name>/data/memory.db``.

These tests build two profile-shaped vault roots (``<root>/memory`` +
``<root>/data``), index a UNIQUE fact into EACH, and assert at the DB/result
level (never a status code) that:

1. ``resolve_db_path`` routes each profile memory dir to its OWN co-located
   ``data/memory.db`` (per-persona-unique, never the main DB).
2. A fact indexed only in A is retrieved for A and NOT for B (raw
   ``search_keyword`` — the real per-vault DB routing).
3. The same isolation holds through ``recall_service.recall`` in KEYWORD mode —
   the fallback path that previously ignored ``memory_dir`` entirely and always
   read the main DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Unique tokens that cannot appear in the real main vault — keeps the
# "not recalled by main" assertion deterministic.
_FACT_A = "The persona crypto codeword is Zqxwvblorptium held at block 840000."
_TOKEN_A = "Zqxwvblorptium"
_FACT_B = "The persona sales codeword is Vurmplokthrexia for the Q3 pipeline."
_TOKEN_B = "Vurmplokthrexia"


def _make_profile_vault(root: Path, fact: str) -> Path:
    """Create a profile-shaped vault (<root>/memory + <root>/data), write a
    fact note into memory/, and index it into the resolver-chosen DB.

    Returns the memory dir (what the persona runtime passes to recall()).
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
# 1. Resolver routing — each profile memory dir → its OWN co-located DB.
# ---------------------------------------------------------------------------


def test_resolve_db_path_routes_profile_to_own_data_dir(tmp_path: Path) -> None:
    import config as _cfg

    a_mem = tmp_path / "crypto" / "memory"
    b_mem = tmp_path / "sales" / "memory"
    (a_mem.parent / "data").mkdir(parents=True)
    (b_mem.parent / "data").mkdir(parents=True)
    a_mem.mkdir()
    b_mem.mkdir()

    a_db = _cfg.resolve_db_path(a_mem)
    b_db = _cfg.resolve_db_path(b_mem)

    assert a_db == a_mem.parent / "data" / "memory.db"
    assert b_db == b_mem.parent / "data" / "memory.db"
    # Per-persona-unique, and NEVER the main vault DB (the collision bug).
    assert a_db != b_db
    assert Path(a_db).resolve() != Path(_cfg.DATABASE_PATH).resolve()
    assert Path(b_db).resolve() != Path(_cfg.DATABASE_PATH).resolve()


def test_resolve_db_path_falls_back_when_no_sibling_data(tmp_path: Path) -> None:
    """A dir NOT shaped like a vault root (no sibling data/) keeps the legacy
    slug DB — the profile redirect must not hijack arbitrary memory_dirs."""
    import config as _cfg

    lone = tmp_path / "memory"  # named "memory" but NO sibling data/ dir
    lone.mkdir()
    db = _cfg.resolve_db_path(lone)
    assert db == _cfg.DATA_DIR / "memory.memory.db"


# ---------------------------------------------------------------------------
# 2. DB-level isolation — raw search_keyword through the real vault DB.
# ---------------------------------------------------------------------------


def test_fact_indexed_in_A_not_visible_to_B_or_main(tmp_path: Path) -> None:
    import config as _cfg
    from memory_search import search_keyword

    a_mem = _make_profile_vault(tmp_path / "crypto", _FACT_A)
    b_mem = _make_profile_vault(tmp_path / "sales", _FACT_B)

    a_hits = search_keyword(_TOKEN_A, limit=5, memory_dir=a_mem)
    assert any(_TOKEN_A in r.text for r in a_hits), "A must recall its own fact"

    # B's vault has a DIFFERENT fact — it must not surface A's token.
    b_hits = search_keyword(_TOKEN_A, limit=5, memory_dir=b_mem)
    assert not any(_TOKEN_A in r.text for r in b_hits), (
        "B must NOT recall A's fact (cross-vault leak)"
    )

    # The main vault (default DB) must not know A's synthetic token either.
    main_hits = search_keyword(_TOKEN_A, limit=5, memory_dir=_cfg.MEMORY_DIR)
    assert not any(_TOKEN_A in r.text for r in main_hits), (
        "Main index must NOT recall a persona-only fact"
    )


# ---------------------------------------------------------------------------
# 3. recall_service.recall (KEYWORD mode) — the fallback that previously
#    ignored memory_dir and always read the main DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_keyword_mode_is_vault_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config as _cfg
    from recall_service import SearchMode, recall

    monkeypatch.setattr(_cfg, "RECALL_ENABLED", True, raising=False)
    monkeypatch.setattr(_cfg, "RECALL_MIN_SCORE", 0.0, raising=False)

    a_mem = _make_profile_vault(tmp_path / "crypto", _FACT_A)
    b_mem = _make_profile_vault(tmp_path / "sales", _FACT_B)

    a_resp = await recall(
        _TOKEN_A, memory_dir=a_mem, search_mode=SearchMode.KEYWORD, max_results=5
    )
    assert _TOKEN_A in a_resp.formatted_text, "A recall must return A's fact"

    b_resp = await recall(
        _TOKEN_A, memory_dir=b_mem, search_mode=SearchMode.KEYWORD, max_results=5
    )
    assert _TOKEN_A not in b_resp.formatted_text, (
        "B recall must NOT return A's fact — KEYWORD fallback must honor memory_dir"
    )

    main_resp = await recall(
        _TOKEN_A, memory_dir=_cfg.MEMORY_DIR, search_mode=SearchMode.KEYWORD, max_results=5
    )
    assert _TOKEN_A not in main_resp.formatted_text, (
        "Main recall must NOT return a persona-only fact"
    )
