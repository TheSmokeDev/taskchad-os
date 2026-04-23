"""
Memory Indexer for The Homie search.

Chunks markdown files into ~400-token overlapping segments and indexes them
using the database abstraction layer (SQLite or Postgres).

Usage:
    uv run python memory_index.py              # Index changed files
    uv run python memory_index.py --rebuild    # Force full reindex
    uv run python memory_index.py --stats      # Show index statistics
    uv run python memory_index.py --no-embeddings  # Skip vector embeddings
    uv run python memory_index.py --test       # Dry run (print what would be indexed)
"""

from __future__ import annotations

import argparse
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    MEMORY_DIR,
    SEARCH_CHUNK_MAX_TOKENS,
    SEARCH_CHUNK_OVERLAP_TOKENS,
    ensure_directories,
)
from db import MemoryDB, get_memory_db

SCHEMA_VERSION = 1


@dataclass
class ChunkRecord:
    """A chunk of a markdown file ready for indexing."""

    file_path: str
    start_line: int
    end_line: int
    section_title: str
    content: str
    content_hash: str


def chunk_markdown(
    content: str,
    max_tokens: int = SEARCH_CHUNK_MAX_TOKENS,
    overlap_tokens: int = SEARCH_CHUNK_OVERLAP_TOKENS,
) -> list[ChunkRecord]:
    """
    Split markdown content into overlapping chunks of ~max_tokens.

    Uses ~4 chars/token heuristic. Tracks section titles (# and ## headings)
    and line numbers for each chunk.
    """
    chars_per_token = 4
    max_chars = max_tokens * chars_per_token
    overlap_chars = overlap_tokens * chars_per_token

    lines = content.split("\n")
    if not lines:
        return []

    chunks: list[ChunkRecord] = []
    current_section = ""
    current_chunk_lines: list[str] = []
    current_chunk_start = 1  # 1-indexed line numbers
    current_char_count = 0

    for i, line in enumerate(lines):
        line_num = i + 1  # 1-indexed

        # Track section titles
        stripped = line.strip()
        if stripped.startswith("# ") or stripped.startswith("## "):
            current_section = stripped.lstrip("#").strip()

        current_chunk_lines.append(line)
        current_char_count += len(line) + 1  # +1 for newline

        # Check if chunk is full
        if current_char_count >= max_chars:
            chunk_text = "\n".join(current_chunk_lines)
            if chunk_text.strip():
                chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:16]
                chunks.append(
                    ChunkRecord(
                        file_path="",  # Set by caller
                        start_line=current_chunk_start,
                        end_line=line_num,
                        section_title=current_section,
                        content=chunk_text,
                        content_hash=chunk_hash,
                    )
                )

            # Overlap: keep the last overlap_chars worth of lines
            overlap_lines: list[str] = []
            overlap_count = 0
            for prev_line in reversed(current_chunk_lines):
                overlap_count += len(prev_line) + 1
                overlap_lines.insert(0, prev_line)
                if overlap_count >= overlap_chars:
                    break

            current_chunk_lines = overlap_lines
            current_chunk_start = line_num - len(overlap_lines) + 1
            current_char_count = sum(len(ln) + 1 for ln in current_chunk_lines)

    # Final chunk (remaining lines)
    if current_chunk_lines:
        chunk_text = "\n".join(current_chunk_lines)
        if chunk_text.strip():
            chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:16]
            chunks.append(
                ChunkRecord(
                    file_path="",
                    start_line=current_chunk_start,
                    end_line=len(lines),
                    section_title=current_section,
                    content=chunk_text,
                    content_hash=chunk_hash,
                )
            )

    return chunks


def list_memory_files(memory_dir: Path) -> list[Path]:
    """Find all .md files recursively in memory directory."""
    return sorted(memory_dir.rglob("*.md"))


def _delete_file_chunks(db: MemoryDB, rel_path: str) -> None:
    """Delete all chunks and vectors for a file."""
    chunk_ids = db.get_chunk_ids_for_file(rel_path)
    db.delete_vectors_for_chunk_ids(chunk_ids)
    db.delete_chunks_for_file(rel_path)


def index_file(
    db: MemoryDB,
    file_path: Path,
    memory_dir: Path,
    generate_embeddings: bool = True,
) -> int:
    """
    Index a single file: chunk, embed, store.

    Returns number of chunks created.
    """
    rel_path = file_path.relative_to(memory_dir).as_posix()
    content = file_path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
    stat = file_path.stat()
    now_epoch = int(time.time())

    # Ensure file row exists (FK required by Postgres before inserting chunks)
    db.upsert_file(rel_path, content_hash, stat.st_mtime_ns, stat.st_size, now_epoch)

    # Delete old chunks
    _delete_file_chunks(db, rel_path)

    # Chunk the file
    chunks = chunk_markdown(content)
    if not chunks:
        db.commit()
        return 0

    # Generate embeddings for all chunks at once
    embeddings: list[Any] | None = None
    if generate_embeddings:
        from embeddings import embed_batch

        texts = [c.content for c in chunks]
        embeddings = embed_batch(texts)

    # Insert chunks and vectors
    for i, chunk in enumerate(chunks):
        chunk_id = db.insert_chunk(
            file_path=rel_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            section_title=chunk.section_title,
            content=chunk.content,
            content_hash=chunk.content_hash,
            created_at_epoch=now_epoch,
        )
        if embeddings is not None:
            db.insert_vector(chunk_id, embeddings[i])

    db.commit()
    return len(chunks)


def remove_stale_files(db: MemoryDB, current_paths: set[str]) -> int:
    """Remove index entries for files that no longer exist."""
    indexed = db.get_all_file_paths()
    removed = 0

    for path in indexed:
        if path not in current_paths:
            _delete_file_chunks(db, path)
            db.delete_file(path)
            removed += 1

    if removed > 0:
        db.commit()
    return removed


def sync_index(
    memory_dir: Path = MEMORY_DIR,
    generate_embeddings: bool = True,
    force_rebuild: bool = False,
) -> dict[str, int]:
    """
    Main entry point: sync all memory files into the search index.

    Returns dict with counts: files_total, files_indexed, files_skipped,
    files_removed, chunks_total.
    """
    db = get_memory_db()

    # Read stored model/dim BEFORE init_schema — init_schema unconditionally
    # upserts the meta rows to current config values, which would hide any
    # drift. Wrap in try/except because on a fresh db the meta table doesn't
    # exist yet and get_meta would raise.
    stored_model: str | None = None
    stored_dim: str | None = None
    try:
        stored_model = db.get_meta("embedding_model")
        stored_dim = db.get_meta("embedding_dimensions")
    except Exception:
        pass

    db.init_schema()

    # Check for model change requiring rebuild
    if not force_rebuild and stored_model and stored_model != EMBEDDING_MODEL:
        print(f"Model changed ({stored_model} -> {EMBEDDING_MODEL}), forcing rebuild...")
        force_rebuild = True

    # Dim-drift guard: catches same-model-different-dim swaps (e.g. Matryoshka
    # slice change) that the model-name check above misses. The vec_chunks
    # virtual table bakes the dim into its DDL, so a silent mismatch here would
    # fail inserts at runtime with a shape error.
    if not force_rebuild and stored_dim and stored_dim != str(EMBEDDING_DIMENSIONS):
        print(f"Embedding dim changed ({stored_dim} -> {EMBEDDING_DIMENSIONS}), forcing rebuild...")
        force_rebuild = True

    if force_rebuild:
        db.bulk_clear()

    files = list_memory_files(memory_dir)
    current_paths: set[str] = set()

    files_indexed = 0
    files_skipped = 0
    total_chunks = 0

    for file_path in files:
        rel_path = file_path.relative_to(memory_dir).as_posix()
        current_paths.add(rel_path)
        content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()

        if not force_rebuild and db.get_file_hash(rel_path) == content_hash:
            files_skipped += 1
            continue

        print(f"  Indexing: {rel_path}")
        chunks_created = index_file(db, file_path, memory_dir, generate_embeddings)
        total_chunks += chunks_created
        files_indexed += 1

    files_removed = remove_stale_files(db, current_paths)

    db.close()

    return {
        "files_total": len(files),
        "files_indexed": files_indexed,
        "files_skipped": files_skipped,
        "files_removed": files_removed,
        "chunks_total": total_chunks,
    }


def print_stats() -> None:
    """Print index statistics."""
    try:
        db = get_memory_db()
        db.init_schema()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    stats = db.get_stats()
    db.close()

    print(f"Backend: {stats['backend']}")
    if "db_size_kb" in stats:
        print(f"Size: {stats['db_size_kb']:.1f} KB")
    print(f"Model: {stats['model']}")
    print(f"Files indexed: {stats['files']}")
    print(f"Chunks: {stats['chunks']}")
    print(f"Vectors: {stats['vectors']}")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Index memory files for search")
    parser.add_argument("--no-embeddings", action="store_true", help="Skip vector embeddings")
    parser.add_argument("--rebuild", action="store_true", help="Force full reindex")
    parser.add_argument("--stats", action="store_true", help="Show index statistics")
    parser.add_argument("--test", action="store_true", help="Dry run (list files only)")
    args = parser.parse_args()

    ensure_directories()

    if args.stats:
        print_stats()
        return

    if args.test:
        files = list_memory_files(MEMORY_DIR)
        print(f"Found {len(files)} memory files:")
        for f in files:
            rel = f.relative_to(MEMORY_DIR).as_posix()
            size = f.stat().st_size
            print(f"  {rel} ({size} bytes)")
        return

    print("Syncing memory index...")
    results = sync_index(
        generate_embeddings=not args.no_embeddings,
        force_rebuild=args.rebuild,
    )

    print("\nDone!")
    print(f"  Files total: {results['files_total']}")
    print(f"  Indexed: {results['files_indexed']}")
    print(f"  Skipped (unchanged): {results['files_skipped']}")
    print(f"  Removed (stale): {results['files_removed']}")
    print(f"  Chunks created: {results['chunks_total']}")


if __name__ == "__main__":
    main()
