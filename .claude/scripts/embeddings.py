"""
FastEmbed wrapper for memory search embeddings.

Provides lazy-loaded embedding model with batch support and
serialization helpers for sqlite-vec storage.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from config import EMBEDDING_CACHE_DIR, EMBEDDING_MODEL

if TYPE_CHECKING:
    from fastembed import TextEmbedding

# Lazy singleton — model loaded on first use (~80MB download)
_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    """Get or create the embedding model singleton."""
    global _model  # noqa: PLW0603
    if _model is None:
        from fastembed import TextEmbedding

        EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(
            model_name=EMBEDDING_MODEL,
            cache_dir=str(EMBEDDING_CACHE_DIR),
        )
    return _model


def embed_text(text: str) -> NDArray[np.float32]:
    """Embed a single text string into a 384-dim float32 vector."""
    model = _get_model()
    results = list(model.embed([text]))
    embedding: NDArray[np.float32] = np.array(results[0], dtype=np.float32)
    return embedding


def embed_batch(texts: list[str], batch_size: int = 32) -> list[NDArray[np.float32]]:
    """Embed a batch of texts. Returns list of 384-dim float32 vectors."""
    if not texts:
        return []
    model = _get_model()
    results = list(model.embed(texts, batch_size=batch_size))
    return [np.array(r, dtype=np.float32) for r in results]


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
