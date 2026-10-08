"""Day 32: failure handling and retry policy in the OCC client.

A scripted backend stands in for SAP Commerce: each OCC request consumes the
next outcome (a status code, a JSON body, or an httpx exception class), so a
test reads as "the backend does this, then this".
"""

import asyncio

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_commerce import server
from mcp_commerce.config import OccCredentials
from mcp_commerce.occ_client import TOKEN_PATH, OccClient, OccError

OK = {"ok": True}


class ScriptedOcc:
    def __init__(self, *outcomes, expires_in: int = 3600) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.expires_in = expires_in

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"t{self.token_calls}", "expires_in": self.expires_in})
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome("scripted failure", request=request)
        if isinstance(outcome, httpx.Response):
            return outcome
        if isinstance(outcome, int):
            return httpx.Response(outcome, json={"errors": [{"type": "X", "message": f"backend said {outcome}"}]})
        return httpx.Response(200, json=outcome)

    def client(self, credentials: OccCredentials | None = None) -> OccClient:
        return OccClient(
            "http://occ.test", "shop",
            credentials=credentials,
            transport=httpx.MockTransport(self.handle),
            retry_delays=(0, 0),  # same policy, no real sleeping
        )


CUSTOMER = OccCredentials("mobile_android", "secret", username="demo@example.com", password="pw")


# --- reads: idempotent, so retried

async def test_get_retries_a_5xx_then_succeeds():
    occ = ScriptedOcc(503, OK)
    assert await occ.client().get("products/SH-204") == OK
    assert len(occ.requests) == 2


async def test_get_retries_a_read_timeout_then_succeeds():
    occ = ScriptedOcc(httpx.ReadTimeout, OK)
    assert await occ.client().get("products/SH-204") == OK


async def test_get_gives_up_after_three_attempts():
    occ = ScriptedOcc(500, 502, 500)
    with pytest.raises(OccError, match="failed with HTTP 500, after retries.*don't guess"):
        await occ.client().get("products/SH-204")
    assert len(occ.requests) == 3


async def test_client_errors_are_not_retried():
    occ = ScriptedOcc(400)
    with pytest.raises(OccError, match="backend said 400"):
        await occ.client().get("products/NOPE")
    assert len(occ.requests) == 1


# --- writes: not idempotent, so never retried once sent

async def test_post_timeout_is_not_retried_and_points_at_get_cart():
    occ = ScriptedOcc(httpx.ReadTimeout, OK)
    with pytest.raises(OccError, match="timed out during an update.*may or may not.*get_cart"):
        await occ.client().post("users/current/carts/1/entries", json={"quantity": 1})
    assert len(occ.requests) == 1  # the second outcome was never used


async def test_post_5xx_is_not_retried():
    occ = ScriptedOcc(500, OK)
    with pytest.raises(OccError, match="HTTP 500 during an update.*get_cart"):
        await occ.client().post("users/current/carts/1/entries", json={"quantity": 1})
    assert len(occ.requests) == 1


async def test_post_connect_error_is_retried_because_nothing_was_sent():
    occ = ScriptedOcc(httpx.ConnectError, OK)
    assert await occ.client().post("users/current/carts") == OK
    assert len(occ.requests) == 2


async def test_unreachable_backend_after_retries():
    occ = ScriptedOcc(httpx.ConnectError, httpx.ConnectError, httpx.ConnectError)
    with pytest.raises(OccError, match="unreachable"):
        await occ.client().get("products/SH-204")


# --- rate limiting (Day 33)

def too_many(retry_after: str | None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers, json={"errors": [{"type": "X", "message": "slow down"}]})


async def test_short_retry_after_is_waited_out_even_for_a_write():
    # A 429 was rejected before it ran, so unlike a 5xx a POST may retry it.
    occ = ScriptedOcc(too_many("0"), OK)
    assert await occ.client().post("users/current/carts/1/entries", json={"quantity": 1}) == OK
    assert len(occ.requests) == 2


async def test_long_retry_after_goes_back_to_the_model():
    occ = ScriptedOcc(too_many("120"), OK)
    with pytest.raises(OccError, match="rate-limiting.*about 120 seconds"):
        await occ.client().get("products/SH-204")
    assert len(occ.requests) == 1  # didn't sit in the call for two minutes


async def test_retry_after_as_http_date():
    occ = ScriptedOcc(too_many("Wed, 21 Oct 2015 07:28:00 GMT"), OK)  # in the past: retry now
    assert await occ.client().get("products/SH-204") == OK


async def test_persistent_429_stops_after_the_retry_budget():
    occ = ScriptedOcc(too_many("0"), too_many("0"), too_many("0"))
    with pytest.raises(OccError, match="rate-limiting"):
        await occ.client().get("products/SH-204")
    assert len(occ.requests) == 3


# --- auth

async def test_token_is_fetched_once_and_sent():
    occ = ScriptedOcc(OK, OK)
    client = occ.client(CUSTOMER)
    await client.get("users/current/orders")
    await client.get("users/current/orders")
    assert occ.token_calls == 1
    assert all(r.headers["Authorization"] == "Bearer t1" for r in occ.requests)


async def test_expired_token_401_refreshes_and_replays_even_a_write():
    occ = ScriptedOcc(401, OK)
    assert await occ.client(CUSTOMER).post("users/current/carts/1/entries", json={"quantity": 1}) == OK
    assert occ.token_calls == 2
    assert [r.headers["Authorization"] for r in occ.requests] == ["Bearer t1", "Bearer t2"]


async def test_401_after_refresh_stops_instead_of_looping():
    occ = ScriptedOcc(401, 401, OK)
    with pytest.raises(OccError, match="needs an operator"):
        await occ.client(CUSTOMER).get("users/current/orders")
    assert len(occ.requests) == 2


async def test_token_near_expiry_is_renewed_before_use():
    occ = ScriptedOcc(OK, OK, expires_in=10)  # inside the 30s refresh margin
    client = occ.client(CUSTOMER)
    await client.get("users/current/orders")
    await client.get("users/current/orders")
    assert occ.token_calls == 2


async def test_concurrent_calls_share_one_token_fetch():
    occ = ScriptedOcc(OK, OK, OK)
    client = occ.client(CUSTOMER)
    await asyncio.gather(*(client.get("users/current/orders") for _ in range(3)))
    assert occ.token_calls == 1


async def test_rejected_credentials_are_an_operator_problem():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    client = OccClient("http://occ.test", "shop", credentials=CUSTOMER, transport=httpx.MockTransport(handle))
    with pytest.raises(OccError, match="needs an operator"):
        await client.get("users/current/orders")


# --- end to end: what the model actually receives

async def test_tool_surfaces_the_failure_as_a_readable_error(monkeypatch):
    occ = ScriptedOcc(503, 503, 503)
    monkeypatch.setattr(server, "occ", occ.client())
    with pytest.raises(ToolError, match="HTTP 503, after retries"):
        await server.mcp.call_tool("get_order_status", {"order_code": "1042"})
