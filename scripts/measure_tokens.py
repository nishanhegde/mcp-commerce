"""Day 33: what each tool result costs in tokens, and what the shaping saves.

    uv run scripts/measure_tokens.py

For one representative call per tool, against the mock OCC, it counts:
  raw      the untrimmed OCC response (fields=FULL), as if we passed it through
  pretty   our trimmed result, pretty-printed (the MCP SDK's default for a dict)
  compact  our trimmed result as the tools now send it (shaping.to_text)

Counts come from the Messages count_tokens endpoint (free, exact for the
model), minus the overhead of an empty-ish message. Needs ANTHROPIC_API_KEY.
"""

import asyncio
import json

import anthropic

from mcp_commerce import cart, catalog, orders
from mcp_commerce.config import load_settings
from mcp_commerce.occ_client import client_from_settings
from mcp_commerce.shaping import to_text

MODEL = "claude-opus-5"


async def scenarios(occ) -> list[tuple[str, dict, dict]]:
    """(label, raw OCC response, trimmed tool payload) per tool."""
    cart_id = (await cart.create_cart(occ))["cart_id"]
    for code in ("SH-204", "SK-010", "HB-300"):
        await cart.add_to_cart(occ, cart_id, code, 2)
    user = occ.user_path
    return [
        ("search_products (5 hits)",
         await occ.get("products/search", query="running:relevance", pageSize=10, fields="FULL"),
         await catalog.search_products(occ, "running", 0, 10, "relevance", False)),
        ("get_product (4 variants)",
         await occ.get("products/SH-204", fields="FULL"),
         await catalog.get_product(occ, "SH-204")),
        ("get_cart (3 lines)",
         await occ.get(f"{user}/carts/{cart_id}", fields="FULL"),
         await cart.get_cart(occ, cart_id)),
        ("get_order_history (4 orders)",
         await occ.get(f"{user}/orders", pageSize=5, fields="FULL"),
         await orders.get_order_history(occ, 0, 5, None)),
        ("get_order_status (1 shipment)",
         await occ.get(f"{user}/orders/1042", fields="FULL"),
         await orders.get_order_status(occ, "1042")),
    ]


async def main() -> None:
    settings = load_settings()  # also loads .env, so ANTHROPIC_API_KEY is set below
    client = anthropic.AsyncAnthropic()

    async def tokens(text: str) -> int:
        r = await client.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": text}])
        return r.input_tokens

    baseline = await tokens("x") - 1
    occ = client_from_settings(settings.occ)
    rows = []
    for label, raw, payload in await scenarios(occ):
        texts = (json.dumps(raw, indent=2, ensure_ascii=False), json.dumps(payload, indent=2, ensure_ascii=False),
                 to_text(payload).text)
        counts = [await tokens(t) - baseline for t in texts]
        rows.append((label, *counts))

    print(f"Tokens per tool result ({MODEL}, mock OCC)\n")
    print("| Tool call | raw OCC | trimmed, pretty | trimmed, compact | compact vs raw |")
    print("|---|--:|--:|--:|--:|")
    for label, raw, pretty, compact in rows:
        print(f"| {label} | {raw} | {pretty} | {compact} | {compact / raw:.0%} |")
    totals = [sum(col) for col in zip(*(r[1:] for r in rows))]
    print(f"| **all five** | {totals[0]} | {totals[1]} | {totals[2]} | {totals[2] / totals[0]:.0%} |")


if __name__ == "__main__":
    asyncio.run(main())
