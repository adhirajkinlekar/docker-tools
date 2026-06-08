"""
Payment Analysis – Web UI backend
──────────────────────────────────
FastAPI app that:
  • Serves a single-page chat UI at GET /
  • Streams agent events (tool calls + final answer) via SSE at POST /chat
  • Clears the in-memory session at DELETE /chat

Each session keeps a pruned message history so repeated turns stay within
the GitHub Models 8 000-token request limit.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import AsyncGenerator

import openai
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.sse import sse_client

# ── Config ────────────────────────────────────────────────────────────────────

MCP_SERVER_URL  = os.environ.get("MCP_SERVER_URL",  "http://mcp-server:8000/sse")
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY",  "") or os.environ.get("GITHUB_TOKEN", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
MODEL           = os.environ.get("AGENT_MODEL",     "gpt-4o")

# GitHub Models free tier: hard 8 000-token request cap.
TOOL_OUTPUT_MAX_CHARS = 2_000

SYSTEM_PROMPT = """
You are a payment operations analyst with direct access to:
  • MinIO object storage – folders containing ACH/NACHA payment files (Jan–Jun 2026)
  • An MSSQL database – client profiles and 6 months of transaction history

ACH files contain PII (account numbers, names, individual IDs) which are
automatically redacted. You will see [REDACTED] in those fields – never
attempt to reconstruct them.

When asked to analyse a folder (e.g. "Analyse payment files in IMM-CTC-DROP"):
1. Call fetch_and_parse_ach_files to get file summaries for the folder
2. Extract company_names / company_identification values
3. Call get_client_info with those identifiers
4. For flagged or high-value clients, call get_client_recent_transactions
5. Produce a structured analysis covering:
   • File summary (count, date range, total credits/debits)
   • Month-on-month trend (volumes are available for Jan–Jun 2026)
   • Per-company breakdown and client status
   • Risk indicators (suspended accounts, growing returns, failed transactions)
   • Recommendations for any anomalies detected

Be concise but thorough. Use markdown formatting.
""".strip()

# ── OpenAI client ─────────────────────────────────────────────────────────────

_oai_kwargs: dict = {"api_key": OPENAI_API_KEY}
if OPENAI_BASE_URL:
    _oai_kwargs["base_url"] = OPENAI_BASE_URL

oai = openai.AsyncOpenAI(**_oai_kwargs)

# ── In-memory sessions ────────────────────────────────────────────────────────
# Maps session_id → pruned message list [system, last_user, last_assistant]

sessions: dict[str, list[dict]] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

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
    """Return a clean error string, unwrapping anyio BaseExceptionGroup if needed."""
    if isinstance(exc, BaseExceptionGroup):
        # Recursively unwrap to the first leaf exception
        return _unwrap_exc(exc.exceptions[0])
    return str(exc)


# ── Agent stream ──────────────────────────────────────────────────────────────

async def agent_stream(session_id: str, user_message: str) -> AsyncGenerator[str, None]:
    """
    Run the agentic tool-use loop and yield SSE events:
      tool_call  – name + args of each tool being called
      message    – final assistant answer (markdown)
      error      – if something goes wrong
      done       – signals end of stream
    """
    # Restore or create session history
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    messages = list(sessions[session_id])  # shallow copy for this request
    messages.append({"role": "user", "content": user_message})

    try:
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()

                tools_result = await mcp_session.list_tools()
                oai_tools    = _mcp_to_oai_tools(tools_result.tools)

                # Agentic tool-use loop
                while True:
                    response = await oai.chat.completions.create(
                        model=MODEL,
                        messages=messages,
                        tools=oai_tools,
                        tool_choice="auto",
                    )

                    choice  = response.choices[0]
                    message = choice.message
                    messages.append(message.model_dump(exclude_unset=True))

                    if choice.finish_reason == "stop":
                        content = message.content or ""
                        # Prune and persist session (keep only system + last pair)
                        last_user = next(m for m in reversed(messages) if m["role"] == "user")
                        sessions[session_id] = [
                            messages[0],
                            last_user,
                            {"role": "assistant", "content": content},
                        ]
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
                                    result.content[0].text
                                    if result.content
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
                            yield _sse("message", content=message.content)
                        break

    except BaseException as exc:
        # anyio wraps exceptions from its task groups in BaseExceptionGroup.
        # Unwrap to surface the real error message.
        msg = _unwrap_exc(exc)
        yield _sse("error", content=msg)
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
    body = await request.json()
    user_message = (body.get("message") or "").strip()
    session_id   = body.get("session_id") or str(uuid.uuid4())

    if not user_message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    return StreamingResponse(
        agent_stream(session_id, user_message),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@app.delete("/chat")
async def clear_session(request: Request):
    body       = await request.json()
    session_id = body.get("session_id")
    if session_id and session_id in sessions:
        del sessions[session_id]
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
