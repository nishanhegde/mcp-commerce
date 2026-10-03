"""Cart tools' logic: OCC request + response trimming. See catalog.py for the pattern."""

from occ_client import OccClient, OccError

CART_FIELDS = (
    "code,totalItems,totalUnitCount,subTotal(value,currencyIso),totalPrice(value),"
    "entries(entryNumber,quantity,basePrice(value),totalPrice(value),product(code,name))"
)
MODIFICATION_FIELDS = (
    "statusCode,quantityAdded,quantity,"
    "entry(entryNumber,quantity,totalPrice(value,currencyIso),product(code,name))"
)


async def create_cart(client: OccClient) -> dict:
    raw = await client.post(f"{client.user_path}/carts", fields="code")
    return {"cart_id": raw["code"]}


async def get_cart(client: OccClient, cart_id: str) -> dict:
    raw = await client.get(f"{client.user_path}/carts/{cart_id}", fields=CART_FIELDS)
    return {
        "cart_id": raw["code"],
        "currency": raw["subTotal"]["currencyIso"],
        "entries": [
            {
                "entry_number": e["entryNumber"],
                "product_code": e["product"]["code"],
                "name": e["product"]["name"],
                "quantity": e["quantity"],
                "unit_price": e["basePrice"]["value"],
                "line_total": e["totalPrice"]["value"],
            }
            for e in raw.get("entries", [])
        ],
        "subtotal": raw["subTotal"]["value"],
        "total": raw["totalPrice"]["value"],
        "item_count": raw["totalUnitCount"],
    }


async def add_to_cart(client: OccClient, cart_id: str, product_code: str, quantity: int) -> dict:
    raw = await client.post(
        f"{client.user_path}/carts/{cart_id}/entries",
        json={"product": {"code": product_code}, "quantity": quantity},
        fields=MODIFICATION_FIELDS,
    )

    # OCC reports stock problems as HTTP 200 + a statusCode, not as an error.
    # Reading only the HTTP status would tell the model "added" when nothing was.
    status = raw["statusCode"]
    added = raw["quantityAdded"]
    if status == "noStock" or added == 0:
        raise OccError(f"Nothing was added: product '{product_code}' is out of stock (or the cart already holds all available units).")

    entry = raw["entry"]
    result = {
        "cart_id": cart_id,
        "entry_number": entry["entryNumber"],
        "product_code": entry["product"]["code"],
        "name": entry["product"]["name"],
        "quantity_added": added,
        "quantity_in_cart": entry["quantity"],
        "line_total": entry["totalPrice"]["value"],
        "currency": entry["totalPrice"]["currencyIso"],
    }
    if status == "lowStock":
        result["message"] = f"Only {added} of the {quantity} requested were available, so {added} were added."
    return result
