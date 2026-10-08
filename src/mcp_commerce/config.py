"""Day 34: every setting the server reads, in one place, checked at startup.

Nothing else in the codebase touches os.environ. load_settings() reads the
environment once, validates it, and returns frozen dataclasses; the rest of
the code takes those as arguments.

Where values come from, highest precedence first:
  1. the process environment, e.g. the `env` block of claude_desktop_config.json
     or .mcp.json, or the shell
  2. .env in the working directory (gitignored; see .env.example)
  3. the defaults below

The .env is a development convenience, read from the working directory. That
is the repo when run as `uv --directory <repo> run mcp-commerce`, which sets
the cwd. Day 34 read it from this file's own folder, which stopped making sense
once the code became an installed package: that folder is site-packages. An
installed server (uvx) gets its settings from the client's `env` block instead.

Validation collects every problem before failing, so one restart shows all of
them. Problems stop startup; warnings are logged and the server runs.

Check a configuration without starting the server:  mcp-commerce --check-config
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv

TRANSPORTS = ("stdio", "http")
OCC_MODES = ("mock", "live")
# B2C customer, B2B org user. The only two OCC user roots the tools support.
USER_PATHS = ("users/current", "orgUsers/current")
# The value .env.example ships with. Treated as unset, so copying the example
# as it stands can't put a server on the network with a known key.
PLACEHOLDER_API_KEY = "change-me"


class ConfigError(Exception):
    """Raised with every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class OccCredentials:
    """An OCC OAuth client. With username/password it's the password grant (a
    customer token, needed for users/current); without, client_credentials."""

    client_id: str
    # repr=False: these never reach a log line or a traceback via repr().
    client_secret: str = field(default="", repr=False)
    username: str | None = None
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ServerSettings:
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OccSettings:
    mode: Literal["mock", "live"] = "mock"
    base_url: str | None = None  # live only; host root, without /occ/v2
    base_site: str = "shop"
    user_path: str = "users/current"
    credentials: OccCredentials | None = None
    ca_bundle: str | None = None  # PEM file, for a backend with a private CA


@dataclass(frozen=True)
class Settings:
    server: ServerSettings
    occ: OccSettings
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        """One line for the startup log. Names what's in use, never a secret."""
        s, o = self.server, self.occ
        server = f"transport={s.transport}" + (f" {s.host}:{s.port}" if s.transport == "http" else "")
        if o.mode == "mock":
            backend = "occ=mock"
        else:
            if o.credentials is None:
                auth = "anonymous"
            elif o.credentials.username:
                auth = f"password grant as {o.credentials.username}"
            else:
                auth = f"client_credentials as {o.credentials.client_id}"
            backend = f"occ=live {o.base_url} ({auth})"
        return f"{server} {backend} site={o.base_site} user_path={o.user_path}"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate the configuration; raise ConfigError listing every problem.

    Tests pass `env` as a plain dict, which skips .env and os.environ entirely.
    """
    if env is None:
        # override=False: a value already in the environment beats .env.
        load_dotenv(Path.cwd() / ".env", override=False)
        env = os.environ

    def get(name: str) -> str | None:
        # Empty counts as unset: client configs often carry "KEY": "" placeholders.
        value = env.get(name, "").strip()
        return value or None

    problems: list[str] = []
    warnings: list[str] = []

    # --- MCP server ---------------------------------------------------------
    transport = (get("MCP_TRANSPORT") or "stdio").lower()
    if transport not in TRANSPORTS:
        problems.append(f"MCP_TRANSPORT={transport!r}: must be one of {', '.join(TRANSPORTS)}")

    port = 8000
    if raw_port := get("MCP_PORT"):
        try:
            port = int(raw_port)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            problems.append(f"MCP_PORT={raw_port!r}: must be a port number, 1-65535")

    api_key = get("MCP_API_KEY")
    if api_key == PLACEHOLDER_API_KEY:
        api_key = None
    if transport == "http" and not api_key:
        problems.append("MCP_API_KEY: required when MCP_TRANSPORT=http (and not the 'change-me' placeholder)")

    server = ServerSettings(
        transport=transport,  # type: ignore[arg-type]  # checked above
        host=get("MCP_HOST") or "127.0.0.1",
        port=port,
        api_key=api_key,
    )

    # --- OCC backend --------------------------------------------------------
    mode = (get("OCC_MODE") or "mock").lower()
    base_url = get("OCC_BASE_URL")
    if mode not in OCC_MODES:
        problems.append(f"OCC_MODE={mode!r}: must be one of {', '.join(OCC_MODES)}")
    elif mode == "mock" and base_url:
        # The ambiguous case the explicit mode exists for: refuse to guess.
        problems.append(
            "OCC_BASE_URL is set but OCC_MODE is mock. Set OCC_MODE=live to use that backend, "
            "or unset OCC_BASE_URL to use the built-in mock"
        )
    elif mode == "live":
        if not base_url:
            problems.append("OCC_BASE_URL: required when OCC_MODE=live, e.g. https://localhost:9002")
        else:
            base_url, url_problem = _check_base_url(base_url)
            if url_problem:
                problems.append(url_problem)
            elif urlsplit(base_url).scheme == "http" and urlsplit(base_url).hostname not in ("localhost", "127.0.0.1"):
                warnings.append(f"OCC_BASE_URL {base_url} is plain http: OAuth secrets cross the network unencrypted")

    base_site = get("OCC_BASE_SITE") or "shop"
    if "/" in base_site:
        problems.append(f"OCC_BASE_SITE={base_site!r}: a site id like 'electronics', not a path")

    user_path = get("OCC_USER_PATH") or "users/current"
    if user_path not in USER_PATHS:
        problems.append(f"OCC_USER_PATH={user_path!r}: must be {USER_PATHS[0]} (B2C) or {USER_PATHS[1]} (B2B)")

    credentials = None
    client_id, username, password = get("OCC_CLIENT_ID"), get("OCC_USERNAME"), get("OCC_PASSWORD")
    if bool(username) != bool(password):
        problems.append("OCC_USERNAME and OCC_PASSWORD: set both (password grant) or neither (client_credentials)")
    if username and not client_id:
        problems.append("OCC_USERNAME is set without OCC_CLIENT_ID: the password grant needs an OAuth client")
    if client_id:
        credentials = OccCredentials(
            client_id=client_id,
            client_secret=get("OCC_CLIENT_SECRET") or "",
            username=username,
            password=password,
        )

    ca_bundle = get("OCC_CA_BUNDLE")
    if ca_bundle and not Path(ca_bundle).expanduser().is_file():
        problems.append(f"OCC_CA_BUNDLE={ca_bundle!r}: no such file")

    if mode == "mock":
        ignored = [n for n in ("OCC_CLIENT_ID", "OCC_USERNAME", "OCC_CA_BUNDLE") if get(n)]
        if ignored:
            warnings.append(f"OCC_MODE=mock ignores {', '.join(ignored)}")
        credentials, ca_bundle = None, None
    elif mode == "live" and (credentials is None or credentials.username is None):
        # Not an error: catalog tools work anonymously on OCC.
        warnings.append(
            f"OCC_MODE=live without OCC_USERNAME/OCC_PASSWORD: {user_path} needs a customer token, "
            "so cart and order tools will fail; only catalog search and product detail will work"
        )

    if problems:
        raise ConfigError(problems)

    occ = OccSettings(
        mode=mode,  # type: ignore[arg-type]  # checked above
        base_url=base_url if mode == "live" else None,
        base_site=base_site,
        user_path=user_path,
        credentials=credentials,
        ca_bundle=str(Path(ca_bundle).expanduser()) if ca_bundle else None,
    )
    return Settings(server=server, occ=occ, warnings=tuple(warnings))


def _check_base_url(url: str) -> tuple[str, str | None]:
    """Normalise OCC_BASE_URL; return (url, problem or None)."""
    url = url.rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return url, f"OCC_BASE_URL={url!r}: must be an http(s) URL, e.g. https://localhost:9002"
    if "/occ/v2" in parts.path:
        # The common copy-paste from Swagger UI. The client adds /occ/v2/{site}
        # itself, so leaving it in would request /occ/v2/occ/v2/... and 404.
        return url, f"OCC_BASE_URL={url!r}: give the host root only; the client adds /occ/v2/{{site}}"
    return url, None


def check() -> int:
    """`mcp-commerce --check-config`: print the resolved settings or the problems.

    Returns the process exit code.
    """
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"mcp-commerce: {exc}", file=sys.stderr)
        return 1
    print(settings.summary())
    for warning in settings.warnings:
        print(f"warning: {warning}")
    return 0
