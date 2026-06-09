"""
ACH RAG Indexer
───────────────
Watches the 'Payment-Files-Archive' folder in MinIO and indexes ACH file
summaries into the Qdrant 'ach_index' collection for semantic retrieval.

Responsibilities:
  1. On startup: create Qdrant collection if needed, reindex existing files,
     register MinIO bucket notification webhook
  2. POST /webhook  – receives MinIO S3 event notifications; indexes new files
  3. GET  /search   – semantic search over the ACH index (called by MCP server)
  4. GET  /health   – liveness probe

Embedding model: BAAI/bge-small-en-v1.5 (384-dim, runs fully local via ONNX)
"""

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
from fastapi import FastAPI, Query, Request
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
# ARN used in put_bucket_notification_configuration to target webhook target "1"
WEBHOOK_ARN      = "arn:minio:sqs::1:webhook"

# ── Globals (initialised in lifespan) ─────────────────────────────────────────

qdrant: QdrantClient
embedder: TextEmbedding
s3: "boto3.client"   # type: ignore[type-arg]

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
    """
    Extract one summary dict per batch from an ACH file.
    Uses batch control record (type 8) totals when available.
    No PII fields are read.
    """
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
            # Accumulate entry-level totals (overridden by batch control below)
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
            # Batch control is authoritative – override running totals
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

    # Strip internal flag before returning
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
    else:
        logger.info("Qdrant collection '%s' already exists", COLLECTION)


def _point_id(key: str, batch_idx: int) -> str:
    """Deterministic UUID keyed on (bucket_key, batch_idx) for idempotent upserts."""
    h = hashlib.md5(f"{BUCKET}/{key}/{batch_idx}".encode()).hexdigest()
    return str(uuid.UUID(h))


def _already_indexed(point_id: str) -> bool:
    results = qdrant.retrieve(
        collection_name=COLLECTION,
        ids=[point_id],
        with_payload=False,
        with_vectors=False,
    )
    return len(results) > 0


# ── Core indexing logic ────────────────────────────────────────────────────────

def _index_file(key: str) -> None:
    """Fetch one ACH file from MinIO, parse it, embed each batch, upsert to Qdrant."""
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

    points: list[PointStruct] = []
    for i, batch in enumerate(batches):
        pid = _point_id(key, i)
        if _already_indexed(pid):
            logger.debug("Already indexed: %s batch %d (skipping)", filename, i)
            continue

        summary = _make_summary(filename, batch)
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
    """
    Tell MinIO to POST to /webhook on this container whenever a new object
    is created under Payment-Files-Archive/ in the payments bucket.

    Requires MinIO to be started with:
      MINIO_NOTIFY_WEBHOOK_ENABLE_1=on
      MINIO_NOTIFY_WEBHOOK_ENDPOINT_1=http://indexer:8001/webhook
    """
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
        logger.warning("New files won't trigger real-time indexing (reindex on restart)")


# ── Startup reindex ────────────────────────────────────────────────────────────

def _reindex_existing() -> None:
    """Index any files already in Payment-Files-Archive that aren't in Qdrant yet."""
    prefix = f"{RAG_FOLDER}/"
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET, Prefix=prefix)
        keys  = [
            obj["Key"]
            for page in pages
            for obj in (page.get("Contents") or [])
            if not obj["Key"].endswith("/")   # skip folder markers
        ]
    except Exception as exc:
        logger.warning("Could not list existing files: %s", exc)
        return

    if not keys:
        logger.info("No existing files to reindex in %s/%s", BUCKET, RAG_FOLDER)
        return

    logger.info("Reindexing %d existing file(s) in %s/%s …", len(keys), BUCKET, RAG_FOLDER)
    for key in sorted(keys):
        _index_file(key)


# ── FastAPI app ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant, embedder, s3  # noqa: PLW0603

    logger.info("Indexer starting up …")

    qdrant   = QdrantClient(url=QDRANT_URL)
    embedder = TextEmbedding("BAAI/bge-small-en-v1.5")
    s3       = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )

    _ensure_collection()
    _reindex_existing()
    _setup_minio_notification()

    logger.info("Indexer ready")
    yield
    logger.info("Indexer shutting down")


app = FastAPI(title="ACH RAG Indexer", lifespan=lifespan)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def minio_webhook(request: Request):
    """
    MinIO S3 event webhook.
    Called by MinIO whenever a new object is created in Payment-Files-Archive/.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    records = body.get("Records") or []
    indexed = 0
    for rec in records:
        event_name = rec.get("eventName", "")
        if not event_name.startswith("s3:ObjectCreated"):
            continue

        s3_info = rec.get("s3", {})
        key     = unquote_plus(s3_info.get("object", {}).get("key", ""))

        if key.startswith(f"{RAG_FOLDER}/") and not key.endswith("/"):
            _index_file(key)
            indexed += 1

    return {"ok": True, "indexed": indexed}


@app.get("/search")
async def search(
    q:     str = Query(..., description="Natural language query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
):
    """
    Semantic search over the ACH index.
    Called by the rag_search_payments MCP tool in the mcp-server.
    """
    try:
        vector  = list(embedder.embed([q]))[0].tolist()
        results = qdrant.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=limit,
            with_payload=True,
        ).points
        hits = []
        for r in results:
            p = r.payload or {}
            hits.append({
                "score":                 round(r.score, 4),
                "filename":              p.get("filename"),
                "folder":                p.get("folder"),
                "company_name":          p.get("company_name"),
                "company_id":            p.get("company_id"),
                "effective_date":        p.get("effective_date"),
                "total_credit_dollars":  p.get("total_credit_dollars"),
                "total_debit_dollars":   p.get("total_debit_dollars"),
                "entry_count":           p.get("entry_count"),
                "entry_description":     p.get("entry_description"),
            })
        return {"query": q, "results": hits, "count": len(hits)}
    except Exception as exc:
        logger.exception("Search failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
