"""Async HTTP client for the OCC API.

Mode comes from config.OccSettings (OCC_MODE; see config.py):
  mock   in-process mock (mock_occ), no second process to run
  live   a real SAP Commerce instance at OCC_BASE_URL

Both modes go through the same code: the mock honours the OCC contract, so the
tools above this layer can't tell which one they're talking to.

Day 32: failures. Every way a call can fail ends as an OccError, whose message
is written for the model: what happened, and what it can do next. Retries
follow one rule: retry only when repeating the request can't change the
outcome.
  - GETs are idempotent: retry timeouts, dropped connections and 5xx.
  - POSTs (cart writes) are not. If the request reached the server and no clean
    answer came back, the entry may already be in the cart, and a blind retry
    doubles the quantity. So: no retry, and the error tells the model to check
    with get_cart first. The one safe case is a connect failure: the request
    never left this process, so there is nothing to duplicate.
  - An expired token (401) is safe to replay for any method: OCC rejected the
    request before running it. Refresh once, replay once.
  - Rate limiting (429) is the same kind of rejection, so any method may retry,
    but only by waiting out Retry-After, and only if that wait is short. A long
    wait goes back to the model as "wait N seconds", not as a blocked tool call.
"""

import asyncio
import logging
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_commerce.config import OccCredentials, OccSettings

logger = logging.getLogger("mcp-commerce.occ")

OCC_PREFIX = "occ/v2"
TOKEN_PATH = "/authorizationserver/oauth/token"
# Fail fast on connect; reads get longer. Worst case for a GET is three
# attempts (~31s): inside an MCP client's request timeout, but slow in chat.
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
RETRY_DELAYS = (0.25, 1.0)  # seconds before the 2nd and 3rd attempt
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET"})
MAX_RETRY_AFTER = 5.0  # wait out a 429 inside the call only if it's at most this long
DEFAULT_RETRY_AFTER = 1.0  # a 429 without a usable Retry-After header
TOKEN_REFRESH_MARGIN = 30.0  # renew this many seconds before the token expires

# Never sent: safe to retry whatever the method. (ConnectTimeout is also a
# TimeoutException, which is why this tuple is caught first.)
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class OccError(ToolError):
    """A backend failure worded for the model.

    Subclassing ToolError means the SDK turns it into an `is_error=True` result
    with this message, instead of a crash the model can't read.
    """


class _TokenSource:
    """Fetches, caches and renews the OAuth token. One per OccClient."""

    def __init__(self, http: httpx.AsyncClient, credentials: OccCredentials) -> None:
        self._http = http
        self._credentials = credentials
        self._token: str | None = None
        self._expires_at = 0.0
        # Concurrent tool calls can all find the token expired at once. The lock
        # makes the first one fetch and the rest reuse its result.
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        if self._token is None or time.monotonic() >= self._expires_at:
            await self.refresh(stale=self._token)
        assert self._token is not None
        return self._token

    async def refresh(self, stale: str | None) -> None:
        """Get a new token, unless another caller already replaced `stale`."""
        async with self._lock:
            if self._token != stale:
                return
            c = self._credentials
            data = {"client_id": c.client_id, "client_secret": c.client_secret}
            if c.username:
                data |= {"grant_type": "password", "username": c.username, "password": c.password or ""}
            else:
                data["grant_type"] = "client_credentials"
            try:
                response = await self._http.post(TOKEN_PATH, data=data)
            except httpx.TransportError as exc:
                logger.warning("OCC token request failed: %r", exc)
                raise OccError("The commerce backend is unreachable right now. Try again shortly.") from None
            if not response.is_success:
                logger.error("OCC token request rejected: HTTP %s", response.status_code)
                raise OccError(_AUTH_FAILED)
            body = response.json()
            self._token = body["access_token"]
            self._expires_at = time.monotonic() + float(body.get("expires_in", 3600)) - TOKEN_REFRESH_MARGIN
            logger.info("OCC token refreshed")


class OccClient:
    def __init__(
        self,
        base_url: str | None,
        base_site: str,
        user_path: str = "users/current",
        *,
        credentials: OccCredentials | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        ca_bundle: str | None = None,
    ) -> None:
        self._base_site = base_site
        # B2C is users/current; B2B uses orgUsers/current. Config, never a tool argument.
        self.user_path = user_path
        self._retry_delays = retry_delays
        if base_url:
            # `transport` is how tests stand in for a misbehaving backend.
            # A local SAP Commerce serves a self-signed cert on :9002. Trust it
            # by pointing at its CA (OCC_CA_BUNDLE); verification stays on.
            self._http = httpx.AsyncClient(
                base_url=base_url, timeout=TIMEOUT, transport=transport, verify=ca_bundle or True
            )
            logger.info("OCC backend: %s (site=%s)", base_url, base_site)
        else:
            # Imported here so a real-backend deployment never loads the mock.
            from mcp_commerce.mock_occ.app import app as mock_app

            self._http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=mock_app),
                base_url="http://mock-occ",
                timeout=TIMEOUT,
            )
            logger.info("OCC backend: in-process mock (site=%s)", base_site)
        self._auth = _TokenSource(self._http, credentials) if credentials else None

    async def get(self, path: str, **params: str | int) -> dict:
        """GET {base}/occ/v2/{site}/{path}; returns JSON or raises OccError."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: dict | None = None, **params: str | int) -> dict:
        """POST with an optional JSON body; returns JSON or raises OccError."""
        return await self._request("POST", path, params=params, json=json)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"/{OCC_PREFIX}/{self._base_site}/{path}"
        idempotent = method in IDEMPOTENT_METHODS
        retries_left = len(self._retry_delays)
        token_refreshed = False

        while True:
            token = await self._auth.token() if self._auth else None
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                response = await self._http.request(method, url, headers=headers, **kwargs)
            except _NOT_SENT as exc:
                logger.warning("OCC not reached %s %s: %r", method, url, exc)
                if retries_left:
                    retries_left = await self._backoff(retries_left)
                    continue
                raise OccError("The commerce backend is unreachable right now. Try again shortly.") from None
            except httpx.TransportError as exc:
                # Sent, but no clean answer: a read timeout or a dropped connection.
                logger.warning("OCC no response %s %s: %r", method, url, exc)
                if idempotent and retries_left:
                    retries_left = await self._backoff(retries_left)
                    continue
                what = "timed out" if isinstance(exc, httpx.TimeoutException) else "dropped the connection"
                raise OccError(_no_answer(idempotent, what)) from None

            if response.status_code == 401 and self._auth and not token_refreshed:
                logger.info("OCC 401 on %s %s; refreshing token and replaying", method, url)
                await self._auth.refresh(stale=token)
                token_refreshed = True
                continue
            if response.is_success:
                return response.json()
            if response.status_code == 429:
                wait = _retry_after_seconds(response)
                logger.warning("OCC 429 %s %s; Retry-After %.1fs", method, url, wait)
                if retries_left and wait <= MAX_RETRY_AFTER:
                    await asyncio.sleep(wait)
                    retries_left -= 1
                    continue
                raise OccError(
                    f"The commerce backend is rate-limiting requests. Wait about {math.ceil(wait)} seconds "
                    "before calling tools again; don't retry in a loop."
                )
            if response.status_code in RETRYABLE_STATUS:
                logger.warning("OCC HTTP %s %s %s", response.status_code, method, url)
                if idempotent and retries_left:
                    retries_left = await self._backoff(retries_left)
                    continue
                raise OccError(_no_answer(idempotent, f"failed with HTTP {response.status_code}"))
            if response.status_code == 401:
                raise OccError(_AUTH_FAILED)
            # Other 4xx: the request itself is wrong (unknown product, bad
            # quantity). Retrying can't help; OCC's own message tells the model why.
            raise OccError(_error_message(response))

    async def _backoff(self, retries_left: int) -> int:
        delay = self._retry_delays[len(self._retry_delays) - retries_left]
        # Jitter, so many clients that failed together don't retry in lockstep.
        await asyncio.sleep(delay * random.uniform(0.5, 1.5))
        return retries_left - 1


_AUTH_FAILED = (
    "Authentication with the commerce backend failed. This needs an operator to fix; "
    "don't retry. Tell the customer the system can't look this up right now."
)


def _no_answer(idempotent: bool, what: str) -> str:
    if idempotent:
        return f"The commerce backend {what}, after retries. Try again later, and don't guess the answer meanwhile."
    return (
        f"The commerce backend {what} during an update, so it may or may not have been applied. "
        "Call get_cart to see the cart's actual state before trying again."
    )


def segment(value: str, what: str) -> str:
    """Make a tool argument safe to put in a URL path, as exactly one segment.

    httpx resolves dot segments before sending, so an order_code of
    "../../../users/someone@example.com/orders" would request another
    customer's orders, which a client_credentials token is allowed to read.
    The model must never choose the identity (docs/tool-surface.md, principle
    2), and a prompt injection can choose the model's arguments. So "/" is
    percent-encoded, and "." and "..", the only values encoding leaves
    meaningful, are refused.
    """
    if value in ("", ".", ".."):
        raise OccError(f"{value!r} is not a valid {what}.")
    return quote(value, safe="")


def _retry_after_seconds(response: httpx.Response) -> float:
    """Retry-After is either delay-seconds ("30") or an HTTP date."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return DEFAULT_RETRY_AFTER
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def _error_message(response: httpx.Response) -> str:
    """OCC errors are `{"errors": [{"type", "message"}]}`; surface the message."""
    try:
        errors = response.json()["errors"]
        return "; ".join(e["message"] for e in errors)
    except (ValueError, KeyError, TypeError):
        logger.warning("OCC %s with unparseable body", response.status_code)
        return f"The commerce backend returned an error (HTTP {response.status_code})."


def client_from_settings(settings: OccSettings) -> OccClient:
    return OccClient(
        base_url=settings.base_url,  # None in mock mode
        base_site=settings.base_site,
        user_path=settings.user_path,
        credentials=settings.credentials,
        ca_bundle=settings.ca_bundle,
    )
