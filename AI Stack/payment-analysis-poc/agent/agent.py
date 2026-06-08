"""
Payment Analysis Agent
──────────────────────
Connects to the FastMCP server via SSE and answers natural-language queries
about ACH payment files using the OpenAI-compatible API.

Works with GitHub Models (PAT), Azure OpenAI, or plain OpenAI:

  GitHub Models (recommended for PAT-based access):
    OPENAI_API_KEY=<GitHub PAT – any scope, or no scope>
    OPENAI_BASE_URL=https://models.inference.ai.azure.com
    AGENT_MODEL=gpt-4o

  Azure OpenAI:
    OPENAI_API_KEY=<Azure key>
    OPENAI_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deployment>
    AGENT_MODEL=gpt-4o

  Plain OpenAI:
    OPENAI_API_KEY=sk-...
    AGENT_MODEL=gpt-4o
"""

import asyncio
import json
import os
import sys

import openai
from mcp import ClientSession
from mcp.client.sse import sse_client

# ── Config ────────────────────────────────────────────────────────────────────

MCP_SERVER_URL  = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000/sse")
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("GITHUB_TOKEN", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
MODEL           = os.environ.get("AGENT_MODEL", "gpt-4o")

# GitHub Models free tier caps request bodies at 8 000 tokens.
# Cap each tool response and prune history after every turn to stay under the limit.
TOOL_OUTPUT_MAX_CHARS = 2_000

if not OPENAI_API_KEY:
    print("ERROR: set OPENAI_API_KEY (or GITHUB_TOKEN for GitHub Models)", file=sys.stderr)
    sys.exit(1)

# ── Async OpenAI client (must be async to avoid blocking the event loop) ──────

_client_kwargs: dict = {"api_key": OPENAI_API_KEY}
if OPENAI_BASE_URL:
    _client_kwargs["base_url"] = OPENAI_BASE_URL

client = openai.AsyncOpenAI(**_client_kwargs)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a payment operations analyst with direct access to:
  • Azure Blob Storage – folders containing ACH/NACHA payment files
  • An MSSQL database – client profiles and transaction history

IMPORTANT: ACH files contain PII (account numbers, names, individual IDs).
These are automatically redacted before being returned to you – you will see
[REDACTED] in those fields. Never attempt to reconstruct redacted values.

When asked to analyse a folder (e.g. "Analyse payment files in IMM-CTC-DROP"):
1. Call fetch_and_parse_ach_files to retrieve and parse the ACH data
2. Extract company_name / company_identification values from batch headers
3. Call get_client_info with those identifiers
4. For flagged or high-value clients, call get_client_recent_transactions
5. Produce a structured analysis covering:
   • File summary (file count, date range, total credit/debit)
   • Per-company breakdown (volumes, transaction types)
   • Client status (active vs suspended/closed)
   • Risk indicators (suspended accounts, high debit volume, failed transactions)
   • Recommendations if any anomalies are detected

Be concise but complete.
""".strip()

BANNER = f"""
╔══════════════════════════════════════════════════════╗
║   Payment Analysis Agent  (OpenAI · {MODEL})
╚══════════════════════════════════════════════════════╝
Type a query or one of these shortcuts:
  list       – list all payment folders
  clients    – list all known clients
  help       – show this message
  exit / q   – quit
"""

SHORTCUTS = {
    "list":    "List all payment folders in the payments container.",
    "clients": "List all clients registered in the database.",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mcp_to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def _read_input(prompt: str) -> str:
    """Blocking stdin read – runs in a thread so it doesn't block the event loop."""
    return input(prompt)


# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent():
    print(f"\nConnecting to MCP server at {MCP_SERVER_URL} …", flush=True)

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            mcp_tools = [
                {
                    "name":         t.name,
                    "description":  t.description or "",
                    "input_schema": t.inputSchema,
                }
                for t in tools_result.tools
            ]
            oai_tools = _mcp_to_openai_tools(mcp_tools)

            print(f"Connected. {len(mcp_tools)} tools: {[t['name'] for t in mcp_tools]}")
            print(BANNER)

            messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

            while True:
                # ── Read user input (in thread so the event loop stays free) ──
                try:
                    loop = asyncio.get_event_loop()
                    raw  = await loop.run_in_executor(None, _read_input, "You ▶ ")
                    raw  = raw.strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye.")
                    break

                if not raw:
                    continue
                if raw.lower() in ("exit", "quit", "q"):
                    print("Goodbye.")
                    break
                if raw.lower() == "help":
                    print(BANNER)
                    continue

                user_text = SHORTCUTS.get(raw.lower(), raw)
                messages.append({"role": "user", "content": user_text})

                # ── Agentic tool-use loop ────────────────────────────────────
                while True:
                    response = await client.chat.completions.create(
                        model=MODEL,
                        messages=messages,
                        tools=oai_tools,
                        tool_choice="auto",
                    )

                    choice  = response.choices[0]
                    message = choice.message

                    messages.append(message.model_dump(exclude_unset=True))

                    if choice.finish_reason == "stop":
                        print(f"\nAgent ▶\n{message.content or ''}\n")
                        # Prune history: keep only system + last user + last assistant answer.
                        # This prevents accumulated tool messages from blowing the token budget
                        # on the next turn (GitHub Models free tier: 8 000 token request limit).
                        last_user      = next(m for m in reversed(messages) if m["role"] == "user")
                        last_assistant = {"role": "assistant", "content": message.content or ""}
                        messages[:] = [messages[0], last_user, last_assistant]
                        break

                    if choice.finish_reason == "tool_calls" and message.tool_calls:
                        for tc in message.tool_calls:
                            fn   = tc.function
                            args = json.loads(fn.arguments or "{}")

                            preview = fn.arguments or ""
                            if len(preview) > 120:
                                preview = preview[:117] + "…"
                            print(f"  ⚙  {fn.name}({preview})", flush=True)

                            try:
                                result = await session.call_tool(fn.name, args)
                                output = (
                                    result.content[0].text
                                    if result.content
                                    else json.dumps({"result": "empty"})
                                )
                            except Exception as exc:
                                output = json.dumps({"error": str(exc)})
                                print(f"     ✗ Tool error: {exc}", file=sys.stderr)

                            # Truncate large tool responses to stay under the token limit
                            if len(output) > TOOL_OUTPUT_MAX_CHARS:
                                output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated for token limit]"

                            messages.append(
                                {
                                    "role":         "tool",
                                    "tool_call_id": tc.id,
                                    "content":      output,
                                }
                            )
                    else:
                        if message.content:
                            print(f"\nAgent ▶\n{message.content}\n")
                        break


if __name__ == "__main__":
    asyncio.run(run_agent())
