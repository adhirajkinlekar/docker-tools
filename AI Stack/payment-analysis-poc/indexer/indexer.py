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
  4. GET  /aggregate – full company-level rollup without semantic filtering
  5. GET  /health   – liveness probe
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
import pymssql
from botocore.client import Config
from fastembed import TextEmbedding
from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

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
MSSQL_SERVER     = os.environ.get("MSSQL_SERVER",     "mssql")
MSSQL_DATABASE   = os.environ.get("MSSQL_DATABASE",   "PaymentDB")
MSSQL_USER       = os.environ.get("MSSQL_USER",       "sa")
MSSQL_PASSWORD   = os.environ.get("MSSQL_PASSWORD",   "")
BUCKET           = "payments"
RAG_FOLDER       = "Archive"
COLLECTION       = "ach_index"
VECTOR_SIZE      = 384
WEBHOOK_ARN      = "arn:minio:sqs::1:webhook"

# HNSW index parameters — explicit for deterministic search quality.
# m=16: connections per node (higher = better recall, more memory)
# ef_construct=128: build-time search width (higher = better index quality)
HNSW_M            = 16
HNSW_EF_CONSTRUCT = 128

# ── Globals (initialised in lifespan) ─────────────────────────────────────────

qdrant:   QdrantClient
embedder: TextEmbedding
s3:       "boto3.client"   # type: ignore[type-arg]

# ── Minimal ACH batch-level parser ────────────────────────────────────────────

DEBIT_CODES  = {"27", "28", "29", "37", "38", "55"}
CREDIT_CODES = {"22", "23", "24", "32", "33", "52"}


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
    """
    Build the text string that gets embedded into a vector.

    Richer text → richer vector. Include:
    - Company identity (name + ID)
    - Financial signals (credit/debit amounts, ratio, direction)
    - Temporal context (date)
    - Transaction class and description
    """
    cr = batch["total_credit"]
    db = batch["total_debit"]

    # Credit/debit ratio as a semantic signal
    if db > 0:
        ratio_str = f"credit-to-debit ratio {cr / db:.2f}"
    elif cr > 0:
        ratio_str = "credit-only batch"
    else:
        ratio_str = "debit-only batch"

    # Dominant direction
    direction = "credit" if cr >= db else "debit"

    return (
        f"Company: {batch['company_name']} (ID: {batch['company_id']}). "
        f"Folder: {RAG_FOLDER}. File: {filename}. "
        f"Date: {batch['effective_date']}. "
        f"Entry class: {batch['entry_class']}. "
        f"Description: {batch['entry_description']}. "
        f"Entries: {batch['entry_count']}. "
        f"Credits: ${cr:,.2f}. Debits: ${db:,.2f}. "
        f"Dominant direction: {direction}. {ratio_str}."
    )


# ── Qdrant helpers ─────────────────────────────────────────────────────────────

def _ensure_collection() -> None:
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
                hnsw_config=HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT),
            ),
        )
        logger.info("Created Qdrant collection '%s' (HNSW m=%d ef=%d)",
                    COLLECTION, HNSW_M, HNSW_EF_CONSTRUCT)

    # Payload indexes for O(log n) filtered queries instead of full scans
    _ensure_payload_index("company_name",   PayloadSchemaType.KEYWORD)
    _ensure_payload_index("effective_date", PayloadSchemaType.KEYWORD)
    _ensure_payload_index("entry_class",    PayloadSchemaType.KEYWORD)


def _ensure_payload_index(field: str, schema_type: PayloadSchemaType) -> None:
    try:
        qdrant.create_payload_index(
            collection_name=COLLECTION,
            field_name=field,
            field_schema=schema_type,
        )
        logger.info("Payload index ensured: %s (%s)", field, schema_type)
    except Exception:
        pass  # already exists — not an error


def _point_id(key: str, batch_idx: int) -> str:
    h = hashlib.md5(f"{BUCKET}/{key}/{batch_idx}".encode()).hexdigest()
    return str(uuid.UUID(h))


def _get_already_indexed(keys_and_indices: list[tuple[str, int]]) -> set[str]:
    """Single bulk Qdrant retrieve to check which point IDs already exist."""
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


# ── SQL dual-write ─────────────────────────────────────────────────────────────

def _sql_conn():
    """Open a fresh pymssql connection (short-lived, closed by caller)."""
    return pymssql.connect(
        server=MSSQL_SERVER,
        user=MSSQL_USER,
        password=MSSQL_PASSWORD,
        database=MSSQL_DATABASE,
        login_timeout=10,
        as_dict=False,
    )


def _write_batches_to_sql(records: list[dict]) -> None:
    """
    Idempotent MERGE of ACH batch records into the ach_batches table.

    Each record must have:
        qdrant_point_id, filename, folder, company_name, company_id,
        entry_class, entry_description, effective_date (str YYYY-MM-DD or "unknown"),
        total_credit, total_debit, entry_count

    Uses MERGE so re-indexing the same file is a no-op for existing rows.
    """
    if not records:
        return

    merge_sql = """
        MERGE ach_batches AS tgt
        USING (SELECT %s AS qdrant_point_id) AS src
        ON tgt.qdrant_point_id = src.qdrant_point_id
        WHEN NOT MATCHED THEN INSERT (
            qdrant_point_id, filename, folder, company_name, company_id,
            entry_class, entry_description, effective_date,
            total_credit, total_debit, entry_count
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
    """

    try:
        with _sql_conn() as conn:
            cur = conn.cursor()
            for r in records:
                eff = r["effective_date"]
                # Convert "unknown" / malformed dates to NULL
                eff_val = eff if (eff and eff != "unknown" and len(eff) == 10) else None
                cur.execute(merge_sql, (
                    r["qdrant_point_id"],
                    r["qdrant_point_id"],
                    r["filename"],
                    r["folder"],
                    r["company_name"],
                    r["company_id"],
                    r["entry_class"],
                    r["entry_description"],
                    eff_val,
                    r["total_credit"],
                    r["total_debit"],
                    r["entry_count"],
                ))
            conn.commit()
        logger.info("SQL: upserted %d batch record(s) into ach_batches", len(records))
    except Exception as exc:
        # SQL failure must never block Qdrant indexing — log and continue.
        logger.error("SQL dual-write failed (%s) — Qdrant index is unaffected", exc)


# ── Core indexing logic ────────────────────────────────────────────────────────

def _index_file(key: str) -> None:
    """
    Fetch one ACH file from MinIO, parse it, embed all new batches in a
    single batched call, then upsert to Qdrant.
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

    keys_and_indices    = [(key, i) for i in range(len(batches))]
    already_indexed_ids = _get_already_indexed(keys_and_indices)

    # Collect all new batches that need embedding
    new_batches: list[tuple[int, dict, str]] = []  # (idx, batch, point_id)
    for i, batch in enumerate(batches):
        pid = _point_id(key, i)
        if pid not in already_indexed_ids:
            new_batches.append((i, batch, pid))

    if not new_batches:
        logger.info("All batches in %s already indexed", filename)
        return

    # Batch all summaries into a single embed() call — far faster than one-by-one
    summaries = [_make_summary(filename, b) for _, b, _ in new_batches]
    vectors   = list(embedder.embed(summaries))   # returns a generator; materialize

    points: list[PointStruct] = []
    for (i, batch, pid), vector in zip(new_batches, vectors):
        points.append(PointStruct(
            id=pid,
            vector=vector.tolist(),
            payload={
                "filename":             filename,
                "folder":               RAG_FOLDER,
                "company_name":         batch["company_name"],
                "company_id":           batch["company_id"],
                "entry_class":          batch["entry_class"],
                "effective_date":       batch["effective_date"],
                "total_credit_dollars": batch["total_credit"],
                "total_debit_dollars":  batch["total_debit"],
                "entry_count":          batch["entry_count"],
                "entry_description":    batch["entry_description"],
                "summary":              summaries[new_batches.index((i, batch, pid))],
                "indexed_at":           time.time(),
            },
        ))

    qdrant.upsert(collection_name=COLLECTION, points=points)
    logger.info("Indexed %d new batch(es) from %s into Qdrant", len(points), filename)

    # Dual-write structured data to MSSQL for SQL-based analytics.
    # Qdrant vectors are for semantic search; SQL is for aggregations/trends.
    sql_records = [
        {
            "qdrant_point_id":  pid,
            "filename":         filename,
            "folder":           RAG_FOLDER,
            "company_name":     batch["company_name"],
            "company_id":       batch["company_id"],
            "entry_class":      batch["entry_class"],
            "entry_description": batch["entry_description"],
            "effective_date":   batch["effective_date"],
            "total_credit":     batch["total_credit"],
            "total_debit":      batch["total_debit"],
            "entry_count":      batch["entry_count"],
        }
        for (_, batch, pid) in new_batches
    ]
    _write_batches_to_sql(sql_records)


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

def _sync_qdrant_to_sql() -> None:
    """
    Backfill SQL from existing Qdrant payloads.

    Runs once at startup after _reindex_existing.  Scrolls every point in the
    Qdrant collection and writes any that aren't already in ach_batches.
    This is a no-op for healthy restarts (MERGE ignores duplicates) and a
    full population on first run after this feature was introduced.
    """
    logger.info("SQL backfill: scanning Qdrant collection '%s' …", COLLECTION)
    records: list[dict] = []
    offset = None

    try:
        while True:
            result = qdrant.scroll(
                collection_name=COLLECTION,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, next_offset = result
            for p in points:
                pl = p.payload or {}
                records.append({
                    "qdrant_point_id":  str(p.id),
                    "filename":         pl.get("filename", ""),
                    "folder":           pl.get("folder", RAG_FOLDER),
                    "company_name":     pl.get("company_name", ""),
                    "company_id":       pl.get("company_id", ""),
                    "entry_class":      pl.get("entry_class", ""),
                    "entry_description": pl.get("entry_description", ""),
                    "effective_date":   pl.get("effective_date", ""),
                    "total_credit":     float(pl.get("total_credit_dollars", 0) or 0),
                    "total_debit":      float(pl.get("total_debit_dollars",  0) or 0),
                    "entry_count":      int(pl.get("entry_count",  0) or 0),
                })
            if next_offset is None:
                break
            offset = next_offset
    except Exception as exc:
        logger.error("SQL backfill: Qdrant scroll failed: %s", exc)
        return

    if not records:
        logger.info("SQL backfill: nothing in Qdrant yet")
        return

    _write_batches_to_sql(records)
    logger.info("SQL backfill complete — synced %d Qdrant point(s) to ach_batches", len(records))


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

    qdrant = QdrantClient(url=QDRANT_URL)
    s3     = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )

    loop     = asyncio.get_running_loop()   # get_event_loop() deprecated in 3.10+
    embedder = await loop.run_in_executor(
        None, lambda: TextEmbedding("BAAI/bge-small-en-v1.5")
    )

    _ensure_collection()
    _setup_minio_notification()

    def _startup_sync():
        _reindex_existing()     # embed any new files into Qdrant
        _sync_qdrant_to_sql()   # backfill SQL from all Qdrant payloads

    asyncio.ensure_future(loop.run_in_executor(None, _startup_sync))

    logger.info("Indexer ready – accepting requests (reindex + SQL sync running in background)")
    yield
    logger.info("Indexer shutting down")


app = FastAPI(title="ACH RAG Indexer", lifespan=lifespan)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def minio_webhook(request: Request, background_tasks: BackgroundTasks):
    """MinIO S3 event webhook – returns immediately, indexes in background."""
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
    q:         str   = Query(..., min_length=1, max_length=500, description="Natural language query"),
    limit:     int   = Query(20, ge=1, le=50),
    threshold: float = Query(0.55, ge=0.0, le=1.0),
):
    """Semantic search over the ACH index."""
    try:
        loop    = asyncio.get_running_loop()
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
                "entry_class":          r.payload.get("entry_class"),
                "effective_date":       r.payload.get("effective_date"),
                "total_credit_dollars": r.payload.get("total_credit_dollars"),
                "total_debit_dollars":  r.payload.get("total_debit_dollars"),
                "entry_count":          r.payload.get("entry_count"),
                "entry_description":    r.payload.get("entry_description"),
            }
            for r in results
        ]
        return {"query": q, "results": hits, "count": len(hits)}
    except Exception:
        logger.exception("Search failed")
        return JSONResponse({"error": "Search failed. Please try again."}, status_code=500)


@app.get("/aggregate")
async def aggregate():
    """
    Full company-level rollup by scrolling ALL indexed Archive documents.
    No semantic filtering — every document is included.
    """
    try:
        companies: dict[str, dict] = {}
        offset = None

        while True:
            result = qdrant.scroll(
                collection_name=COLLECTION,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, next_offset = result

            for p in points:
                pl   = p.payload or {}
                name = pl.get("company_name", "UNKNOWN")
                cid  = pl.get("company_id",   "")
                dt   = pl.get("effective_date", "")
                cr   = float(pl.get("total_credit_dollars", 0) or 0)
                db   = float(pl.get("total_debit_dollars",  0) or 0)
                ec   = int(pl.get("entry_count", 0) or 0)
                desc = pl.get("entry_description", "")
                ecls = pl.get("entry_class", "")

                if name not in companies:
                    companies[name] = {
                        "company_name":         name,
                        "company_id":           cid,
                        "file_count":           0,
                        "total_credit_dollars": 0.0,
                        "total_debit_dollars":  0.0,
                        "total_entries":        0,
                        "earliest_date":        dt,
                        "latest_date":          dt,
                        "descriptions":         set(),
                        "entry_classes":        set(),
                    }

                c = companies[name]
                c["file_count"]           += 1
                c["total_credit_dollars"]  = round(c["total_credit_dollars"] + cr, 2)
                c["total_debit_dollars"]   = round(c["total_debit_dollars"]  + db, 2)
                c["total_entries"]        += ec
                if desc: c["descriptions"].add(desc)
                if ecls: c["entry_classes"].add(ecls)
                if dt and dt < c["earliest_date"]: c["earliest_date"] = dt
                if dt and dt > c["latest_date"]:   c["latest_date"]   = dt

            if next_offset is None:
                break
            offset = next_offset

        rows = []
        for c in companies.values():
            total = c["total_credit_dollars"] + c["total_debit_dollars"]
            ratio = round(c["total_credit_dollars"] / c["total_debit_dollars"], 2) \
                    if c["total_debit_dollars"] > 0 else None
            rows.append({
                "company_name":         c["company_name"],
                "company_id":           c["company_id"],
                "file_count":           c["file_count"],
                "total_credit_dollars": c["total_credit_dollars"],
                "total_debit_dollars":  c["total_debit_dollars"],
                "total_volume_dollars": round(total, 2),
                "credit_debit_ratio":   ratio,
                "total_entries":        c["total_entries"],
                "date_range":           f"{c['earliest_date']} → {c['latest_date']}",
                "entry_descriptions":   sorted(c["descriptions"]),
                "entry_classes":        sorted(c["entry_classes"]),
            })

        rows.sort(key=lambda r: r["total_credit_dollars"], reverse=True)
        return {"companies": rows, "total_companies": len(rows)}

    except Exception:
        logger.exception("Aggregate failed")
        return JSONResponse({"error": "Aggregate failed."}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
