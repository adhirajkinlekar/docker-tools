"""
Payment Analysis – Web UI backend
──────────────────────────────────
Provider strategy:
  • "auto"     – try primary (external API), fall back to Ollama on quota/rate errors
  • "primary"  – always use the external provider (Groq / OpenAI / GitHub Models)
  • "ollama"   – always use the local Ollama instance

Improvements over v1:
  • stream=True on all LLM calls → live token streaming to the browser
  • MAX_ITERATIONS guard prevents infinite tool-call loops
  • Smart JSON-aware tool output truncation (6 000 chars)
  • Retry with exponential backoff for transient 5xx / connection errors
  • Session store with TTL + max-size eviction (no unbounded memory growth)
  • MCP tool list cached after first fetch (saves one round-trip per request)
  • AsyncQdrantClient used in cache (non-blocking event loop)
  • Deprecated on_event replaced with lifespan context manager
  • Request-scoped IDs in all log messages
  • Token usage logged after each LLM call
  • Generic error messages returned to the user (detail stays in server logs)
  • Dynamic date injected into system prompt
  • Auto-fallback rollback is message-safe (partial state cleaned up on switch)
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import AsyncGenerator, Literal

import httpx
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

MCP_SERVER_URL         = os.environ.get("MCP_SERVER_URL",        "http://mcp-server:8000/sse")
OPENAI_API_KEY         = os.environ.get("OPENAI_API_KEY",        "") or os.environ.get("GITHUB_TOKEN", "")
OPENAI_BASE_URL        = os.environ.get("OPENAI_BASE_URL",       "")
PRIMARY_MODEL          = os.environ.get("AGENT_MODEL",           "gpt-4o")
OLLAMA_HOST_URL        = os.environ.get("OLLAMA_HOST_URL",       "http://host.docker.internal:11434/v1")
OLLAMA_HOST_MODEL      = os.environ.get("OLLAMA_HOST_MODEL",     "qwen2.5:7b")
OLLAMA_CONTAINER_URL   = os.environ.get("OLLAMA_CONTAINER_URL",  "http://ollama:11434/v1")
OLLAMA_CONTAINER_MODEL = os.environ.get("OLLAMA_CONTAINER_MODEL","qwen2.5:1.5b")
QDRANT_URL             = os.environ.get("QDRANT_URL",            "http://qdrant:6333")

# Active Ollama URL/model – resolved at startup by _detect_ollama()
OLLAMA_MODEL: str = OLLAMA_CONTAINER_MODEL  # overridden in lifespan

# Agent loop limits
MAX_ITERATIONS    = 10      # max tool-call rounds per request
MAX_RETRIES       = 2       # retries for transient LLM errors
RETRY_BASE_DELAY  = 1.0     # seconds; doubles each retry

# Tool output budget
TOOL_OUTPUT_MAX_CHARS = 6_000  # per tool call

# Error conditions that trigger provider switch
_FALLBACK_HTTP_CODES  = {429, 413}
_FALLBACK_ERROR_CODES = {"tokens_limit_reached", "rate_limit_exceeded",
                         "insufficient_quota",   "quota_exceeded"}

# ── Session store (TTL + max-size eviction) ───────────────────────────────────

class _SessionStore:
    """In-process session store with TTL and LRU-style eviction."""

    def __init__(self, maxsize: int = 200, ttl_seconds: float = 3600.0):
        self._data: dict[str, tuple[float, list]] = {}
        self._maxsize  = maxsize
        self._ttl      = ttl_seconds

    def get(self, key: str) -> list | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.time() - ts > self._ttl:
            del self._data[key]
            return None
        return val

    def set(self, key: str, value: list) -> None:
        self._evict()
        self._data[key] = (time.time(), value)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]
        for k in expired:
            del self._data[k]
        if len(self._data) >= self._maxsize:
            oldest = sorted(self._data.items(), key=lambda x: x[1][0])
            for k, _ in oldest[: len(self._data) - self._maxsize + 1]:
                del self._data[k]


sessions = _SessionStore()

# ── System prompt (built at request time so date is always current) ───────────

def _build_system_prompt() -> str:
    today = date.today().strftime("%B %d, %Y")
    return f"""
You are a payment operations analyst. Today is {today}.

You have direct access to:
  • MinIO object storage – folders containing ACH/NACHA payment files
  • An MSSQL database – client profiles and transaction history
  • A semantic vector index – pre-indexed summaries of Archive files

ACH files contain PII (account numbers, names, individual IDs) which are
automatically redacted. You will see [REDACTED] in those fields. Never
attempt to reconstruct redacted values.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL ROUTING – choose based on the folder requested:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

▸ Any folder EXCEPT Archive  →  use MCP tools
  1. fetch_and_parse_ach_files(folder_path=<FOLDER>) to get file summaries
  2. get_client_info(company_identifiers=[...]) to look up client records
  3. get_client_recent_transactions(client_id=...) for flagged/high-value clients
  4. Synthesise: file summary, MoM trend, per-company breakdown, risk indicators

▸ Archive folder  →  use RAG tool only
  1. rag_search_payments(query=<natural language question>)
  2. Synthesise the returned hits into a structured analysis
  Note: do NOT call fetch_and_parse_ach_files for Archive

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ERROR HANDLING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If a tool returns an "error" field, report it to the user and stop.
Do not attempt to fill in missing data or retry the same tool call.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For all analyses produce a structured markdown report covering:
  • **File / batch summary** – count, date range, total credits/debits
  • **Month-on-month trend** – compare periods where data is available
  • **Per-company breakdown** – volumes, transaction types, client status
  • **Risk indicators** – suspended accounts, debit spikes, return growth
  • **Recommendations** – flag any anomalies with suggested action

Be concise but thorough.
""".strip()


# ── OpenAI clients ────────────────────────────────────────────────────────────

def _make_client(api_key: str, base_url: str | None) -> openai.AsyncOpenAI:
    kwargs: dict = {"api_key": api_key or "none"}
    if base_url:
        kwargs["base_url"] = base_url
    return openai.AsyncOpenAI(**kwargs)


oai_primary = _make_client(OPENAI_API_KEY, OPENAI_BASE_URL or None)
oai_ollama  = _make_client("ollama", OLLAMA_CONTAINER_URL)  # overridden in lifespan

cache = SemanticCache(QDRANT_URL)

# MCP tool list – fetched once and cached (tools are static at server startup)
_cached_oai_tools: list[dict] | None = None

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


def _is_transient_error(exc: Exception) -> bool:
    """True for 5xx / connection errors worth retrying."""
    if isinstance(exc, openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
        return True
    return False


def _truncate_tool_output(output: str) -> str:
    """
    Truncate tool output to TOOL_OUTPUT_MAX_CHARS.
    Tries JSON-aware truncation first (shortens list fields) before hard-cutting.
    """
    if len(output) <= TOOL_OUTPUT_MAX_CHARS:
        return output

    try:
        data = json.loads(output)
        for key in ("files", "clients", "transactions", "results", "batches", "hits"):
            if key in data and isinstance(data[key], list) and len(data[key]) > 3:
                original = len(data[key])
                data[key] = data[key][:3]
                data[f"_{key}_note"] = f"{original - 3} additional items omitted to fit context window"
                candidate = json.dumps(data, indent=2)
                if len(candidate) <= TOOL_OUTPUT_MAX_CHARS:
                    return candidate
    except (json.JSONDecodeError, TypeError):
        pass

    return (
        output[:TOOL_OUTPUT_MAX_CHARS]
        + f"\n... [truncated: {len(output) - TOOL_OUTPUT_MAX_CHARS} chars omitted]"
    )


def _resolve_provider(choice: ProviderChoice) -> tuple[openai.AsyncOpenAI, str]:
    if choice == "ollama":
        return oai_ollama, OLLAMA_MODEL
    return oai_primary, PRIMARY_MODEL


async def _get_tools(mcp_session: ClientSession) -> list[dict]:
    """Return cached MCP tool list, fetching once on first call."""
    global _cached_oai_tools
    if _cached_oai_tools is None:
        tools = (await mcp_session.list_tools()).tools
        _cached_oai_tools = _mcp_to_oai_tools(tools)
        logger.info("MCP tools cached: %s", [t["function"]["name"] for t in _cached_oai_tools])
    return _cached_oai_tools


# ── Ollama auto-detection ─────────────────────────────────────────────────────

async def _detect_ollama() -> tuple[str, str]:
    """
    Ping host Ollama first; if reachable use it with the best/host model.
    Otherwise fall back to the containerised Ollama with the small model.
    Returns (base_url, model).
    """
    host_base = OLLAMA_HOST_URL.rstrip("/v1").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{host_base}/api/tags")
            if r.status_code == 200:
                logger.info(
                    "Native Ollama detected – using host instance with model '%s'",
                    OLLAMA_HOST_MODEL,
                )
                return OLLAMA_HOST_URL, OLLAMA_HOST_MODEL
    except Exception:
        pass
    logger.info(
        "Host Ollama not reachable – using container instance with model '%s'",
        OLLAMA_CONTAINER_MODEL,
    )
    return OLLAMA_CONTAINER_URL, OLLAMA_CONTAINER_MODEL


# ── Agent loop (streaming, single provider) ───────────────────────────────────

async def _run_agent(
    messages:    list[dict],
    oai_tools:   list[dict],
    mcp_session: ClientSession,
    client:      openai.AsyncOpenAI,
    model:       str,
    req_id:      str,
) -> AsyncGenerator[str, None]:
    """
    Inner streaming loop.
    - Yields SSE `token` events for live text streaming.
    - Yields SSE `tool_call` events when tools are invoked.
    - Yields SSE `message` event with the final complete response.
    - Appends a {"_final": content} sentinel to messages so the caller can cache.
    - Raises openai errors so the outer scope can decide whether to fall back.
    """
    for iteration in range(1, MAX_ITERATIONS + 1):

        # ── LLM call with retry for transient errors ──────────────────────────
        stream = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                stream = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=oai_tools,
                    tool_choice="auto",
                    stream=True,
                )
                break
            except Exception as exc:
                if _is_transient_error(exc) and attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "[%s] Transient error (attempt %d/%d), retrying in %.1fs: %s",
                        req_id, attempt + 1, MAX_RETRIES + 1, delay, exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise  # quota errors or non-transient → propagate to caller

        # ── Consume the stream ────────────────────────────────────────────────
        content_buf: list[str]          = []
        tool_calls_map: dict[int, dict] = {}
        finish_reason: str | None       = None
        usage_info: dict | None         = None

        async for chunk in stream:
            if not chunk.choices:
                # Some providers send a final chunk with usage only
                if hasattr(chunk, "usage") and chunk.usage:
                    usage_info = {
                        "prompt_tokens":     chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens":      chunk.usage.total_tokens,
                    }
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason

            delta = choice.delta

            # Stream content tokens live
            if delta.content:
                content_buf.append(delta.content)
                yield _sse("token", content=delta.content)

            # Accumulate tool call deltas
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id":       "",
                            "type":     "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc_delta.id:
                        tool_calls_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_map[idx]["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_map[idx]["function"]["arguments"] += tc_delta.function.arguments

        full_content = "".join(content_buf)

        if usage_info:
            logger.info("[%s] Token usage iter=%d: %s", req_id, iteration, usage_info)

        # ── Final message ─────────────────────────────────────────────────────
        if finish_reason == "stop" or (not tool_calls_map and full_content):
            messages.append({"role": "assistant", "content": full_content})
            yield _sse("message", content=full_content)
            messages.append({"_final": full_content})
            return

        # ── Tool calls ────────────────────────────────────────────────────────
        if tool_calls_map:
            tool_calls_list = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())]
            assistant_msg: dict = {"role": "assistant", "tool_calls": tool_calls_list}
            if full_content:
                assistant_msg["content"] = full_content
            messages.append(assistant_msg)

            for tc in tool_calls_list:
                fn       = tc["function"]
                args_str = fn["arguments"] or "{}"
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}

                yield _sse("tool_call", name=fn["name"], args=args_str)
                logger.info("[%s] Tool call: %s(%s)", req_id, fn["name"], args_str[:120])

                try:
                    result = await mcp_session.call_tool(fn["name"], args)
                    output = (
                        result.content[0].text if result.content
                        else json.dumps({"result": "empty"})
                    )
                except Exception as exc:
                    logger.error("[%s] Tool error %s: %s", req_id, fn["name"], exc)
                    output = json.dumps({"error": "Tool call failed. Please try a different approach."})

                output = _truncate_tool_output(output)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})

        else:
            # No content and no tool calls – shouldn't happen but break to avoid infinite loop
            logger.warning("[%s] Unexpected: no content or tool calls at iteration %d", req_id, iteration)
            break

    # Reached max iterations
    logger.warning("[%s] MAX_ITERATIONS (%d) reached", req_id, MAX_ITERATIONS)
    yield _sse("error", content="The agent reached its maximum step limit. Please try a more specific query.")


# ── Agent stream (with provider fallback) ─────────────────────────────────────

async def agent_stream(
    session_id:   str,
    user_message: str,
    provider:     ProviderChoice = "auto",
    req_id:       str = "",
) -> AsyncGenerator[str, None]:

    req_id = req_id or str(uuid.uuid4())[:8]
    logger.info("[%s] session=%s provider=%s query='%s'", req_id, session_id, provider, user_message[:80])

    # ── 1. Semantic cache check ───────────────────────────────────────────────
    cached = await cache.get(user_message)
    if cached is not None:
        ttl_h = float(os.environ.get("CACHE_TTL_HOURS", "24"))
        logger.info("[%s] Cache HIT", req_id)
        yield _sse("cache_hit", content=cached, ttl_hours=ttl_h)
        yield _sse("done")
        return

    # ── 2. Prepare session ────────────────────────────────────────────────────
    history = sessions.get(session_id)
    if history is None:
        history = [{"role": "system", "content": _build_system_prompt()}]

    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    final_content: str | None = None

    try:
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                oai_tools = await _get_tools(mcp_session)

                if provider == "ollama":
                    async for event in _run_agent(
                        messages, oai_tools, mcp_session, oai_ollama, OLLAMA_MODEL, req_id
                    ):
                        yield event

                elif provider == "primary":
                    async for event in _run_agent(
                        messages, oai_tools, mcp_session, oai_primary, PRIMARY_MODEL, req_id
                    ):
                        yield event

                else:
                    # ── Auto mode: primary first, fall back on quota errors ────
                    snapshot_len = len(messages)
                    try:
                        async for event in _run_agent(
                            messages, oai_tools, mcp_session, oai_primary, PRIMARY_MODEL, req_id
                        ):
                            yield event

                    except (openai.RateLimitError, openai.APIStatusError) as exc:
                        if _is_quota_error(exc):
                            # Roll back any partial message state from the failed run
                            del messages[snapshot_len:]

                            yield _sse("clear_response")   # tell UI to clear current bubble
                            yield _sse(
                                "provider_switch",
                                from_provider="primary",
                                to_provider="ollama",
                                reason=str(exc)[:120],
                            )
                            logger.warning("[%s] Quota hit – switching to Ollama: %s", req_id, exc)

                            async for event in _run_agent(
                                messages, oai_tools, mcp_session, oai_ollama, OLLAMA_MODEL, req_id
                            ):
                                yield event
                        else:
                            raise

                # ── Extract final content for caching ────────────────────────
                for m in reversed(messages):
                    if m.get("_final") is not None:
                        final_content = m["_final"]
                        break

                # ── Persist pruned session (system + last user + last answer) ─
                if final_content is not None:
                    last_user = next(
                        (m for m in reversed(messages) if m.get("role") == "user"), None
                    )
                    new_history = [messages[0]]
                    if last_user:
                        new_history.append(last_user)
                    new_history.append({"role": "assistant", "content": final_content})
                    sessions.set(session_id, new_history)

        # ── 3. Cache successful response ──────────────────────────────────────
        if final_content:
            await cache.set(user_message, final_content)

    except BaseException as exc:
        logger.exception("[%s] Agent stream error", req_id)
        yield _sse("error", content="An error occurred processing your request. Please try again.")
    finally:
        yield _sse("done")


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global oai_ollama, OLLAMA_MODEL
    ollama_url, OLLAMA_MODEL = await _detect_ollama()
    oai_ollama = _make_client("ollama", ollama_url)
    logger.info("Startup complete – primary=%s  ollama=%s (%s)", PRIMARY_MODEL, OLLAMA_MODEL, ollama_url)
    yield
    logger.info("Shutting down")


app = FastAPI(title="Payment Analysis UI", lifespan=lifespan)
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
    req_id       = str(uuid.uuid4())[:8]

    if provider not in ("auto", "primary", "ollama"):
        provider = "auto"
    if not user_message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    return StreamingResponse(
        agent_stream(session_id, user_message, provider, req_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
            "X-Request-Id":     req_id,
        },
    )


@app.get("/providers")
async def get_providers():
    return {
        "primary": {"base_url": OPENAI_BASE_URL or "https://api.openai.com", "model": PRIMARY_MODEL},
        "ollama":  {"base_url": str(oai_ollama.base_url), "model": OLLAMA_MODEL},
    }


@app.delete("/chat")
async def clear_session(request: Request):
    body = await request.json()
    sid  = body.get("session_id")
    if sid:
        sessions.delete(sid)
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
    checks: dict[str, str] = {}

    # Qdrant
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{QDRANT_URL.rstrip('/')}/healthz")
            checks["qdrant"] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
    except Exception as exc:
        checks["qdrant"] = f"error: {exc}"

    # MCP server
    mcp_health_url = MCP_SERVER_URL.replace("/sse", "/health")
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(mcp_health_url)
            checks["mcp_server"] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
    except Exception as exc:
        checks["mcp_server"] = f"error: {exc}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
