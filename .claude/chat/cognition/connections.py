"""Emergent connection discovery between vault notes.

Finds pairs of notes that are semantically similar but have no
wiki-link edge. Reports potential missing links during daily reflection.

Pattern: cognition/graph.py — graph operations + memory_search.py vector search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cognition.graph import build_memory_graph


@dataclass
class PotentialConnection:
    """A pair of notes that might be related but aren't linked."""

    note_a: str
    note_b: str
    similarity: float
    shared_terms: list[str] = field(default_factory=list)


async def find_emergent_connections(
    memory_dir: Path,
    similarity_threshold: float = 0.75,
    max_results: int = 10,
) -> list[PotentialConnection]:
    """Find semantically similar notes with no existing graph edge.

    Runs during daily reflection (not per-turn). Can afford to be slow.
    Uses embeddings from the memory database.
    """
    graph = build_memory_graph(memory_dir)

    # Build set of existing edges (both directions) — keyed by STEM
    # because _load_note_embeddings() returns stem-keyed dicts.
    # graph.forward_links is path-keyed, so convert via path_to_stem.
    existing_edges: set[tuple[str, str]] = set()
    for src_path, target_paths in graph.forward_links.items():
        src_stem = graph.path_to_stem.get(src_path, src_path)
        for tgt_path in target_paths:
            tgt_stem = graph.path_to_stem.get(tgt_path, tgt_path)
            existing_edges.add((src_stem, tgt_stem))
            existing_edges.add((tgt_stem, src_stem))

    # Get note embeddings from the database
    try:
        note_embeddings = _load_note_embeddings()
    except Exception:
        return []

    if len(note_embeddings) < 2:
        return []

    # Compare all pairs (O(n^2) but n is small — typically <100 notes)
    import numpy as np

    stems = list(note_embeddings.keys())
    connections: list[PotentialConnection] = []

    for i in range(len(stems)):
        for j in range(i + 1, len(stems)):
            a, b = stems[i], stems[j]
            # Skip if edge already exists
            if (a, b) in existing_edges:
                continue

            # Compute cosine similarity
            vec_a = np.array(note_embeddings[a])
            vec_b = np.array(note_embeddings[b])
            norm_product = np.linalg.norm(vec_a) * np.linalg.norm(vec_b) + 1e-9
            similarity = float(np.dot(vec_a, vec_b) / norm_product)

            if similarity >= similarity_threshold:
                path_a = graph.stem_to_path.get(a, a)
                path_b = graph.stem_to_path.get(b, b)
                connections.append(PotentialConnection(
                    note_a=path_a,
                    note_b=path_b,
                    similarity=similarity,
                ))

    # Sort by similarity descending, cap results
    connections.sort(key=lambda c: c.similarity, reverse=True)
    return connections[:max_results]


def _load_note_embeddings() -> dict[str, list[float]]:
    """Load one embedding per note file from the memory database.

    Uses raw SQL on memory.db: first chunk per file joined with vec_chunks.
    """
    from db import get_memory_db

    db = get_memory_db()
    db.init_schema()

    note_embeddings: dict[str, list[float]] = {}

    try:
        if hasattr(db, "conn"):
            # SQLite path
            cursor = db.conn.execute(
                """
                SELECT c.file_path, v.embedding
                FROM chunks c
                JOIN vec_chunks v ON c.id = v.id
                WHERE c.id IN (
                    SELECT MIN(id) FROM chunks GROUP BY file_path
                )
                """
            )
            for row in cursor:
                stem = Path(row[0]).stem.lower()
                # sqlite-vec returns bytes; decode if needed
                embedding = row[1]
                if isinstance(embedding, bytes):
                    import struct

                    n_floats = len(embedding) // 4
                    embedding = list(struct.unpack(f"{n_floats}f", embedding))
                if isinstance(embedding, (list, tuple)) and len(embedding) > 0:
                    note_embeddings[stem] = list(embedding)
    except Exception:
        pass
    finally:
        try:
            db.close()
        except Exception:
            pass

    return note_embeddings
