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
import os
import sys
from typing import Any
from urllib.parse import urlparse

from serena.pipe_protocol import PipeCatalog, PipeEnvelope, PipeHandshake, PipeProtocolError

log = logging.getLogger(__name__)


class PipeNotImplementedError(NotImplementedError):
    """Raised when pipe-transport functionality is invoked before its task lands."""


def run_pipe_client(daemon_url: str, project_root: str) -> None:
    """Run the per-client pipe forwarder until the upstream stdio is closed.

    Connects to the Serena daemon over a Unix socket, completes the
    ``mcp/session/open`` handshake (asserting ``project_root`` as the
    session_id), then enters a long-lived bidirectional forwarder that pumps
    newline-delimited JSON-RPC frames between the upstream stdio (e.g. Claude
    Code) and the daemon. The initial daemon connection is retried with
    bounded backoff on transient socket failure (see
    :data:`_RECONNECT_ATTEMPTS_ENV`); on exhausted retries the function
    raises :exc:`SystemExit(1)` so Claude Code surfaces the failure to the
    operator rather than silently hanging.

    :param daemon_url: URI of the Serena daemon's listening socket. Only the
        ``unix://`` scheme is currently accepted; TCP and other schemes are
        reserved for future tasks.
    :param project_root: Absolute path of the project this pipe-client operates
        on. Asserted as the session_id at handshake; the daemon keys per-session
        state on it. Project root IS the session identity, so a respawned
        pipe-client into the same cwd re-attaches to the same daemon-side state
        and activation is preserved across pipe-process restarts.
    :raises ValueError: if ``daemon_url`` is not a ``unix://`` URL or has an
        empty path component.
    :raises PipeProtocolError: if the daemon's handshake response is malformed.
    :raises SystemExit: on exhausted reconnect attempts so the parent process
        sees a non-zero exit code instead of an opaque traceback.
    """
    # parse the unix:// URL to extract the socket path; reject anything else
    # so the operator gets a clear error rather than an obscure socket failure
    socket_path = _parse_unix_url(daemon_url)

    # delegate to the asyncio main; on exhausted-retry failure we translate the
    # socket error into SystemExit(1) so Claude Code's MCP startup surfaces a
    # clear non-zero exit instead of a noisy traceback
    try:
        asyncio.run(_run_pipe_main(socket_path, project_root))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        log.error(
            "serena-pipe: failed to connect to daemon at %s after retries: %s",
            socket_path,
            exc,
        )
        raise SystemExit(1) from exc


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


async def _handshake(socket_path: str, project_root: str) -> str:
    """Connect to ``socket_path`` and run the ``mcp/session/open`` exchange.

    :param socket_path: Filesystem path of the daemon's listening Unix socket.
    :param project_root: Absolute path of the project this pipe-client operates
        on. Sent in the handshake meta as the session_id; the daemon trusts it
        verbatim (no daemon-side UUID minting).
    :returns: The session_id echoed back by the daemon (== ``project_root``).
    """
    # establish the bidirectional stream; if the daemon isn't running this
    # fails immediately with FileNotFoundError or ConnectionRefusedError,
    # both of which produce a clear operator-facing message
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        # send mcp/session/open carrying project_root as session_id and await
        # the response; the daemon answers with a single envelope echoing the
        # session_id back in its meta channel
        writer.write(PipeHandshake.request(project_root).to_bytes())
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


async def _handshake_on(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, project_root: str) -> str:
    """Run the ``mcp/session/open`` exchange on an already-open connection.

    Unlike :func:`_handshake`, this helper does not open or close the
    connection: the caller manages the reader/writer lifecycle so the
    forwarder can reuse the same streams for frame forwarding after the
    handshake completes.

    :param reader: Async reader connected to the daemon's listener.
    :param writer: Async writer connected to the daemon's listener.
    :param project_root: Absolute path of the project this pipe-client operates
        on. Sent in the handshake meta as the session_id; the daemon trusts it
        verbatim (no daemon-side UUID minting).
    :returns: The session_id echoed back by the daemon (== ``project_root``).
    :raises PipeProtocolError: if the daemon's response is malformed or omits
        ``meta.session_id``.
    """
    # send mcp/session/open carrying project_root as session_id and await the response
    writer.write(PipeHandshake.request(project_root).to_bytes())
    await writer.drain()
    line = await reader.readuntil(b"\n")
    envelope = PipeEnvelope.from_bytes(line)
    return PipeHandshake.session_id_from_response(envelope)


async def _fetch_catalog_on(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session_id: str,
) -> list[dict[str, Any]]:
    """Fetch the daemon's authoritative tool catalog over an already-open connection.

    The pipe issues this exchange exactly once per connection, immediately
    after :func:`_handshake_on` succeeds. The daemon answers from its
    :class:`~serena.daemon_pipe.CatalogProvider`; FastMCP never sees the
    request. The returned list is cached for the connection's lifetime and
    used by :func:`_run_forwarder` to answer every upstream ``tools/list``
    locally without round-tripping to the daemon.

    :param reader: Async reader connected to the daemon's listener.
    :param writer: Async writer connected to the daemon's listener.
    :param session_id: The handshake-asserted session_id; stamped on the
        outbound envelope's ``meta`` channel since post-handshake envelopes
        always carry it. Also used to validate the response's ``meta``.
    :returns: The list of tool definitions the daemon advertised.
    :raises PipeProtocolError: if the daemon's response is malformed, has the
        wrong shape, or stamps a different ``session_id`` on its ``meta``.
    """
    # build the catalog request and stamp it with the now-known session_id;
    # PipeCatalog.request() leaves meta empty on purpose so the same encoder
    # works whether or not the caller has a session_id yet
    request = PipeCatalog.request()
    envelope = PipeEnvelope(meta={"session_id": session_id}, frame=request.frame)
    writer.write(envelope.to_bytes())
    await writer.drain()

    # receive exactly one envelope; the daemon answers a catalog request
    # synchronously so this readuntil is bounded by the daemon's catalog
    # production time
    line = await reader.readuntil(b"\n")
    response = PipeEnvelope.from_bytes(line)

    # verify the response stamps the same session_id we sent; a mismatch is
    # either a daemon bug or evidence that envelopes are being routed across
    # connections, both of which we surface as a PipeProtocolError rather than
    # silently trusting the catalog
    response_session_id = response.meta.get("session_id")
    if response_session_id != session_id:
        raise PipeProtocolError(
            f"catalog response session_id mismatch; sent {session_id!r}, got {response_session_id!r}"
        )

    return PipeCatalog.tools_from_response(response)


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


_RECONNECT_ATTEMPTS_ENV: str = "SERENA_PIPE_RECONNECT_ATTEMPTS"
"""Environment variable controlling how many times the pipe-client retries
the initial daemon socket connection before giving up. Default ``5``."""

_RECONNECT_ATTEMPTS_DEFAULT: int = 5
"""Default number of connection attempts when ``SERENA_PIPE_RECONNECT_ATTEMPTS``
is unset, empty, or unparsable."""

_RECONNECT_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)
"""Backoff schedule (seconds) between successive reconnect attempts. The last
entry is reused if the configured attempt count exceeds the schedule length."""


def _resolve_max_reconnect_attempts() -> int:
    """Read :data:`_RECONNECT_ATTEMPTS_ENV` from the environment.

    :returns: Configured attempt count clamped to a positive integer; on
        unset, empty, unparsable, or sub-1 values, falls back to
        :data:`_RECONNECT_ATTEMPTS_DEFAULT`.
    """
    # consult the environment; treat unset / empty as "use the default"
    raw = os.environ.get(_RECONNECT_ATTEMPTS_ENV)
    if raw is None or raw.strip() == "":
        return _RECONNECT_ATTEMPTS_DEFAULT

    # parse to int; non-integer values fall back with a warning so operators
    # see the misconfiguration rather than getting silent default behaviour
    try:
        n = int(raw.strip())
    except ValueError:
        log.warning(
            "serena-pipe: %s=%r is not a valid integer; using default %d",
            _RECONNECT_ATTEMPTS_ENV,
            raw,
            _RECONNECT_ATTEMPTS_DEFAULT,
        )
        return _RECONNECT_ATTEMPTS_DEFAULT

    # require at least one attempt; zero or negative would mean "never connect"
    if n < 1:
        log.warning(
            "serena-pipe: %s=%d is below the minimum of 1; using default %d",
            _RECONNECT_ATTEMPTS_ENV,
            n,
            _RECONNECT_ATTEMPTS_DEFAULT,
        )
        return _RECONNECT_ATTEMPTS_DEFAULT

    return n


async def _open_daemon_socket_with_retry(socket_path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open the daemon Unix socket with bounded retry on transient failure.

    The daemon may briefly be unavailable when the pipe-client first runs
    (daemon restarting, socket file racing into existence under the
    operator's launchd reload, transient permission flicker, etc.). Per the
    project-root-as-session-id design, the pipe-client may retry the
    handshake with the same ``project_root`` because daemon-side per-session
    state is no longer evicted on socket disconnect: a successful late
    handshake re-attaches to the daemon's existing per-session entry.

    :param socket_path: Filesystem path of the daemon's Unix socket.
    :returns: ``(reader, writer)`` for the established connection.
    :raises OSError: After the configured attempt count is exhausted, the
        last connection error is re-raised so the caller can exit non-zero.
    """
    # determine the retry budget; honour SERENA_PIPE_RECONNECT_ATTEMPTS or fall back
    max_attempts = _resolve_max_reconnect_attempts()
    last_exc: BaseException | None = None

    # attempt the connection up to max_attempts times with the configured backoff
    for attempt in range(max_attempts):
        try:
            return await asyncio.open_unix_connection(socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            last_exc = exc
            # final attempt failed; let the caller decide what to do
            if attempt + 1 == max_attempts:
                break
            # reuse the last backoff entry for any attempts beyond the schedule length
            backoff = _RECONNECT_BACKOFFS[min(attempt, len(_RECONNECT_BACKOFFS) - 1)]
            log.warning(
                "serena-pipe: daemon socket %s unreachable (attempt %d/%d): %s — retrying in %.1fs",
                socket_path,
                attempt + 1,
                max_attempts,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)

    # exhausted: surface a single ERROR-level summary so operators see one log line
    log.error(
        "serena-pipe: daemon socket %s unreachable after %d attempts; last error: %s",
        socket_path,
        max_attempts,
        last_exc,
    )
    assert last_exc is not None
    raise last_exc


async def _run_pipe_main(socket_path: str, project_root: str) -> None:
    """Open the daemon socket, complete the handshake, then run the forwarder.

    :param socket_path: Filesystem path of the daemon's Unix socket.
    :param project_root: Absolute path of the project this pipe-client operates
        on; supplied as the session_id at handshake.
    :raises OSError: If the daemon socket cannot be reached after the
        configured number of retry attempts (see
        :data:`_RECONNECT_ATTEMPTS_ENV`). The caller (:func:`run_pipe_client`)
        translates this into a non-zero exit so Claude Code surfaces the
        failure to the operator.
    """
    # establish the long-lived bidirectional stream with bounded retry; transient
    # daemon unavailability (restart, socket racing into existence) is recoverable
    # because daemon-side per-session state survives socket-level churn under the
    # project-root-as-session-id contract
    daemon_reader, daemon_writer = await _open_daemon_socket_with_retry(socket_path)
    try:
        # complete the handshake on the same connection we will keep open for
        # forwarding; we assert project_root as the session_id and the daemon
        # echoes it back so the rest of this connection's frames are tagged
        # with the same project_root in their meta channel
        session_id = await _handshake_on(daemon_reader, daemon_writer, project_root)
        log.info("serena-pipe handshake complete; session_id=%s", session_id)

        # fetch the daemon's authoritative tool catalog before any upstream
        # tools/list arrives; the forwarder caches this list for the
        # connection's lifetime and answers tools/list locally so the catalog
        # stays stable across daemon-side churn within this connection
        catalog = await _fetch_catalog_on(daemon_reader, daemon_writer, session_id)
        log.info("serena-pipe catalog fetched; %d tools advertised", len(catalog))

        # bind the upstream stdio (Claude Code <-> pipe) to async streams; the
        # forwarder pumps frames between these streams and the daemon socket
        stdin_reader, stdout_writer = await _attach_stdio()

        # run the bidirectional forwarder until either side disconnects; this
        # is the long-lived loop that keeps the pipe process alive for the
        # client's session
        await _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_reader, daemon_writer, catalog)
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
    catalog: list[dict[str, Any]] | None = None,
) -> None:
    """Pump newline-delimited JSON-RPC frames between upstream stdio and the daemon.

    Two concurrent pumps run side-by-side: ``upstream_to_daemon`` reads
    JSON-RPC frames from stdin, wraps each in a :class:`PipeEnvelope` stamped
    with ``meta.session_id``, and writes the envelope to the daemon socket;
    ``daemon_to_upstream`` reads envelopes from the daemon, unwraps each, and
    writes the inner frame line-by-line back to stdout. The first pump to
    terminate cancels the other so neither hangs after one direction closes.

    Upstream ``tools/list`` requests are answered locally from ``catalog``
    instead of being forwarded -- the daemon already advertised its
    authoritative catalog at handshake time and the forwarder caches that
    list for the connection's lifetime. Notifications (``tools/list``
    without an ``id``) are still forwarded so the daemon's notification
    bookkeeping stays consistent.

    :param session_id: The handshake-asserted session_id stamped on every
        outbound envelope.
    :param stdin_reader: Async reader for upstream JSON-RPC frames.
    :param stdout_writer: Async writer for downstream JSON-RPC frames.
    :param daemon_reader: Async reader for envelopes from the daemon.
    :param daemon_writer: Async writer for envelopes to the daemon.
    :param catalog: Tool definitions returned by the daemon at handshake
        time. ``None`` is treated as an empty catalog so callers from T1-T3
        tests that predate T4 continue to work without modification.
    """
    # treat None as empty so legacy callers (and any test that drives the
    # forwarder without a catalog argument) get tools/list answered with no
    # tools rather than a TypeError on None subscripting
    cached_catalog: list[dict[str, Any]] = catalog if catalog is not None else []

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

            # answer tools/list requests locally from the cached catalog;
            # only requests (those carrying an ``id``) get a synthetic
            # response, notifications fall through to the daemon so any
            # bookkeeping that depends on seeing them stays consistent
            if frame.get("method") == "tools/list" and "id" in frame:
                response_frame = {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": {"tools": cached_catalog},
                }
                response_bytes = (json.dumps(response_frame) + "\n").encode("utf-8")
                try:
                    stdout_writer.write(response_bytes)
                    await stdout_writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    return
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
