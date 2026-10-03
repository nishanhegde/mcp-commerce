"""Day 28: product tools, end to end through the MCP server against the mock OCC."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mock_occ.app import apply_fields, parse_fields
from server import mcp


async def call(name: str, **args) -> dict:
    """Call a tool the way a host would and decode its JSON text content."""
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


# --- fields= mini-language (the mock must honour it for trimming to mean anything)

def test_fields_nested_and_lists():
    data = {"a": 1, "b": {"c": 2, "d": 3}, "e": [{"f": 1, "g": 2}], "h": 9}
    tree = parse_fields("a,b(c),e(f)")
    assert apply_fields(data, tree) == {"a": 1, "b": {"c": 2}, "e": [{"f": 1}]}


def test_fields_presets_keep_everything():
    assert parse_fields("DEFAULT") is None and parse_fields("") is None


# --- search_products

async def test_search_matches_all_keywords():
    result = await call("search_products", query="running shoes")
    codes = {p["code"] for p in result["products"]}
    assert codes == {"SH-204", "SH-311", "SH-120", "SH-450", "SH-099"}
    assert result["currency"] == "INR"
    assert result["total_results"] == 5


async def test_search_response_is_trimmed():
    result = await call("search_products", query="running shoes")
    assert set(result["products"][0]) == {"code", "name", "price", "stock"}


async def test_search_price_asc_and_in_stock_answers_the_budget_question():
    result = await call(
        "search_products", query="running shoes", sort="price_asc", in_stock_only=True
    )
    prices = [p["price"] for p in result["products"]]
    assert prices == sorted(prices)
    assert all(p["stock"] != "outOfStock" for p in result["products"])
    assert "SH-099" not in {p["code"] for p in result["products"]}
    assert [p["code"] for p in result["products"] if p["price"] < 5000] == ["SH-120", "SH-204"]


async def test_search_pagination():
    page0 = await call("search_products", query="running shoes", page_size=2)
    page2 = await call("search_products", query="running shoes", page_size=2, page=2)
    assert (page0["total_pages"], len(page0["products"])) == (3, 2)
    assert len(page2["products"]) == 1


async def test_search_no_results_explains_itself():
    result = await call("search_products", query="submarine")
    assert result["products"] == []
    assert "No products matched 'submarine'" in result["message"]


async def test_search_rejects_oversized_page():
    with pytest.raises(ToolError):
        await mcp.call_tool("search_products", {"query": "shoes", "page_size": 500})


# --- get_product

async def test_get_product_has_price_stock_and_variants():
    result = await call("get_product", product_code="SH-311")
    assert result["price"] == 5499.0 and result["currency"] == "INR"
    assert result["stock"] == "lowStock" and result["stock_level"] == 3
    assert result["categories"] == ["Running Shoes", "Footwear"]
    sizes = {v["size"]: v["stock"] for v in result["variants"]}
    assert sizes == {"7": "inStock", "8": "inStock", "9": "inStock", "10": "outOfStock"}


async def test_get_product_without_variants_omits_the_key():
    result = await call("get_product", product_code="HB-300")
    assert "variants" not in result


async def test_get_unknown_product_is_an_error_result_the_model_can_read():
    with pytest.raises(ToolError, match="Product with code 'NOPE' not found"):
        await mcp.call_tool("get_product", {"product_code": "NOPE"})
