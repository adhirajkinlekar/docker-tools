"""
Semantic response cache backed by Qdrant.

Uses fastembed (local ONNX model – no external API required) to embed queries,
then stores/retrieves agent responses by cosine similarity.

Flow:
  1. Incoming query → embed → search Qdrant (filtered to non-expired entries)
  2. If best match score ≥ THRESHOLD  → return cached response (cache hit)
  3. Otherwise                        → run agent, embed result, upsert into Qdrant

TTL:
  Each entry stores a `cached_at` Unix timestamp. On lookup, only entries
  younger than CACHE_TTL_HOURS are considered. Expired entries are pruned
  lazily on each cache.clear() call (or immediately via clear_expired()).
  Configure via CACHE_TTL_HOURS env var (default: 24).

Embedding model: BAAI/bge-small-en-v1.5
  • 384-dimensional vectors
  • ~130 MB (downloaded once at image build time)
  • Runs fully local via ONNX Runtime – no API key needed
"""

import asyncio
import logging
import os
import time
import uuid

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    Range,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger(__name__)

COLLECTION  = "response_cache"
VECTOR_SIZE = 384     # bge-small-en-v1.5 output dimension
THRESHOLD   = 0.88    # cosine similarity – tune up for stricter matching
TTL_HOURS   = float(os.environ.get("CACHE_TTL_HOURS", "24"))


class SemanticCache:
    def __init__(self, qdrant_url: str):
        self._client   = QdrantClient(url=qdrant_url)
        self._embedder = TextEmbedding("BAAI/bge-small-en-v1.5")
        self._ensure_collection()
        logger.info(
            "SemanticCache ready (collection=%s, threshold=%.2f, ttl=%.0fh)",
            COLLECTION, THRESHOLD, TTL_HOURS,
        )

    # ── Collection bootstrap ──────────────────────────────────────────────────

    def _ensure_collection(self) -> None:
        existing = {c.name for c in self._client.get_collections().collections}
        if COLLECTION not in existing:
            self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            logger.info("Created Qdrant collection '%s'", COLLECTION)

    # ── Embedding (sync, wrapped for async callers) ───────────────────────────

    def _embed_sync(self, text: str) -> list[float]:
        return list(self._embedder.embed([text]))[0].tolist()

    async def _embed(self, text: str) -> list[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_sync, text)

    # ── TTL filter ────────────────────────────────────────────────────────────

    @staticmethod
    def _ttl_filter() -> Filter:
        """Return a Qdrant filter that excludes entries older than TTL_HOURS."""
        cutoff = time.time() - TTL_HOURS * 3600
        return Filter(
            must=[
                FieldCondition(
                    key="cached_at",
                    range=Range(gte=cutoff),
                )
            ]
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def get(self, query: str) -> str | None:
        """
        Return a cached response for a semantically similar, non-expired query.
        Returns None on cache miss or error (agent will run normally).
        """
        try:
            vector  = await self._embed(query)
            results = self._client.search(
                collection_name=COLLECTION,
                query_vector=vector,
                query_filter=self._ttl_filter(),
                limit=1,
                score_threshold=THRESHOLD,
                with_payload=True,
            )
            if results:
                hit     = results[0]
                age_min = (time.time() - hit.payload.get("cached_at", 0)) / 60
                logger.info(
                    "Cache HIT  score=%.4f  age=%.1fmin  query='%s'",
                    hit.score, age_min, hit.payload.get("query", "")[:80],
                )
                return hit.payload.get("response")
            logger.info("Cache MISS for query='%s'", query[:80])
        except Exception:
            logger.exception("SemanticCache.get failed – falling through to agent")
        return None

    async def set(self, query: str, response: str) -> None:
        """Embed and store a query→response pair with the current timestamp."""
        try:
            vector = await self._embed(query)
            self._client.upsert(
                collection_name=COLLECTION,
                points=[
                    PointStruct(
                        id=str(uuid.uuid4()),
                        vector=vector,
                        payload={
                            "query":     query,
                            "response":  response,
                            "cached_at": time.time(),   # Unix timestamp for TTL
                        },
                    )
                ],
            )
            logger.info("Cache SET (ttl=%.0fh) for query='%s'", TTL_HOURS, query[:80])
        except Exception:
            logger.exception("SemanticCache.set failed – response not cached")

    def clear(self) -> None:
        """Drop and recreate the collection (wipes all cached responses)."""
        self._client.delete_collection(COLLECTION)
        self._ensure_collection()
        logger.info("Cache cleared")

    def clear_expired(self) -> int:
        """Delete only expired entries. Returns count of deleted points."""
        try:
            cutoff = time.time() - TTL_HOURS * 3600
            result = self._client.delete(
                collection_name=COLLECTION,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="cached_at",
                            range=Range(lt=cutoff),
                        )
                    ]
                ),
            )
            logger.info("Pruned expired cache entries (cutoff age=%.0fh)", TTL_HOURS)
            return getattr(result, "deleted", 0)
        except Exception:
            logger.exception("clear_expired failed")
            return 0
