"""Day 21 throwaway: smallest possible MCP server to prove the SDK works.

Run with: uv run mcp dev examples/hello_server.py
Opens the MCP Inspector in a browser so you can call the tool by hand.
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("hello-mcp-commerce")


@mcp.tool()
def ping(name: str) -> str:
    """Reply with a greeting. Proves tool calls round-trip through MCP."""
    return f"pong, {name}"


if __name__ == "__main__":
    mcp.run()
