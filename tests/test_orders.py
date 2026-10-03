"""Day 30: order tools (the WISMO backbone), through the MCP server against the mock OCC."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server import mcp


async def call(name: str, **args) -> dict:
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


# --- get_order_history

async def test_history_is_newest_first_and_trimmed():
    result = await call("get_order_history", page_size=10)
    assert [o["order_code"] for o in result["orders"]] == ["1050", "1042", "1038", "1029"]
    assert result["orders"][1] == {"order_code": "1042", "placed": "2026-08-08", "status": "IN_PROCESS", "total": 8598.0}
    assert result["currency"] == "INR" and result["total_results"] == 4


async def test_history_pagination():
    page1 = await call("get_order_history", page_size=3, page=1)
    assert page1["total_pages"] == 2
    assert [o["order_code"] for o in page1["orders"]] == ["1029"]


async def test_history_status_filter():
    result = await call("get_order_history", status="COMPLETED")
    assert [o["order_code"] for o in result["orders"]] == ["1038"]


async def test_history_no_match_explains_itself():
    result = await call("get_order_history", status="NOPE")
    assert result["orders"] == [] and "No orders found with status NOPE" in result["message"]


async def test_history_rejects_oversized_page():
    with pytest.raises(ToolError):
        await mcp.call_tool("get_order_history", {"page_size": 100})


# --- get_order_status

async def test_wismo_order_1042():
    assert await call("get_order_status", order_code="1042") == {
        "order_code": "1042",
        "status": "IN_PROCESS",
        "shipments": [{
            "status": "SHIPPED", "tracking_id": "BD-99120384", "carrier": "BlueDart",
            "estimated_delivery": "2026-08-13", "shipped_on": "2026-08-10",
        }],
    }


async def test_missing_optional_fields_are_omitted_not_invented():
    shipment = (await call("get_order_status", order_code="1038"))["shipments"][0]
    assert shipment["status"] == "DELIVERY_COMPLETED" and shipment["carrier"] == "Delhivery"
    assert "estimated_delivery" not in shipment  # the backend had none


async def test_undispatched_order_says_so():
    result = await call("get_order_status", order_code="1050")
    assert result["shipments"] == [] and "not been dispatched" in result["message"]


async def test_unknown_order_is_an_error_result():
    with pytest.raises(ToolError, match="Order with code '9999' not found"):
        await mcp.call_tool("get_order_status", {"order_code": "9999"})


async def test_response_leaks_no_address_or_payment_data():
    text = (await mcp.call_tool("get_order_status", {"order_code": "1042"})).content[0].text
    assert "Pune" not in text and "Example Road" not in text and "CARD" not in text
