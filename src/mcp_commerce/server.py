"""The MCP server: seven tools over SAP Commerce OCC, and one prompt.

Settings come from config.py (env, then .env, then defaults); a bad
configuration stops startup with every problem listed. Transport:
  MCP_TRANSPORT=stdio (default)   spawned by the client as a subprocess; no auth
  MCP_TRANSPORT=http              standalone streamable-HTTP server; bearer auth required

Run over stdio:     uv run mcp-commerce
Run over HTTP:      MCP_TRANSPORT=http MCP_API_KEY=... uv run mcp-commerce
Run with inspector: uv run mcp dev src/mcp_commerce/server.py
"""

import hmac
import logging
import sys
from typing import Annotated

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from mcp_commerce import cart, catalog, orders
from mcp_commerce.config import ConfigError, ServerSettings, load_settings
from mcp_commerce.occ_client import client_from_settings
from mcp_commerce.shaping import to_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-commerce")

try:
    settings = load_settings()
except ConfigError as exc:
    # stderr, then exit: Claude Desktop shows this in its MCP server log.
    sys.exit(f"mcp-commerce: {exc}")
logger.info("config: %s", settings.summary())
for warning in settings.warnings:
    logger.warning("config: %s", warning)


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


def _build_server(s: ServerSettings) -> MCPServer:
    if s.transport != "http":
        return MCPServer("mcp-commerce")
    assert s.api_key  # config refuses http without one
    base_url = f"http://{s.host}:{s.port}"
    return MCPServer(
        "mcp-commerce",
        token_verifier=StaticBearerVerifier(s.api_key),
        # AuthSettings is shaped for OAuth; with a static key there is no real
        # authorization server, so issuer_url is a placeholder.
        auth=AuthSettings(issuer_url=base_url, resource_server_url=f"{base_url}/mcp"),
    )


mcp = _build_server(settings.server)
occ = client_from_settings(settings.occ)


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
) -> TextContent:
    """Search the catalog by keyword and return matching products with price and stock status.

    Call this when the customer describes what they want ("running shoes", "USB-C hub")
    and you don't have a product code. Results are paged; if total_pages > 1 and nothing
    fits, request the next page rather than repeating the query. For a budget ("under
    5000") use sort=price_asc and read the prices; there is no price filter. To inspect
    one specific product use get_product.
    """
    logger.info("search_products query=%r page=%d sort=%s in_stock_only=%s", query, page, sort, in_stock_only)
    return to_text(await catalog.search_products(occ, query, page, page_size, sort, in_stock_only))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_product(
    product_code: Annotated[str, Field(description='Product code from search_products, e.g. "SH-204".')],
) -> TextContent:
    """Get details for one product by its code: name, summary, price, categories, stock, variants.

    Call this after search_products when the customer asks about a specific result, or when
    they give you a product code directly. Variants (e.g. sizes) each carry their own stock
    status, so use this to answer "do you have it in size 9?". Not for browsing; use
    search_products for that.
    """
    logger.info("get_product code=%r", product_code)
    return to_text(await catalog.get_product(occ, product_code))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
async def create_cart() -> TextContent:
    """Create a new, empty shopping cart and return its cart_id.

    Call this once, when the customer first wants to buy something and you have no cart yet.
    Reuse the returned cart_id for every later cart call; do not create a second cart for the
    same conversation.
    """
    logger.info("create_cart")
    return to_text(await cart.create_cart(occ))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_cart(
    cart_id: Annotated[str, Field(description="Cart id returned by create_cart.")],
) -> TextContent:
    """Show the contents of a cart: each line with quantity and price, plus subtotal and total.

    Call this to answer "what's in my cart?" or to verify state after any cart change.
    Read-only. Each line has an entry_number.
    """
    logger.info("get_cart cart_id=%r", cart_id)
    return to_text(await cart.get_cart(occ, cart_id))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
async def add_to_cart(
    cart_id: Annotated[str, Field(description="Cart id returned by create_cart.")],
    product_code: Annotated[str, Field(description='Product code from search_products or get_product, e.g. "SH-204".')],
    quantity: Annotated[int, Field(ge=1, le=10, description="Units to add, 1-10 per call.")] = 1,
) -> TextContent:
    """Add a product to a cart.

    Call this when the customer says they want to buy or add an item. Needs a cart_id (from
    create_cart) and a product_code (from search or detail). Adds to the existing quantity if
    the product is already in the cart. If stock is short, fewer units than requested are
    added and the result says so; check quantity_added. If the product is out of stock this
    fails and nothing is added.
    """
    logger.info("add_to_cart cart_id=%r product_code=%r quantity=%d", cart_id, product_code, quantity)
    return to_text(await cart.add_to_cart(occ, cart_id, product_code, quantity))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_order_history(
    page: Annotated[int, Field(ge=0, description="Zero-based page number.")] = 0,
    page_size: Annotated[int, Field(ge=1, le=20, description="Orders per page, 1-20.")] = 5,
    status: Annotated[str | None, Field(description='Only orders with this status, e.g. "COMPLETED". Omit for all.')] = None,
) -> TextContent:
    """List the customer's past orders, newest first, with code, date, status and total.

    Call this when the customer wants their order history or doesn't know their order number.
    Then call get_order_status with an order_code from the result.
    """
    logger.info("get_order_history page=%d page_size=%d status=%r", page, page_size, status)
    return to_text(await orders.get_order_history(occ, page, page_size, status))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_order_status(
    order_code: Annotated[str, Field(description='Order code from get_order_history or the customer, e.g. "1042".')],
) -> TextContent:
    """Answer "where is my order?": order status, the items ordered, and each shipment's status, tracking id and dates.

    Call this when the customer asks about delivery, shipping, or order location. Each shipment
    includes tracking_id, carrier, shipped_on and estimated_delivery only when the backend has
    them. If a field is missing, say it isn't available; never guess a carrier or delivery date.
    An order with no shipments hasn't been dispatched yet.
    """
    logger.info("get_order_status order_code=%r", order_code)
    return to_text(await orders.get_order_status(occ, order_code))


@mcp.prompt()
def where_is_my_order(order_code: str = "") -> str:
    """Answer a customer's "where is my order?" from live order data.

    Prompts are for the human to pick (a slash command in most hosts); tools are
    for the model. This one only frames the task: every fact still comes from
    the tools, so nothing here can go stale.

    Args:
        order_code: The order number, if the customer has it. Leave empty to look it up.
    """
    logger.info("prompt where_is_my_order order_code=%r", order_code)
    find = (
        f"Look up order {order_code} with get_order_status."
        if order_code
        else "The customer doesn't have the order number: find it with get_order_history, "
        "then call get_order_status."
    )
    return (
        f"A customer is asking where their order is. {find} Tell them the order status and, "
        "for each shipment, the carrier, tracking id and estimated delivery if the result has "
        "them. If a field is missing, say it isn't available; never guess a carrier or a date. "
        "Keep it to a few sentences."
    )


def run() -> None:
    """Serve on the configured transport. Blocks until the client disconnects."""
    if settings.server.transport == "http":
        mcp.run("streamable-http", host=settings.server.host, port=settings.server.port)
    else:
        mcp.run()


if __name__ == "__main__":
    run()
