"""Day 33: the last step before a tool result reaches the model.

Two jobs, both about tokens:
  1. Serialize compactly. Returning a dict lets the MCP SDK pretty-print it
     (indent=2), and every newline and indent is paid for in tokens on every
     call. Returning TextContent ourselves bypasses that. (scripts/measure_tokens.py
     has the numbers.)
  2. Cap lists that OCC doesn't page for us: product variants, cart entries,
     order lines. Searches and order history are paged by OCC and bounded by the
     tools' page_size limits instead.
"""

import json
import logging

from mcp.types import TextContent

logger = logging.getLogger("mcp-commerce.shaping")

# Per-list caps. Generous for a B2C shopper; a B2B cart with hundreds of lines
# hits them, and the model is told rather than silently shown a partial list.
MAX_VARIANTS = 20
MAX_CART_ENTRIES = 50
MAX_ORDER_ITEMS = 20
MAX_SHIPMENTS = 10

# A soft budget for any one result. Not enforced (truncating JSON would break
# it): exceeding it means a cap above is missing, so it's logged as a bug.
RESULT_BUDGET_CHARS = 6000


def capped(result: dict, key: str, items: list, limit: int) -> None:
    """Set result[key] to at most `limit` items; flag it if anything was dropped.

    Follows docs/tool-surface.md rule 7: a cut list says so (`truncated`) and
    how big it really was (`<key>_total`), so "20 variants" can't be read as
    "there are 20 variants".
    """
    result[key] = items[:limit]
    if len(items) > limit:
        result["truncated"] = True
        result[f"{key}_total"] = len(items)


def past_last_page(page: int, total_pages: int, total_results: int) -> str | None:
    """OCC answers a page past the end with an empty list and HTTP 200. Unexplained,
    the model reads that as "nothing matched" and rewrites a query that was fine."""
    if page and total_results and page >= total_pages:
        return (
            f"Page {page} is past the end: there are {total_results} results on "
            f"{total_pages} page(s), numbered from 0. Nothing is wrong with the query."
        )
    return None


def to_text(payload: dict) -> TextContent:
    # separators: no spaces after , and :  ensure_ascii=False: "₹" stays one
    # character instead of becoming the six-character escape ₹.
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(text) > RESULT_BUDGET_CHARS:
        logger.warning("tool result is %d chars, over the %d budget", len(text), RESULT_BUDGET_CHARS)
    return TextContent(type="text", text=text)
