"""
sentence-transformers wrapper for memory search embeddings.

Uses EmbeddingGemma-300m @ 512 Matryoshka dims (upgraded from FastEmbed+MiniLM
on 2026-04-11). Provides lazy-loaded embedding model with explicit query vs
document prompts, which EmbeddingGemma was trained with and needs for correct
asymmetric retrieval behavior.

- embed_text(text)  → query-style prompt    (single-text = always a query here)
- embed_batch(texts) → document-style prompt (batch = always indexing here)

Both return L2-normalized 512-dim float32 vectors. Matryoshka truncation from
768 → 512 happens inside the SentenceTransformer graph via truncate_dim=512.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from config import EMBEDDING_CACHE_DIR, EMBEDDING_MODEL

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

# Lazy singleton — model loaded on first use (~600MB download one-time).
# Subsequent cold starts read from EMBEDDING_CACHE_DIR (E: drive).
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Get or create the embedding model singleton."""
    global _model  # noqa: PLW0603
    if _model is None:
        from sentence_transformers import SentenceTransformer

        EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model = SentenceTransformer(
            EMBEDDING_MODEL,
            device="cpu",
            cache_folder=str(EMBEDDING_CACHE_DIR),
            truncate_dim=512,  # Matryoshka → 512 dims inside the model graph
        )
    return _model


def embed_text(text: str) -> NDArray[np.float32]:
    """Embed a single query string into a 512-dim L2-normalized float32 vector.

    Uses the query-side prompt template ("task: search result | query: ...").
    This is called from memory_search.py and recall_service, which only embed
    user queries — never documents.
    """
    model = _get_model()
    vec = model.encode_query(text, normalize_embeddings=True)
    return np.asarray(vec, dtype=np.float32)


def embed_batch(texts: list[str], batch_size: int = 32) -> list[NDArray[np.float32]]:
    """Embed a batch of document chunks into 512-dim L2-normalized float32 vectors.

    Uses the document-side prompt template ("title: none | text: ...").
    This is called from memory_index.py when indexing vault files — never queries.
    """
    if not texts:
        return []
    model = _get_model()
    vecs = model.encode_document(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
    )
    return [np.asarray(v, dtype=np.float32) for v in vecs]


def embedding_to_bytes(embedding: NDArray[np.float32]) -> bytes:
    """Serialize embedding to bytes for sqlite-vec storage."""
    return embedding.tobytes()


def bytes_to_embedding(data: bytes) -> NDArray[np.float32]:
    """Deserialize bytes back to embedding array."""
    arr: NDArray[np.float32] = np.frombuffer(data, dtype=np.float32).copy()
    return arr


def text_hash(text: str) -> str:
    """SHA-256 prefix (16 chars) for content deduplication."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
