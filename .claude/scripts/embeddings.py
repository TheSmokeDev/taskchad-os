"""
FastEmbed wrapper for memory search embeddings.

Uses BAAI/bge-base-en-v1.5 via FastEmbed / ONNX Runtime (swapped from
EmbeddingGemma-300m on 2026-04-22). Public Apache-2.0 model, no HF auth,
no torch dependency, deterministic across platforms.

BGE v1.5 was retrained to be less sensitive to query/passage prefixes than
BGE v1, and FastEmbed's model registry classifies its prefix behavior as
"not so necessary". We therefore use a single symmetric embed() path for
both queries and passages — keeps the code path trivial and matches the
model's intended usage. If a future model requires asymmetric prefixes,
introduce per-model handling here, not in every caller.

- embed_text(text)   → single-input
- embed_batch(texts) → batch-input

Both return L2-normalized 768-dim float32 vectors. BGE v1.5 outputs are
already unit-normalized; we defensively re-normalize to preserve the
downstream contract expected by sqlite-vec / pgvector consumers.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from config import EMBEDDING_CACHE_DIR, EMBEDDING_MODEL

if TYPE_CHECKING:
    from fastembed import TextEmbedding

# Lazy singleton — ONNX model downloaded on first use (~130 MB one-time).
# Subsequent cold starts read from EMBEDDING_CACHE_DIR.
_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    """Get or create the FastEmbed model singleton."""
    global _model  # noqa: PLW0603
    if _model is None:
        from fastembed import TextEmbedding

        EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(
            model_name=EMBEDDING_MODEL,
            cache_dir=str(EMBEDDING_CACHE_DIR),
        )
    return _model


def _normalize(vec: NDArray[np.float32]) -> NDArray[np.float32]:
    """L2-normalize a 1-D vector, avoiding division by zero."""
    norm = np.linalg.norm(vec)
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32, copy=False)


def embed_text(text: str) -> NDArray[np.float32]:
    """Embed a single string into a 768-dim L2-normalized float32 vector.

    Called from memory_search.py and recall_service for user queries. BGE v1.5
    uses a single symmetric embed path — see module docstring.
    """
    model = _get_model()
    vec = next(iter(model.embed([text])))
    return _normalize(np.asarray(vec, dtype=np.float32))


def embed_batch(texts: list[str], batch_size: int = 32) -> list[NDArray[np.float32]]:
    """Embed a batch of strings into 768-dim L2-normalized float32 vectors.

    Called from memory_index.py when indexing vault files. FastEmbed handles
    batching internally via onnxruntime; `batch_size` is forwarded and also
    kept for API parity with the old sentence-transformers implementation.
    """
    if not texts:
        return []
    model = _get_model()
    vecs = model.embed(texts, batch_size=batch_size)
    return [_normalize(np.asarray(v, dtype=np.float32)) for v in vecs]


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
