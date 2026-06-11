"""
Semantic response cache backed by Qdrant (async).

Uses fastembed (local ONNX model – no external API required) to embed queries,
then stores/retrieves agent responses by cosine similarity.
"""

import asyncio
import logging
import os
import time
import uuid

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    HnswConfigDiff,
    PayloadSchemaType,
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
        self._qdrant_url = qdrant_url
        self._client: AsyncQdrantClient | None = None
        self._embedder: TextEmbedding | None   = None
        self._ready    = False

    # ── Lazy init ─────────────────────────────────────────────────────────────

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        loop = asyncio.get_running_loop()   # get_event_loop() deprecated in 3.10+

        if self._embedder is None:
            self._embedder = await loop.run_in_executor(
                None, lambda: TextEmbedding("BAAI/bge-small-en-v1.5")
            )

        self._client = AsyncQdrantClient(url=self._qdrant_url)
        await self._ensure_collection()
        self._ready = True
        logger.info(
            "SemanticCache ready (collection=%s, threshold=%.2f, ttl=%.0fh)",
            COLLECTION, THRESHOLD, TTL_HOURS,
        )

    async def _ensure_collection(self) -> None:
        collections = await self._client.get_collections()
        existing = {c.name for c in collections.collections}
        if COLLECTION not in existing:
            await self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(
                    size=VECTOR_SIZE,
                    distance=Distance.COSINE,
                    # Explicit HNSW params for deterministic search quality
                    hnsw_config=HnswConfigDiff(m=16, ef_construct=128),
                ),
            )
            logger.info("Created Qdrant collection '%s'", COLLECTION)

        # Payload index on cached_at enables O(log n) TTL filtering
        # instead of a full collection scan on every cache lookup
        try:
            await self._client.create_payload_index(
                collection_name=COLLECTION,
                field_name="cached_at",
                field_schema=PayloadSchemaType.FLOAT,
            )
        except Exception:
            pass  # already exists

    # ── Embedding ─────────────────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: list(self._embedder.embed([text]))[0].tolist()
        )

    # ── TTL filter ────────────────────────────────────────────────────────────

    @staticmethod
    def _ttl_filter() -> Filter:
        cutoff = time.time() - TTL_HOURS * 3600
        return Filter(
            must=[FieldCondition(key="cached_at", range=Range(gte=cutoff))]
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def get(self, query: str) -> str | None:
        try:
            await self._ensure_ready()
            vector  = await self._embed(query)
            results = await self._client.search(
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
        try:
            await self._ensure_ready()
            vector = await self._embed(query)
            await self._client.upsert(
                collection_name=COLLECTION,
                points=[
                    PointStruct(
                        id=str(uuid.uuid4()),
                        vector=vector,
                        payload={
                            "query":     query[:80] + ("…" if len(query) > 80 else ""),
                            "response":  response,
                            "cached_at": time.time(),
                        },
                    )
                ],
            )
            logger.info("Cache SET (ttl=%.0fh) for query='%s'", TTL_HOURS, query[:80])
        except Exception:
            logger.exception("SemanticCache.set failed – response not cached")

    def clear(self) -> None:
        """Synchronous clear – drops and recreates the collection."""
        import qdrant_client as qc
        sync_client = qc.QdrantClient(url=self._qdrant_url)
        try:
            sync_client.delete_collection(COLLECTION)
        except Exception:
            pass
        sync_client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        self._ready = False
        logger.info("Cache cleared")

    def clear_expired(self) -> int:
        import qdrant_client as qc
        from qdrant_client.models import FilterSelector
        sync_client = qc.QdrantClient(url=self._qdrant_url)
        cutoff = time.time() - TTL_HOURS * 3600
        try:
            result = sync_client.delete(
                collection_name=COLLECTION,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="cached_at", range=Range(lt=cutoff))]
                    )
                ),
            )
            logger.info("Pruned expired cache entries")
            return getattr(result, "deleted", 0)
        except Exception:
            logger.exception("clear_expired failed")
            return 0
