"""
FastMCP Payment Analysis Server
────────────────────────────────
Exposes tools for:
  - Fetching/parsing ACH files from MinIO (Active folder)
  - SQL analytics over historical ACH batch data (query_ach_analytics)
  - Semantic similarity search over the Archive vector index (rag_search_payments)
  - Querying client records and transaction history from MSSQL
Transport: SSE on port 8000  →  http://localhost:8000/sse
"""

import os
import json
import logging
import httpx
from typing import Annotated
from fastmcp import FastMCP
from fastmcp.utilities.types import AnyUrl
from pydantic import Field
from tools.blob_tools import BlobTools
from tools.db_tools import DbTools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Initialise dependencies ──────────────────────────────────────────────────

INDEXER_URL         = os.environ.get("INDEXER_URL",         "http://indexer:8001")
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
You are connected to three live data sources for payment analysis:

  Active folder   → fetch_and_parse_ach_files()              current in-flight ACH files
  Archive folder  → query_ach_analytics(group_by, filters)   SQL aggregations over all historical ACH data
                    rag_search_payments(query)                semantic search for narrative/similarity queries
  Client database → list_all_clients()                       all registered clients
                    get_client_info(identifiers)              profile + status + limits
                    get_client_recent_transactions(id)        transaction history

TOOL SELECTION RULES — follow these exactly:

  Use query_ach_analytics for ANY question involving:
    numbers, totals, rankings, comparisons, trends, date ranges, anomalies,
    "how much", "which companies", "highest", "lowest", "over time", "by month"
    Examples: "total credits by company", "monthly trend for HARBOR LOGISTICS",
              "companies with debit-heavy ratios", "Q4 2025 volumes"

  Use rag_search_payments ONLY for:
    narrative similarity ("find batches that look like X"),
    when the user describes a pattern in words rather than numbers.
    Do NOT use it for aggregations or questions that have a definite numeric answer.

These tools are designed to be combined. A single tool call rarely gives a
complete answer. After each result, consider what other tools fill the gaps.

If a tool returns {"error": "..."}, stop and report it. Do not fabricate data.
""",
)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def fetch_and_parse_ach_files(
    folder_path: Annotated[str, Field(default="Active", description='Virtual folder. Always "Active" — do not pass "Archive".')] = "Active",
    bucket:      Annotated[str, Field(default="payments", description="MinIO bucket name.")] = "payments",
) -> str:
    """
    Fetch and parse all ACH/NACHA payment files from the Active folder.

    Returns structured, non-PII batch data: company name, ACH company ID,
    transaction codes, credit/debit totals, effective date, and entry count.
    PII fields (account numbers, individual names, IDs) are replaced with [REDACTED].

    After calling this tool:
      - Call get_client_info() with the company names/IDs found to check account
        status, credit limits, and risk rating for each company
      - Call get_client_recent_transactions() for any company showing unusual
        debit spikes, high volumes, or mismatched batch sizes
      - Cross-reference with get_archive_summary() to compare against historical baselines
    """
    try:
        folder_path = _validate_folder_path(folder_path)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    logger.info("fetch_and_parse_ach_files(folder=%s, bucket=%s)", folder_path, bucket)
    return blob.fetch_and_parse_ach(bucket, folder_path)


@mcp.tool()
def get_client_info(
    company_identifiers: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=20,
            description="List of ACH company IDs (10-digit strings) or company names. Max 20 items.",
        ),
    ],
) -> str:
    """
    Look up client records in the database by company name or ACH company ID.

    Returns account status (ACTIVE / SUSPENDED / CLOSED), credit limit, industry,
    risk rating, and lifetime transaction totals for each matched client.

    After calling this tool:
      - If a client is SUSPENDED or CLOSED, call get_client_recent_transactions()
        to understand what led to that status
      - If a client is ACTIVE but high-risk, call get_client_recent_transactions()
        to review recent activity
      - Cross-reference the client's ACH company ID against Active file data to
        check whether they are currently submitting payments
    """
    # Guard: cap list size to prevent oversized IN clauses
    if len(company_identifiers) > 20:
        company_identifiers = company_identifiers[:20]
    logger.info("get_client_info(identifiers=%s)", company_identifiers)
    return db.get_client_info(company_identifiers)


@mcp.tool()
def get_client_recent_transactions(
    client_id: Annotated[str, Field(min_length=1, max_length=50, description="Internal client ID from get_client_info.")],
    days:      Annotated[int, Field(default=30, ge=1, le=365,    description="Lookback window in days. Use 90 or 365 for trend analysis.")] = 30,
) -> str:
    """
    Retrieve transaction history for a specific client from the database.

    Returns individual transactions with date, amount, type (CREDIT/DEBIT),
    status (COMPLETED / REVERSED / FAILED), and a net-position summary.
    Use days=90 or days=365 for trend analysis.

    Combine with fetch_and_parse_ach_files or rag_search_payments to compare
    the database transaction view against the originating ACH batch data.
    """
    days = max(1, min(days, 365))
    logger.info("get_client_recent_transactions(client=%s, days=%d)", client_id, days)
    return db.get_client_recent_transactions(client_id, days)


@mcp.tool()
def query_ach_analytics(
    group_by: Annotated[
        str,
        Field(
            default="company",
            description=(
                'Dimension to aggregate by. '
                '"company" — one row per company (default, use for overviews and rankings). '
                '"month" — one row per calendar month (use for trends and seasonality). '
                '"entry_class" — one row per ACH entry class code (PPD/CCD/CTX etc). '
                '"company_month" — one row per company per month (use for per-company trends). '
                '"company_entry_class" — one row per company per entry class.'
            ),
        ),
    ] = "company",
    company_name: Annotated[
        str | None,
        Field(default=None, description="Filter to a specific company (partial match, case-insensitive)."),
    ] = None,
    date_from: Annotated[
        str | None,
        Field(default=None, description="Start of date range, ISO format YYYY-MM-DD (inclusive)."),
    ] = None,
    date_to: Annotated[
        str | None,
        Field(default=None, description="End of date range, ISO format YYYY-MM-DD (inclusive)."),
    ] = None,
    entry_class: Annotated[
        str | None,
        Field(default=None, description='Filter by ACH entry class code, e.g. "PPD", "CCD", "CTX".'),
    ] = None,
    order_by: Annotated[
        str,
        Field(
            default="total_credit",
            description=(
                'Column to sort by (descending). '
                '"total_credit" | "total_debit" | "total_volume" | "credit_debit_ratio" | "date"'
            ),
        ),
    ] = "total_credit",
    limit: Annotated[
        int,
        Field(default=50, ge=1, le=200, description="Maximum rows to return."),
    ] = 50,
) -> str:
    """
    SQL-powered analytics over all historical ACH batch data in the Archive folder.

    This is the RIGHT tool for any question involving numbers, aggregations,
    rankings, trends, date ranges, or anomaly detection.  It runs a real SQL
    GROUP BY query — not a vector scan — so results are exact and exhaustive.

    Examples of what this tool answers well:
      • "What are the total credits and debits for each company?"
        → group_by="company"
      • "Show me HARBOR LOGISTICS month by month"
        → group_by="company_month", company_name="HARBOR LOGISTICS"
      • "Which companies had the highest debit volume in Q4 2025?"
        → group_by="company", date_from="2025-10-01", date_to="2025-12-31", order_by="total_debit"
      • "Show monthly payment trends across all companies"
        → group_by="month", order_by="date"
      • "What entry classes does ACME MANUFACTURING use?"
        → group_by="company_entry_class", company_name="ACME"
      • "Which companies have a credit/debit ratio below 1 (debit-heavy)?"
        → group_by="company", order_by="credit_debit_ratio" (look for ratio < 1 in results)

    After calling this tool:
      - For companies with anomalous ratios or spike months, call
        get_client_info() to check account status and risk rating
      - Call get_client_recent_transactions() to compare ACH batch data against
        the database transaction records for the same period
      - Call fetch_and_parse_ach_files() to check whether flagged companies
        are still active today
    """
    logger.info(
        "query_ach_analytics(group_by=%s, company=%s, from=%s, to=%s, class=%s, order=%s, limit=%d)",
        group_by, company_name, date_from, date_to, entry_class, order_by, limit,
    )
    return db.query_ach_analytics(
        group_by=group_by,
        company_name=company_name,
        date_from=date_from,
        date_to=date_to,
        entry_class=entry_class,
        order_by=order_by,
        limit=limit,
    )


@mcp.tool()
async def rag_search_payments(
    query: Annotated[str, Field(min_length=3, max_length=500, description="Natural language description of what you're looking for.")],
    limit: Annotated[int, Field(default=20, ge=1, le=50,       description="Maximum results to return.")] = 20,
) -> str:
    """
    Semantic similarity search over pre-indexed ACH batch summaries (Archive folder).

    Use this ONLY for narrative or pattern-matching queries — when the user
    describes a situation in words rather than asking for numbers.
    Examples: "batches that look like fraud", "unusual patterns in logistics companies",
              "find batches similar to the PINNACLE TRADING spike".

    Do NOT use this tool for aggregation, totals, rankings, or date-range questions.
    For those, use query_ach_analytics() which runs an exact SQL query.

    Returns individual batch records with a cosine similarity score — not
    exhaustive, only semantically similar results above the relevance threshold.

    After calling this tool:
      - Call query_ach_analytics() with company_name filter to get exact numbers
        for any company that appears suspicious in the results
      - Call get_client_info() to check account status and risk rating
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
    except Exception:
        logger.exception("rag_search_payments failed")
        return json.dumps({"error": "Search service unavailable. Please try again later."})


@mcp.tool()
def list_all_clients() -> str:
    """
    Return all registered clients from the database with their current status.

    Returns company name, ACH company ID, account status (ACTIVE / SUSPENDED /
    CLOSED), industry, and risk rating for every client.

    After calling this tool:
      - Call get_client_info() on specific clients for full profile detail
      - Call fetch_and_parse_ach_files() to check whether any SUSPENDED or
        CLOSED clients are still submitting ACH batches (a compliance risk)
      - Call get_archive_summary() to compare known clients against historical
        ACH file data
    """
    logger.info("list_all_clients()")
    return db.get_all_clients()


# ── Health endpoint ───────────────────────────────────────────────────────────

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount


async def health(_):
    return JSONResponse({"status": "ok"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    sse_app  = mcp.http_app(transport="sse")
    combined = Starlette(
        routes=[
            Route("/health", health),
            Mount("/", app=sse_app),
        ]
    )

    logger.info("Starting FastMCP server on 0.0.0.0:8000  (SSE at /sse)")
    uvicorn.run(combined, host="0.0.0.0", port=8000, log_level="info")
