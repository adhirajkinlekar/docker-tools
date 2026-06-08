# Payment Analysis POC

A Dockerised proof-of-concept where a Claude-powered agent answers natural-language queries about ACH payment files stored in Azure Blob Storage, cross-referenced against an MSSQL client database.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  docker-compose network                                          │
│                                                                  │
│  ┌─────────────────┐          ┌──────────────────────────────┐   │
│  │   Claude Agent  │──SSE──▶  │      FastMCP Server          │   │
│  │  (agent/)       │          │      (mcp-server/)           │   │
│  └─────────────────┘          │                              │   │
│                               │  Tools:                      │   │
│                               │  • list_payment_folders      │   │
│                               │  • fetch_and_parse_ach_files │   │
│                               │  • get_client_info           │   │
│                               │  • get_client_recent_txns    │   │
│                               │  • get_container_summary     │   │
│                               │  • list_all_clients          │   │
│                               └──────┬──────────────┬────────┘   │
│                                      │              │            │
│                              Azure SDK           pymssql         │
│                                      ▼              ▼            │
│                          ┌──────────────┐  ┌────────────────┐   │
│                          │   Azurite    │  │   SQL Server   │   │
│                          │  (Blob emu.) │  │   (PaymentDB)  │   │
│                          │  port 10000  │  │   port 1433    │   │
│                          └──────────────┘  └────────────────┘   │
│                                                                  │
│   ┌──────────┐  (runs once, then exits)                          │
│   │   Seed   │──uploads ACH files──▶ Azurite                    │
│   └──────────┘                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### Key design decisions

| Concern | Choice | Reason |
|---|---|---|
| MCP framework | **FastMCP 2.x** | Decorator-based tools, minimal boilerplate |
| MCP transport | **SSE** over HTTP | Works across Docker service boundaries |
| Blob storage | **Azurite** locally, real Azure in prod | Drop-in emulator, same SDK |
| DB driver | **pymssql** | No ODBC setup needed inside Alpine/Debian |
| PII handling | Redacted in parser before returning to agent | Data never leaves the MCP server unmasked |
| Agent model | **claude-sonnet-4-6** (configurable via `AGENT_MODEL`) | Best reasoning for multi-step tool use |

---

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose v2
- An Anthropic API key

### 2. Configure environment

```bash
cd payment-analysis-poc
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY
```

### 3. Start all services

```bash
docker compose up --build
```

This will:
1. Start Azurite (blob storage emulator)
2. Start SQL Server and run `db/init.sql` (schema + seed data)
3. Start the FastMCP server on port 8000
4. Run the seed container (uploads sample ACH files, then exits)
5. Start the interactive agent

### 4. Interact with the agent

Once you see `Agent ▶` in the logs:

```bash
docker compose run --rm agent
```

Example queries:
```
You ▶  Analyse payment files in IMM-CTC-DROP
You ▶  List all payment folders
You ▶  Are there any suspended clients in the CORP-BATCH folder?
You ▶  Show me recent transactions for PAYROLL EXPRESS
You ▶  clients
```

---

## Project Structure

```
payment-analysis-poc/
├── docker-compose.yml
├── .env.example
│
├── mcp-server/                 # FastMCP server
│   ├── server.py               # Tool definitions + SSE server
│   ├── tools/
│   │   ├── blob_tools.py       # Azure Blob Storage operations
│   │   └── db_tools.py         # MSSQL queries
│   └── utils/
│       └── ach_parser.py       # NACHA parser – PII is redacted here
│
├── agent/
│   └── agent.py                # Interactive Claude agent (MCP client)
│
├── db/
│   ├── Dockerfile              # Custom MSSQL image with init entrypoint
│   ├── entrypoint.sh           # Waits for SQL Server, then runs init.sql
│   └── init.sql                # Schema + seed data (5 clients, ~100 transactions)
│
└── seed/
    └── seed.py                 # Generates valid NACHA files + uploads to Azurite
```

---

## Seed Data

### Azure Blob (`payments` container)

| Folder | Files | Companies |
|---|---|---|
| `IMM-CTC-DROP` | 2 ACH files | ACME CORP, PAYROLL EXPRESS |
| `CORP-BATCH` | 2 ACH files | GLOBAL TECH INC, RETAIL SOLUTIONS |
| `HEALTH-PAYMENTS` | 1 ACH file | HEALTH SERVICES LLC |

### MSSQL (`PaymentDB.clients`)

| Client | Status | Risk | Credit Limit |
|---|---|---|---|
| ACME CORP | ACTIVE | LOW | $500,000 |
| GLOBAL TECH INC | ACTIVE | LOW | $1,000,000 |
| RETAIL SOLUTIONS | **SUSPENDED** | **HIGH** | $250,000 |
| HEALTH SERVICES LLC | ACTIVE | LOW | $750,000 |
| PAYROLL EXPRESS | ACTIVE | MEDIUM | $2,000,000 |

RETAIL SOLUTIONS is intentionally suspended with failed transactions — a good test for the agent's risk analysis.

---

## MCP Tools Reference

| Tool | Description |
|---|---|
| `list_payment_folders` | List virtual folders in a blob container |
| `fetch_and_parse_ach_files` | Fetch + parse ACH files from a folder; PII auto-redacted |
| `get_client_info` | Look up clients by ACH company ID or name |
| `get_client_recent_transactions` | Transaction history for a specific client |
| `get_container_summary` | High-level blob container overview |
| `list_all_clients` | All clients in the database |

---

## Connecting to a Real Azure Blob / SQL Server

Update `.env`:

```env
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
MSSQL_SERVER=your-server.database.windows.net
MSSQL_DATABASE=PaymentDB
MSSQL_USER=youruser
MSSQL_SA_PASSWORD=yourpassword
```

No code changes required — everything reads from environment variables.

---

## Swap the Agent Model

Set `AGENT_MODEL` in docker-compose or `.env`:

```env
AGENT_MODEL=claude-opus-4-6
```

Or to use **Ollama** locally instead of Claude API, replace the `anthropic.Anthropic` call in `agent/agent.py` with an OpenAI-compatible client pointed at your Ollama endpoint (`http://ollama:11434/v1`).
