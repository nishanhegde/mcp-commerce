"""Mock SAP Commerce OCC backend (Days 28-30: products, carts and orders).

Same paths, query parameters and error envelope as the real OCC v2 API, so the
MCP tools can't tell the difference. Runs two ways:

  in-process   occ_client mounts this app through httpx's ASGITransport
               (default when OCC_BASE_URL is unset; no second process)
  standalone   uv run uvicorn mock_occ.app:app --port 9001
               then curl 'localhost:9001/occ/v2/shop/products/search?query=shoes'
"""

import uuid
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mcp_commerce.mock_occ.seed import ORDERS, PRODUCTS, price_dto

DEFAULT_PAGE_SIZE = 20

# --- `fields=` handling ------------------------------------------------------
# OCC's response-trimming mini-language: "a,b(c,d(e))". DEFAULT / BASIC / FULL
# are named presets; the mock treats all three as "everything".

FieldTree = dict[str, "FieldTree | None"]


def parse_fields(spec: str) -> FieldTree | None:
    """'products(code,price(value)),pagination' -> nested dict; None = keep all."""
    if not spec or spec.upper() in {"DEFAULT", "BASIC", "FULL"}:
        return None
    tree: FieldTree = {}
    depth, start = 0, 0
    parts: list[str] = []
    for i, ch in enumerate(spec):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(spec[start:i])
            start = i + 1
    parts.append(spec[start:])
    for part in parts:
        name, paren, rest = part.partition("(")
        tree[name.strip()] = parse_fields(rest[:-1]) if paren else None
    return tree


def apply_fields(value: Any, tree: FieldTree | None) -> Any:
    if tree is None:
        return value
    if isinstance(value, list):
        return [apply_fields(v, tree) for v in value]
    if isinstance(value, dict):
        return {k: apply_fields(v, tree[k]) for k, v in value.items() if k in tree}
    return value


# --- errors ------------------------------------------------------------------

def occ_error(status: int, type_: str, message: str) -> JSONResponse:
    """OCC's error envelope: {"errors": [{"type", "message"}]}."""
    return JSONResponse({"errors": [{"type": type_, "message": message}]}, status_code=status)


# --- search ------------------------------------------------------------------

def _parse_query(query: str) -> tuple[str, str, dict[str, str]]:
    """OCC query syntax `freeText:sortCode:facetKey:facetValue[:...]`."""
    parts = query.split(":")
    text = parts[0].strip().lower()
    sort = parts[1] if len(parts) > 1 and parts[1] else "relevance"
    facets = dict(zip(parts[2::2], parts[3::2]))
    return text, sort, facets


def _matches(product: dict, tokens: list[str]) -> bool:
    haystack = " ".join(
        [product["name"], product["summary"], *(c["name"] for c in product["categories"])]
    ).lower()
    return all(token in haystack for token in tokens)


router = APIRouter(prefix="/occ/v2/{base_site_id}")


@router.get("/products/search")
def search_products(
    base_site_id: str,
    query: str = "",
    currentPage: int = 0,
    pageSize: int = DEFAULT_PAGE_SIZE,
    fields: str = "DEFAULT",
):
    text, sort, facets = _parse_query(query)
    results = [p for p in PRODUCTS if _matches(p, text.split())]
    if facets.get("inStockFlag") == "true":
        results = [p for p in results if p["stock"]["stockLevelStatus"] != "outOfStock"]
    if sort in {"price-asc", "price-desc"}:
        results.sort(key=lambda p: p["price"]["value"], reverse=sort == "price-desc")

    total = len(results)
    total_pages = max(1, -(-total // pageSize))
    page = results[currentPage * pageSize : (currentPage + 1) * pageSize]
    body = {
        "freeTextSearch": text,
        "currentQuery": {"query": {"value": query}, "url": f"/search?q={query}"},
        "products": page,
        "pagination": {
            "currentPage": currentPage,
            "pageSize": pageSize,
            "sort": sort,
            "totalPages": total_pages,
            "totalResults": total,
        },
        "sorts": [{"code": c, "selected": c == sort} for c in ("relevance", "price-asc", "price-desc")],
        "facets": [
            {"name": "Availability", "priority": 0, "values": [{"name": "In Stock", "query": {"query": {"value": f"{text}:{sort}:inStockFlag:true"}}}]},
            {"name": "Price", "priority": 1, "values": []},
        ],
        "breadcrumbs": [],
    }
    return apply_fields(body, parse_fields(fields))


@router.get("/products/{product_code}")
def get_product(base_site_id: str, product_code: str, fields: str = "DEFAULT"):
    for product in PRODUCTS:
        if product["code"] == product_code:
            return apply_fields(product, parse_fields(fields))
    # Real OCC answers an unknown product with 400 UnknownIdentifierError.
    return occ_error(400, "UnknownIdentifierError", f"Product with code '{product_code}' not found!")


# --- carts -------------------------------------------------------------------
# State is in memory: it resets when the process restarts (and in tests, via
# reset_state()). Internal shape: {"code", "guid", "lines": [{"product", "quantity"}]};
# the OCC DTO is derived from it on every read.

CARTS: dict[str, dict] = {}
_cart_counter = 1000


def reset_state() -> None:
    global _cart_counter
    CARTS.clear()
    _cart_counter = 1000


def _by_code(product_code: str) -> dict | None:
    return next((p for p in PRODUCTS if p["code"] == product_code), None)


def _entry_dto(number: int, line: dict) -> dict:
    product = line["product"]
    unit = product["price"]["value"]
    return {
        "entryNumber": number,
        "quantity": line["quantity"],
        "basePrice": price_dto(unit),
        "totalPrice": price_dto(unit * line["quantity"]),
        "product": {"code": product["code"], "name": product["name"], "url": product["url"]},
        "updateable": True,
    }


def _cart_dto(cart: dict) -> dict:
    entries = [_entry_dto(i, line) for i, line in enumerate(cart["lines"])]
    total = sum(e["totalPrice"]["value"] for e in entries)
    return {
        "type": "cartWsDTO",
        "code": cart["code"],
        "guid": cart["guid"],
        "entries": entries,
        "totalItems": len(entries),
        "totalUnitCount": sum(e["quantity"] for e in entries),
        "subTotal": price_dto(total),
        "totalPrice": price_dto(total),
        "totalTax": price_dto(0),
        "appliedVouchers": [],
        "net": True,
        "store": "shop",
    }


def _cart_or_error(cart_id: str) -> dict | JSONResponse:
    # Real OCC: 400 CartError. Carts are scoped to the user, which the mock ignores.
    return CARTS.get(cart_id) or occ_error(400, "CartError", "Cart not found.")


class ProductRef(BaseModel):
    code: str


class OrderEntryIn(BaseModel):
    product: ProductRef
    quantity: int = 1


@router.post("/users/{user_id}/carts", status_code=201)
def create_cart(base_site_id: str, user_id: str, fields: str = "DEFAULT"):
    global _cart_counter
    _cart_counter += 1
    cart = {"code": f"{_cart_counter:08d}", "guid": str(uuid.uuid4()), "lines": []}
    CARTS[cart["code"]] = cart
    return apply_fields(_cart_dto(cart), parse_fields(fields))


@router.get("/users/{user_id}/carts/{cart_id}")
def get_cart(base_site_id: str, user_id: str, cart_id: str, fields: str = "DEFAULT"):
    cart = _cart_or_error(cart_id)
    if isinstance(cart, JSONResponse):
        return cart
    return apply_fields(_cart_dto(cart), parse_fields(fields))


@router.post("/users/{user_id}/carts/{cart_id}/entries")
def add_cart_entry(
    base_site_id: str, user_id: str, cart_id: str, body: OrderEntryIn, fields: str = "DEFAULT"
):
    """Returns an OCC CartModification. Stock problems are a 200 with a statusCode
    (success / lowStock / noStock), exactly like the real API: the caller must
    read statusCode, not just the HTTP status."""
    cart = _cart_or_error(cart_id)
    if isinstance(cart, JSONResponse):
        return cart
    product = _by_code(body.product.code)
    if product is None:
        return occ_error(400, "UnknownIdentifierError", f"Product with code '{body.product.code}' not found!")
    if body.quantity < 1:
        return occ_error(400, "CartEntryError", "Quantity must be at least 1.")

    line = next((l for l in cart["lines"] if l["product"]["code"] == product["code"]), None)
    in_cart = line["quantity"] if line else 0
    available = max(0, product["stock"].get("stockLevel", 0) - in_cart)
    added = min(body.quantity, available)
    status = "noStock" if added == 0 else "success" if added == body.quantity else "lowStock"

    if added:
        if line is None:
            line = {"product": product, "quantity": 0}
            cart["lines"].append(line)
        line["quantity"] += added
    modification = {
        "statusCode": status,
        "quantityAdded": added,
        "quantity": body.quantity,
        "entry": _entry_dto(cart["lines"].index(line), line) if line else None,
    }
    return apply_fields(modification, parse_fields(fields))


# --- orders ------------------------------------------------------------------
# Read-only seed data (see mock_occ/seed.py); orders are created by checkout,
# which is out of scope for v1.

@router.get("/users/{user_id}/orders")
def order_history(
    base_site_id: str,
    user_id: str,
    currentPage: int = 0,
    pageSize: int = 20,
    statuses: str = "",
    fields: str = "DEFAULT",
):
    wanted = {s.strip() for s in statuses.split(",") if s.strip()}
    matches = [o for o in ORDERS if not wanted or o["status"] in wanted]
    matches.sort(key=lambda o: o["placed"], reverse=True)  # newest first, like OCC
    page = matches[currentPage * pageSize : (currentPage + 1) * pageSize]
    body = {
        "orders": [
            {k: o[k] for k in ("code", "guid", "placed", "status", "statusDisplay")}
            | {"total": o["totalPrice"]}
            for o in page
        ],
        "pagination": {
            "currentPage": currentPage,
            "pageSize": pageSize,
            "sort": "byDate",
            "totalPages": max(1, -(-len(matches) // pageSize)),
            "totalResults": len(matches),
        },
        "sorts": [{"code": "byDate", "selected": True}, {"code": "byOrderNumber", "selected": False}],
    }
    return apply_fields(body, parse_fields(fields))


@router.get("/users/{user_id}/orders/{order_code}")
def get_order(base_site_id: str, user_id: str, order_code: str, fields: str = "DEFAULT"):
    order = next((o for o in ORDERS if o["code"] == order_code), None)
    if order is None:
        return occ_error(400, "UnknownIdentifierError", f"Order with code '{order_code}' not found")
    return apply_fields(order, parse_fields(fields))


app = FastAPI(title="Mock SAP Commerce OCC")
app.include_router(router)
