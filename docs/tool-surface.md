# Tool-surface design — mcp-commerce v1

Which parts of the SAP Commerce OCC API this server exposes to an LLM agent,
under what names, and why. Written Day 26, before the tools exist; Days 27–30
implement it. Where the implementation drifts, update this doc, not the reverse.

## Design principles

1. **Tools model agent tasks, not endpoints.** OCC has hundreds of endpoints. An
   agent choosing between 80 tools picks worse than one choosing between 10.
   Each tool answers one question an agent actually asks ("where is my order?"),
   even when that means trimming or combining OCC responses.
2. **The model never picks the identity.** OCC paths are
   `/{baseSiteId}/users/{userId}/...`. `baseSiteId` and `userId` are **server
   configuration**, not tool arguments. If the model could pass `userId`, a
   prompt injection could read someone else's orders. v1 is single-user
   (`current`); multi-user means per-session identity resolved from the auth
   layer, still never a tool argument.
3. **Trim responses.** OCC returns very large JSON by default. Every call uses
   the OCC `fields=` parameter so the model sees only what it needs. Tokens are
   cost and noise.
4. **Errors are answers, not exceptions.** An unknown order returns a short,
   model-readable message ("No order found for code X"), as `get_order_status`
   already does, so the agent can recover instead of the call failing opaquely.
5. **Read and write are separated and annotated.** Read tools are marked
   read-only/idempotent via MCP tool annotations; cart mutations are not. Hosts
   can then auto-approve reads and prompt on writes.

## Shipped vs. deferred (updated Day 30)

Shipped in v1 (Days 28-30), per the roadmap: `search_products`, `get_product`,
`create_cart`, `get_cart`, `add_to_cart`, `get_order_history`, `get_order_status`.
Designed below but **deferred**: `check_stock` (stock status already comes back on
search and detail), `update_cart_entry`, `remove_cart_entry`, `get_order`. The
order-history tool is named `get_order_history`, as in the roadmap.

## Endpoints in v1

Base: `/occ/v2/{baseSiteId}` (all paths below are relative to it).

| Capability | OCC endpoint | Tool |
|---|---|---|
| Product search | `GET /products/search?query&currentPage&pageSize&fields` | `search_products` |
| Product detail | `GET /products/{productCode}?fields` | `get_product` |
| Stock | `GET /products/{productCode}/stock/{storeName}` (and `stock` on product detail) | `check_stock` |
| Create cart | `POST /users/current/carts` | `create_cart` |
| Read cart | `GET /users/current/carts/{cartId}` | `get_cart` |
| Add to cart | `POST /users/current/carts/{cartId}/entries` | `add_to_cart` |
| Change quantity | `PATCH /users/current/carts/{cartId}/entries/{entryNumber}` | `update_cart_entry` |
| Remove line | `DELETE /users/current/carts/{cartId}/entries/{entryNumber}` | `remove_cart_entry` |
| Order history | `GET /users/current/orders?currentPage&pageSize&statuses` | `get_order_history` |
| Order detail | `GET /users/current/orders/{code}` | `get_order` |
| Order status (WISMO) | same endpoint as order detail, heavily trimmed | `get_order_status` |

Verify exact stock endpoint shape and `fields` support against the target
environment's OCC version when wiring Day 27; stock is the endpoint most likely
to differ between SAP Commerce versions and configurations.

## Tools

All tool descriptions are written for the model: say what it does *and when to
use it*. Names are `verb_noun`, snake_case, no vendor prefix (the MCP server name
already namespaces them).

### Catalog (read-only)

**`search_products(query: str, page: int = 0, page_size: int = 10, sort: "relevance" | "price_asc" | "price_desc" = "relevance", in_stock_only: bool = False)`**
Full-text product search. Returns code, name, price, stock status, and
pagination (`total_results`, `total_pages`), with `currency` once at the top.
`page_size` is bounded to 1-20 in the tool schema so the model can't pull the
whole catalog into context, and sees the limit up front.

**`get_product(product_code: str)`**
Detail for one product: name, summary description, price, categories, stock
status. Use after a search, or when the user names a product code.

**`check_stock(product_code: str, store_name: str | None = None)`**
Availability: `stock_level_status` (`inStock` / `lowStock` / `outOfStock`) and
the level where OCC exposes it. With `store_name`, availability at that
point-of-service; without, online availability. Separate from `get_product`
because "is it in stock at the Pune store?" is its own question.

### Cart (write)

**`create_cart()`** → `{cart_id}`. Creates an empty cart for the configured user.
**`get_cart(cart_id: str)`** (read-only) → entries (entry number, product code,
name, quantity, unit price, line total), subtotal, total, item count.
**`add_to_cart(cart_id: str, product_code: str, quantity: int = 1)`** → the line
(`entry_number`, `quantity_added`, `quantity_in_cart`, `line_total`). Guard rails
(Day 29): `quantity` is 1-10 per call, enforced in the tool schema. OCC reports
stock problems as **HTTP 200 + `statusCode`**, so the tool reads `statusCode`:
`lowStock` adds what exists and returns a `message` saying how many; `noStock`
(or nothing added) is an error result and the cart is unchanged. Unknown product
or cart are OCC 400s, passed through as error results.
**`update_cart_entry(cart_id: str, entry_number: int, quantity: int)`** →
updated cart. Sets, not increments. `quantity=0` is rejected with a message
pointing at `remove_cart_entry`: one obvious way to delete, no overloaded zero.
**`remove_cart_entry(cart_id: str, entry_number: int)`** → updated cart.

Cart ids are passed explicitly rather than held in server session state. It
keeps the server stateless (it already runs with streamable HTTP and could be
horizontally scaled) and makes tool calls reproducible in evals.

### Orders (read-only)

**`get_order_history(page: int = 0, page_size: int = 5, status: str | None = None)`**
Order history, newest first: code, placed date, status, total. Use when the
customer doesn't know their order number.

**`get_order(order_code: str)`**
Full order: entries, totals, delivery address (city/postcode only, not the full
street address), payment type, consignments.

**`get_order_status(order_code: str)`**
The WISMO tool — the one the Month 1 → Month 2 demo runs. Returns only: order
status, per-consignment status, tracking id, and shipped date. Deliberately
narrower than `get_order`: for "where is my order?" the model needs five fields,
not forty, and a small result is harder to mis-summarize.

> OCC carries `trackingID` on consignments, but **carrier name and estimated
> delivery are not standard OCC fields**: they come from a shipping integration
> or custom attributes. v1 returns what OCC actually has and the mock may extend
> the consignment with those fields, marked as such. The tool must never invent
> a carrier or ETA the backend didn't provide.

## Tool descriptions (the text the model sees)

Written Day 27. These become the docstrings verbatim on Day 28+. The model picks
a tool from name + description alone, so each one states *when to call it*, *when
not to*, and what comes back. Pattern: one sentence of what, one of when, one of
the confusable neighbour ("not X, use Y").

| Tool | Description |
|---|---|
| `search_products` | Search the catalog by keyword and return matching products with price and stock status. Call this when the customer describes what they want ("running shoes", "USB-C hub") and you don't have a product code. Results are paged; if `total_pages` > 1 and nothing fits, request the next page rather than repeating the query. To inspect one specific product use `get_product`. |
| `get_product` | Get details for one product by its code: name, description, price, categories, stock status. Call this after `search_products` when the customer asks about a specific result, or when they give you a product code directly. Not for browsing; use `search_products` for that. |
| `check_stock` | Check whether a product is available, online or at a specific store. Call this when the customer asks "is it in stock?" or "can I pick it up at <store>?". Returns `inStock`, `lowStock` or `outOfStock`, plus a quantity only if the backend exposes one. Do not promise delivery dates from this; it reports availability only. |
| `create_cart` | Create a new, empty shopping cart and return its `cart_id`. Call this once, when the customer first wants to buy something and you have no cart yet. Reuse the returned `cart_id` for every later cart call; do not create a second cart for the same conversation. |
| `get_cart` | Show the contents of a cart: each line with quantity and price, plus subtotal and total. Call this to answer "what's in my cart?" or to verify state after any cart change. Read-only. |
| `add_to_cart` | Add a product to a cart. Call this when the customer says they want to buy or add an item. Needs a `cart_id` (from `create_cart`) and a `product_code` (from search or detail). Adds to the existing quantity if the product is already in the cart. To set an exact quantity use `update_cart_entry`. |
| `update_cart_entry` | Set the quantity of an existing cart line to an exact number. Call this for "make it 3" or "change to 2". `entry_number` comes from `get_cart`. To remove a line use `remove_cart_entry`; quantity 0 is rejected. |
| `remove_cart_entry` | Delete one line from a cart. Call this for "remove the keyboard" or "take that out". `entry_number` comes from `get_cart`. Removes the whole line regardless of quantity. |
| `get_order_history` | List the customer's past orders, newest first, with code, date, status and total. Call this when the customer wants their order history or doesn't know their order number. Then call `get_order_status` or `get_order` with a code from the result. |
| `get_order` | Get the full record of one order: items, totals, payment type, delivery city and shipment details. Call this for questions about what was in an order or what it cost. For "where is my order?" use `get_order_status`, which is smaller and faster to read. |
| `get_order_status` | Answer "where is my order?" Returns the order status, each shipment's status, tracking number and shipped date. Call this when the customer asks about delivery, shipping, or order location. If an order has no tracking number yet, say so; never guess a carrier or delivery date. |

Parameter docs go in the docstring `Args:` block, not the description above:
state the *format* ("order code, digits only, e.g. `1042`") and the *source*
("from `get_cart`"). Source hints are what let the model chain tools without
asking the customer for internal ids.

## Response shapes (token-lean)

Rules:

1. **Always pass OCC `fields=`.** OCC's `DEFAULT` level on search returns facets,
   sorts, breadcrumbs, images, classifications and spell-check suggestions;
   `FULL` on an order is worse. The `fields` string is built per tool and lives
   next to the tool, not in config.
2. **Flatten and rename to snake_case** so the model sees a consistent house
   style, not OCC's nested camelCase. The mapping is one small function per tool.
3. **Currency once, not per price.** Prices are numbers (the model must compare
   "under ₹5000"); `currency` is a single top-level field. No `formattedValue`
   strings: they duplicate `value`, and the model formats fine on its own.
4. **Status as a code, never a localized label.** `statusDisplay` is dropped.
5. **No images, URLs, or media.** The model cannot see them and they are the
   longest strings in any OCC payload.
6. **Counts and flags over lists where the question allows it.** Order list rows
   carry `item_count`, not entries; entries live in `get_order`.
7. **Cap, and say so.** If a result is truncated, include `truncated: true` and
   how to get the rest (next page). Silent truncation reads as "that's all there is".

Examples (mock data):

`search_products("running shoes")` — `fields=products(code,name,price(value,currencyIso),stock(stockLevelStatus)),pagination(currentPage,pageSize,totalResults,totalPages)`

```json
{
  "currency": "INR",
  "products": [
    {"code": "SH-204", "name": "Cloudrunner 2", "price": 4299, "stock": "inStock"},
    {"code": "SH-311", "name": "Trail Pro",     "price": 5499, "stock": "lowStock"}
  ],
  "page": 0, "total_pages": 3, "total_results": 27
}
```

`get_cart` — `fields=code,entries(entryNumber,quantity,basePrice(value),totalPrice(value),product(code,name)),subTotal(value),totalPrice(value),totalItems`

```json
{
  "cart_id": "00001234", "currency": "INR",
  "entries": [{"entry_number": 0, "product_code": "SH-204", "name": "Cloudrunner 2",
               "quantity": 2, "unit_price": 4299, "line_total": 8598}],
  "subtotal": 8598, "total": 8598, "item_count": 2
}
```

`get_order_status("1042")` — `fields=code,status,consignments(code,status,trackingID,statusDate)`

```json
{
  "order_code": "1042", "status": "SHIPPED",
  "shipments": [{"status": "SHIPPED", "tracking_id": "BD-99120384", "shipped_on": "2026-08-10"}]
}
```

Rough budget (to be measured on Day 28 with the mock, then written back here):
`search_products` page of 10 under ~600 tokens, `get_cart` under ~300,
`get_order_status` under ~100. If a real OCC payload for the same call is 10×
that, the `fields=` string is the first thing to check.

**Decided Day 28: no `max_price` argument.** OCC filters price only through
predefined facet ranges, not arbitrary bounds, so a `max_price` would have to be
faked by post-filtering a page, which makes the pagination numbers lie. Instead
`search_products` exposes what OCC genuinely supports: `sort` (`price_asc` /
`price_desc`) and `in_stock_only` (the `inStockFlag` facet), and the description
tells the model to read the numeric prices for a budget. Measured on the mock:
the trimmed search response is ~670 characters against ~11,900 for the raw one.

## Resources and prompts

- **Resource** `catalog://products` (exists today): static-ish reference data
  the host can attach as context. Stays small; real catalogs are reachable via
  `search_products`, never dumped wholesale.
- **Resource** `store://config` (new, Day 27+): base site, currency, available
  stores. Helps the model pass valid `store_name` values.
- **Prompt** `summarize_order` (exists today): user-invoked template for support
  agents. Prompts are for the *human* to trigger; tools are for the *model*.

## Out of scope for v1 (and why)

- **Checkout / place order, payment:** irreversible, money-moving. Belongs in
  Month 6's `agentic-checkout`, with human confirmation designed in properly.
- **Order cancellation/return:** OCC support depends on the ordermanagement
  add-on; real side effects. Candidate for v2 behind explicit confirmation.
- **Customer account/profile, addresses, consent:** PII surface for no WISMO or
  shopping value in v1.
- **Promotions, vouchers, wishlists, reviews, CMS/ASM:** not needed for the
  demo flows; each adds tools the model might mis-select.
- **Admin / backoffice / ImpEx:** different API, different trust level.

Also retired: the Day 22 toy tools `echo` and `product_lookup` are replaced by
`search_products` / `get_product` once the OCC client lands. `echo` is dropped.

## B2C vs B2B

v1 is B2C-shaped (`users/current`, card payment, anonymous-friendly catalog).
SAP Commerce B2B (the b2bacceleratoraddon / `b2bocc` APIs) changes what a "cart",
an "order" and even a "price" mean. An agent that ignores this will confidently do
the wrong thing. What changes, and what the tool surface does about it:

| Area | B2C | B2B | Impact on the tool surface |
|---|---|---|---|
| **Identity** | One customer = one user | A *user* belongs to an **org unit** (B2BUnit); permissions, budgets and catalogs hang off the unit | Identity still comes from server config. v2 resolves `user` **and** `unit` from the auth layer; neither is ever a tool argument. |
| **API path** | `/users/{userId}/carts`, `/orders` | `/orgUsers/{userId}/carts`, `/orders`, plus `/orgUnits`, `/costcenters`, `/orgCustomers` | The path prefix is config (`OCC_USER_PATH`), so the same tools run against either. Confirm the prefix on the target version. |
| **Pricing** | List price, sale price | Contract / unit-specific price lists, **volume (tiered) pricing**, price visible only after login | `price` may be a range. `get_product` returns `volume_prices: [{min_qty, price}]` when present; `search_products` returns the unit price for qty 1 and never a made-up "from" price. |
| **Quantity rules** | Any positive integer | Minimum order qty, **order multiples**, units of measure (box of 12) | `add_to_cart` / `update_cart_entry` surface the backend's rejection verbatim ("quantity must be a multiple of 12") instead of retrying blind. `get_product` exposes `min_order_qty`, `unit` when set. |
| **Catalog visibility** | Same for everyone | Products and categories can be restricted per unit | A 404 on `get_product` can mean "you can't see it" as well as "doesn't exist". Message: "not available to this account", no distinction leaked. |
| **Checkout** | Pay by card | **Payment type**: card or *account*; **cost center** required for account; **PO number** optional/required | Out of scope in v1 with the rest of checkout, but `get_order` returns `payment_type`, `cost_center`, `purchase_order_number` so support questions work. |
| **Approvals** | None | Orders over a budget go to **approval** (`PENDING_APPROVAL`) before they exist as real orders | New status values. `get_order_status` must treat `PENDING_APPROVAL` / `REJECTED` as first-class and say *"waiting for approval by <approver>"*, not "processing". |
| **Order scope** | "My orders" | My orders **vs the whole unit's** orders (a buyer's colleague placed it) | `get_order_history` gains `scope: "mine" | "unit"` in v2, gated by the user's permissions, not by what the model asks for. |
| **Quick order / bulk** | Rare | Typical: paste 40 SKUs and quantities | One `add_to_cart` per line is 40 calls. v2 candidate: `add_many_to_cart(items: [{product_code, quantity}])`, capped (≤50) and reported per line (added / rejected + reason). |
| **Quotes / replenishment** | None | Quotes, scheduled reorders | Out of scope. They're workflows with state; each wants its own tool group later. |

Two cross-cutting consequences:

- **Statuses are an open set.** B2B adds order states (approval, rejected, merchant
  check) and consignment states vary by fulfilment config. The server passes
  `status` through as a string and the tool descriptions list *examples*, never
  an exhaustive enum. A closed `Literal[...]` would turn a new status into a
  validation error.
- **Partial failure is normal in B2B.** A cart with 20 lines can have 3 rejected
  for qty rules. Cart-mutating tools return what succeeded *and* what didn't,
  per line, rather than all-or-nothing.

v1 stays B2C because the mock models B2C. What v1 *does* lock in: no hardcoded
path prefix, statuses as strings, `get_order` fields that already include
B2B-only values when present (omitted when null, so B2C responses stay lean).

## Mock vs. sandbox

**Decision: build v1 against an OCC-shaped mock that lives *inside* this repo
(`mock_occ/`), not against the Month 1 mock and not against a real instance.**

- **Not the Month 1 mock.** It isn't OCC-shaped: flat `/products/{sku}`, integer
  rupee prices, no `baseSiteId`/`users/current`, no `consignments`. Reshaping it
  would break Month 1's tests and its portfolio story, and this repo is meant to
  be open source: it can't depend on a sibling private repo.
- **OCC-shaped means the same paths and field names** (`code`, `price.value`,
  `stock.stockLevelStatus`, `entries[].entryNumber`, `consignments[].trackingID`,
  `pagination.totalResults`), seeded with a handful of products and orders
  including order `1042` so the WISMO demo stays comparable across months.
- **Swap path:** the HTTP client takes `OCC_BASE_URL` and OCC credentials from
  env. Moving to a real sandbox is a config change, not a code change: if the
  mock honors the OCC contract, the tools can't tell the difference. That is
  the claim this repo's README can make.
- **Real sandbox: deferred.** There is no free public SAP Commerce Cloud
  sandbox. Revisit only if a project client allows access.

## Two different auth problems (don't conflate them)

1. **Agent/host → this MCP server:** Day 25. Bearer token via `MCP_API_KEY`.
2. **This MCP server → OCC:** Day 27. OCC uses OAuth2 (`/authorizationserver/oauth/token`);
   the server will fetch and cache a token from `OCC_CLIENT_ID`, `OCC_CLIENT_SECRET`
   (plus user credentials if using the password grant) from env. The mock will
   accept a fixed token so the same code path is exercised. Never hardcoded,
   never returned in a tool result or log line.
