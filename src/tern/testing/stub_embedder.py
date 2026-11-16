"""``StubEmbedder``: a deterministic feature-hashing embedder for tests and smoke (plan 05 step 2).

Tokens are the ``\\w+`` runs of ``fold(text).casefold()`` (the same folding as FTS, so Greek with
or without tonos maps to the same vector). Each token is hashed with keyed ``hashlib.blake2b``
(never ``hash()``, which is salted per process): the digest picks a bucket in ``0..dim-1`` and a
sign, and the bucket gets ±1. So texts sharing words get similar vectors, and the result is the
same in every process and on every run.

The vector is never all zero, which no real embedder returns and ``vec.insert`` rejects: a text
with no tokens, or whose signed buckets cancel out exactly, gets a fixed nonzero vector (one-hot
at a bucket derived from ``seed``). Vectors are not normalised; ``vec`` normalises.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from tern.index.chunks import fold

_TOKEN = re.compile(r"\w+")


class StubEmbedder:
    """Deterministic ``Embedder`` (``tern.search.embedder.Embedder``) with no model behind it."""

    def __init__(self, dim: int = 64, seed: int = 0) -> None:
        if dim < 1:
            raise ValueError("dim must be >= 1")
        if not 0 <= seed < 2**64:
            raise ValueError("seed must be in 0..2**64-1")
        self.dim = dim
        self.seed = seed
        self.model_id: str = f"stub-fh-{dim}-{seed}"
        self._key = seed.to_bytes(8, "little")

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16, key=self._key).digest()
        bucket = int.from_bytes(digest[:8], "little") % self.dim
        sign = 1.0 if digest[8] & 1 else -1.0
        return bucket, sign

    def empty_vector(self) -> list[float]:
        """The fixed vector for a text with no tokens (or whose buckets cancel out)."""
        bucket, _ = self._bucket("\x00stub-empty")
        vector = [0.0] * self.dim
        vector[bucket] = 1.0
        return vector

    def vector(self, text: str) -> list[float]:
        """The vector of one text."""
        vector = [0.0] * self.dim
        for token in _TOKEN.findall(fold(text).casefold()):
            bucket, sign = self._bucket(token)
            vector[bucket] += sign
        if not any(vector):
            return self.empty_vector()
        return vector

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if isinstance(texts, str):
            raise TypeError("embed_documents takes a sequence of texts, not one str")
        return [self.vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self.vector(text)
