"""
FastMCP Payment Analysis Server
Exposes tools for:
  - Listing/fetching ACH files from MinIO (S3-compatible object storage)
  - Querying client data from MSSQL
  - Semantic RAG search over the Archive index
Transport: SSE on port 8000  →  http://localhost:8000/sse

Improvements over v1:
  • rag_search_payments is async (uses httpx instead of blocking requests)
  • RAG search passes score_threshold so only relevant results are returned
  • Input validation on folder_path to prevent path traversal
  • Secrets are required (no silent defaults)
"""

import os
import json
import logging
import httpx
from fastmcp import FastMCP
from tools.blob_tools import BlobTools
from tools.db_tools import DbTools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Initialise dependencies ──────────────────────────────────────────────────

INDEXER_URL = os.environ.get("INDEXER_URL", "http://indexer:8001")
RAG_SCORE_THRESHOLD = float(os.environ.get("RAG_SCORE_THRESHOLD", "0.55"))

blob = BlobTools(
    endpoint=os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
)

db = DbTools(
    server=os.environ["MSSQL_SERVER"],
    database=os.environ["MSSQL_DATABASE"],
    username=os.environ["MSSQL_USER"],
    password=os.environ["MSSQL_PASSWORD"],
)

# ── Input validation ──────────────────────────────────────────────────────────

def _validate_folder_path(folder_path: str) -> str:
    """Sanitise folder path – reject path traversal attempts."""
    cleaned = folder_path.strip().lstrip("/").replace("..", "")
    if not cleaned:
        raise ValueError("folder_path must not be empty")
    return cleaned


# ── FastMCP app ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="Payment Analysis MCP",
    instructions="""
You are connected to a payment analysis system with three data sources:

1. MinIO object storage – ACH/NACHA payment files in virtual folders
   Tools: list_payment_folders, fetch_and_parse_ach_files, get_container_summary

2. MSSQL database – client profiles and transaction history
   Tools: get_client_info, get_client_recent_transactions, list_all_clients

3. Qdrant vector index – pre-indexed ACH batch summaries for the Archive folder only
   Tool: rag_search_payments  ← use ONLY for Archive queries

If a tool returns {"error": "..."}, report it to the user and stop.
Do not attempt to fill in data from memory or retry the same failed tool call.
""",
)

# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def list_payment_folders(bucket: str = "payments") -> str:
    """
    List all virtual folders and files inside a MinIO bucket.
    Use this to discover available payment drop folders before fetching files.
    """
    logger.info("list_payment_folders(bucket=%s)", bucket)
    return blob.list_folders(bucket)


@mcp.tool()
def fetch_and_parse_ach_files(folder_path: str, bucket: str = "payments") -> str:
    """
    Fetch ACH/NACHA payment files from the specified folder in MinIO,
    parse them according to the NACHA standard, and return structured non-PII data.

    PII fields (account numbers, individual names, individual IDs) are automatically
    replaced with [REDACTED]. Returned data includes company names, identifiers,
    transaction codes, credit/debit amounts, effective dates, and batch totals.

    Use this tool for any folder EXCEPT Archive (use rag_search_payments for Archive).

    Args:
        folder_path: Virtual directory path inside the container (e.g. "Active")
        bucket: Bucket name (default: "payments")
    """
    try:
        folder_path = _validate_folder_path(folder_path)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    logger.info("fetch_and_parse_ach_files(folder=%s, bucket=%s)", folder_path, bucket)
    return blob.fetch_and_parse_ach(bucket, folder_path)


@mcp.tool()
def get_client_info(company_identifiers: list[str]) -> str:
    """
    Query the MSSQL database for client records matching the given company
    identifiers (ACH company IDs or company names from batch headers).

    Returns client profile data including account status (ACTIVE / SUSPENDED / CLOSED),
    credit limit, industry, risk rating, and lifetime transaction totals.

    Args:
        company_identifiers: List of ACH company IDs or company names to look up
    """
    logger.info("get_client_info(identifiers=%s)", company_identifiers)
    return db.get_client_info(company_identifiers)


@mcp.tool()
def get_client_recent_transactions(client_id: str, days: int = 30) -> str:
    """
    Retrieve recent transaction history for a specific client from the database.

    Returns individual transactions with dates, amounts, types (CREDIT/DEBIT),
    statuses, and a net-position summary for the period.

    Args:
        client_id: The internal client ID (from get_client_info results)
        days: How many days back to look (default: 30, max: 365)
    """
    days = max(1, min(days, 365))  # clamp to safe range
    logger.info("get_client_recent_transactions(client=%s, days=%d)", client_id, days)
    return db.get_client_recent_transactions(client_id, days)


@mcp.tool()
def get_container_summary(bucket: str = "payments") -> str:
    """
    Get a high-level summary of all files in a MinIO bucket, grouped by folder.
    Useful for an overview before drilling into a specific folder.
    """
    logger.info("get_container_summary(bucket=%s)", bucket)
    return blob.get_container_summary(bucket)


@mcp.tool()
async def rag_search_payments(query: str, limit: int = 10) -> str:
    """
    Semantic vector search over pre-indexed ACH summaries from the Archive folder.

    Use this tool INSTEAD of fetch_and_parse_ach_files when the user asks about
    the 'Archive' folder. Files there are indexed at upload time and can be
    queried with natural language – no need to fetch raw files.

    Returns ranked batch summaries with company name, date, credit/debit totals,
    and relevance score. Only results above the relevance threshold are returned.

    Args:
        query: Natural language query, e.g. "high credit volume June 2026",
               "DIGITAL MERCHANTS debits", "companies with anomalous batches"
        limit: Maximum number of results to return (default: 10, max: 50)
    """
    limit = max(1, min(limit, 50))
    logger.info("rag_search_payments(query='%s', limit=%d)", query, limit)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{INDEXER_URL}/search",
                params={"q": query, "limit": limit, "threshold": RAG_SCORE_THRESHOLD},
            )
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)
    except httpx.HTTPStatusError as exc:
        logger.exception("rag_search_payments HTTP error")
        return json.dumps({"error": f"Search service returned {exc.response.status_code}"})
    except Exception as exc:
        logger.exception("rag_search_payments failed")
        return json.dumps({"error": "Search service unavailable. Please try again later."})


@mcp.tool()
def list_all_clients() -> str:
    """
    Return a summary list of clients registered in the database.
    Useful for understanding which companies are known clients before
    cross-referencing against ACH file data.

    Returns company name, ACH company ID, account status, industry, and risk rating.
    Does not include transaction detail – use get_client_recent_transactions for that.
    """
    logger.info("list_all_clients()")
    return db.get_all_clients()


# ── Health endpoint (for Docker healthcheck) ──────────────────────────────────

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount


async def health(_):
    return JSONResponse({"status": "ok"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    sse_app = mcp.http_app(transport="sse")

    combined = Starlette(
        routes=[
            Route("/health", health),
            Mount("/", app=sse_app),
        ]
    )

    logger.info("Starting FastMCP server on 0.0.0.0:8000  (SSE at /sse)")
    uvicorn.run(combined, host="0.0.0.0", port=8000, log_level="info")
