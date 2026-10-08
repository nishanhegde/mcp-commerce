"""Day 31: the demo agent. One command runs the full WISMO flow through this server.

    uv run examples/demo.py                 # default: "where's my order?" for a delayed order
    uv run examples/demo.py "Can I get a USB-C hub delivered this week?"
    uv run examples/demo.py --verbose       # also show the server's own log lines

What happens: this script starts the server as a stdio subprocess (always stdio,
whatever .env says, against the mock OCC unless OCC_MODE=live),
discovers its tools over MCP, and runs a Claude agent loop against them.
The default question doesn't give an order number, so the expected chain is
get_order_history -> get_order_status -> (delayed) get_product / search_products
-> an answer that states the delay and offers concrete options unprompted.

The loop is hand-written rather than tool_runner (which commerce-agent-cli's
mcp_agent.py uses) so every tool call *and its result* can be printed as it
happens. That trace is the point of a demo.
"""

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "claude-opus-5"
MAX_TOOL_ROUNDS = 10  # a WISMO chain needs ~4; this only stops a runaway loop
ROOT = Path(__file__).resolve().parent.parent  # the repo, where .env lives

DEFAULT_QUESTION = "Hi, I ordered running shoes back in August and they still haven't arrived. Where's my order?"

SYSTEM = """You are the customer support agent for an online sports retailer.
Today is {today}.

Use your tools for every fact: order status, tracking, stock, prices. Never
guess a carrier, a date, or a stock level. If the customer has no order
number, find it with get_order_history.

A shipment is delayed when its estimated_delivery is before today and it has
not been delivered. When an order is delayed:
1. Say so plainly first: carrier, tracking id, the original estimate. Don't
   speculate about why.
2. Without being asked, check whether the ordered items are in stock now
   (get_product), and offer concrete options: a replacement shipment if the
   item is in stock, otherwise similar in-stock products (search_products).
3. Don't create carts or change anything until the customer picks an option.

Be brief: a few short paragraphs, prices in the order's currency."""


def _anthropic_tool(tool) -> dict:
    """MCP tool definition -> Anthropic tool param. Same JSON Schema, different envelope."""
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}


def _one_line(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def run(question: str, verbose: bool) -> None:
    server = StdioServerParameters(
        command=sys.executable,  # this venv's Python: no nested `uv run`
        args=["-m", "mcp_commerce"],
        cwd=ROOT,
        # Inherit the environment (OCC_BASE_URL etc.) but force stdio: a .env
        # set up for HTTP would otherwise make the subprocess bind a port and
        # never answer on its stdin/stdout.
        env={**os.environ, "MCP_TRANSPORT": "stdio"},
    )
    # The server logs every tool call to stderr. Useful when debugging,
    # noise in a demo, so it's opt-in.
    errlog = sys.stderr if verbose else open(os.devnull, "w")

    async with stdio_client(server, errlog=errlog) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = [_anthropic_tool(t) for t in (await session.list_tools()).tools]
        print(f"mcp-commerce: {len(tools)} tools ({', '.join(t['name'] for t in tools)})\n")
        print(f"customer> {question}\n")

        client = anthropic.AsyncAnthropic()
        system = SYSTEM.format(today=date.today().isoformat())
        messages: list[dict] = [{"role": "user", "content": question}]

        for _ in range(MAX_TOOL_ROUNDS):
            response = await client.messages.create(
                model=MODEL, max_tokens=2048, system=system, tools=tools, messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                answer = "".join(b.text for b in response.content if b.type == "text")
                print(f"agent> {answer.strip()}")
                return

            results = []
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    print(f"  (thinking aloud) {_one_line(block.text)}")
                if block.type != "tool_use":
                    continue
                args = ", ".join(f"{k}={v!r}" for k, v in block.input.items())
                print(f"  -> {block.name}({args})")
                result = await session.call_tool(block.name, block.input)
                text = "".join(c.text for c in result.content if c.type == "text")
                print(f"  <- {'ERROR ' if result.is_error else ''}{_one_line(text)}")
                # is_error goes back to the model too, so it can recover
                # (retry, ask the customer) instead of treating the message as data.
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": text, "is_error": bool(result.is_error),
                })
            messages.append({"role": "user", "content": results})
            print()

        print(f"agent> (stopped after {MAX_TOOL_ROUNDS} tool rounds without a final answer)")


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Run the WISMO demo agent against mcp-commerce.")
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION, help="What the customer asks.")
    parser.add_argument("--verbose", action="store_true", help="Show the MCP server's log output.")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (put it in .env; see .env.example)")
    asyncio.run(run(args.question, args.verbose))


if __name__ == "__main__":
    main()
