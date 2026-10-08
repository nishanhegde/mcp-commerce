"""Catalog tools' logic: OCC request + response trimming.

Kept apart from server.py so it can be tested without MCP. Each function owns
its `fields=` string (what we ask OCC for) and its mapping (what the model
sees). See docs/tool-surface.md, "Response shapes".
"""

from typing import Literal

from mcp_commerce.occ_client import OccClient
from mcp_commerce.shaping import MAX_VARIANTS, capped, past_last_page

SortOrder = Literal["relevance", "price_asc", "price_desc"]
_OCC_SORT = {"relevance": "relevance", "price_asc": "price-asc", "price_desc": "price-desc"}

SEARCH_FIELDS = (
    "products(code,name,price(value,currencyIso),stock(stockLevelStatus)),"
    "pagination(currentPage,totalResults,totalPages)"
)
PRODUCT_FIELDS = (
    "code,name,summary,price(value,currencyIso),stock(stockLevelStatus,stockLevel),"
    "categories(name),"
    "variantOptions(code,stock(stockLevelStatus),variantOptionQualifiers(qualifier,value))"
)


async def search_products(
    client: OccClient,
    query: str,
    page: int,
    page_size: int,
    sort: SortOrder,
    in_stock_only: bool,
) -> dict:
    # OCC query syntax: freeText:sortCode[:facetKey:facetValue]. The model never
    # sees this; it gets plain arguments and we build the string here.
    occ_query = f"{query}:{_OCC_SORT[sort]}"
    if in_stock_only:
        occ_query += ":inStockFlag:true"

    raw = await client.get(
        "products/search",
        query=occ_query,
        currentPage=page,
        pageSize=page_size,
        fields=SEARCH_FIELDS,
    )

    products = raw["products"]
    result: dict = {}
    if products:
        # Currency once at the top, not per price (docs: Response shapes, rule 3).
        result["currency"] = products[0]["price"]["currencyIso"]
    result["products"] = [
        {
            "code": p["code"],
            "name": p["name"],
            "price": p["price"]["value"],
            "stock": p["stock"]["stockLevelStatus"],
        }
        for p in products
    ]
    pagination = raw["pagination"]
    result["page"] = pagination["currentPage"]
    result["total_pages"] = pagination["totalPages"]
    result["total_results"] = pagination["totalResults"]
    if not products:
        result["message"] = past_last_page(page, result["total_pages"], result["total_results"]) or (
            f"No products matched '{query}'"
            + (" in stock" if in_stock_only else "")
            + ". Try fewer or different keywords."
        )
    return result


async def get_product(client: OccClient, product_code: str) -> dict:
    raw = await client.get(f"products/{product_code}", fields=PRODUCT_FIELDS)

    result = {
        "code": raw["code"],
        "name": raw["name"],
        "summary": raw.get("summary", ""),
        "price": raw["price"]["value"],
        "currency": raw["price"]["currencyIso"],
        "stock": raw["stock"]["stockLevelStatus"],
    }
    # stockLevel is only exposed by some OCC configurations; omit rather than null.
    if "stockLevel" in raw["stock"]:
        result["stock_level"] = raw["stock"]["stockLevel"]
    result["categories"] = [c["name"] for c in raw.get("categories", [])]
    if raw.get("variantOptions"):
        capped(result, "variants", [_variant(v) for v in raw["variantOptions"]], MAX_VARIANTS)
    return result


def _variant(option: dict) -> dict:
    variant = {"code": option["code"], "stock": option["stock"]["stockLevelStatus"]}
    # Qualifiers are generic (size, color, ...): flatten to {"size": "9"}.
    for q in option["variantOptionQualifiers"]:
        variant[q["qualifier"]] = q["value"]
    return variant
