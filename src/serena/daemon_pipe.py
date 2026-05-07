"""Daemon-side Unix-socket listener for the per-client pipe transport.

Each pipe forwarder process opens a connection to this listener; the per-
connection handler runs the ``mcp/session/open`` handshake (T2), allocates a
fresh UUID4 ``session_id``, and registers the new (session_id -> connection)
record so future eviction logic (T6) can find it. T3 will extend the per-
connection handler to forward JSON-RPC frames between the pipe and FastMCP;
T2 only delivers the handshake.

The listener is intentionally decoupled from :class:`SerenaMCPFactory` so it
can be exercised in tests without spinning up a full MCP server. T5 will wire
the listener into the daemon's startup once frame forwarding (T3) and the
session-key derivation (T5) are in place.
"""

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass

from serena.pipe_protocol import PipeEnvelope, PipeHandshake, PipeProtocolError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipeConnection:
    """One pipe forwarder's daemon-side connection record.

    :ivar session_id: Daemon-allocated UUID4 issued at handshake time. The
        ContextVar that T5 introduces will be set to this value before any
        forwarded frame is dispatched to FastMCP.
    :ivar reader: Inbound async stream from the pipe.
    :ivar writer: Outbound async stream to the pipe; the daemon writes
        responses here.
    """

    session_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


class PipeListener:
    """Daemon-side Unix-socket listener that runs the pipe handshake on connect.

    Lifecycle:

    1. ``await listener.start(socket_path)`` -- bind a Unix socket at
       ``socket_path`` and begin accepting connections.
    2. For each accepted connection the listener reads the first envelope,
       asserts it is a ``mcp/session/open`` request, allocates a UUID4
       ``session_id``, sends the response, and registers a
       :class:`PipeConnection` keyed by that ``session_id``.
    3. ``await listener.stop()`` -- close the server and any tracked
       connections.

    T2 stops at step 2: the per-connection handler returns once the handshake
    is delivered, leaving the connection registered but otherwise idle. T3
    will replace the handler's tail with a bidirectional forwarder loop.
    """

    def __init__(self) -> None:
        # ``_connections`` is keyed by session_id so the eviction finalizer in
        # T6 can locate the right PipeConnection from a session_id alone; the
        # mapping is populated by the per-connection handler after the
        # handshake completes
        self._connections: dict[str, PipeConnection] = {}
        self._server: asyncio.Server | None = None

    @property
    def connections(self) -> dict[str, PipeConnection]:
        """Return a snapshot of the live (session_id -> PipeConnection) map.

        The returned dict is a copy; callers may mutate it freely without
        affecting the listener's internal state.
        """
        return dict(self._connections)

    async def start(self, socket_path: str) -> None:
        """Bind the listener to ``socket_path`` and begin accepting connections.

        :param socket_path: Filesystem path for the listening Unix socket. Any
            stale file at this path is removed first to avoid ``EADDRINUSE``.
        :raises RuntimeError: if the listener is already started.
        """
        if self._server is not None:
            raise RuntimeError("PipeListener.start called twice; call stop() first")

        # remove any stale socket file from a prior run; binding on top of a
        # leftover inode produces EADDRINUSE on Linux/macOS
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)

        # start the asyncio Unix server; ``_handle_connection`` is called for
        # each accepted client and runs the handshake before returning
        self._server = await asyncio.start_unix_server(self._handle_connection, path=socket_path)
        log.info("PipeListener bound to %s", socket_path)

    async def stop(self) -> None:
        """Close the listener and any active pipe connections."""
        if self._server is None:
            return

        # close the listener first so no new connections can race against the
        # connection cleanup below
        self._server.close()
        await self._server.wait_closed()
        self._server = None

        # close every tracked pipe-connection writer; the per-connection
        # handler (once T3 lands its forwarder loop) detects the closed
        # transport on its next read and exits cleanly
        for connection in self._connections.values():
            connection.writer.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await connection.writer.wait_closed()
        self._connections.clear()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handshake-then-park handler for one accepted pipe connection.

        :param reader: Stream reader for inbound envelopes from the pipe.
        :param writer: Stream writer for outbound envelopes to the pipe.
        """
        # read the first envelope; the pipe MUST send mcp/session/open before
        # any forwarded frame, so this read is the handshake by construction
        try:
            envelope = await self._receive_envelope(reader)
        except (PipeProtocolError, asyncio.IncompleteReadError, ConnectionError) as exc:
            log.warning("PipeListener: malformed first envelope; closing connection: %s", exc)
            writer.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()
            return

        if not PipeHandshake.is_request(envelope):
            log.warning("PipeListener: first envelope is not mcp/session/open; closing: %r", envelope)
            writer.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()
            return

        # allocate a fresh UUID4 session_id; per the design plan the daemon
        # is the sole authority for session_id assignment, so no client-
        # asserted id is ever trusted
        session_id = uuid.uuid4().hex
        connection = PipeConnection(session_id=session_id, reader=reader, writer=writer)
        self._connections[session_id] = connection

        # deliver the handshake response so the pipe can stamp future frames
        # with this session_id; the response also doubles as the readiness
        # signal for T3 frame forwarding to begin once that task lands
        try:
            await self._send_envelope(writer, PipeHandshake.response(session_id))
        except (BrokenPipeError, ConnectionResetError) as exc:
            log.warning("PipeListener: response delivery failed for session_id=%s: %s", session_id, exc)
            self._connections.pop(session_id, None)
            writer.close()
            return

        log.info("PipeListener: handshake complete; session_id=%s", session_id)
        # T2 leaves the connection registered but idle; T3 will replace this
        # tail with the bidirectional forwarder loop. We do NOT close the
        # writer here: closing would make T3's frame forwarding impossible
        # to add without a reconnect. ``stop()`` is the only place where the
        # listener tears connections down in T2.

    @staticmethod
    async def _receive_envelope(reader: asyncio.StreamReader) -> PipeEnvelope:
        """Receive one newline-terminated envelope from ``reader``."""
        line = await reader.readuntil(b"\n")
        return PipeEnvelope.from_bytes(line)

    @staticmethod
    async def _send_envelope(writer: asyncio.StreamWriter, envelope: PipeEnvelope) -> None:
        """Send one newline-terminated envelope on ``writer``."""
        writer.write(envelope.to_bytes())
        await writer.drain()
