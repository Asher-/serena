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
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

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


class FrameHandler(ABC):
    """Strategy for processing one forwarded JSON-RPC frame on the daemon side.

    The pipe forwards every JSON-RPC frame upstream sends to the daemon as a
    :class:`PipeEnvelope`; the listener strips the envelope and calls
    :meth:`handle` with the frame and the connection's daemon-allocated
    ``session_id``. Concrete strategies wrap the frame in whatever response
    machinery the daemon needs (e.g. FastMCP dispatch, a test echo); T3 wires
    the transport, T5 supplies the production strategy that integrates with
    :class:`SerenaMCPFactory`.
    """

    @abstractmethod
    async def handle(self, session_id: str, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Process one forwarded JSON-RPC frame and optionally produce a response.

        :param session_id: The daemon-allocated session_id for the originating
            pipe connection. Strategies that need to bind per-session state
            (e.g. ``_PIPE_SESSION_ID_VAR`` in T5) read this value.
        :param frame: The JSON-RPC frame as a parsed dict (already JSON-decoded
            by :class:`PipeEnvelope`). Strategies treat this as opaque input.
        :returns: A response frame to ferry back over the pipe, or ``None`` for
            JSON-RPC notifications and any other request that does not produce
            a response.
        """


class _NullFrameHandler(FrameHandler):
    """Default :class:`FrameHandler` that drops every frame without responding.

    Used when :class:`PipeListener` is constructed without an explicit handler
    -- the existing T1/T2 tests connect, complete the handshake, and close
    without ever forwarding a frame, so the null behaviour preserves their
    semantics. Production callers always inject a real handler.
    """

    async def handle(self, session_id: str, frame: dict[str, Any]) -> dict[str, Any] | None:
        # log loudly so a misconfigured daemon (handler-less, but actually
        # forwarding traffic) is easy to spot in operator output rather than
        # silently swallowing JSON-RPC requests
        log.warning(
            "PipeListener: no FrameHandler configured; dropping frame for session_id=%s frame_id=%r method=%r",
            session_id,
            frame.get("id"),
            frame.get("method"),
        )
        return None


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

    def __init__(self, frame_handler: FrameHandler | None = None) -> None:
        # ``_connections`` is keyed by session_id so the eviction finalizer in
        # T6 can locate the right PipeConnection from a session_id alone; the
        # mapping is populated by the per-connection handler after the
        # handshake completes
        self._connections: dict[str, PipeConnection] = {}
        self._server: asyncio.Server | None = None

        # the strategy that processes forwarded JSON-RPC frames; None falls
        # back to a null handler so T1/T2 tests (which never forward a frame
        # past the handshake) continue to pass unchanged
        self._frame_handler: FrameHandler = frame_handler if frame_handler is not None else _NullFrameHandler()

        # track the per-connection handler tasks so stop() can deterministically
        # await them and avoid leaking a forwarder loop past the listener's
        # lifetime; entries are added in _handle_connection and discarded when
        # the handler returns
        self._handler_tasks: set[asyncio.Task[None]] = set()

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
        # forwarder detects the closed transport on its next read and exits
        # via its finally block, which deregisters the connection
        for connection in list(self._connections.values()):
            connection.writer.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await connection.writer.wait_closed()

        # await every per-connection handler task so the loop's teardown does
        # not race with a still-running forwarder; cancelling first guarantees
        # we unwind even if a handler is blocked in readuntil for some reason
        pending = list(self._handler_tasks)
        for task in pending:
            if not task.done():
                task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._connections.clear()
        self._handler_tasks.clear()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handshake-then-forward handler for one accepted pipe connection.

        :param reader: Stream reader for inbound envelopes from the pipe.
        :param writer: Stream writer for outbound envelopes to the pipe.
        """
        # register this handler task so stop() can deterministically await its
        # exit; without tracking, a still-running forwarder loop can race
        # asyncio.run's teardown and trigger I/O-on-closed-stream warnings
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)

        try:
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
            # signal for T3 frame forwarding to begin
            try:
                await self._send_envelope(writer, PipeHandshake.response(session_id))
            except (BrokenPipeError, ConnectionResetError) as exc:
                log.warning("PipeListener: response delivery failed for session_id=%s: %s", session_id, exc)
                self._connections.pop(session_id, None)
                writer.close()
                return

            log.info("PipeListener: handshake complete; session_id=%s", session_id)

            # T3: enter the long-lived bidirectional forwarder loop; the loop
            # pumps envelopes between the pipe and the configured FrameHandler
            # until the pipe disconnects or the listener is stopped
            try:
                await self._forward_frames(connection)
            finally:
                # the forwarder exited; deregister the connection and close the
                # writer cleanly so stop() doesn't double-close. Eviction of
                # per-session state on disconnect is T6 territory; T3 only owns
                # the transport-level teardown.
                self._connections.pop(session_id, None)
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    writer.close()
                    await writer.wait_closed()
        finally:
            if task is not None:
                self._handler_tasks.discard(task)

    async def _forward_frames(self, connection: PipeConnection) -> None:
        """Pump envelopes between one pipe connection and the FrameHandler.

        Reads envelopes from ``connection.reader`` one at a time, validates the
        ``meta.session_id`` matches the connection's allocated id, dispatches
        the frame to :attr:`_frame_handler`, and writes any response back as a
        new envelope on ``connection.writer``. The loop runs until EOF, a
        transport error, or a malformed envelope ends it; no exception
        propagates out so the caller's ``finally`` always runs.

        :param connection: The handshake-completed pipe connection. Its
            ``session_id`` is the value the daemon allocated in
            :meth:`_handle_connection`.
        """
        reader, writer, session_id = connection.reader, connection.writer, connection.session_id

        while True:
            # read one envelope or break on EOF/disconnect; the pipe peer
            # closing its writer surfaces here as IncompleteReadError so the
            # forwarder unwinds cleanly without a per-call timeout
            try:
                envelope = await self._receive_envelope(reader)
            except (asyncio.IncompleteReadError, ConnectionError):
                log.info("PipeListener: pipe closed; session_id=%s", session_id)
                return
            except PipeProtocolError as exc:
                log.warning("PipeListener: malformed envelope from session_id=%s; closing: %s", session_id, exc)
                return

            # validate meta.session_id matches the connection's allocated id;
            # the daemon owns session_id assignment so any mismatch is a
            # contract violation by the pipe and the frame is dropped without
            # invoking the handler
            envelope_session_id = envelope.meta.get("session_id")
            if envelope_session_id != session_id:
                log.warning(
                    "PipeListener: dropping envelope with wrong session_id=%r (expected %s)",
                    envelope_session_id,
                    session_id,
                )
                continue

            # dispatch the frame; the handler may return None for notifications
            # or any other request that does not produce a response
            try:
                response_frame = await self._frame_handler.handle(session_id, envelope.frame)
            except Exception as exc:
                log.exception("PipeListener: FrameHandler raised for session_id=%s: %s", session_id, exc)
                continue

            # ferry the response back as an envelope stamped with the same
            # session_id so the pipe can correlate (and T5 can rely on the
            # pairing for any future per-session response routing)
            if response_frame is None:
                continue
            response_envelope = PipeEnvelope(meta={"session_id": session_id}, frame=response_frame)
            try:
                await self._send_envelope(writer, response_envelope)
            except (BrokenPipeError, ConnectionResetError) as exc:
                log.warning("PipeListener: response delivery failed for session_id=%s: %s", session_id, exc)
                return
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
