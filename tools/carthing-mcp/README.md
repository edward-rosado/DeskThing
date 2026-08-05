# Car Thing MCP server

> **Fork-only.** This is Claude/agent tooling, not part of the DeskThing
> project — it is deliberately excluded from upstream pull requests.

Exposes the Car Thing's screen and browser as MCP tools, so an agent can
**see** the display rather than just save a file to look at later. The device
firmware has no `screencap`, so before the Bluetooth tunnel there was no way to
observe what it was rendering at all.

| Tool | What it does |
| --- | --- |
| `carthing_status` | Link state, transport, protocol version, forwarded services |
| `carthing_screenshot` | Returns the display as a viewable image |
| `carthing_eval` | Runs JS in the live page, returns JSON |
| `carthing_console` | Collects console output and exceptions for N seconds |
| `carthing_navigate` | Points the page at a URL |
| `carthing_reload` | Reloads the page |

## How it works

It imports [`../../DeskThingServer/bt_source/tools/carthing-debug.py`](../../DeskThingServer/bt_source/tools/carthing-debug.py)
as its engine, so there is exactly one implementation of the transport and CDP
logic — the CLI is the upstream-bound tool, this is a thin agent-facing wrapper.

Dependency-free: it speaks JSON-RPC over stdio directly, so it runs from a plain
checkout with nothing installed.

Set `CARTHING_DEBUG_CLI` if your checkout is somewhere other than
`~/Spotify_Thing/DeskThing/`.

## Registering it

`.mcp.json` in the workspace root (`~/Spotify_Thing/`) so any agent working
there picks it up:

```json
{
  "mcpServers": {
    "carthing": {
      "command": "python3",
      "args": ["/Users/misteredr/Spotify_Thing/DeskThing/tools/carthing-mcp/server.py"]
    }
  }
}
```

## Checking it by hand

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"carthing_status","arguments":{}}}' \
  | python3 server.py
```

## Keeping it off upstream

It lives on a commit marked *fork only* on the `local/bt-transport` branch. When
assembling a PR branch, that commit is left out — the same treatment as the
local dock-icon commit. The CLI and `docs/device-debugging.md` **do** go
upstream; they are useful to any DeskThing developer.
