"""Day 29: cart tools and their guard rails, through the MCP server against the mock OCC."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server import mcp


async def call(name: str, **args) -> dict:
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


async def new_cart() -> str:
    return (await call("create_cart"))["cart_id"]


async def test_create_cart_returns_only_the_id():
    assert await call("create_cart") == {"cart_id": "00001001"}


async def test_carts_get_distinct_ids():
    assert await new_cart() != await new_cart()


async def test_empty_cart():
    cart = await call("get_cart", cart_id=await new_cart())
    assert cart["entries"] == [] and cart["item_count"] == 0
    assert cart["subtotal"] == 0 and cart["currency"] == "INR"


async def test_add_then_get_cart_with_totals():
    cart_id = await new_cart()
    added = await call("add_to_cart", cart_id=cart_id, product_code="SH-204", quantity=2)
    assert added == {
        "cart_id": cart_id, "entry_number": 0, "product_code": "SH-204",
        "name": "Cloudrunner 2", "quantity_added": 2, "quantity_in_cart": 2,
        "line_total": 8598.0, "currency": "INR",
    }
    await call("add_to_cart", cart_id=cart_id, product_code="SK-010")  # quantity defaults to 1
    cart = await call("get_cart", cart_id=cart_id)
    assert [e["product_code"] for e in cart["entries"]] == ["SH-204", "SK-010"]
    assert cart["entries"][0] == {
        "entry_number": 0, "product_code": "SH-204", "name": "Cloudrunner 2",
        "quantity": 2, "unit_price": 4299.0, "line_total": 8598.0,
    }
    assert cart["subtotal"] == cart["total"] == 8598.0 + 499.0
    assert cart["item_count"] == 3


async def test_adding_same_product_merges_into_one_line():
    cart_id = await new_cart()
    await call("add_to_cart", cart_id=cart_id, product_code="SH-204", quantity=1)
    second = await call("add_to_cart", cart_id=cart_id, product_code="SH-204", quantity=2)
    assert second["quantity_added"] == 2 and second["quantity_in_cart"] == 3
    assert len((await call("get_cart", cart_id=cart_id))["entries"]) == 1


# --- guard rails: all of these must reach the model as is_error results -------

@pytest.mark.parametrize("quantity", [0, -1, 11, 500])
async def test_quantity_outside_1_to_10_is_rejected(quantity):
    cart_id = await new_cart()
    with pytest.raises(ToolError):
        await mcp.call_tool("add_to_cart", {"cart_id": cart_id, "product_code": "SH-204", "quantity": quantity})
    assert (await call("get_cart", cart_id=cart_id))["entries"] == []


async def test_unknown_product():
    cart_id = await new_cart()
    with pytest.raises(ToolError, match="Product with code 'NOPE' not found"):
        await mcp.call_tool("add_to_cart", {"cart_id": cart_id, "product_code": "NOPE"})


async def test_unknown_cart():
    with pytest.raises(ToolError, match="Cart not found"):
        await mcp.call_tool("add_to_cart", {"cart_id": "99999999", "product_code": "SH-204"})
    with pytest.raises(ToolError, match="Cart not found"):
        await mcp.call_tool("get_cart", {"cart_id": "99999999"})


async def test_out_of_stock_product_is_an_error_and_adds_nothing():
    cart_id = await new_cart()
    with pytest.raises(ToolError, match="out of stock"):
        await mcp.call_tool("add_to_cart", {"cart_id": cart_id, "product_code": "KB-200"})
    assert (await call("get_cart", cart_id=cart_id))["entries"] == []


async def test_low_stock_adds_what_exists_and_says_so():
    cart_id = await new_cart()  # SH-311 has 3 in stock
    result = await call("add_to_cart", cart_id=cart_id, product_code="SH-311", quantity=5)
    assert result["quantity_added"] == 3 and result["quantity_in_cart"] == 3
    assert "Only 3 of the 5 requested" in result["message"]


async def test_cart_already_holding_all_stock_is_an_error():
    cart_id = await new_cart()
    await call("add_to_cart", cart_id=cart_id, product_code="SH-311", quantity=3)
    with pytest.raises(ToolError, match="Nothing was added"):
        await mcp.call_tool("add_to_cart", {"cart_id": cart_id, "product_code": "SH-311"})


# --- annotations (docs/tool-surface.md principle 5) ----------------------------

async def test_read_only_hints_split_reads_from_writes():
    tools = {t.name: t for t in await mcp.list_tools()}
    hints = {name: tools[name].annotations.read_only_hint for name in (
        "search_products", "get_product", "get_cart", "create_cart", "add_to_cart")}
    assert hints["search_products"] and hints["get_product"] and hints["get_cart"]
    assert not hints["create_cart"] and not hints["add_to_cart"]
