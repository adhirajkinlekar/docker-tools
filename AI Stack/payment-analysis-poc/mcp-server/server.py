"""
FastMCP Payment Analysis Server
Exposes tools for:
  - Listing/fetching ACH files from MinIO (S3-compatible object storage)
  - Querying client data from MSSQL
Transport: SSE on port 8000  →  http://localhost:8000/sse
"""

import os
import json
import logging
from fastmcp import FastMCP
from tools.blob_tools import BlobTools
from tools.db_tools import DbTools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Initialise dependencies ──────────────────────────────────────────────────

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

# ── FastMCP app ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="Payment Analysis MCP",
    instructions="""
You are connected to a payment analysis system.
Available capabilities:
  1. Browse Azure Blob Storage folders containing ACH payment files
  2. Fetch and parse ACH files – PII (account numbers, names, IDs) is auto-redacted
  3. Query the MSSQL database for client profiles and transaction history
  4. Cross-reference payment file data with database client records

Typical workflow for "Analyse payment files in <FOLDER>":
  1. fetch_and_parse_ach_files(folder_path=<FOLDER>)
  2. Extract company_names / company_identifiers from the parsed data
  3. get_client_info(company_identifiers=[...])
  4. For interesting clients: get_client_recent_transactions(client_id=...)
  5. Synthesise findings into a clear analysis
""",
)

# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def list_payment_folders(bucket: str = "payments") -> str:
    """
    List all virtual folders and files inside an Azure Blob container.
    Use this to discover available payment drop folders before fetching files.
    """
    logger.info("list_payment_folders(bucket=%s)", bucket)
    return blob.list_folders(bucket)


@mcp.tool()
def fetch_and_parse_ach_files(folder_path: str, bucket: str = "payments") -> str:
    """
    Fetch ACH/NACHA payment files from the specified folder in Azure Blob Storage,
    parse them according to the NACHA standard, and return structured non-PII data.

    PII fields (account numbers, individual names, individual IDs) are automatically
    replaced with [REDACTED]. Returned data includes:
      - Company names and identifiers
      - Transaction codes and descriptions
      - Credit/debit amounts
      - Effective dates
      - Batch and file-level totals

    Args:
        folder_path: Virtual directory path inside the container (e.g. "IMM-CTC-DROP")
        container_name: Blob container name (default: "payments")
    """
    logger.info("fetch_and_parse_ach_files(folder=%s, bucket=%s)", folder_path, bucket)
    return blob.fetch_and_parse_ach(bucket, folder_path)


@mcp.tool()
def get_client_info(company_identifiers: list[str]) -> str:
    """
    Query the MSSQL database for client records matching the given company
    identifiers (ACH company IDs or company names from batch headers).

    Returns client profile data including:
      - Account status (ACTIVE / SUSPENDED / CLOSED)
      - Credit limit and industry
      - Risk rating
      - Lifetime transaction totals (credits vs debits)

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
        days: How many days back to look (default: 30)
    """
    logger.info("get_client_recent_transactions(client=%s, days=%d)", client_id, days)
    return db.get_client_recent_transactions(client_id, days)


@mcp.tool()
def get_container_summary(bucket: str = "payments") -> str:
    """
    Get a high-level summary of all files in an Azure Blob container,
    grouped by folder. Useful for an overview before drilling into a folder.
    """
    logger.info("get_container_summary(bucket=%s)", bucket)
    return blob.get_container_summary(bucket)


@mcp.tool()
def list_all_clients() -> str:
    """
    Return the full list of clients registered in the database.
    Useful for understanding which companies are known clients before
    cross-referencing against ACH file data.
    """
    logger.info("list_all_clients()")
    return db.get_all_clients()


# ── Health endpoint (for Docker healthcheck) ──────────────────────────────────

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


async def health(_):
    return JSONResponse({"status": "ok"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from starlette.middleware import Middleware

    # Mount health check alongside FastMCP's SSE app
    sse_app = mcp.http_app(transport="sse")

    app = Starlette(
        routes=[Route("/health", health)],
        middleware=[],
    )

    # Compose: health at /health, MCP SSE at /sse
    from starlette.routing import Mount
    combined = Starlette(
        routes=[
            Route("/health", health),
            Mount("/", app=sse_app),
        ]
    )

    logger.info("Starting FastMCP server on 0.0.0.0:8000  (SSE at /sse)")
    uvicorn.run(combined, host="0.0.0.0", port=8000, log_level="info")
