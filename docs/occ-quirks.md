# OCC quirks this server smooths over

The OCC API was designed for storefronts written by people who read its docs.
An LLM agent reads only the tool descriptions and results, so every place OCC
is surprising becomes a place the model guesses wrong. This lists each one and
where the server absorbs it. Written Day 33, from the code; the mock reproduces
each quirk so tests cover it. Behaviour that varies by SAP Commerce version or
configuration is marked *(varies)*: check it against the target environment.

## Responses

| Quirk | What OCC does | What the model sees instead | Where |
|---|---|---|---|
| Huge default payloads | `DEFAULT`/`FULL` return images, URLs, facets, breadcrumbs, `formattedValue`s. A 5-hit search is ~7,200 tokens | `fields=` on every call, then a flat snake_case mapping. Same search: 225 tokens | each tool's `*_FIELDS`; `docs/tool-surface.md` |
| `fields=` fails silently | Asking for a field that doesn't exist omits it, no error. A typo in a `fields` string looks like missing data | Optional fields are read with `.get()` and left out when absent, never sent as `null`. The tests pin the expected shape, so a typo shows up as a failing test | `orders._shipment`, `catalog.get_product` |
| Currency on every price | `price: {value, currencyIso, formattedValue, priceType}` per price | One top-level `currency`; prices are bare numbers the model can compare | all tools |
| Localized labels | `statusDisplay` and friends are translated per site | Status codes only | `orders.py` |
| Generic variant qualifiers | Sizes and colours are `variantOptionQualifiers: [{qualifier, value}]` | Flattened to `{"code": ..., "size": "9", "stock": ...}` | `catalog._variant` |
| Optional `stockLevel` *(varies)* | The number appears only if the site exposes it; `stockLevelStatus` is always there | `stock` always; `stock_level` only when OCC sent it | `catalog.get_product` |
| Timestamps | `2026-08-08T10:15:00+0000`, an offset with no colon | Date part only (`placed`, `shipped_on`). The model doesn't need the time of day, and nothing parses the offset | `orders.py` |
| Non-standard consignment fields *(varies)* | `carrier` and `estimatedDelivery` aren't core OCC; they come from a shipping integration, if any | Included only when present. The tool description tells the model never to guess them | `orders.STATUS_FIELDS`, `get_order_status` docstring |
| Order lines not on the status call | The fields you need depend on the question | `get_order_status` fetches `entries(...)` too, so a delayed order can be acted on without a second call | `orders.py` (Day 31) |

## Requests

| Quirk | What OCC does | How the server handles it | Where |
|---|---|---|---|
| Search query mini-language | `freeText:sortCode:facetKey:facetValue`, e.g. `running:price-asc:inStockFlag:true` | The model passes `query`, `sort`, `in_stock_only`; the string is built server-side | `catalog.search_products` |
| No price-range filter | Price filtering only through predefined facet ranges *(varies)* | No `max_price` argument (it would make the page counts wrong). `sort=price_asc` plus reading the prices instead | `docs/tool-surface.md`, "Decided Day 28" |
| Zero-based pages, past-the-end is 200 | `currentPage=7` on a 1-page result returns an empty list, HTTP 200 | Explicit "page 7 is past the end … nothing is wrong with the query", so the model doesn't rewrite a good query | `shaping.past_last_page` |
| Unbounded sub-lists | Variants, cart entries and order lines aren't paged | Capped, with `truncated` and `<list>_total` | `shaping.py` |
| B2C vs B2B paths | `users/current/...` vs `orgUsers/current/...` | `OCC_USER_PATH` config, never a tool argument | `occ_client.py` |

## Writes

| Quirk | What OCC does | How the server handles it | Where |
|---|---|---|---|
| Stock failure is a success | Add-to-cart with no stock returns **HTTP 200** with `statusCode: noStock` and `quantityAdded: 0`; partial stock is `lowStock` | `noStock` or `quantityAdded == 0` → an error result. `lowStock` → success with a message saying how many were actually added | `cart.add_to_cart` |
| Add-to-cart isn't idempotent | Repeating the POST adds the quantity again | Never retried after it may have reached the server. The error tells the model to `get_cart` first | `occ_client.py` |

## Failures and limits

| Quirk | What happens | How the server handles it | Where |
|---|---|---|---|
| Error envelope | `{"errors": [{"type", "message"}]}`, or an HTML error page from a proxy | The `message`s are passed through; an unparseable body becomes "HTTP 502"-style text | `occ_client._error_message` |
| Token endpoint is outside OCC | `/authorizationserver/oauth/token`, not under `/occ/v2` | Separate path; password grant for a customer, client_credentials otherwise | `occ_client._TokenSource` |
| Tokens expire mid-session | 401 `InvalidTokenError` | Renewed 30s before `expires_in`; on a 401, one refresh and one replay | `occ_client.py` |
| Rate limits come from in front of OCC *(varies)* | OCC itself doesn't throttle per client; a CDN, WAF or API gateway in front of it does, with 429 and sometimes no `Retry-After` | `Retry-After` ≤ 5s is waited out (any method: a 429 was never executed). Longer goes back to the model as "wait about N seconds"; a missing header counts as 1s | `occ_client.py` |
| 5xx and timeouts | Transient, or a write that may have committed | Reads retry twice with jittered backoff; writes don't | `occ_client.py` (Day 32) |

## Not smoothed over (yet)

- **Anonymous and guest carts** use a `guid` and `users/anonymous`. v1 is a single
  logged-in user.
- **Multiple base sites** (one store per country) need per-session site
  resolution; today it's one `OCC_BASE_SITE`.
- **Cart expiry.** OCC drops abandoned carts after a configurable period, and a
  stale `cart_id` then comes back as a 400 "Cart not found". The model sees that
  message, but nothing tells it to create a new cart.
- **A client-side concurrency limit.** Parallel tool calls each make their own
  OCC request. Fine for one shopper; a shared deployment would want a semaphore
  in `OccClient`.
