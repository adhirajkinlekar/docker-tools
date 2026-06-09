# Payment Analysis POC

A fully Dockerised proof-of-concept that lets you query ACH/NACHA payment files and client data using natural language. Type a question in the web UI; an LLM agent answers by calling MCP tools or a RAG index, depending on which folder you're asking about.

---

## Architecture

```
Browser
  │
  │  HTTP + SSE (streaming)
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Web UI  (FastAPI + SSE · port 3000)                                │
│  • Provider selector: Auto / External / Ollama                      │
│  • Semantic response cache (Qdrant)                                 │
└────────┬──────────────────────────────────────────┬────────────────┘
         │ MCP over SSE                             │ OpenAI-compat API
         ▼                                          ▼
┌─────────────────────────┐          ┌──────────────────────────────┐
│  FastMCP Server         │          │  LLM Provider                │
│  (port 8000)            │          │  • GitHub Models (primary)   │
│                         │          │  • Ollama / qwen2.5:3b       │
│  MCP Tools:             │          │    (local fallback)          │
│  • list_payment_folders │          └──────────────────────────────┘
│  • fetch_and_parse_ach  │
│  • get_client_info      │──boto3──▶ MinIO (port 9000)
│  • get_client_txns      │           payments bucket
│  • get_container_summary│             ├── IMM-CTC-DROP/   ← MCP
│  • list_all_clients     │──pymssql──▶ ├── CORP-BATCH/     ← MCP
│  • rag_search_payments  │──HTTP───▶   ├── HEALTH-PAYMENTS/← MCP
└─────────────────────────┘  indexer    └── Archive/        ← RAG
                                │
                     ┌──────────┴───────────┐
                     │  ACH Indexer         │
                     │  (port 8001)         │
                     │  • MinIO webhook     │
                     │  • fastembed         │
                     │  • Qdrant upsert     │
                     └──────────┬───────────┘
                                │
               ┌────────────────┼────────────────┐
               ▼                ▼                ▼
         ┌──────────┐   ┌─────────────┐  ┌──────────────┐
         │  Qdrant  │   │    MinIO    │  │  SQL Server  │
         │ port 6333│   │  port 9000  │  │  port 1433   │
         │          │   │             │  │  PaymentDB   │
         │ach_index │   │  payments/  │  │  clients +   │
         │response_ │   │  bucket     │  │  transactions│
         │cache     │   └─────────────┘  └──────────────┘
         └──────────┘
```

### Dual retrieval modes

| Folder | How it works |
|---|---|
| `IMM-CTC-DROP`, `CORP-BATCH`, `HEALTH-PAYMENTS` | **MCP tools** — agent fetches raw ACH files from MinIO, parses them, queries the database |
| `Archive` | **RAG** — files are indexed into Qdrant at upload time; agent queries the vector index with `rag_search_payments` |

The LLM picks the right path automatically based on the folder name.

### Semantic response cache

Every agent response is embedded and stored in Qdrant's `response_cache` collection. Semantically similar follow-up questions are answered instantly from cache (configurable TTL, default 24 h).

---

## Quick Start

### Prerequisites

- Docker + Docker Compose v2
- One of: a GitHub PAT, Groq API key, OpenAI API key — or just Ollama (fully local, no key needed)

### 1. Configure

```bash
cd payment-analysis-poc
cp .env.example .env
```

Edit `.env` and set your provider. Examples:

```env
# GitHub Models (free with a GitHub account — no scopes needed on the PAT)
OPENAI_API_KEY=ghp_...
OPENAI_BASE_URL=https://models.inference.ai.azure.com
AGENT_MODEL=gpt-4o

# Groq (fast, free hosted tier)
OPENAI_API_KEY=gsk_...
OPENAI_BASE_URL=https://api.groq.com/openai/v1
AGENT_MODEL=llama-3.3-70b-versatile

# Ollama only (no external API, fully local)
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://ollama:11434/v1
AGENT_MODEL=qwen2.5:3b
```

### 2. Build and start

```bash
docker compose up --build -d
```

First run pulls ~2–4 GB of models (Ollama + fastembed). Subsequent starts are fast.

### 3. Seed sample data

```bash
docker compose run --rm --no-deps seed
```

Uploads 42 ACH files (Jan–Jun 2026) across all four folders. The indexer will automatically index the `Archive/` files via the MinIO webhook.

### 4. Open the UI

[http://localhost:3000](http://localhost:3000)

---

## Example Queries

```
Analyse payment files in IMM-CTC-DROP
Are there any suspended clients in CORP-BATCH?
Show me recent transactions for PAYROLL EXPRESS
Summarise all payment activity in the Archive folder
Which companies in Archive had unusual debit spikes?
List all clients
```

---

## Project Structure

```
payment-analysis-poc/
├── docker-compose.yml
├── .env.example
│
├── mcp-server/                   # FastMCP server (SSE · port 8000)
│   ├── server.py                 # Tool definitions
│   ├── tools/
│   │   ├── blob_tools.py         # MinIO / S3 operations
│   │   └── db_tools.py           # MSSQL queries
│   └── utils/
│       └── ach_parser.py         # NACHA parser — PII redacted here
│
├── indexer/                      # ACH RAG Indexer (port 8001)
│   ├── indexer.py                # Webhook + search + startup reindex
│   ├── Dockerfile
│   └── requirements.txt
│
├── ui/                           # Web UI (FastAPI + SSE · port 3000)
│   ├── main.py                   # Agent loop, provider switching, SSE streaming
│   ├── cache.py                  # Semantic response cache (Qdrant)
│   ├── static/
│   │   └── index.html            # Chat UI
│   ├── Dockerfile
│   └── requirements.txt
│
├── db/
│   ├── Dockerfile
│   ├── entrypoint.sh
│   └── init.sql                  # Schema + 5 clients + 6 months of transactions
│
└── seed/
    └── seed.py                   # Generates valid NACHA files + uploads to MinIO
```

---

## Seed Data

### MinIO (`payments` bucket)

| Folder | Files | Companies | Pattern |
|---|---|---|---|
| `IMM-CTC-DROP` | 12 | ACME CORP, PAYROLL EXPRESS | Steady growth; March bonus spike for Payroll |
| `CORP-BATCH` | 12 | GLOBAL TECH INC, RETAIL SOLUTIONS | Retail degrades Jan→Jun, ends SUSPENDED |
| `HEALTH-PAYMENTS` | 6 | HEALTH SERVICES | Higher Q1 (winter billing), plateau Q2 |
| `Archive` | 12 | NEXUS PAYMENTS, DIGITAL MERCHANTS | Merchants have a sharp debit spike in April |

All files span January–June 2026 with intentional anomalies for the agent to detect.

### MSSQL (`PaymentDB`)

| Client | Status | Risk | Credit Limit |
|---|---|---|---|
| ACME CORP | ACTIVE | LOW | $500,000 |
| GLOBAL TECH INC | ACTIVE | LOW | $1,000,000 |
| RETAIL SOLUTIONS | **SUSPENDED** | **HIGH** | $250,000 |
| HEALTH SERVICES | ACTIVE | LOW | $750,000 |
| PAYROLL EXPRESS | ACTIVE | MEDIUM | $2,000,000 |

---

## MCP Tools Reference

| Tool | Description |
|---|---|
| `list_payment_folders` | List all folders in the payments bucket |
| `fetch_and_parse_ach_files` | Fetch + parse ACH files from a folder; PII auto-redacted |
| `get_client_info` | Look up clients by ACH company ID or name |
| `get_client_recent_transactions` | Transaction history for a specific client |
| `get_container_summary` | High-level bucket overview grouped by folder |
| `list_all_clients` | All clients in the database |
| `rag_search_payments` | Semantic search over the pre-indexed Archive folder |

---

## Provider Switching

The UI has three modes selectable per message:

| Mode | Behaviour |
|---|---|
| **Auto** | Tries the primary provider; falls back to Ollama on quota/rate errors |
| **External** | Always uses the configured primary provider (GitHub Models, Groq, OpenAI) |
| **Ollama** | Always uses the local Ollama container — fully private, no data leaves your machine |

Configure the primary provider and fallback model in `.env`:

```env
AGENT_MODEL=gpt-4o          # primary
FALLBACK_MODEL=qwen2.5:3b   # Ollama fallback
```

---

## Services & Ports

| Service | Host port | Notes |
|---|---|---|
| Web UI | 3000 | Main chat interface |
| FastMCP server | 8000 | SSE endpoint at `/sse` |
| ACH Indexer | 8001 | Webhook at `/webhook`, search at `/search` |
| MinIO API | 19000 | S3-compatible |
| MinIO Console | 19001 | Web UI for browsing buckets |
| SQL Server | 1433 | `PaymentDB` database |
| Qdrant | 6334 | REST API |
| Ollama | 11435 | OpenAI-compatible API |
