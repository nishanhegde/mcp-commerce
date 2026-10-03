"""Day 22-30: MCP server with tools, a resource, and a prompt template.

Transport is chosen by env (see .env.example):
  MCP_TRANSPORT=stdio (default)   spawned by the client as a subprocess; no auth
  MCP_TRANSPORT=http              standalone streamable-HTTP server; bearer auth required

Run over stdio:     uv run server.py
Run over HTTP:      MCP_TRANSPORT=http MCP_API_KEY=... uv run server.py
Run with inspector: uv run mcp dev server.py
"""

import hmac
import json
import logging
import os
import sys
from typing import Annotated

from dotenv import load_dotenv
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

import cart
import catalog
import orders
from occ_client import client_from_env

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-commerce")

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))


class StaticBearerVerifier:
    """Accepts exactly one bearer token, read from the environment.

    Stand-in for a real OAuth token verifier: same TokenVerifier protocol, so
    swapping in JWT/introspection later changes this class and nothing else.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def verify_token(self, token: str) -> AccessToken | None:
        # compare_digest: constant-time, so response timing can't leak the key.
        if hmac.compare_digest(token, self._api_key):
            return AccessToken(token=token, client_id="static-api-key", scopes=[])
        logger.warning("rejected bearer token")
        return None


def _build_server() -> MCPServer:
    if TRANSPORT != "http":
        return MCPServer("mcp-commerce")
    api_key = os.environ.get("MCP_API_KEY")
    if not api_key:
        sys.exit("MCP_API_KEY must be set when MCP_TRANSPORT=http")
    base_url = f"http://{HOST}:{PORT}"
    return MCPServer(
        "mcp-commerce",
        token_verifier=StaticBearerVerifier(api_key),
        # AuthSettings is shaped for OAuth; with a static key there is no real
        # authorization server, so issuer_url is a placeholder.
        auth=AuthSettings(issuer_url=base_url, resource_server_url=f"{base_url}/mcp"),
    )


mcp = _build_server()
occ = client_from_env()

_FAKE_CATALOG = {
    "sku-100": {"name": "Wireless Mouse", "price": 24.99, "in_stock": True},
    "sku-200": {"name": "Mechanical Keyboard", "price": 89.99, "in_stock": False},
    "sku-300": {"name": "USB-C Hub", "price": 34.50, "in_stock": True},
}

@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def search_products(
    query: Annotated[str, Field(description='Keywords, e.g. "running shoes". No filters or operators.')],
    page: Annotated[int, Field(ge=0, description="Zero-based page number.")] = 0,
    page_size: Annotated[int, Field(ge=1, le=20, description="Results per page, 1-20.")] = 10,
    sort: Annotated[
        catalog.SortOrder,
        Field(description="relevance (default), or price_asc / price_desc to find cheapest or priciest first."),
    ] = "relevance",
    in_stock_only: Annotated[bool, Field(description="Exclude out-of-stock products.")] = False,
) -> dict:
    """Search the catalog by keyword and return matching products with price and stock status.

    Call this when the customer describes what they want ("running shoes", "USB-C hub")
    and you don't have a product code. Results are paged; if total_pages > 1 and nothing
    fits, request the next page rather than repeating the query. For a budget ("under
    5000") use sort=price_asc and read the prices; there is no price filter. To inspect
    one specific product use get_product.
    """
    logger.info("search_products query=%r page=%d sort=%s in_stock_only=%s", query, page, sort, in_stock_only)
    return await catalog.search_products(occ, query, page, page_size, sort, in_stock_only)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_product(
    product_code: Annotated[str, Field(description='Product code from search_products, e.g. "SH-204".')],
) -> dict:
    """Get details for one product by its code: name, summary, price, categories, stock, variants.

    Call this after search_products when the customer asks about a specific result, or when
    they give you a product code directly. Variants (e.g. sizes) each carry their own stock
    status, so use this to answer "do you have it in size 9?". Not for browsing; use
    search_products for that.
    """
    logger.info("get_product code=%r", product_code)
    return await catalog.get_product(occ, product_code)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
async def create_cart() -> dict:
    """Create a new, empty shopping cart and return its cart_id.

    Call this once, when the customer first wants to buy something and you have no cart yet.
    Reuse the returned cart_id for every later cart call; do not create a second cart for the
    same conversation.
    """
    logger.info("create_cart")
    return await cart.create_cart(occ)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_cart(
    cart_id: Annotated[str, Field(description="Cart id returned by create_cart.")],
) -> dict:
    """Show the contents of a cart: each line with quantity and price, plus subtotal and total.

    Call this to answer "what's in my cart?" or to verify state after any cart change.
    Read-only. Each line has an entry_number.
    """
    logger.info("get_cart cart_id=%r", cart_id)
    return await cart.get_cart(occ, cart_id)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
async def add_to_cart(
    cart_id: Annotated[str, Field(description="Cart id returned by create_cart.")],
    product_code: Annotated[str, Field(description='Product code from search_products or get_product, e.g. "SH-204".')],
    quantity: Annotated[int, Field(ge=1, le=10, description="Units to add, 1-10 per call.")] = 1,
) -> dict:
    """Add a product to a cart.

    Call this when the customer says they want to buy or add an item. Needs a cart_id (from
    create_cart) and a product_code (from search or detail). Adds to the existing quantity if
    the product is already in the cart. If stock is short, fewer units than requested are
    added and the result says so; check quantity_added. If the product is out of stock this
    fails and nothing is added.
    """
    logger.info("add_to_cart cart_id=%r product_code=%r quantity=%d", cart_id, product_code, quantity)
    return await cart.add_to_cart(occ, cart_id, product_code, quantity)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_order_history(
    page: Annotated[int, Field(ge=0, description="Zero-based page number.")] = 0,
    page_size: Annotated[int, Field(ge=1, le=20, description="Orders per page, 1-20.")] = 5,
    status: Annotated[str | None, Field(description='Only orders with this status, e.g. "COMPLETED". Omit for all.')] = None,
) -> dict:
    """List the customer's past orders, newest first, with code, date, status and total.

    Call this when the customer wants their order history or doesn't know their order number.
    Then call get_order_status with an order_code from the result.
    """
    logger.info("get_order_history page=%d page_size=%d status=%r", page, page_size, status)
    return await orders.get_order_history(occ, page, page_size, status)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_order_status(
    order_code: Annotated[str, Field(description='Order code from get_order_history or the customer, e.g. "1042".')],
) -> dict:
    """Answer "where is my order?": order status plus each shipment's status, tracking id and dates.

    Call this when the customer asks about delivery, shipping, or order location. Each shipment
    includes tracking_id, carrier, shipped_on and estimated_delivery only when the backend has
    them. If a field is missing, say it isn't available; never guess a carrier or delivery date.
    An order with no shipments hasn't been dispatched yet.
    """
    logger.info("get_order_status order_code=%r", order_code)
    return await orders.get_order_status(occ, order_code)


@mcp.resource("catalog://products")
def product_catalog() -> str:
    """Snapshot of the full product catalog, as JSON."""
    logger.info("resource read catalog://products")
    return json.dumps(_FAKE_CATALOG, indent=2)


@mcp.prompt()
def summarize_order(order_id: str, sku: str, status: str) -> str:
    """Draft a customer-support summary for one order line.

    Args:
        order_id: The order identifier, e.g. "ORD-4821".
        sku: The SKU on the order line.
        status: Current fulfillment status, e.g. "shipped", "delayed".
    """
    logger.info("prompt summarize_order order_id=%r sku=%r status=%r", order_id, sku, status)
    product = _FAKE_CATALOG.get(sku)
    product_name = product["name"] if product else sku
    return (
        f"Summarize order {order_id} for a customer support agent. "
        f"The order contains: {product_name} (SKU {sku}). "
        f"Current status: {status}. "
        "Write two sentences: what the customer ordered, and what to tell them about its status."
    )


if __name__ == "__main__":
    if TRANSPORT == "http":
        mcp.run("streamable-http", host=HOST, port=PORT)
    else:
        mcp.run()
