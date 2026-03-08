# solvemcp

Small Python client for Model Context Protocol (MCP) servers.

It provides:
- `stdio` transport for local subprocess servers.
- Streamable HTTP transport for modern MCP servers.
- Legacy HTTP+SSE fallback for older servers.
- Dynamic Python callables for tools returned by `tools/list`.

## Installation

```bash
pip install solvemcp
```

## Quick Start

### Connect to a local stdio server

```python
from solvemcp import MCPClient

cmd = ["python", "-m", "my_mcp_server"]
with MCPClient.stdio(cmd) as mcp:
    print(mcp.tools.keys())
    res = mcp.echo(text="hello")
    print(res)
```

### Connect to an HTTP MCP server

```python
with MCPClient.http("https://mcp.grep.app") as mcp:
    res = mcp.searchGitHub(query="class PtyProcess", language=["Python"])
    print(res)
```

## How Tool Calls Work

For each server tool with a valid Python identifier name, `MCPClient` adds a method dynamically.
Example: a server tool named `echo` becomes `mcp.echo(...)`.

Equivalent forms:

```python
mcp.echo(text="hi")
mcp.call_tool("echo", text="hi")
```

## Streaming

For Streamable HTTP servers, use `rpc_stream` or `call_tool_stream`:

```python
for msg in mcp.rpc_stream("tools/list", params={}): print(msg)
```

## Module Layout

- `solvemcp/client.py`: `MCPClient` request lifecycle, initialization, tool binding, and RPC helpers.
- `solvemcp/transports.py`: transport implementations plus SSE parsers and error types.

## Development

### Run tests:

```bash
python tests/tests.py
```

### Versioning and Release

Use fastship:

```bash
ship-release-gh
ship-pypi
ship-bump
```

