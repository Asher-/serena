"""Per-client stdio pipe forwarder for the Serena MCP daemon.

The pipe is a thin per-client process spawned by an MCP host (e.g. Claude Code)
that forwards JSON-RPC frames over stdio to a single shared Serena daemon
listening on a Unix-domain socket. It owns a stable ``session_id`` allocated by
the daemon at handshake time and tags every forwarded frame with that id so the
daemon can pin per-session state (active project, cursor manager, ...) to the
client's lifetime rather than to the upstream MCP transport's lifetime.

This module is the entry point added in T1 of
plan://Serena:serena/serena-pipe-implementation. T2 layers the handshake
protocol; T3 wires bidirectional frame forwarding; T4 fetches the tool catalog
from the daemon at startup. Until those land, :func:`run_pipe_client` only
validates that the CLI dispatched correctly and then refuses to run, so an
operator who attempts to use the transport before it is complete sees a clear
error rather than silent no-op.
"""

import asyncio
import logging
from urllib.parse import urlparse

from serena.pipe_protocol import PipeEnvelope, PipeHandshake

log = logging.getLogger(__name__)


class PipeNotImplementedError(NotImplementedError):
    """Raised when pipe-transport functionality is invoked before its task lands."""


def run_pipe_client(daemon_url: str) -> None:
    """Run the per-client pipe forwarder until the upstream stdio is closed.

    :param daemon_url: URI of the Serena daemon's listening socket. Only the
        ``unix://`` scheme is currently accepted; TCP and other schemes are
        reserved for future tasks.
    :raises ValueError: if ``daemon_url`` is not a ``unix://`` URL or has an
        empty path component.
    :raises PipeProtocolError: if the daemon's handshake response is malformed.
    :raises PipeNotImplementedError: while T3 frame forwarding is pending; T2
        completes the handshake but cannot yet ferry tool calls back and forth.
    """
    # parse the unix:// URL to extract the socket path; reject anything else
    # so the operator gets a clear error rather than an obscure socket failure
    socket_path = _parse_unix_url(daemon_url)

    # run the handshake under asyncio; the function is sync-callable so it
    # plugs straight into the click handler in cli.py without further wiring
    session_id = asyncio.run(_handshake(socket_path))
    log.info("serena-pipe handshake complete; daemon-asserted session_id=%s", session_id)

    # raise so the operator sees a clear T3-pending signal rather than a
    # silent no-op; remove this raise once T3's frame forwarder loop lands
    raise PipeNotImplementedError(
        "serena-pipe handshake (T2) succeeded; T3 frame forwarding is not yet implemented. "
        "See plan://Serena:serena/serena-pipe-implementation."
    )


def _parse_unix_url(daemon_url: str) -> str:
    """Extract the filesystem path from a ``unix://`` URL.

    :param daemon_url: The daemon URL as supplied via ``--daemon-url``.
    :returns: The Unix-socket filesystem path component.
    :raises ValueError: if the scheme is not ``unix`` or the path is empty.
    """
    parsed = urlparse(daemon_url)
    if parsed.scheme != "unix":
        raise ValueError(f"daemon_url must use the unix:// scheme; got scheme={parsed.scheme!r}")
    if not parsed.path:
        raise ValueError(f"daemon_url has no socket path component: {daemon_url!r}")
    return parsed.path


async def _handshake(socket_path: str) -> str:
    """Connect to ``socket_path`` and run the ``mcp/session/open`` exchange.

    :param socket_path: Filesystem path of the daemon's listening Unix socket.
    :returns: The daemon-asserted UUID4 ``session_id`` extracted from the
        response's ``meta`` channel.
    """
    # establish the bidirectional stream; if the daemon isn't running this
    # fails immediately with FileNotFoundError or ConnectionRefusedError,
    # both of which produce a clear operator-facing message
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        # send mcp/session/open and await the response; the daemon answers
        # with a single envelope whose meta channel carries the session_id
        writer.write(PipeHandshake.request().to_bytes())
        await writer.drain()
        line = await reader.readuntil(b"\n")
        envelope = PipeEnvelope.from_bytes(line)
        return PipeHandshake.session_id_from_response(envelope)
    finally:
        # always close the writer cleanly so the daemon's connection handler
        # exits without warnings; T3 will instead keep the connection open
        # for the forwarder's lifetime
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
