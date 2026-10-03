"""Seed catalog for the mock OCC backend.

Shaped like SAP Commerce OCC `ProductWsDTO` responses, including the verbose
parts (images, url, ratings, manufacturer) that the MCP tools deliberately strip.
That bulk is the point: it makes `fields=` trimming measurable.
"""

CURRENCY = "INR"

_SIZES = ["7", "8", "9", "10"]


def price_dto(value: float) -> dict:
    return {
        "currencyIso": CURRENCY,
        "priceType": "BUY",
        "value": value,
        "formattedValue": f"₹{value:,.2f}",
    }


def _stock(status: str, level: int | None) -> dict:
    stock: dict = {"stockLevelStatus": status, "isValueRounded": False}
    if level is not None:
        stock["stockLevel"] = level
    return stock


def _product(
    code: str,
    name: str,
    price: float,
    status: str,
    level: int | None,
    summary: str,
    categories: list[str],
    sizes: list[str] | None = None,
) -> dict:
    slug = name.lower().replace(" ", "-")
    product = {
        "code": code,
        "name": name,
        "url": f"/p/{slug}/{code}",
        "summary": summary,
        "description": f"<p>{summary}</p><ul><li>Free returns</li><li>1 year warranty</li></ul>",
        "manufacturer": "Acme Sports",
        "price": price_dto(price),
        "stock": _stock(status, level),
        "purchasable": status != "outOfStock",
        "availableForPickup": status != "outOfStock",
        "averageRating": 4.3,
        "numberOfReviews": 128,
        "configurable": False,
        "images": [
            {"imageType": "PRIMARY", "format": fmt, "url": f"/medias/{code}-{fmt}.jpg"}
            for fmt in ("thumbnail", "product", "zoom")
        ],
        "categories": [{"code": c.lower().replace(" ", "-"), "name": c} for c in categories],
    }
    if sizes:
        def size_status(size: str) -> str:
            # Out of stock when the base product is; on a low-stock product the
            # largest size is the one that has sold through.
            if status == "outOfStock" or (status == "lowStock" and size == sizes[-1]):
                return "outOfStock"
            return "inStock"

        product["variantOptions"] = [
            {
                "code": f"{code}-{size}",
                "url": f"/p/{slug}/{code}-{size}",
                "priceData": price_dto(price),
                "stock": _stock(size_status(size), None),
                "variantOptionQualifiers": [{"qualifier": "size", "name": "Size", "value": size}],
            }
            for size in sizes
        ]
    return product


PRODUCTS: list[dict] = [
    _product("SH-204", "Cloudrunner 2", 4299.0, "inStock", 42,
             "Cushioned daily trainer for road running.", ["Running Shoes", "Footwear"], _SIZES),
    _product("SH-311", "Trail Pro", 5499.0, "lowStock", 3,
             "Grippy trail running shoes with a rock plate.", ["Running Shoes", "Footwear"], _SIZES),
    _product("SH-120", "Sprint Lite", 3799.0, "inStock", 120,
             "Lightweight running shoes for short fast runs.", ["Running Shoes", "Footwear"], _SIZES),
    _product("SH-450", "Marathon Elite", 8999.0, "inStock", 15,
             "Carbon-plated running shoes for race day.", ["Running Shoes", "Footwear"], _SIZES),
    _product("SH-099", "Road Racer", 4799.0, "outOfStock", 0,
             "Responsive running shoes for tempo days.", ["Running Shoes", "Footwear"], _SIZES),
    _product("SK-010", "Performance Socks", 499.0, "inStock", 500,
             "Cushioned cotton socks for long distances.", ["Accessories"]),
    _product("WB-500", "Insulated Water Bottle", 799.0, "inStock", 60,
             "750 ml steel bottle, keeps drinks cold for 24 hours.", ["Accessories"]),
    _product("KB-200", "Mechanical Keyboard", 8999.0, "outOfStock", 0,
             "Tenkeyless keyboard with hot-swappable switches.", ["Electronics"]),
    _product("HB-300", "USB-C Hub", 3450.0, "inStock", 33,
             "Seven-port USB-C hub with HDMI and card reader.", ["Electronics"]),
]


# --- orders ------------------------------------------------------------------
# Status vocabularies vary between SAP Commerce configurations; the tools pass
# them through as strings. `carrier` and `estimatedDelivery` on a consignment are
# NOT standard OCC fields (they come from a shipping integration or custom
# attributes), so the mock marks them as extensions and some orders omit them.

def _order(code: str, placed: str, status: str, lines: list[tuple[str, int]], consignments: list[dict]) -> dict:
    by_code = {p["code"]: p for p in PRODUCTS}
    entries = [
        {
            "entryNumber": i,
            "quantity": qty,
            "basePrice": price_dto(by_code[sku]["price"]["value"]),
            "totalPrice": price_dto(by_code[sku]["price"]["value"] * qty),
            "product": {"code": sku, "name": by_code[sku]["name"]},
        }
        for i, (sku, qty) in enumerate(lines)
    ]
    return {
        "code": code,
        "guid": f"guid-{code}",
        "placed": f"{placed}T10:15:00+0000",
        "status": status,
        "statusDisplay": status.replace("_", " ").title(),
        "entries": entries,
        "consignments": consignments,
        "totalItems": len(entries),
        "totalPrice": price_dto(sum(e["totalPrice"]["value"] for e in entries)),
        "paymentType": {"code": "CARD", "displayName": "Credit Card"},
        "deliveryAddress": {"town": "Pune", "postalCode": "411001", "line1": "12 Example Road"},
    }


ORDERS: list[dict] = [
    # The Month 1 WISMO order: shipped, in transit.
    _order("1042", "2026-08-08", "IN_PROCESS", [("SH-204", 2)], [{
        "code": "a1042", "status": "SHIPPED", "statusDate": "2026-08-10T09:00:00+0000",
        "shippingDate": "2026-08-10T09:00:00+0000", "trackingID": "BD-99120384",
        "carrier": "BlueDart", "estimatedDelivery": "2026-08-13",
    }]),
    # Placed but not dispatched: no consignments yet.
    _order("1050", "2026-09-28", "CREATED", [("HB-300", 1), ("SK-010", 2)], []),
    # Delivered long ago.
    _order("1038", "2026-07-20", "COMPLETED", [("WB-500", 1)], [{
        "code": "a1038", "status": "DELIVERY_COMPLETED", "statusDate": "2026-07-24T14:30:00+0000",
        "shippingDate": "2026-07-21T09:00:00+0000", "trackingID": "DT-77120045", "carrier": "Delhivery",
    }]),
    _order("1029", "2026-06-02", "CANCELLED", [("SH-099", 1)], []),
]
