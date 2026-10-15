"""Embedding provider interface with fake (hash-based) and local (sentence-transformers) implementations."""

import hashlib
import math
from typing import Protocol

from app.core.config import get_settings

settings = get_settings()

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbeddingProvider(Protocol):
    """Protocol for document and query vector embedding."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of document chunk texts without instruction prefix."""
        ...

    def embed_query(self, query: str) -> list[float]:
        """Embed a single user search query with retrieval instruction prefix."""
        ...


class FakeEmbeddingProvider:
    """Deterministic hash-based 384-dimensional unit vector embedder.

    Requires zero downloads and runs fully offline for local development, tests, and CI.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _hash_vector(self, text: str) -> list[float]:
        """Compute a deterministic pseudo-random unit vector from text."""
        if not text:
            raw = [0.0] * self.dim
            raw[0] = 1.0
            return raw

        vec: list[float] = []
        for i in range(self.dim):
            # Seed hash with index to generate varied dimensions
            seed = f"{i}:{text}".encode()
            h = hashlib.sha256(seed).digest()
            val = int.from_bytes(h[:4], "big", signed=True) / (2**31)
            vec.append(val)

        # L2 normalization to unit sphere
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [round(x / norm, 6) for x in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_vector(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        prefixed = f"{BGE_QUERY_PREFIX}{query}"
        return self._hash_vector(prefixed)


class LocalEmbeddingProvider:
    """CPU-only sentence-transformers provider for production and high-fidelity embedding."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        batch_size: int = 32,
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.batch_size = batch_size
        self.model = SentenceTransformer(model_name, device="cpu")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()  # type: ignore[no-any-return]

    def embed_query(self, query: str) -> list[float]:
        prefixed = f"{BGE_QUERY_PREFIX}{query}"
        embeddings = self.model.encode(
            [prefixed],
            batch_size=1,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings[0].tolist()  # type: ignore[no-any-return]


_provider_singleton: EmbeddingProvider | None = None


def get_embedding_provider(
    provider_name: str | None = None,
    model_name: str | None = None,
    dim: int | None = None,
    batch_size: int | None = None,
) -> EmbeddingProvider:
    """Return embedding provider singleton according to configuration."""
    global _provider_singleton
    if _provider_singleton is not None and provider_name is None:
        return _provider_singleton

    p_name = (provider_name or settings.EMBEDDING_PROVIDER).lower().strip()
    m_name = model_name or settings.EMBEDDING_MODEL
    b_size = batch_size or settings.EMBEDDING_BATCH_SIZE
    e_dim = dim or settings.EMBEDDING_DIMENSION

    if p_name == "local":
        provider: EmbeddingProvider = LocalEmbeddingProvider(model_name=m_name, batch_size=b_size)
    else:
        provider = FakeEmbeddingProvider(dim=e_dim)

    if provider_name is None:
        _provider_singleton = provider
    return provider
