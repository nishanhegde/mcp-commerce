"""mcp-commerce: an MCP server for SAP Commerce (OCC)."""

from importlib.metadata import version

# Single source of truth is pyproject.toml; this reads the installed metadata.
__version__ = version("mcp-commerce")
