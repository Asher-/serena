# Running Serena Behind a Tool Multiplexer

This guide describes a deployment pattern in which Serena does **not** speak
directly to its MCP clients. Instead, a small *multiplexer* process holds the
externally-published port, and Serena binds to a private internal port that
only the multiplexer connects to. The multiplexer can front several MCP
backends at once and merge their tool catalogs into a single namespace, so
clients see one server.

This pattern is purely a deployment choice. Serena's wire protocol, session
model, and tool surface are unchanged — the multiplexer forwards bytes.

## Why front Serena with a multiplexer?

Three motivations, in order of how much they actually bite:

1. **Backend restarts no longer end client sessions.** When clients connect
   directly to Serena and Serena exits (crash, redeploy, `launchctl bootout`
   then `bootstrap`, etc.), every open MCP session is gone. A multiplexer that
   queues or holds the client side across a backend restart lets a client
   issue another tool call before Serena has come back, then have it complete
   once Serena returns.
2. **Tool list is captured once at handshake.** Streamable-HTTP MCP clients
   typically read `tools/list` once during initialization and never re-read
   it. If Serena restarts mid-session, the client's view of the tool catalog
   is frozen at whatever was published at handshake. A multiplexer that owns
   the published session can keep that view stable across backend churn.
3. **Multiple MCP backends behind one URL.** If the same client also talks to
   another MCP server (in our setup, `brain-mcp`), aggregating them behind one
   `tools/list` lets the client treat the union as a single server, with each
   tool name namespaced by its backend.

If none of those apply to your setup, you do not need a multiplexer — point
your client straight at Serena's listening port and skip this guide.

## Network shape

With the multiplexer in place:

```
client (Claude Code, Cursor, …)
    │  POST /mcp  http://127.0.0.1:<public-port>
    ▼
brain-mcp-multiplexer  ── public port (e.g. 9101)
    ├──> serena         ── internal port (e.g. 9102)
    └──> brain-mcp      ── internal port (e.g. 7677)
```

Without the multiplexer, the client connects directly to Serena's port and
no aggregation happens:

```
client ── POST /mcp http://127.0.0.1:<serena-port> ──> serena
```

Both modes are supported. The choice is a launchd / config detail, not a
build-time switch.

## Configuring Serena to bind to a private port

Serena's launchd setup ships with `~/Library/LaunchAgents/ai.strong.serena.plist`
and a wrapper at `~/Library/Scripts/serena-launchd.sh`. To put Serena behind a
multiplexer, change the port in **both** places:

1. **Wrapper script** — set `PORT` to the new internal port (e.g. `9102`).
   Update the healthcheck URL on the same line if the script bakes the port
   into a `curl` invocation.
2. **Plist** — verify any environment block or program-arguments list that
   carries the port references the same value.

Reload the daemon so the change takes effect:

```bash
launchctl bootout gui/$UID/ai.strong.serena
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.strong.serena.plist
launchctl print gui/$UID/ai.strong.serena | head
lsof -i :9102 -sTCP:LISTEN
```

The last command should show Serena holding the new port. The previous public
port is now free for the multiplexer.

## Pointing the client at the multiplexer

The MCP client config (e.g. `~/.claude.json` for Claude Code) should keep the
**original** URL — the one that previously pointed at Serena directly. The
multiplexer is the new owner of that port, so existing client configuration
continues to work without change:

```json
{
  "mcpServers": {
    "serena": {
      "type": "http",
      "url": "http://127.0.0.1:9101/mcp"
    }
  }
}
```

If the multiplexer aggregates additional backends, those tools appear in the
same `tools/list` response, prefixed by their backend's name. The client does
not need a separate entry per backend.

## Verifying the chain

After the daemon is up, verify the layers in order:

```bash
# 1. Serena is bound to the internal port.
lsof -i :9102 -sTCP:LISTEN

# 2. The multiplexer is bound to the public port.
lsof -i :9101 -sTCP:LISTEN

# 3. tools/list through the multiplexer returns Serena's tools.
curl -sS -X POST http://127.0.0.1:9101/mcp \
  -H 'Content-Type: application/json' \
  -H 'MCP-Protocol-Version: 2025-06-18' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | jq '.result.tools | length'
```

The third command's count should match Serena's own catalog size (plus any
other backends the multiplexer fronts).

## Testing Serena directly, without the multiplexer in the loop

For Serena development, integration tests, or debugging a behaviour that you
suspect is multiplexer-side, it is easier to bypass the multiplexer and talk
to Serena directly on its internal port:

```bash
curl -sS -X POST http://127.0.0.1:9102/mcp \
  -H 'Content-Type: application/json' \
  -H 'MCP-Protocol-Version: 2025-06-18' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18",
        "capabilities":{},
        "clientInfo":{"name":"test","version":"0"}}}'
```

The streamable-HTTP test pattern in `test/serena/test_concurrent_sse.py`
already supports either endpoint — pass the internal port if you want a
direct conversation, the public port if you want the multiplexed view.

When you point a client at `127.0.0.1:9102` instead of `127.0.0.1:9101`, you
get exactly the upstream Serena experience documented in
`docs/02-usage/030_clients.md`. Nothing in Serena's session model or tool
surface changes between the two paths.

## Backend-restart survival

A useful end-to-end check that the multiplexer is doing its job:

1. Open a streamable-HTTP MCP session through the public port.
2. Issue a successful tool call.
3. Kill Serena (`kill -TERM <pid>` — launchd will respawn it).
4. Issue another tool call **before** Serena's healthcheck succeeds.
5. The call should hold (queued by the multiplexer) and complete once Serena
   is back, rather than failing the session.

Repeat for any other backend the multiplexer fronts. If a call fails the
session instead of holding, file the failure mode against the multiplexer —
do not paper over it with a Serena-side workaround.

## Per-session state and the multiplexer

Serena's per-session state (`_active_projects_by_session`,
`_cursor_managers_by_session`) is keyed off the MCP session object. The
multiplexer forwards bytes, so the session object Serena sees is still tied
1-to-1 with a real client session. The per-session GC eviction added in
commit `187015b5` continues to fire when a client session is closed, whether
the multiplexer is in the path or not.

If you observe per-session state lingering longer than expected, check
`~/Library/Logs/serena/serena.log` for the `Evicted per-session state` debug
lines. Their absence is a signal that the multiplexer is holding sessions
open beyond their client lifetime.
