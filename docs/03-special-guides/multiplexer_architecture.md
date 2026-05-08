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
`_cursor_managers_by_session`) is keyed off the in-process MCP `Session`
object Serena receives, via `id(mcp_ctx.session)`. The MCP SDK's
streamable-HTTP transport, in stateful mode, normally keeps one `Session`
alive per `Mcp-Session-Id` for the lifetime of that ID, so within a single
client session every tool call would land in the same per-session slot.

In practice that 1-to-1 mapping is **not** preserved when Serena sits behind
the current `brain-mcp-multiplexer`. The multiplexer treats Serena as an
upstream backend and does not consistently propagate the client's
`Mcp-Session-Id` header into its outbound requests; calls in the same client
batch arrive at Serena under different (or absent) `Mcp-Session-Id` values
and therefore land in distinct `Session` objects, with different
`id(mcp_ctx.session)` keys. Each fresh `Session` is GC'd once its
short-lived task ends, the per-session finalizer fires, and the next call
finds an empty slot and falls through to `_legacy_active_project` — which
is whichever project the most recent activator (potentially a sibling
client) wrote. This is the proximate cause of the "globally-active project
rotated between sibling workers" failure mode reported when running
parallel sub-agents through Serena.

The eviction path itself (`_evict_session_state`, registered by
`_register_session_finalizer` in `agent.py`) is correct; the missing piece
is a **stable per-client identifier** that survives transport churn and
multiplexer fan-out. The intended fix is to introduce a per-client `pipe`
layer (see the design plan tracked under
`plan://Serena:serena/serena-mcp-pipe-redesign`) whose handshake gives
Serena one stable session id for the entire client lifetime.

Until that lands, when you observe per-session state lingering longer than
expected — or rotating between sibling clients — check
`~/Library/Logs/serena/serena.log` for the `Evicted per-session state`
debug lines and the frequency of session-key churn. Their absence is a
signal that the multiplexer is holding sessions open beyond their client
lifetime; an unexpectedly high rate is a signal that the multiplexer is
fragmenting one client into many Serena sessions, which is the bug the
pipe layer addresses.


## Pipe transport

The pipe transport is the resolution of the failure mode the previous section
diagnoses. In pipe mode each client speaks stdio to a small *per-client
forwarder process*; the forwarder dials a single shared Serena daemon over a
Unix-domain socket and tags every forwarded JSON-RPC frame with a stable
session id the daemon issued at connect time. The daemon pins all per-session
state to that id, so it survives streamable-HTTP request-task teardown,
multiplexer fan-out, and any other transport churn between client and daemon.

The plan that drove this work is tracked under
`plan://Serena:serena/serena-mcp-pipe-redesign` (design) and
`plan://Serena:serena/serena-pipe-implementation` (implementation).

### Process shape

```
Claude Code, Cursor, …  ── stdio ──>  serena-pipe forwarder  ── Unix socket ──>  serena daemon
        (one client)                      (one per client)                          (one per machine)
```

Each client owns its own forwarder process. The forwarder is started by
`serena start-mcp-server --transport pipe`; it does not host an MCP server,
it forwards bytes. The daemon is the only process that holds tool state,
language servers, and per-session slots — there is one daemon per machine
regardless of how many clients are connected.

### Handshake-asserted session id

When a forwarder connects to the daemon's Unix socket, it sends a handshake
frame; the daemon allocates a fresh UUID4 hex string as the session id and
returns it on the same handshake. The forwarder holds that id for its
lifetime; every subsequent JSON-RPC frame it forwards carries the id in the
pipe protocol's metadata channel (not the upstream `Mcp-Session-Id` header,
which does not reach handlers). The daemon's pipe listener sets the ContextVar
`_PIPE_SESSION_ID_VAR` before invoking the FastMCP tool handler, so the
handler resolves per-session state under the pinned id.

The id is opaque to the client and survives transport churn at every layer
the previous section diagnoses: the same id is used across thousands of
streamable-HTTP request-tasks within the daemon, across multiplexer-side
session fragmentation if a multiplexer also sits in the path, and across
daemon catalog refreshes the forwarder may issue.

### Eviction on pipe disconnect

When the forwarder process exits — clean shutdown, SIGKILL, broken socket —
the daemon's `PipeListener` notices the socket EOF and runs every registered
disconnect handler. The wired-in handler is
`SerenaAgent.evict_pipe_session`, which pops the session id out of every
per-session dict (today `_active_projects_by_session` and
`_cursor_managers_by_session`) and logs the eviction at debug level. Any
subsequent forwarder connection gets a fresh UUID4 session id with a clean
slate; the daemon never reuses a session id across forwarder lifetimes.

This is the load-bearing distinction from the previous section's diagnosis:
eviction is keyed on **pipe-connection close**, not on the per-request
streamable-HTTP task end. A client that issues ten thousand tool calls over
one pipe session triggers eviction exactly once — when the client
disconnects — not ten thousand times.

### Why this fixes the failure mode

The previous section's failure mode reduces to: the per-session slot is
keyed on something that does not survive the client's lifetime. The pipe
session id IS that lifetime — it is allocated once at connect, held for
the forwarder's whole life, and evicted exactly once on disconnect. Inside
an MCP call the daemon's getters (`_active_project`, `get_cursor_manager`,
…) resolve under the pinned id; they cannot fall through to a sibling
client's slot because the per-session lookup either returns the value this
session itself wrote or fails closed (the IRONCLAD guard from the parent
design plan, Phase 5a). The pipe makes that fail-closed branch a non-event
under normal operation, because the per-session lookup never misses for
transport-churn reasons.

### Coexistence with the multiplexer

The multiplexer continues to provide catalog stability and backend-restart
absorption for clients that go through it. The pipe transport addresses a
disjoint problem (per-client session pinning) and is the recommended path
for clients that run multiple sub-agents in parallel — most notably Claude
Code in parallel-Task mode. A deployment can use either, neither, or both:
the daemon binds its Unix socket only when started with
`--pipe-socket-path`, and continues to accept streamable-HTTP traffic on
its public port whether or not the pipe is enabled.

### Enabling the pipe transport on the daemon

Activating the pipe layer is a one-time launchd plist change to the Serena
daemon's ProgramArguments. The current `start-mcp-server` invocation in
your daemon plist (whatever transport it already uses — `stdio`,
`streamable-http`, or otherwise) needs the additional argument:

```text
--pipe-socket-path /tmp/serena-daemon.sock
```

With that flag the daemon binds the Unix socket at startup and runs the
pipe listener concurrently with whatever transport it was already serving.
Clients that go through the pipe forwarder use the same socket path via
`--daemon-url unix:///tmp/serena-daemon.sock`; the matching client-side
invocation is documented in
[Connecting Your MCP Client](../02-usage/030_clients.md)
§Advanced: Pipe transport.

The operator plist lives at `~/Library/LaunchAgents/ai.strong.serena.plist`
(or the matching launchd-loaded variant for your installation). Append
`--pipe-socket-path /tmp/serena-daemon.sock` to the daemon's
`start-mcp-server` invocation: if the plist invokes `serena` directly, edit
the plist's `<array>` of `ProgramArguments`; if the plist invokes a wrapper
script that exec's `serena start-mcp-server` (a common EADDRINUSE-prevention
pattern), edit the script's exec line instead. Then reload the daemon:

```bash
launchctl bootout gui/$UID/ai.strong.serena
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.strong.serena.plist
launchctl print gui/$UID/ai.strong.serena | head
ls -l /tmp/serena-daemon.sock
```

The last command should show the socket file owned by the daemon's user.

This single restart cycle activates two changes at once:

1. **The pipe transport.** The daemon now accepts forwarder connections at
   `/tmp/serena-daemon.sock`. Clients started with `--transport pipe
   --daemon-url unix:///tmp/serena-daemon.sock` get a stable handshake-asserted
   session id and the per-session-state guarantees described above.
2. **The IRONCLAD fail-closed guard committed as `cea6bdf5`.** The daemon's
   `_active_project` and `get_cursor_manager` resolution now raises a clear
   "No active project for this MCP session" error inside MCP calls instead
   of silently falling through to `_legacy_active_project`. After the
   restart, parallel-batch dispatch becomes architecturally safe — the pipe
   ensures the per-session lookup never misses for transport-churn reasons,
   and the IRONCLAD guard catches any remaining real misuse loudly.

Because the restart kicks every active client (the daemon owns all live MCP
sessions), schedule it during a quiet window: confirm with each connected
client's operator before bootout, and have clients reconnect after the
bootstrap completes.
