"""
Payment Analysis – Web UI backend
──────────────────────────────────
Provider strategy:
  • "auto"     – try primary (external API), fall back to Ollama on quota/rate errors
  • "primary"  – always use the external provider (Groq / OpenAI / GitHub Models)
  • "ollama"   – always use the local Ollama container

Auto-fallback triggers on: 429 rate-limit, 413 token-limit, quota_exceeded.
A provider_switch SSE event is emitted so the UI can notify the user.
"""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import AsyncGenerator, Literal

import openai
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.sse import sse_client

from cache import SemanticCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MCP_SERVER_URL   = os.environ.get("MCP_SERVER_URL",   "http://mcp-server:8000/sse")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY",   "") or os.environ.get("GITHUB_TOKEN", "")
OPENAI_BASE_URL  = os.environ.get("OPENAI_BASE_URL",  "")
PRIMARY_MODEL    = os.environ.get("AGENT_MODEL",      "gpt-4o")
OLLAMA_BASE_URL  = os.environ.get("OLLAMA_BASE_URL",  "http://ollama:11434/v1")
OLLAMA_MODEL     = os.environ.get("FALLBACK_MODEL",   "llama3.1:8b")
QDRANT_URL       = os.environ.get("QDRANT_URL",       "http://qdrant:6333")

TOOL_OUTPUT_MAX_CHARS = 2_000

# Error conditions that warrant switching to the fallback provider
_FALLBACK_HTTP_CODES  = {429, 413}
_FALLBACK_ERROR_CODES = {"tokens_limit_reached", "rate_limit_exceeded",
                         "insufficient_quota",   "quota_exceeded"}

SYSTEM_PROMPT = """
You are a payment operations analyst with direct access to:
  • MinIO object storage – folders containing ACH/NACHA payment files (Jan–Jun 2026)
  • An MSSQL database – client profiles and 6 months of transaction history
  • A semantic vector index – pre-indexed summaries of Archive files

ACH files contain PII (account numbers, names, individual IDs) which are
automatically redacted. You will see [REDACTED] in those fields – never
attempt to reconstruct them.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL ROUTING – choose based on the folder requested:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

▸ IMM-CTC-DROP, CORP-BATCH, HEALTH-PAYMENTS  →  MCP tools
  1. fetch_and_parse_ach_files(folder_path=<FOLDER>) to get file summaries
  2. get_client_info(company_identifiers=[...]) to look up client records
  3. get_client_recent_transactions(client_id=...) for flagged/high-value clients
  4. Synthesise: file summary, MoM trend, per-company breakdown, risk indicators

▸ Archive  →  RAG tool
  1. rag_search_payments(query=<natural language question>) to retrieve relevant
     pre-indexed ACH batch summaries via semantic similarity
  2. Synthesise the returned hits into a structured analysis
  Note: do NOT call fetch_and_parse_ach_files for Archive

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For all analyses, produce a structured report covering:
  • File / batch summary (count, date range, total credits/debits)
  • Month-on-month trend (Jan–Jun 2026 data is available)
  • Per-company breakdown and client status
  • Risk indicators (suspended accounts, debit spikes, return growth)
  • Recommendations for any anomalies detected

Be concise but thorough. Use markdown formatting.
""".strip()

# ── OpenAI clients ────────────────────────────────────────────────────────────

def _make_client(api_key: str, base_url: str | None) -> openai.AsyncOpenAI:
    kwargs: dict = {"api_key": api_key or "none"}
    if base_url:
        kwargs["base_url"] = base_url
    return openai.AsyncOpenAI(**kwargs)

oai_primary  = _make_client(OPENAI_API_KEY, OPENAI_BASE_URL or None)
oai_ollama   = _make_client("ollama",        OLLAMA_BASE_URL)

cache = SemanticCache(QDRANT_URL)

# ── In-memory sessions ────────────────────────────────────────────────────────

sessions: dict[str, list[dict]] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

ProviderChoice = Literal["auto", "primary", "ollama"]


def _mcp_to_oai_tools(tools) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name":        t.name,
                "description": t.description or "",
                "parameters":  t.inputSchema,
            },
        }
        for t in tools
    ]


def _sse(event_type: str, **kwargs) -> str:
    return f"data: {json.dumps({'type': event_type, **kwargs})}\n\n"


def _unwrap_exc(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return _unwrap_exc(exc.exceptions[0])
    return str(exc)


def _is_quota_error(exc: Exception) -> bool:
    """Return True if this exception should trigger a provider fallback."""
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code in _FALLBACK_HTTP_CODES:
            return True
        try:
            code = exc.response.json().get("error", {}).get("code", "")
            return code in _FALLBACK_ERROR_CODES
        except Exception:
            pass
    return False


def _resolve_provider(choice: ProviderChoice) -> tuple[openai.AsyncOpenAI, str]:
    """Return (client, model) for the given provider choice."""
    if choice == "ollama":
        return oai_ollama, OLLAMA_MODEL
    return oai_primary, PRIMARY_MODEL   # "primary" or initial "auto" attempt


# ── Agent loop (single provider attempt) ─────────────────────────────────────

async def _run_agent(
    messages: list[dict],
    oai_tools: list[dict],
    mcp_session: ClientSession,
    client: openai.AsyncOpenAI,
    model: str,
) -> AsyncGenerator[str, None]:
    """
    Inner loop: runs the tool-use cycle with a given client/model.
    Yields SSE tool_call / message events.
    Raises openai.APIStatusError / openai.RateLimitError on provider errors
    (caller decides whether to fall back).
    Returns final_content via StopAsyncIteration value (Python 3.10+ trick) —
    here we just set it in the outer scope via a list.
    """
    final = []

    while True:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=oai_tools,
            tool_choice="auto",
        )

        choice  = response.choices[0]
        message = choice.message
        messages.append(message.model_dump(exclude_unset=True))

        if choice.finish_reason == "stop":
            content = message.content or ""
            final.append(content)
            yield _sse("message", content=content)
            break

        if choice.finish_reason == "tool_calls" and message.tool_calls:
            for tc in message.tool_calls:
                fn   = tc.function
                args = json.loads(fn.arguments or "{}")

                yield _sse("tool_call", name=fn.name, args=fn.arguments or "{}")

                try:
                    result = await mcp_session.call_tool(fn.name, args)
                    output = (
                        result.content[0].text if result.content
                        else json.dumps({"result": "empty"})
                    )
                except Exception as exc:
                    output = json.dumps({"error": str(exc)})

                if len(output) > TOOL_OUTPUT_MAX_CHARS:
                    output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      output,
                })
        else:
            if message.content:
                final.append(message.content)
                yield _sse("message", content=message.content)
            break

    # Stash final content so the outer scope can cache it
    if final:
        messages.append({"_final": final[0]})   # sentinel picked up below


# ── Agent stream (with fallback logic) ───────────────────────────────────────

async def agent_stream(
    session_id: str,
    user_message: str,
    provider: ProviderChoice = "auto",
) -> AsyncGenerator[str, None]:

    # ── 1. Semantic cache check ───────────────────────────────────────────────
    cached = await cache.get(user_message)
    if cached is not None:
        ttl_h = float(os.environ.get("CACHE_TTL_HOURS", "24"))
        yield _sse("cache_hit", content=cached, ttl_hours=ttl_h)
        yield _sse("done")
        return

    # ── 2. Prepare session ────────────────────────────────────────────────────
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    messages = list(sessions[session_id])
    messages.append({"role": "user", "content": user_message})

    final_content: str | None = None

    try:
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                oai_tools = _mcp_to_oai_tools((await mcp_session.list_tools()).tools)

                client, model = _resolve_provider(
                    "primary" if provider != "ollama" else "ollama"
                )

                if provider != "auto":
                    # Fixed provider — stream events directly, no buffering needed
                    async for event in _run_agent(messages, oai_tools, mcp_session, client, model):
                        yield event
                else:
                    # Auto mode — buffer so we can intercept quota errors mid-loop
                    events_buffer: list[str] = []

                    try:
                        async for event in _run_agent(messages, oai_tools, mcp_session, client, model):
                            events_buffer.append(event)

                    except (openai.RateLimitError, openai.APIStatusError) as exc:
                        if _is_quota_error(exc):
                            # Yield any tool_call chips already collected, then announce switch
                            for e in events_buffer:
                                if '"tool_call"' in e:
                                    yield e
                            yield _sse(
                                "provider_switch",
                                from_provider="primary",
                                to_provider="ollama",
                                reason=str(exc)[:120],
                            )
                            logger.warning("Primary quota hit – retrying with Ollama: %s", exc)

                            # Roll back messages to the last user turn
                            messages = [m for m in messages if not m.get("_final")]
                            while messages and messages[-1]["role"] != "user":
                                messages.pop()

                            events_buffer = []

                            # Retry with Ollama — stream directly (no further fallback possible)
                            async for event in _run_agent(
                                messages, oai_tools, mcp_session, oai_ollama, OLLAMA_MODEL
                            ):
                                yield event
                            events_buffer = []  # already streamed
                        else:
                            raise

                    # Flush buffered events from successful primary run
                    for event in events_buffer:
                        yield event

                # Extract final content from sentinel
                for m in reversed(messages):
                    if m.get("_final"):
                        final_content = m["_final"]
                        break

                # Persist pruned session
                if final_content:
                    last_user = next(m for m in reversed(messages) if m.get("role") == "user")
                    sessions[session_id] = [
                        messages[0],
                        last_user,
                        {"role": "assistant", "content": final_content},
                    ]

        # ── 3. Cache successful response ──────────────────────────────────────
        # Must be inside the try block — code after `finally: yield` may not
        # run if the client closes the connection before the generator is drained.
        if final_content:
            await cache.set(user_message, final_content)

    except BaseException as exc:
        yield _sse("error", content=_unwrap_exc(exc))
    finally:
        yield _sse("done")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Payment Analysis UI")
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/chat")
async def chat(request: Request):
    body         = await request.json()
    user_message = (body.get("message") or "").strip()
    session_id   = body.get("session_id") or str(uuid.uuid4())
    provider     = body.get("provider", "auto")

    if provider not in ("auto", "primary", "ollama"):
        provider = "auto"
    if not user_message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    return StreamingResponse(
        agent_stream(session_id, user_message, provider),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/providers")
async def get_providers():
    return {
        "primary": {"base_url": OPENAI_BASE_URL or "https://api.openai.com", "model": PRIMARY_MODEL},
        "ollama":  {"base_url": OLLAMA_BASE_URL, "model": OLLAMA_MODEL},
    }


@app.delete("/chat")
async def clear_session(request: Request):
    body = await request.json()
    sid  = body.get("session_id")
    if sid and sid in sessions:
        del sessions[sid]
    return {"ok": True}


@app.delete("/cache")
async def clear_cache():
    cache.clear()
    return {"ok": True}


@app.delete("/cache/expired")
async def clear_expired_cache():
    return {"ok": True, "deleted": cache.clear_expired()}


@app.get("/health")
async def health():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
