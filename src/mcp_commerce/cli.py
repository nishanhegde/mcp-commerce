"""The `mcp-commerce` command (the entry point in pyproject.toml).

    mcp-commerce                  run the server; transport from MCP_TRANSPORT
    mcp-commerce --check-config   print the resolved configuration, or every problem, and exit
    mcp-commerce --version

Kept apart from server.py on purpose: importing the server loads and validates
the configuration and builds the OCC client, which --check-config and
--version shouldn't trigger.
"""

import argparse
import sys

from mcp_commerce import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-commerce",
        description="MCP server for SAP Commerce (OCC). Configured by environment variables; see the README.",
    )
    parser.add_argument(
        "--check-config", action="store_true", help="validate the configuration, print it, and exit"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.check_config:
        from mcp_commerce.config import check

        sys.exit(check())

    from mcp_commerce.server import run

    run()
