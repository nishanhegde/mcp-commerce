"""Day 33: response shaping. Compact output, list caps, pagination edges, size budget."""

import json

from mcp_commerce.server import mcp
from mcp_commerce.shaping import RESULT_BUDGET_CHARS, capped, to_text


async def text_of(name: str, **args) -> str:
    return (await mcp.call_tool(name, args)).content[0].text


# --- compact serialization

def test_compact_json_has_no_padding_and_keeps_unicode():
    text = to_text({"name": "Café", "price": 4299.0, "tags": ["a", "b"]}).text
    assert text == '{"name":"Café","price":4299.0,"tags":["a","b"]}'


async def test_tool_results_are_compact():
    text = await text_of("get_order_status", order_code="1042")
    assert "\n" not in text and ": " not in text
    assert json.loads(text)["order_code"] == "1042"


# --- caps

def test_capped_flags_a_cut_list():
    result: dict = {}
    capped(result, "variants", list(range(25)), 20)
    assert result == {"variants": list(range(20)), "truncated": True, "variants_total": 25}


def test_capped_leaves_a_short_list_unflagged():
    result: dict = {}
    capped(result, "variants", [1, 2], 20)
    assert result == {"variants": [1, 2]}


# --- pagination edges

async def test_search_page_past_the_end_is_not_reported_as_no_match():
    result = json.loads(await text_of("search_products", query="running", page=7))
    assert result["products"] == []
    assert "past the end" in result["message"] and "No products matched" not in result["message"]


async def test_search_with_no_matches_still_says_so():
    result = json.loads(await text_of("search_products", query="zzzz"))
    assert "No products matched 'zzzz'" in result["message"]


async def test_history_page_past_the_end():
    result = json.loads(await text_of("get_order_history", page=3, page_size=5))
    assert "past the end" in result["message"]


# --- size budget: a regression guard for every tool at its largest legal request

async def test_every_tool_result_fits_the_budget():
    cart_id = json.loads(await text_of("create_cart"))["cart_id"]
    for code in ("SH-204", "SH-120", "SK-010", "HB-300", "WB-500"):
        await mcp.call_tool("add_to_cart", {"cart_id": cart_id, "product_code": code, "quantity": 2})
    calls = [
        ("search_products", {"query": "running", "page_size": 20}),
        ("get_product", {"product_code": "SH-204"}),
        ("get_cart", {"cart_id": cart_id}),
        ("get_order_history", {"page_size": 20}),
        ("get_order_status", {"order_code": "1042"}),
    ]
    for name, args in calls:
        assert len(await text_of(name, **args)) < RESULT_BUDGET_CHARS, name
