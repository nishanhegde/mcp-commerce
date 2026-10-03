"""Async HTTP client for the OCC API.

Mode is picked by env (see .env.example):
  OCC_BASE_URL unset   in-process mock (mock_occ), no second process to run
  OCC_BASE_URL=https://host:9002   a real SAP Commerce instance

Both modes go through the same code: the mock honours the OCC contract, so the
tools above this layer can't tell which one they're talking to.
"""

import logging
import os

import httpx
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger("mcp-commerce.occ")

OCC_PREFIX = "occ/v2"
TIMEOUT_SECONDS = 10.0


class OccError(ToolError):
    """A backend failure worded for the model.

    Subclassing ToolError means the SDK turns it into an `is_error=True` result
    with this message, instead of a crash the model can't read.
    """


class OccClient:
    def __init__(self, base_url: str | None, base_site: str, user_path: str = "users/current") -> None:
        self._base_site = base_site
        # B2C is users/current; B2B uses orgUsers/current. Config, never a tool argument.
        self.user_path = user_path
        if base_url:
            self._http = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT_SECONDS)
            logger.info("OCC backend: %s (site=%s)", base_url, base_site)
        else:
            # Imported here so a real-backend deployment never loads the mock.
            from mock_occ.app import app as mock_app

            self._http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=mock_app),
                base_url="http://mock-occ",
                timeout=TIMEOUT_SECONDS,
            )
            logger.info("OCC backend: in-process mock (site=%s)", base_site)

    async def get(self, path: str, **params: str | int) -> dict:
        """GET {base}/occ/v2/{site}/{path}; returns JSON or raises OccError."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: dict | None = None, **params: str | int) -> dict:
        """POST with an optional JSON body; returns JSON or raises OccError."""
        return await self._request("POST", path, params=params, json=json)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"/{OCC_PREFIX}/{self._base_site}/{path}"
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException:
            logger.warning("OCC timeout %s %s", method, url)
            raise OccError("The commerce backend timed out. Try again in a moment.") from None
        except httpx.TransportError as exc:
            logger.warning("OCC unreachable %s %s: %s", method, url, exc)
            raise OccError("The commerce backend is unreachable right now.") from None

        if response.is_success:
            return response.json()
        raise OccError(_error_message(response))


def _error_message(response: httpx.Response) -> str:
    """OCC errors are `{"errors": [{"type", "message"}]}`; surface the message."""
    try:
        errors = response.json()["errors"]
        return "; ".join(e["message"] for e in errors)
    except (ValueError, KeyError, TypeError):
        logger.warning("OCC %s with unparseable body", response.status_code)
        return f"The commerce backend returned an error (HTTP {response.status_code})."


def client_from_env() -> OccClient:
    return OccClient(
        base_url=os.environ.get("OCC_BASE_URL") or None,
        base_site=os.environ.get("OCC_BASE_SITE", "shop"),
        user_path=os.environ.get("OCC_USER_PATH", "users/current"),
    )
