"""Security properties the README promises.

A scripted transport records the exact path httpx sends, after its own
dot-segment resolution, which is what a real OCC would receive."""

from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_commerce import cart, catalog, config, orders
from mcp_commerce.occ_client import OccClient

TRAVERSAL = "../../../users/victim@example.com/orders"
ENCODED = quote(TRAVERSAL, safe="")


def recording_client() -> tuple[OccClient, list[str]]:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode().split("?")[0])
        return httpx.Response(404, json={"errors": [{"type": "UnknownIdentifierError", "message": "not found"}]})

    return OccClient("https://occ.test", "shop", transport=httpx.MockTransport(handle)), paths


@pytest.mark.parametrize("call, expected", [
    (lambda c: orders.get_order_status(c, TRAVERSAL), f"/occ/v2/shop/users/current/orders/{ENCODED}"),
    (lambda c: cart.get_cart(c, TRAVERSAL), f"/occ/v2/shop/users/current/carts/{ENCODED}"),
    (lambda c: cart.add_to_cart(c, TRAVERSAL, "SH-204", 1), f"/occ/v2/shop/users/current/carts/{ENCODED}/entries"),
    (lambda c: catalog.get_product(c, TRAVERSAL), f"/occ/v2/shop/products/{ENCODED}"),
])
async def test_a_path_argument_cannot_reach_another_customer(call, expected):
    client, paths = recording_client()
    with pytest.raises(ToolError):
        await call(client)
    assert paths == [expected]  # one request, the whole argument inside one segment


@pytest.mark.parametrize("value", ["..", ".", ""])
async def test_dot_segments_are_refused_before_any_request(value):
    client, paths = recording_client()
    with pytest.raises(ToolError, match="not a valid order code"):
        await orders.get_order_status(client, value)
    assert paths == []


def test_env_file_is_the_checkout_root():
    assert config.ENV_FILE == Path(__file__).resolve().parents[1] / ".env"


def test_a_dotenv_in_the_working_directory_is_ignored(tmp_path, monkeypatch):
    # The project an MCP client has open is not trusted to configure the server.
    (tmp_path / ".env").write_text("OCC_MODE=live\nOCC_BASE_URL=https://attacker.example\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "no-such-dir" / ".env")
    for name in ("OCC_MODE", "OCC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert config.load_settings().occ.mode == "mock"
