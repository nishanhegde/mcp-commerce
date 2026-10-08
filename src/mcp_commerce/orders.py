"""Order tools' logic: OCC request + response trimming. See catalog.py for the pattern."""

from mcp_commerce.occ_client import OccClient
from mcp_commerce.shaping import MAX_ORDER_ITEMS, MAX_SHIPMENTS, capped, past_last_page

HISTORY_FIELDS = (
    "orders(code,placed,status,total(value,currencyIso)),"
    "pagination(currentPage,totalPages,totalResults)"
)
# carrier / estimatedDelivery are not standard OCC consignment fields. Asking for
# a field the backend lacks just omits it, so one string works on both.
STATUS_FIELDS = (
    "code,status,entries(quantity,product(code,name)),"
    "consignments(status,trackingID,shippingDate,carrier,estimatedDelivery)"
)


async def get_order_history(
    client: OccClient, page: int, page_size: int, status: str | None
) -> dict:
    params: dict = {"currentPage": page, "pageSize": page_size, "fields": HISTORY_FIELDS}
    if status:
        params["statuses"] = status
    raw = await client.get(f"{client.user_path}/orders", **params)

    orders = raw["orders"]
    result: dict = {}
    if orders:
        result["currency"] = orders[0]["total"]["currencyIso"]
    result["orders"] = [
        {
            "order_code": o["code"],
            "placed": o["placed"][:10],  # date only: the time of day is noise
            "status": o["status"],
            "total": o["total"]["value"],
        }
        for o in orders
    ]
    pagination = raw["pagination"]
    result["page"] = pagination["currentPage"]
    result["total_pages"] = pagination["totalPages"]
    result["total_results"] = pagination["totalResults"]
    if not orders:
        result["message"] = past_last_page(page, result["total_pages"], result["total_results"]) or "No orders found" + (f" with status {status}." if status else " for this customer.")
    return result


async def get_order_status(client: OccClient, order_code: str) -> dict:
    raw = await client.get(f"{client.user_path}/orders/{order_code}", fields=STATUS_FIELDS)

    shipments = [_shipment(c) for c in raw.get("consignments", [])]
    # The items let the agent act on a delay (check stock, offer a replacement)
    # without a second round trip to look up what was ordered.
    items = [
        {"product_code": e["product"]["code"], "name": e["product"]["name"], "quantity": e["quantity"]}
        for e in raw.get("entries", [])
    ]
    result: dict = {"order_code": raw["code"], "status": raw["status"]}
    capped(result, "items", items, MAX_ORDER_ITEMS)
    capped(result, "shipments", shipments, MAX_SHIPMENTS)
    if not shipments:
        result["message"] = "No shipments yet: the order has not been dispatched."
    return result


def _shipment(consignment: dict) -> dict:
    shipment = {"status": consignment["status"]}
    # Only what the backend actually provided. Never invent a carrier or an ETA.
    for key, source in (
        ("tracking_id", "trackingID"),
        ("carrier", "carrier"),
        ("estimated_delivery", "estimatedDelivery"),
    ):
        if consignment.get(source):
            shipment[key] = consignment[source]
    if consignment.get("shippingDate"):
        shipment["shipped_on"] = consignment["shippingDate"][:10]
    return shipment
