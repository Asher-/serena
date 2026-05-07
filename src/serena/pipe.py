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
import contextlib
import json
import logging
import sys
from urllib.parse import urlparse

from serena.pipe_protocol import PipeEnvelope, PipeHandshake, PipeProtocolError

log = logging.getLogger(__name__)


class PipeNotImplementedError(NotImplementedError):
    """Raised when pipe-transport functionality is invoked before its task lands."""


def run_pipe_client(daemon_url: str) -> None:
    """Run the per-client pipe forwarder until the upstream stdio is closed.

    Connects to the Serena daemon over a Unix socket, completes the
    ``mcp/session/open`` handshake, then enters a long-lived bidirectional
    forwarder that pumps newline-delimited JSON-RPC frames between the
    upstream stdio (e.g. Claude Code) and the daemon.

    :param daemon_url: URI of the Serena daemon's listening socket. Only the
        ``unix://`` scheme is currently accepted; TCP and other schemes are
        reserved for future tasks.
    :raises ValueError: if ``daemon_url`` is not a ``unix://`` URL or has an
        empty path component.
    :raises PipeProtocolError: if the daemon's handshake response is malformed.
    """
    # parse the unix:// URL to extract the socket path; reject anything else
    # so the operator gets a clear error rather than an obscure socket failure
    socket_path = _parse_unix_url(daemon_url)

    # delegate to the asyncio main; the function is sync-callable so it plugs
    # straight into the click handler in cli.py without further wiring
    asyncio.run(_run_pipe_main(socket_path))


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


async def _handshake_on(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    """Run the ``mcp/session/open`` exchange on an already-open connection.

    Unlike :func:`_handshake`, this helper does not open or close the
    connection: the caller manages the reader/writer lifecycle so the
    forwarder can reuse the same streams for frame forwarding after the
    handshake completes.

    :param reader: Async reader connected to the daemon's listener.
    :param writer: Async writer connected to the daemon's listener.
    :returns: The daemon-asserted UUID4 ``session_id`` extracted from the
        response's ``meta`` channel.
    :raises PipeProtocolError: if the daemon's response is malformed or omits
        ``meta.session_id``.
    """
    # send mcp/session/open and await the response on the same connection
    writer.write(PipeHandshake.request().to_bytes())
    await writer.drain()
    line = await reader.readuntil(b"\n")
    envelope = PipeEnvelope.from_bytes(line)
    return PipeHandshake.session_id_from_response(envelope)


async def _attach_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap the running process's stdin/stdout in async StreamReader/StreamWriter.

    The forwarder treats stdin and stdout as newline-delimited JSON-RPC byte
    streams; this helper installs the protocol bridges that asyncio needs to
    expose those byte streams under its async stream API.

    :returns: ``(stdin_reader, stdout_writer)`` ready for the forwarder loop.
    """
    loop = asyncio.get_running_loop()

    # bind stdin to a StreamReader via a StreamReaderProtocol; the protocol
    # feeds incoming bytes into the reader as they arrive on fd 0
    stdin_reader = asyncio.StreamReader()
    stdin_protocol = asyncio.StreamReaderProtocol(stdin_reader)
    await loop.connect_read_pipe(lambda: stdin_protocol, sys.stdin)

    # bind stdout via connect_write_pipe; the FlowControlMixin protocol gives
    # us write/drain semantics on fd 1 without spinning a custom transport
    stdout_transport, stdout_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    stdout_writer = asyncio.StreamWriter(stdout_transport, stdout_protocol, None, loop)
    return stdin_reader, stdout_writer


async def _run_pipe_main(socket_path: str) -> None:
    """Open the daemon socket, complete the handshake, then run the forwarder.

    :param socket_path: Filesystem path of the daemon's Unix socket.
    """
    # establish the long-lived bidirectional stream; if the daemon isn't
    # running this fails immediately with FileNotFoundError or
    # ConnectionRefusedError, both of which produce a clear operator message
    daemon_reader, daemon_writer = await asyncio.open_unix_connection(socket_path)
    try:
        # complete the handshake on the same connection we will keep open for
        # forwarding; the daemon-asserted session_id stamps every outbound
        # envelope's meta channel for the rest of the connection's lifetime
        session_id = await _handshake_on(daemon_reader, daemon_writer)
        log.info("serena-pipe handshake complete; daemon-asserted session_id=%s", session_id)

        # bind the upstream stdio (Claude Code <-> pipe) to async streams; the
        # forwarder pumps frames between these streams and the daemon socket
        stdin_reader, stdout_writer = await _attach_stdio()

        # run the bidirectional forwarder until either side disconnects; this
        # is the long-lived loop that keeps the pipe process alive for the
        # client's session
        await _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_reader, daemon_writer)
    finally:
        # always close the daemon-side writer cleanly so the daemon's
        # forwarder loop sees EOF and unwinds without warnings
        daemon_writer.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await daemon_writer.wait_closed()


async def _run_forwarder(
    session_id: str,
    stdin_reader: asyncio.StreamReader,
    stdout_writer: asyncio.StreamWriter,
    daemon_reader: asyncio.StreamReader,
    daemon_writer: asyncio.StreamWriter,
) -> None:
    """Pump newline-delimited JSON-RPC frames between upstream stdio and the daemon.

    Two concurrent pumps run side-by-side: ``upstream_to_daemon`` reads
    JSON-RPC frames from stdin, wraps each in a :class:`PipeEnvelope` stamped
    with ``meta.session_id``, and writes the envelope to the daemon socket;
    ``daemon_to_upstream`` reads envelopes from the daemon, unwraps each, and
    writes the inner frame line-by-line back to stdout. The first pump to
    terminate cancels the other so neither hangs after one direction closes.

    :param session_id: The handshake-asserted session_id stamped on every
        outbound envelope.
    :param stdin_reader: Async reader for upstream JSON-RPC frames.
    :param stdout_writer: Async writer for downstream JSON-RPC frames.
    :param daemon_reader: Async reader for envelopes from the daemon.
    :param daemon_writer: Async writer for envelopes to the daemon.
    """

    async def upstream_to_daemon() -> None:
        # read JSON-RPC lines from stdin, wrap in envelopes, send to daemon
        while True:
            try:
                line = await stdin_reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                # accept a final un-newlined fragment if upstream half-closed
                # mid-frame so the daemon sees the last frame before EOF
                if not exc.partial:
                    return
                line = exc.partial
                if not line.endswith(b"\n"):
                    line = line + b"\n"
            except ConnectionError:
                return

            # parse the JSON-RPC frame; a malformed line is dropped with a
            # warning rather than killing the whole forwarder, since one bad
            # frame should not lose the rest of the session
            try:
                frame = json.loads(line.decode("utf-8").rstrip("\n"))
            except json.JSONDecodeError as exc:
                log.error("serena-pipe: malformed upstream frame; dropping: %s", exc)
                continue
            if not isinstance(frame, dict):
                log.error("serena-pipe: upstream frame is not a JSON object; dropping: %r", frame)
                continue

            # wrap and forward; broken-pipe errors here mean the daemon side
            # disappeared, which is the same termination signal as EOF on
            # daemon_reader so we just unwind
            envelope = PipeEnvelope(meta={"session_id": session_id}, frame=frame)
            try:
                daemon_writer.write(envelope.to_bytes())
                await daemon_writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                return

    async def daemon_to_upstream() -> None:
        # read envelopes from daemon, unwrap, write JSON-RPC frames to stdout
        while True:
            try:
                line = await daemon_reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                return

            # decode the envelope; a malformed envelope is logged and dropped
            # for the same defense-in-depth reason as the upstream side
            try:
                envelope = PipeEnvelope.from_bytes(line)
            except PipeProtocolError as exc:
                log.error("serena-pipe: malformed daemon envelope; dropping: %s", exc)
                continue

            # write the frame back to upstream stdio; we re-encode with a
            # trailing newline so the upstream JSON-RPC decoder can split on
            # newlines as the MCP stdio convention requires
            frame_bytes = (json.dumps(envelope.frame) + "\n").encode("utf-8")
            try:
                stdout_writer.write(frame_bytes)
                await stdout_writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                return

    # spawn both pumps; the first to finish cancels the other so we don't
    # leak an orphan task if upstream closes before the daemon (or vice-versa)
    upstream_task = asyncio.create_task(upstream_to_daemon())
    daemon_task = asyncio.create_task(daemon_to_upstream())
    try:
        done, pending = await asyncio.wait(
            [upstream_task, daemon_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # propagate any exception the completed task raised so the caller can
        # log it; cancellation of the other task is intentional and silent
        for task in done:
            task.result()
    finally:
        # belt-and-braces: ensure both tasks are fully reaped even if the
        # ``await asyncio.wait`` itself was cancelled by an outer scope
        for task in (upstream_task, daemon_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
