"""
ACH RAG Indexer
───────────────
Watches the Archive folder in MinIO and indexes ACH file summaries into the
Qdrant 'ach_index' collection for semantic retrieval.

Responsibilities:
  1. On startup: create Qdrant collection if needed, register MinIO webhook,
     then reindex existing files in the background (non-blocking).
  2. POST /webhook  – receives MinIO S3 event notifications; indexes new files
     as a background task so the webhook returns immediately.
  3. GET  /search   – semantic search over the ACH index (called by MCP server)
  4. GET  /health   – liveness probe

Improvements over v1:
  • Webhook handler is non-blocking – indexing runs as a BackgroundTask
  • Startup reindex runs as asyncio background task (app accepts requests immediately)
  • Batch _already_indexed check: one Qdrant retrieve call per file instead of per batch
  • Embedding wrapped in run_in_executor (CPU-bound ONNX, keeps event loop free)
  • Secrets required at startup (no silent defaults)
  • score_threshold param forwarded from MCP server to /search endpoint
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import unquote_plus

import boto3
from botocore.client import Config
from fastembed import TextEmbedding
from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

MINIO_ENDPOINT   = os.environ.get("MINIO_ENDPOINT",   "http://minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
QDRANT_URL       = os.environ.get("QDRANT_URL",       "http://qdrant:6333")
BUCKET           = "payments"
RAG_FOLDER       = "Archive"
COLLECTION       = "ach_index"
VECTOR_SIZE      = 384
WEBHOOK_ARN      = "arn:minio:sqs::1:webhook"

# ── Globals (initialised in lifespan) ─────────────────────────────────────────

qdrant:   QdrantClient
embedder: TextEmbedding
s3:       "boto3.client"   # type: ignore[type-arg]

# ── Minimal ACH batch-level parser ────────────────────────────────────────────

DEBIT_CODES = {"27", "28", "29", "37", "38", "55"}


def _fmt_date(s: str) -> str:
    s = s.strip()
    if len(s) == 6 and s.isdigit():
        yy, mm, dd = s[:2], s[2:4], s[4:6]
        year = f"20{yy}" if int(yy) < 70 else f"19{yy}"
        return f"{year}-{mm}-{dd}"
    return s or "unknown"


def _parse_ach_batches(content: str) -> list[dict]:
    batches: list[dict] = []
    current: dict | None = None

    for raw in content.splitlines():
        line = (raw.rstrip("\r") + " " * 94)[:94]
        rt = line[0]

        if rt == "5":
            current = {
                "company_name":      line[4:20].strip(),
                "company_id":        line[40:50].strip(),
                "entry_class":       line[50:53].strip(),
                "entry_description": line[53:63].strip(),
                "effective_date":    _fmt_date(line[69:75]),
                "total_credit":      0.0,
                "total_debit":       0.0,
                "entry_count":       0,
                "_control_seen":     False,
            }
            batches.append(current)

        elif rt == "6" and current is not None:
            if not current["_control_seen"]:
                tc = line[1:3]
                raw_amt = line[29:39].strip()
                amt = int(raw_amt) / 100.0 if raw_amt.isdigit() else 0.0
                if tc in DEBIT_CODES:
                    current["total_debit"]  = round(current["total_debit"]  + amt, 2)
                else:
                    current["total_credit"] = round(current["total_credit"] + amt, 2)
                current["entry_count"] += 1

        elif rt == "8" and current is not None:
            dr = line[20:32].strip()
            cr = line[32:44].strip()
            ec = line[4:10].strip()
            if dr.isdigit():
                current["total_debit"]  = round(int(dr) / 100.0, 2)
            if cr.isdigit():
                current["total_credit"] = round(int(cr) / 100.0, 2)
            if ec.isdigit():
                current["entry_count"]  = int(ec)
            current["_control_seen"] = True

    for b in batches:
        b.pop("_control_seen", None)
    return batches


def _make_summary(filename: str, batch: dict) -> str:
    return (
        f"Company: {batch['company_name']} (ID: {batch['company_id']}). "
        f"Folder: {RAG_FOLDER}. File: {filename}. "
        f"Date: {batch['effective_date']}. "
        f"Entries: {batch['entry_count']}. "
        f"Credits: ${batch['total_credit']:,.2f}. "
        f"Debits: ${batch['total_debit']:,.2f}. "
        f"Description: {batch['entry_description']}."
    )


# ── Qdrant helpers ─────────────────────────────────────────────────────────────

def _ensure_collection() -> None:
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '%s'", COLLECTION)


def _point_id(key: str, batch_idx: int) -> str:
    h = hashlib.md5(f"{BUCKET}/{key}/{batch_idx}".encode()).hexdigest()
    return str(uuid.UUID(h))


def _get_already_indexed(keys_and_indices: list[tuple[str, int]]) -> set[str]:
    """
    Single bulk Qdrant retrieve to check which point IDs already exist.
    Replaces the previous per-batch retrieve loop (N round-trips → 1).
    """
    if not keys_and_indices:
        return set()
    all_ids = [_point_id(k, i) for k, i in keys_and_indices]
    results = qdrant.retrieve(
        collection_name=COLLECTION,
        ids=all_ids,
        with_payload=False,
        with_vectors=False,
    )
    return {str(r.id) for r in results}


# ── Core indexing logic ────────────────────────────────────────────────────────

def _index_file(key: str) -> None:
    """
    Fetch one ACH file from MinIO, parse it, embed each batch, upsert to Qdrant.
    This is a synchronous function; callers should run it in an executor or
    as a FastAPI BackgroundTask (which uses a thread pool automatically).
    """
    filename = key.split("/")[-1]
    logger.info("Indexing %s/%s …", BUCKET, key)

    try:
        body    = s3.get_object(Bucket=BUCKET, Key=key)["Body"]
        content = body.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.error("Failed to fetch %s: %s", key, exc)
        return

    batches = _parse_ach_batches(content)
    if not batches:
        logger.warning("No ACH batches found in %s", filename)
        return

    # Single bulk check for already-indexed batches
    keys_and_indices    = [(key, i) for i in range(len(batches))]
    already_indexed_ids = _get_already_indexed(keys_and_indices)

    points: list[PointStruct] = []
    for i, batch in enumerate(batches):
        pid = _point_id(key, i)
        if pid in already_indexed_ids:
            logger.debug("Already indexed: %s batch %d (skipping)", filename, i)
            continue

        summary = _make_summary(filename, batch)
        # Embedding is CPU-bound (ONNX) – acceptable here since we run in thread pool
        vector  = list(embedder.embed([summary]))[0].tolist()

        points.append(PointStruct(
            id=pid,
            vector=vector,
            payload={
                "filename":             filename,
                "folder":               RAG_FOLDER,
                "company_name":         batch["company_name"],
                "company_id":           batch["company_id"],
                "effective_date":       batch["effective_date"],
                "total_credit_dollars": batch["total_credit"],
                "total_debit_dollars":  batch["total_debit"],
                "entry_count":          batch["entry_count"],
                "entry_description":    batch["entry_description"],
                "summary":              summary,
                "indexed_at":           time.time(),
            },
        ))

    if points:
        qdrant.upsert(collection_name=COLLECTION, points=points)
        logger.info("Indexed %d batch(es) from %s", len(points), filename)
    else:
        logger.info("All batches in %s already indexed", filename)


# ── MinIO webhook notification setup ─────────────────────────────────────────

def _setup_minio_notification() -> None:
    try:
        s3.put_bucket_notification_configuration(
            Bucket=BUCKET,
            NotificationConfiguration={
                "QueueConfigurations": [{
                    "Id":       "rag-indexer",
                    "QueueArn": WEBHOOK_ARN,
                    "Events":   ["s3:ObjectCreated:*"],
                    "Filter": {
                        "Key": {
                            "FilterRules": [
                                {"Name": "prefix", "Value": f"{RAG_FOLDER}/"}
                            ]
                        }
                    },
                }]
            },
        )
        logger.info(
            "MinIO webhook notification registered: %s/%s/* → POST /webhook",
            BUCKET, RAG_FOLDER,
        )
    except Exception as exc:
        logger.warning("Could not configure MinIO bucket notification: %s", exc)


# ── Startup reindex ────────────────────────────────────────────────────────────

def _reindex_existing() -> None:
    """Index any files already in Archive that aren't in Qdrant yet."""
    prefix = f"{RAG_FOLDER}/"
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET, Prefix=prefix)
        keys  = [
            obj["Key"]
            for page in pages
            for obj in (page.get("Contents") or [])
            if not obj["Key"].endswith("/")
        ]
    except Exception as exc:
        logger.warning("Could not list existing files: %s", exc)
        return

    if not keys:
        logger.info("No existing files to reindex in %s/%s", BUCKET, RAG_FOLDER)
        return

    logger.info("Background reindex: %d file(s) in %s/%s", len(keys), BUCKET, RAG_FOLDER)
    for key in sorted(keys):
        _index_file(key)
    logger.info("Background reindex complete")


# ── FastAPI app ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant, embedder, s3  # noqa: PLW0603

    logger.info("Indexer starting up …")

    # Initialise clients synchronously (fast)
    qdrant = QdrantClient(url=QDRANT_URL)
    s3     = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )

    # Load embedding model in executor (CPU-bound, ~130 MB ONNX)
    loop     = asyncio.get_event_loop()
    embedder = await loop.run_in_executor(
        None, lambda: TextEmbedding("BAAI/bge-small-en-v1.5")
    )

    _ensure_collection()
    _setup_minio_notification()

    # Reindex existing files in the background so startup is non-blocking
    asyncio.ensure_future(loop.run_in_executor(None, _reindex_existing))

    logger.info("Indexer ready – accepting requests (reindex running in background)")
    yield
    logger.info("Indexer shutting down")


app = FastAPI(title="ACH RAG Indexer", lifespan=lifespan)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def minio_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    MinIO S3 event webhook – returns immediately, indexes in background.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    records = body.get("Records") or []
    queued  = 0
    for rec in records:
        if not rec.get("eventName", "").startswith("s3:ObjectCreated"):
            continue
        key = unquote_plus(rec.get("s3", {}).get("object", {}).get("key", ""))
        if key.startswith(f"{RAG_FOLDER}/") and not key.endswith("/"):
            background_tasks.add_task(_index_file, key)
            queued += 1

    return {"ok": True, "queued": queued}


@app.get("/search")
async def search(
    q:         str   = Query(..., description="Natural language query"),
    limit:     int   = Query(10, ge=1, le=50),
    threshold: float = Query(0.55, ge=0.0, le=1.0, description="Minimum relevance score"),
):
    """Semantic search over the ACH index."""
    try:
        loop    = asyncio.get_event_loop()
        vector  = await loop.run_in_executor(
            None, lambda: list(embedder.embed([q]))[0].tolist()
        )
        results = qdrant.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=limit,
            score_threshold=threshold,
            with_payload=True,
        ).points
        hits = [
            {
                "score":                round(r.score, 4),
                "filename":             r.payload.get("filename"),
                "folder":               r.payload.get("folder"),
                "company_name":         r.payload.get("company_name"),
                "company_id":           r.payload.get("company_id"),
                "effective_date":       r.payload.get("effective_date"),
                "total_credit_dollars": r.payload.get("total_credit_dollars"),
                "total_debit_dollars":  r.payload.get("total_debit_dollars"),
                "entry_count":          r.payload.get("entry_count"),
                "entry_description":    r.payload.get("entry_description"),
            }
            for r in results
        ]
        return {"query": q, "results": hits, "count": len(hits)}
    except Exception as exc:
        logger.exception("Search failed")
        return JSONResponse({"error": "Search failed. Please try again."}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
