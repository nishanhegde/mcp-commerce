"""Day 34: the config layer. Every test passes `env` as a dict, so neither the
real environment nor .env can leak in."""

import pytest

from mcp_commerce.config import ConfigError, OccCredentials, load_settings

LIVE = {"OCC_MODE": "live", "OCC_BASE_URL": "https://localhost:9002"}
CUSTOMER = {"OCC_CLIENT_ID": "mobile_android", "OCC_CLIENT_SECRET": "secret",
            "OCC_USERNAME": "demo@example.com", "OCC_PASSWORD": "pw"}


def problems(env: dict) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    return exc.value.problems


def test_empty_env_is_stdio_against_the_mock():
    s = load_settings({})
    assert s.server.transport == "stdio"
    assert s.occ.mode == "mock"
    assert s.occ.base_url is None
    assert s.occ.base_site == "shop"
    assert s.warnings == ()


def test_empty_strings_count_as_unset():
    # Client configs often ship "OCC_BASE_URL": "" as a fill-me-in placeholder.
    s = load_settings({"OCC_MODE": "", "OCC_BASE_URL": "", "MCP_PORT": "  "})
    assert s.occ.mode == "mock"
    assert s.server.port == 8000


def test_live_with_customer_credentials():
    s = load_settings({**LIVE, **CUSTOMER, "OCC_BASE_SITE": "electronics"})
    assert s.occ.base_url == "https://localhost:9002"
    assert s.occ.base_site == "electronics"
    assert s.occ.credentials == OccCredentials("mobile_android", "secret", "demo@example.com", "pw")
    assert s.warnings == ()


def test_trailing_slash_on_base_url_is_dropped():
    assert load_settings({**LIVE, **CUSTOMER, "OCC_BASE_URL": "https://occ.test/"}).occ.base_url == "https://occ.test"


def test_live_needs_a_base_url():
    assert any("OCC_BASE_URL: required" in p for p in problems({"OCC_MODE": "live"}))


def test_base_url_without_live_mode_is_refused_not_guessed():
    assert any("OCC_MODE is mock" in p for p in problems({"OCC_BASE_URL": "https://localhost:9002"}))


def test_base_url_with_occ_path_is_refused():
    found = problems({**LIVE, "OCC_BASE_URL": "https://localhost:9002/occ/v2"})
    assert any("host root only" in p for p in found)


def test_base_url_must_be_http():
    assert any("http(s) URL" in p for p in problems({**LIVE, "OCC_BASE_URL": "localhost:9002"}))


def test_http_transport_needs_a_real_api_key():
    assert any("MCP_API_KEY" in p for p in problems({"MCP_TRANSPORT": "http"}))
    assert any("MCP_API_KEY" in p for p in problems({"MCP_TRANSPORT": "http", "MCP_API_KEY": "change-me"}))
    assert load_settings({"MCP_TRANSPORT": "HTTP", "MCP_API_KEY": "k3y"}).server.transport == "http"


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_bad_port(port):
    assert any("MCP_PORT" in p for p in problems({"MCP_PORT": port}))


def test_half_a_password_grant_is_refused():
    assert any("set both" in p for p in problems({**LIVE, "OCC_CLIENT_ID": "c", "OCC_USERNAME": "u"}))
    assert any("without OCC_CLIENT_ID" in p for p in problems({**LIVE, "OCC_USERNAME": "u", "OCC_PASSWORD": "p"}))


def test_user_path_is_b2c_or_b2b_only():
    assert load_settings({"OCC_USER_PATH": "orgUsers/current"}).occ.user_path == "orgUsers/current"
    assert any("OCC_USER_PATH" in p for p in problems({"OCC_USER_PATH": "users/anonymous"}))


def test_missing_ca_bundle_file():
    assert any("OCC_CA_BUNDLE" in p for p in problems({**LIVE, **CUSTOMER, "OCC_CA_BUNDLE": "/nope.pem"}))


def test_every_problem_is_reported_at_once():
    found = problems({"MCP_TRANSPORT": "grpc", "MCP_PORT": "x", "OCC_MODE": "prod", "OCC_USER_PATH": "me"})
    assert len(found) == 4


def test_live_without_a_customer_warns_but_starts():
    s = load_settings({**LIVE, "OCC_CLIENT_ID": "c"})
    assert s.occ.credentials is not None
    assert any("cart and order tools will fail" in w for w in s.warnings)


def test_mock_ignores_backend_credentials_with_a_warning():
    s = load_settings(CUSTOMER)
    assert s.occ.credentials is None
    assert any("ignores OCC_CLIENT_ID, OCC_USERNAME" in w for w in s.warnings)


def test_plain_http_to_a_remote_host_warns():
    s = load_settings({**LIVE, **CUSTOMER, "OCC_BASE_URL": "http://occ.example.com"})
    assert any("plain http" in w for w in s.warnings)


def test_secrets_never_appear_in_repr_or_summary():
    s = load_settings({**LIVE, **CUSTOMER, "MCP_TRANSPORT": "http", "MCP_API_KEY": "k3y-sup3r"})
    for text in (repr(s), s.summary()):
        assert "k3y-sup3r" not in text
        assert "secret" not in text
        assert "'pw'" not in text
    assert "password grant as demo@example.com" in s.summary()
