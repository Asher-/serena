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
import inspect
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Union

from serena.pipe_protocol import PipeCatalog, PipeEnvelope, PipeHandshake, PipeProtocolError

# T6: signature of a callback registered via :meth:`PipeListener.add_disconnect_handler`.
# A handler may be a plain function (returns ``None``) or an async function (returns an
# awaitable). The listener inspects the return value at call time and awaits it when
# necessary, so callers can register either shape without coordinating with each other.
DisconnectHandler = Callable[[str], Union[None, Awaitable[None]]]

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


class CatalogProvider(ABC):
    """Strategy for producing the daemon's authoritative tool catalog on demand.

    The pipe issues a single :class:`~serena.pipe_protocol.PipeCatalog` request
    immediately after the handshake; the listener intercepts that request
    BEFORE the FrameHandler dispatch path and asks the configured
    :class:`CatalogProvider` to produce the tool list. The result is the
    canonical list of MCP tool definitions (``name``, ``description``,
    ``inputSchema``, etc.) the pipe will use to answer every upstream
    ``tools/list`` locally for the rest of the connection's lifetime.

    The strategy is parameterised on ``session_id`` so a future implementation
    can return a session-scoped catalog (e.g. tools that depend on the
    activated project). T4's :class:`_NullCatalogProvider` and the test
    catalog provider are session-agnostic; T5's FastMCP-backed provider is
    where per-session shaping (if any) would live.
    """

    @abstractmethod
    async def get_catalog(self, session_id: str) -> list[dict[str, Any]]:
        """Return the JSON-serializable tool definitions for ``session_id``.

        :param session_id: The daemon-allocated session_id for the connection
            requesting the catalog. Implementations that emit a session-
            agnostic catalog ignore this parameter.
        :returns: A list of dicts, each shaped as an MCP tool definition. May
            be empty when no tools are exposed (e.g. the null provider).
        """


class _NullCatalogProvider(CatalogProvider):
    """Default :class:`CatalogProvider` that returns an empty tool list.

    Used when :class:`PipeListener` is constructed without an explicit catalog
    provider -- the existing T1/T2/T3 tests never issue a catalog fetch, so
    the null behaviour preserves their semantics. Production callers always
    inject a real provider (T5 will supply one backed by
    :class:`SerenaMCPFactory`).
    """

    async def get_catalog(self, session_id: str) -> list[dict[str, Any]]:
        # an empty list is a well-formed catalog response; the pipe will simply
        # answer every tools/list with no tools, which is the correct behaviour
        # when no provider has been wired
        return []


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

    def __init__(
        self,
        frame_handler: FrameHandler | None = None,
        catalog_provider: CatalogProvider | None = None,
    ) -> None:
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

        # the strategy that supplies the authoritative tool catalog when a pipe
        # issues pipe/catalog/get; None falls back to a null provider so T1-T3
        # tests (which never issue a catalog fetch) continue to pass unchanged
        self._catalog_provider: CatalogProvider = (
            catalog_provider if catalog_provider is not None else _NullCatalogProvider()
        )

        # track the per-connection handler tasks so stop() can deterministically
        # await them and avoid leaking a forwarder loop past the listener's
        # lifetime; entries are added in _handle_connection and discarded when
        # the handler returns
        self._handler_tasks: set[asyncio.Task[None]] = set()

        # T6: callbacks fired when a pipe connection's forwarder loop exits.
        # Registration order is preserved (callers that compose multiple hooks
        # rely on deterministic ordering); each handler receives the
        # connection's daemon-allocated ``session_id``. Handlers are invoked
        # AFTER the connection is removed from :attr:`_connections` so a
        # handler that consults the listener's live state sees the post-
        # disconnect view.
        self._disconnect_handlers: list[DisconnectHandler] = []

    def add_disconnect_handler(self, handler: DisconnectHandler) -> None:
        """Register ``handler`` to fire when any pipe connection disconnects.

        The handler is invoked exactly once per pipe connection, after the
        forwarder loop exits and the connection is removed from
        :attr:`connections`. It is called with the connection's
        daemon-allocated ``session_id`` so eviction logic can drop per-session
        state without holding a reference to the :class:`PipeConnection`.

        Multiple handlers may be registered; they fire in registration order.
        An exception in one handler does not prevent later handlers from
        running -- the listener catches and logs each handler's exception.
        Handlers may be plain or ``async`` functions; the listener awaits the
        return value when it is awaitable.

        :param handler: A callable taking the ``session_id`` (str) and
            returning ``None`` or an awaitable.
        """
        self._disconnect_handlers.append(handler)

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

        The session_id is taken from the handshake request envelope's ``meta``
        field; the daemon does NOT mint one. The pipe-client typically asserts
        its project_root as the session_id, so two pipe-clients on the same
        project ARE the same logical session by design and share daemon-side
        per-session state. Per the project-root-as-session-id contract this
        handler also does NOT trigger eviction on socket disconnect: a respawned
        pipe-client into the same project finds activation preserved.

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

            # the pipe-client supplies session_id in the handshake meta channel;
            # the daemon does NOT mint one. Trust what the client asserts: when
            # two pipe-clients send the same session_id (e.g. same project root)
            # they ARE the same logical session and share per-session state by
            # design, not by collision.
            session_id = envelope.meta.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                log.warning(
                    "PipeListener: handshake meta missing session_id; closing: meta=%r",
                    envelope.meta,
                )
                writer.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await writer.wait_closed()
                return

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
                # writer cleanly so stop() doesn't double-close. Per the
                # project-root-as-session-id design, daemon-side per-session
                # state in SerenaAgent survives this disconnect so a respawned
                # pipe-client into the same project finds activation preserved.
                # Disconnect handlers (if any are registered) still fire as a
                # listener extension point, but the production wiring no longer
                # registers agent.evict_pipe_session on this list.
                self._connections.pop(session_id, None)
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    writer.close()
                    await writer.wait_closed()
                await self._fire_disconnect_handlers(session_id)
        finally:
            if task is not None:
                self._handler_tasks.discard(task)

    async def _fire_disconnect_handlers(self, session_id: str) -> None:
        """Invoke each registered disconnect handler with ``session_id``.

        Handlers are called in registration order. Exceptions are caught and
        logged so a misbehaving handler cannot starve later ones; the
        ``session_id`` is included in the log so eviction failures are
        diagnosable from the daemon logs alone.

        Plain (non-async) handlers run inline; async handlers are awaited.
        ``inspect.isawaitable`` distinguishes the two at call time so callers
        can register either shape without the listener requiring a uniform
        signature.
        """
        for handler in list(self._disconnect_handlers):
            try:
                result = handler(session_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # never propagate: a single misbehaving handler must not
                # affect the listener loop or sibling handlers
                log.exception(
                    "PipeListener: disconnect handler raised for session_id=%s; continuing",
                    session_id,
                )

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

            # intercept pipe/catalog/get BEFORE the FrameHandler; the catalog
            # exchange is internal pipe-protocol traffic that FastMCP must
            # never see, so we answer it locally from the configured
            # CatalogProvider and skip the handler dispatch entirely
            if PipeCatalog.is_request(envelope):
                try:
                    tools = await self._catalog_provider.get_catalog(session_id)
                except Exception as exc:
                    log.exception("PipeListener: CatalogProvider raised for session_id=%s: %s", session_id, exc)
                    continue
                response_envelope = PipeCatalog.response(session_id, tools)
                try:
                    await self._send_envelope(writer, response_envelope)
                except (BrokenPipeError, ConnectionResetError) as exc:
                    log.warning("PipeListener: catalog response delivery failed for session_id=%s: %s", session_id, exc)
                    return
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
