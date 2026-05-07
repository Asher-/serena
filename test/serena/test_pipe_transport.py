"""Pipe transport regression tests.

This module is the canonical home for tests against the per-client stdio pipe
process described in :mod:`serena.pipe` and the daemon-side handshake-asserted
session pinning. T1's CLI scaffolding tests live here together with the deeper
transport regressions added in T7-T10 (cursor lifetime, parallel-sibling
isolation, batch consistency, restart resilience).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from serena.cli import TopLevelCommands
from serena.daemon_pipe import CatalogProvider, FrameHandler, PipeListener
from serena.pipe import _fetch_catalog_on, _handshake, _handshake_on, _parse_unix_url, _run_forwarder, run_pipe_client
from serena.pipe_protocol import PipeCatalog, PipeEnvelope, PipeHandshake, PipeProtocolError


@pytest.fixture
def cli_runner() -> CliRunner:
    """Return a Click :class:`CliRunner` for invoking start-mcp-server in isolation."""
    return CliRunner()


class TestPipeCLI:
    """Verify ``serena start-mcp-server --transport pipe`` plumbing.

    The pipe transport is a per-client forwarder, not a daemon: it MUST dispatch
    to :func:`serena.pipe.run_pipe_client` BEFORE the daemon-side
    :class:`serena.mcp.SerenaMCPFactory` is instantiated. Regular transports
    (``stdio``, ``sse``, ``streamable-http``) must continue to flow through
    :class:`SerenaMCPFactory` unchanged.
    """

    def test_transport_choice_includes_pipe(self) -> None:
        # the click choice list must accept 'pipe' as a new transport value
        transport_param = next(param for param in TopLevelCommands.start_mcp_server.params if param.name == "transport")
        assert isinstance(transport_param.type, click.Choice)
        assert "pipe" in transport_param.type.choices

    def test_daemon_url_option_exists_with_design_default(self) -> None:
        # the new --daemon-url option must exist and default to the design-plan socket path
        daemon_url_param = next(
            (param for param in TopLevelCommands.start_mcp_server.params if param.name == "daemon_url"),
            None,
        )
        assert daemon_url_param is not None, "--daemon-url is missing from start-mcp-server"
        assert daemon_url_param.default == "unix:///tmp/serena-daemon.sock"

    def test_pipe_transport_dispatches_to_pipe_entry_point(self, cli_runner: CliRunner) -> None:
        # transport=pipe MUST NOT instantiate SerenaMCPFactory; it MUST call run_pipe_client
        with (
            patch("serena.pipe.run_pipe_client") as run_pipe,
            patch("serena.cli.SerenaMCPFactory") as factory,
        ):
            result = cli_runner.invoke(
                TopLevelCommands.start_mcp_server,
                ["--transport", "pipe", "--daemon-url", "unix:///tmp/test.sock"],
            )
        assert result.exit_code == 0, result.output
        run_pipe.assert_called_once_with(daemon_url="unix:///tmp/test.sock")
        factory.assert_not_called()

    def test_pipe_transport_uses_default_daemon_url_when_unspecified(self, cli_runner: CliRunner) -> None:
        # omitting --daemon-url with --transport pipe must fall through to the design-plan default
        with (
            patch("serena.pipe.run_pipe_client") as run_pipe,
            patch("serena.cli.SerenaMCPFactory") as factory,
        ):
            result = cli_runner.invoke(TopLevelCommands.start_mcp_server, ["--transport", "pipe"])
        assert result.exit_code == 0, result.output
        run_pipe.assert_called_once_with(daemon_url="unix:///tmp/serena-daemon.sock")
        factory.assert_not_called()

    def test_stdio_transport_does_not_dispatch_to_pipe_entry_point(self, cli_runner: CliRunner) -> None:
        # the legacy stdio path must continue to flow through SerenaMCPFactory unchanged
        with (
            patch("serena.pipe.run_pipe_client") as run_pipe,
            patch("serena.cli.SerenaMCPFactory") as factory,
        ):
            cli_runner.invoke(TopLevelCommands.start_mcp_server, ["--transport", "stdio"])
        run_pipe.assert_not_called()
        factory.assert_called_once()


@pytest.fixture
def socket_path() -> Iterator[str]:
    """Yield a short, unique Unix-socket path; clean up after the test."""
    path = f"/tmp/serena-pipe-test-{uuid.uuid4().hex[:8]}.sock"
    yield path
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


class TestPipeProtocol:
    """Verify pipe-protocol envelope encoding, decoding, and handshake builders."""

    def test_envelope_roundtrip_preserves_meta_and_frame(self) -> None:
        envelope = PipeEnvelope(
            meta={"session_id": "abcd-1234"},
            frame={"jsonrpc": "2.0", "method": "tools/call", "id": 7},
        )
        encoded = envelope.to_bytes()
        assert encoded.endswith(b"\n"), "envelope must terminate with a newline"
        decoded = PipeEnvelope.from_bytes(encoded)
        assert decoded == envelope

    def test_envelope_decoding_strips_optional_trailing_newline(self) -> None:
        envelope = PipeEnvelope(meta={}, frame={"jsonrpc": "2.0", "method": "X"})
        encoded_with_nl = envelope.to_bytes()
        encoded_no_nl = encoded_with_nl.rstrip(b"\n")
        assert PipeEnvelope.from_bytes(encoded_with_nl) == envelope
        assert PipeEnvelope.from_bytes(encoded_no_nl) == envelope

    def test_envelope_rejects_non_dict_payload(self) -> None:
        with pytest.raises(PipeProtocolError):
            PipeEnvelope.from_bytes(b"[1, 2, 3]\n")

    def test_envelope_rejects_missing_meta_channel(self) -> None:
        with pytest.raises(PipeProtocolError, match="meta"):
            PipeEnvelope.from_bytes(b'{"frame": {}}\n')

    def test_envelope_rejects_missing_frame_channel(self) -> None:
        with pytest.raises(PipeProtocolError, match="frame"):
            PipeEnvelope.from_bytes(b'{"meta": {}}\n')

    def test_envelope_rejects_non_object_channel_values(self) -> None:
        with pytest.raises(PipeProtocolError, match="channels"):
            PipeEnvelope.from_bytes(b'{"meta": "hi", "frame": {}}\n')

    def test_handshake_request_uses_documented_method_and_id(self) -> None:
        request = PipeHandshake.request()
        assert request.frame["method"] == "mcp/session/open"
        assert request.frame["jsonrpc"] == "2.0"
        assert request.frame["id"] == 1
        assert request.meta == {}, "request must not pre-assert a session_id"

    def test_handshake_response_carries_session_id_only_in_meta(self) -> None:
        response = PipeHandshake.response("uuid-from-daemon")
        assert response.meta == {"session_id": "uuid-from-daemon"}
        assert "session_id" not in response.frame.get("result", {})

    def test_session_id_from_response_extracts_meta_channel(self) -> None:
        response = PipeHandshake.response("uuid-from-daemon")
        assert PipeHandshake.session_id_from_response(response) == "uuid-from-daemon"

    def test_session_id_from_response_rejects_missing_id(self) -> None:
        bare = PipeEnvelope(meta={}, frame={"jsonrpc": "2.0", "id": 1, "result": {}})
        with pytest.raises(PipeProtocolError):
            PipeHandshake.session_id_from_response(bare)

    def test_session_id_from_response_rejects_non_string_id(self) -> None:
        bogus = PipeEnvelope(meta={"session_id": 12345}, frame={"jsonrpc": "2.0", "id": 1, "result": {}})
        with pytest.raises(PipeProtocolError):
            PipeHandshake.session_id_from_response(bogus)


class TestPipeListener:
    """Verify the daemon-side handshake handler against real Unix sockets."""

    def test_handshake_assigns_uuid_session_id(self, socket_path: str) -> None:
        async def scenario() -> str:
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(PipeHandshake.request().to_bytes())
                await writer.drain()
                response_bytes = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_bytes)
                writer.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await writer.wait_closed()
                return PipeHandshake.session_id_from_response(response)
            finally:
                await listener.stop()

        session_id = asyncio.run(scenario())
        uuid.UUID(hex=session_id)

    def test_listener_registers_connection_after_handshake(self, socket_path: str) -> None:
        async def scenario() -> tuple[str, dict[str, str]]:
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(PipeHandshake.request().to_bytes())
                await writer.drain()
                response_bytes = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_bytes)
                session_id = PipeHandshake.session_id_from_response(response)
                snapshot = {sid: c.session_id for sid, c in listener.connections.items()}
                writer.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await writer.wait_closed()
                return session_id, snapshot
            finally:
                await listener.stop()

        session_id, snapshot = asyncio.run(scenario())
        assert session_id in snapshot
        assert snapshot[session_id] == session_id

    def test_listener_assigns_distinct_ids_to_concurrent_clients(self, socket_path: str) -> None:
        async def scenario() -> list[str]:
            listener = PipeListener()
            await listener.start(socket_path)
            try:

                async def one_client() -> str:
                    reader, writer = await asyncio.open_unix_connection(socket_path)
                    writer.write(PipeHandshake.request().to_bytes())
                    await writer.drain()
                    response_bytes = await reader.readuntil(b"\n")
                    response = PipeEnvelope.from_bytes(response_bytes)
                    writer.close()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        await writer.wait_closed()
                    return PipeHandshake.session_id_from_response(response)

                return list(await asyncio.gather(*[one_client() for _ in range(10)]))
            finally:
                await listener.stop()

        results = asyncio.run(scenario())
        assert len(set(results)) == 10, f"collided session_ids: {results}"

    def test_listener_rejects_non_handshake_first_envelope(self, socket_path: str) -> None:
        async def scenario() -> dict[str, str]:
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                bogus = PipeEnvelope(meta={}, frame={"jsonrpc": "2.0", "method": "tools/call", "id": 1})
                writer.write(bogus.to_bytes())
                await writer.drain()
                tail = await reader.read()
                assert tail == b"", "listener must close on protocol violation"
                snapshot = {sid: c.session_id for sid, c in listener.connections.items()}
                writer.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await writer.wait_closed()
                return snapshot
            finally:
                await listener.stop()

        snapshot = asyncio.run(scenario())
        assert snapshot == {}

    def test_listener_start_twice_raises(self, socket_path: str) -> None:
        async def scenario() -> None:
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                with pytest.raises(RuntimeError, match="twice"):
                    await listener.start(socket_path)
            finally:
                await listener.stop()

        asyncio.run(scenario())

    def test_listener_stop_is_idempotent(self, socket_path: str) -> None:
        async def scenario() -> None:
            listener = PipeListener()
            await listener.stop()
            await listener.start(socket_path)
            await listener.stop()
            await listener.stop()

        asyncio.run(scenario())


class TestPipeClient:
    """Verify the pipe-side run_pipe_client URL handling and sync wrapper."""

    def test_parse_unix_url_extracts_path(self) -> None:
        assert _parse_unix_url("unix:///tmp/serena-daemon.sock") == "/tmp/serena-daemon.sock"

    def test_parse_unix_url_rejects_non_unix_scheme(self) -> None:
        with pytest.raises(ValueError, match="unix"):
            _parse_unix_url("tcp://localhost:9999")

    def test_parse_unix_url_rejects_empty_path(self) -> None:
        with pytest.raises(ValueError, match="path"):
            _parse_unix_url("unix://")

    def test_run_pipe_client_validates_url_before_connect(self) -> None:
        with pytest.raises(ValueError, match="unix"):
            run_pipe_client(daemon_url="http://example.com")

    


class TestPipeHandshakeIntegration:
    """End-to-end handshake against a real listener via the pipe-side helper."""

    def test_handshake_helper_completes_against_real_listener(self, socket_path: str) -> None:
        async def scenario() -> str:
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                return await _handshake(socket_path)
            finally:
                await listener.stop()

        session_id = asyncio.run(scenario())
        uuid.UUID(hex=session_id)



class _EchoFrameHandler(FrameHandler):
    """Test :class:`FrameHandler` that echoes every frame back with the recorded session_id.

    Records each (session_id, frame) pair the listener routes to it so tests can
    assert on the dispatch sequence in addition to the round-trip behaviour.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def handle(self, session_id: str, frame: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append((session_id, dict(frame)))
        return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"echoed": frame}}


class _NoResponseFrameHandler(FrameHandler):
    """Test :class:`FrameHandler` that records every dispatch but never responds.

    Models a JSON-RPC notification handler that returns ``None`` so the
    forwarder exercises its skip-when-no-response path without aborting.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def handle(self, session_id: str, frame: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append((session_id, dict(frame)))
        return None


class _RaisingFrameHandler(FrameHandler):
    """Test :class:`FrameHandler` that always raises -- used to verify the forwarder shields the loop from handler errors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def handle(self, session_id: str, frame: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append((session_id, dict(frame)))
        raise RuntimeError("intentional handler failure")


class _RecordingCatalogProvider(CatalogProvider):
    """Test :class:`CatalogProvider` that returns a canned tool list and records each call.

    Lets catalog tests assert (a) the provider was invoked, (b) it was invoked
    with the expected ``session_id``, and (c) the listener stamped the
    response with the provider's output. Mirrors :class:`_EchoFrameHandler`
    in shape so test scaffolding stays consistent across the two strategy
    families.
    """

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self._tools = list(tools)
        self.calls: list[str] = []

    async def get_catalog(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(session_id)
        # return a fresh copy so any caller-side mutation does not affect the
        # canned baseline used to compare across multiple invocations
        return [dict(tool) for tool in self._tools]


class _RaisingCatalogProvider(CatalogProvider):
    """Test :class:`CatalogProvider` that always raises -- used to verify the listener does not propagate provider errors."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_catalog(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(session_id)
        raise RuntimeError("intentional catalog provider failure")


async def _client_handshake(socket_path: str) -> tuple[str, asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to ``socket_path`` and complete the handshake; return the open streams.

    :returns: ``(session_id, reader, writer)`` -- the streams stay open so
        callers can drive frame forwarding on top of the same connection.
    """
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(PipeHandshake.request().to_bytes())
    await writer.drain()
    response_line = await reader.readuntil(b"\n")
    response = PipeEnvelope.from_bytes(response_line)
    return PipeHandshake.session_id_from_response(response), reader, writer


async def _drain_close(writer: asyncio.StreamWriter) -> None:
    """Close ``writer`` and wait for it, swallowing the usual transport hiccups."""
    writer.close()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await writer.wait_closed()


class TestPipeFrameForwarder:
    """Daemon-side T3 forwarder: PipeListener routes envelopes through a FrameHandler."""

    def test_listener_invokes_handler_with_session_id_and_frame(self, socket_path: str) -> None:
        async def scenario() -> tuple[str, list[tuple[str, dict[str, Any]]], dict[str, Any]]:
            handler = _EchoFrameHandler()
            listener = PipeListener(frame_handler=handler)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)

                # send one forwarded JSON-RPC frame stamped with the daemon's session_id
                request = PipeEnvelope(
                    meta={"session_id": session_id},
                    frame={"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}},
                )
                writer.write(request.to_bytes())
                await writer.drain()

                # read the echoed response
                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                return session_id, handler.calls, response.frame
            finally:
                await listener.stop()

        session_id, calls, response_frame = asyncio.run(scenario())
        assert calls == [(session_id, {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})]
        assert response_frame == {"jsonrpc": "2.0", "id": 7, "result": {"echoed": {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}}}

    def test_listener_stamps_response_with_connection_session_id(self, socket_path: str) -> None:
        async def scenario() -> tuple[str, dict[str, Any]]:
            listener = PipeListener(frame_handler=_EchoFrameHandler())
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame={"jsonrpc": "2.0", "id": 1, "method": "x"}).to_bytes()
                )
                await writer.drain()
                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                return session_id, response.meta
            finally:
                await listener.stop()

        session_id, meta = asyncio.run(scenario())
        assert meta == {"session_id": session_id}

    def test_listener_forwards_many_frames_in_sequence(self, socket_path: str) -> None:
        async def scenario() -> tuple[int, list[int]]:
            handler = _EchoFrameHandler()
            listener = PipeListener(frame_handler=handler)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)

                # send 25 frames serially and collect the response ids
                response_ids: list[int] = []
                for i in range(25):
                    writer.write(
                        PipeEnvelope(
                            meta={"session_id": session_id},
                            frame={"jsonrpc": "2.0", "id": i, "method": "tick"},
                        ).to_bytes()
                    )
                    await writer.drain()
                    response_line = await reader.readuntil(b"\n")
                    response = PipeEnvelope.from_bytes(response_line)
                    response_ids.append(response.frame["id"])
                await _drain_close(writer)
                return len(handler.calls), response_ids
            finally:
                await listener.stop()

        call_count, response_ids = asyncio.run(scenario())
        assert call_count == 25
        assert response_ids == list(range(25))

    def test_listener_drops_envelope_with_wrong_session_id(self, socket_path: str) -> None:
        async def scenario() -> tuple[int, dict[str, Any]]:
            handler = _EchoFrameHandler()
            listener = PipeListener(frame_handler=handler)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)

                # send one impostor frame (wrong session_id) followed by one real frame;
                # the listener must drop the impostor and process the real one
                writer.write(
                    PipeEnvelope(
                        meta={"session_id": "00000000000000000000000000000000"},
                        frame={"jsonrpc": "2.0", "id": 1, "method": "impostor"},
                    ).to_bytes()
                )
                writer.write(
                    PipeEnvelope(
                        meta={"session_id": session_id},
                        frame={"jsonrpc": "2.0", "id": 2, "method": "real"},
                    ).to_bytes()
                )
                await writer.drain()

                # only the real frame should produce a response
                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                return len(handler.calls), response.frame
            finally:
                await listener.stop()

        call_count, response_frame = asyncio.run(scenario())
        assert call_count == 1, "handler must not be invoked for impostor session_id"
        assert response_frame["id"] == 2

    def test_listener_handler_concurrent_clients_get_correct_session_id(self, socket_path: str) -> None:
        async def scenario() -> list[tuple[str, str]]:
            handler = _EchoFrameHandler()
            listener = PipeListener(frame_handler=handler)
            await listener.start(socket_path)
            try:

                async def one_client() -> tuple[str, str]:
                    session_id, reader, writer = await _client_handshake(socket_path)
                    writer.write(
                        PipeEnvelope(
                            meta={"session_id": session_id},
                            frame={"jsonrpc": "2.0", "id": 1, "method": "whoami", "params": {"sid": session_id}},
                        ).to_bytes()
                    )
                    await writer.drain()
                    response_line = await reader.readuntil(b"\n")
                    response = PipeEnvelope.from_bytes(response_line)
                    await _drain_close(writer)
                    # the handler echoes the originating frame in result.echoed.params.sid
                    echoed_sid = response.frame["result"]["echoed"]["params"]["sid"]
                    return session_id, echoed_sid

                return list(await asyncio.gather(*[one_client() for _ in range(8)]))
            finally:
                await listener.stop()

        results = asyncio.run(scenario())
        assert len({r[0] for r in results}) == 8, f"collided session_ids: {results}"
        for session_id, echoed_sid in results:
            assert session_id == echoed_sid, f"client crossover: handshake={session_id} echo={echoed_sid}"

    def test_listener_loop_exits_on_pipe_close(self, socket_path: str) -> None:
        async def scenario() -> int:
            listener = PipeListener(frame_handler=_EchoFrameHandler())
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)
                # close the client side immediately; the daemon's forwarder must
                # observe EOF on its read side and unwind so listener.stop() can
                # complete without hanging or warnings
                await _drain_close(writer)
                # give the daemon a brief moment to observe the close before we
                # snapshot the connections map; the registration is async so a
                # zero-tick yield isn't enough on slow hosts
                for _ in range(50):
                    if not listener.connections:
                        break
                    await asyncio.sleep(0.01)
                return len(listener.connections)
            finally:
                await listener.stop()

        leftover = asyncio.run(scenario())
        assert leftover == 0, "forwarder must deregister the connection when the pipe closes"

    def test_listener_default_handler_drops_frames_silently(self, socket_path: str) -> None:
        async def scenario() -> bool:
            # no frame_handler argument -- the null handler should drop frames
            # without crashing the forwarder; the test asserts the listener
            # stays up by completing a fresh handshake on a second connection
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                session_id_a, reader_a, writer_a = await _client_handshake(socket_path)
                writer_a.write(
                    PipeEnvelope(
                        meta={"session_id": session_id_a},
                        frame={"jsonrpc": "2.0", "id": 1, "method": "noop"},
                    ).to_bytes()
                )
                await writer_a.drain()

                # no response is expected; just make sure a second handshake
                # still succeeds, proving the null-handler path didn't kill the
                # listener
                await asyncio.sleep(0.05)
                session_id_b, reader_b, writer_b = await _client_handshake(socket_path)
                await _drain_close(writer_a)
                await _drain_close(writer_b)
                return session_id_a != session_id_b
            finally:
                await listener.stop()

        assert asyncio.run(scenario())

    def test_listener_continues_after_handler_raises(self, socket_path: str) -> None:
        async def scenario() -> int:
            handler = _RaisingFrameHandler()
            listener = PipeListener(frame_handler=handler)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)
                # send 3 frames; each invokes the handler, which raises;
                # the forwarder must absorb every exception and keep reading
                for i in range(3):
                    writer.write(
                        PipeEnvelope(
                            meta={"session_id": session_id},
                            frame={"jsonrpc": "2.0", "id": i, "method": "boom"},
                        ).to_bytes()
                    )
                await writer.drain()
                # let the daemon process the burst before we close the client
                for _ in range(50):
                    if len(handler.calls) >= 3:
                        break
                    await asyncio.sleep(0.01)
                await _drain_close(writer)
                return len(handler.calls)
            finally:
                await listener.stop()

        assert asyncio.run(scenario()) == 3


class TestPipeForwarderClient:
    """Pipe-side T3 forwarder: ``_run_forwarder`` pumps stdio<->daemon streams."""

    def test_forwarder_stamps_session_id_on_outbound_envelopes(self) -> None:
        async def scenario() -> list[tuple[dict[str, Any], dict[str, Any]]]:
            # construct in-memory pipe stream pairs: (stdin_w -> stdin_r) for
            # upstream and (daemon_w -> daemon_r) for the daemon socket
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            session_id = "deadbeefdeadbeefdeadbeefdeadbeef"

            # spawn the forwarder; we feed frames into stdin_writer and read
            # the resulting envelopes out of daemon_reader
            forwarder = asyncio.create_task(
                _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_in_reader, daemon_writer)
            )

            try:
                # write two JSON-RPC frames into stdin and read the resulting
                # envelopes off the daemon socket
                for i in range(2):
                    stdin_writer.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": "tick"}) + "\n").encode("utf-8"))
                await stdin_writer.drain()

                envelopes: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for _ in range(2):
                    line = await daemon_reader.readuntil(b"\n")
                    envelope = PipeEnvelope.from_bytes(line)
                    envelopes.append((dict(envelope.meta), dict(envelope.frame)))

                # close stdin and the daemon-in side so the forwarder terminates
                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return envelopes
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        envelopes = asyncio.run(scenario())
        assert len(envelopes) == 2
        for i, (meta, frame) in enumerate(envelopes):
            assert meta == {"session_id": "deadbeefdeadbeefdeadbeefdeadbeef"}
            assert frame == {"jsonrpc": "2.0", "id": i, "method": "tick"}

    def test_forwarder_writes_response_frame_to_stdout(self) -> None:
        async def scenario() -> dict[str, Any]:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            session_id = "abcdef0123456789abcdef0123456789"
            forwarder = asyncio.create_task(
                _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_in_reader, daemon_writer)
            )
            try:
                # synthesize an envelope coming back from the daemon
                response = PipeEnvelope(
                    meta={"session_id": session_id},
                    frame={"jsonrpc": "2.0", "id": 42, "result": {"ok": True}},
                )
                daemon_in_writer.write(response.to_bytes())
                await daemon_in_writer.drain()

                # read the resulting JSON-RPC line off stdout
                line = await stdout_reader.readuntil(b"\n")
                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return json.loads(line.decode("utf-8").rstrip("\n"))
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        frame = asyncio.run(scenario())
        assert frame == {"jsonrpc": "2.0", "id": 42, "result": {"ok": True}}

    def test_forwarder_exits_on_stdin_eof(self) -> None:
        async def scenario() -> bool:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            forwarder = asyncio.create_task(
                _run_forwarder("sid", stdin_reader, stdout_writer, daemon_in_reader, daemon_writer)
            )
            try:
                # close the upstream stdin -- the forwarder must observe EOF
                # on its readuntil and unwind both pumps
                stdin_writer.close()
                # also close daemon_in to signal EOF on the other pump
                daemon_in_writer.close()
                await asyncio.wait_for(forwarder, timeout=2.0)
                return True
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        assert asyncio.run(scenario())

    def test_forwarder_drops_malformed_upstream_frame(self) -> None:
        async def scenario() -> dict[str, Any]:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            session_id = "0000000011111111222222223333333"
            forwarder = asyncio.create_task(
                _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_in_reader, daemon_writer)
            )
            try:
                # one bad line followed by one good line; the bad line must be
                # logged and dropped, the good line must pass through
                stdin_writer.write(b"not json\n")
                stdin_writer.write((json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ok"}) + "\n").encode("utf-8"))
                await stdin_writer.drain()

                line = await daemon_reader.readuntil(b"\n")
                envelope = PipeEnvelope.from_bytes(line)

                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return dict(envelope.frame)
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        frame = asyncio.run(scenario())
        assert frame == {"jsonrpc": "2.0", "id": 9, "method": "ok"}


class TestPipeForwarderEnd2End:
    """Full-stack T3: pipe forwarder + PipeListener with echo handler -> round-trip."""

    def test_round_trips_frame_through_pipe_and_listener(self, socket_path: str) -> None:
        async def scenario() -> dict[str, Any]:
            # spin up the daemon side with an echo handler
            handler = _EchoFrameHandler()
            listener = PipeListener(frame_handler=handler)
            await listener.start(socket_path)
            try:
                # construct the pipe-side stream wires; we drive stdin manually
                # and read from stdout manually instead of touching real fds
                stdin_reader, stdin_writer = await _make_memory_stream_pair()
                stdout_reader, stdout_writer = await _make_memory_stream_pair()

                # open the daemon connection, run the handshake, then hand the
                # streams to _run_forwarder; this mirrors what _run_pipe_main
                # does without binding to actual stdio
                daemon_reader, daemon_writer = await asyncio.open_unix_connection(socket_path)
                session_id = await _handshake_on(daemon_reader, daemon_writer)

                forwarder = asyncio.create_task(
                    _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_reader, daemon_writer)
                )
                try:
                    # send a frame on the upstream side
                    stdin_writer.write(
                        (json.dumps({"jsonrpc": "2.0", "id": 13, "method": "echo", "params": {"x": 1}}) + "\n").encode("utf-8")
                    )
                    await stdin_writer.drain()

                    # read the echoed response on stdout
                    response_line = await stdout_reader.readuntil(b"\n")
                    return json.loads(response_line.decode("utf-8").rstrip("\n"))
                finally:
                    stdin_writer.close()
                    daemon_writer.close()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        await daemon_writer.wait_closed()
                    if not forwarder.done():
                        forwarder.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await forwarder
            finally:
                await listener.stop()

        response = asyncio.run(scenario())
        assert response["id"] == 13
        assert response["result"]["echoed"] == {"jsonrpc": "2.0", "id": 13, "method": "echo", "params": {"x": 1}}


class TestPipeCatalogProtocol:
    """Verify pipe-protocol :class:`PipeCatalog` request/response encoding contracts."""

    def test_request_uses_documented_method_and_id(self) -> None:
        request = PipeCatalog.request()
        assert request.frame["method"] == "pipe/catalog/get"
        assert request.frame["jsonrpc"] == "2.0"
        assert request.frame["id"] == 2
        assert request.meta == {}, "request must not pre-assert a session_id"

    def test_request_id_is_distinct_from_handshake(self) -> None:
        # the catalog id MUST differ from the handshake id so they cannot
        # collide if a future revision ever interleaves them on the wire
        assert PipeCatalog.REQUEST_ID != PipeHandshake.REQUEST_ID

    def test_response_carries_session_id_in_meta_and_tools_in_frame(self) -> None:
        tools = [{"name": "noop", "description": "x", "inputSchema": {"type": "object"}}]
        response = PipeCatalog.response("uuid-from-daemon", tools)
        assert response.meta == {"session_id": "uuid-from-daemon"}
        assert response.frame["jsonrpc"] == "2.0"
        assert response.frame["id"] == 2
        assert response.frame["result"] == {"tools": tools}

    def test_is_request_recognises_documented_method(self) -> None:
        envelope = PipeCatalog.request()
        assert PipeCatalog.is_request(envelope) is True

    def test_is_request_rejects_handshake(self) -> None:
        # the handshake's method is mcp/session/open, NOT pipe/catalog/get
        # so is_request must reject it even though the envelope shape matches
        assert PipeCatalog.is_request(PipeHandshake.request()) is False

    def test_is_request_rejects_other_jsonrpc_method(self) -> None:
        bogus = PipeEnvelope(meta={}, frame={"jsonrpc": "2.0", "method": "tools/call", "id": 5})
        assert PipeCatalog.is_request(bogus) is False

    def test_tools_from_response_returns_the_list(self) -> None:
        tools = [{"name": "alpha"}, {"name": "beta"}]
        response = PipeCatalog.response("sid", tools)
        assert PipeCatalog.tools_from_response(response) == tools

    def test_tools_from_response_returns_empty_list_when_daemon_advertises_none(self) -> None:
        # an empty list is a well-formed catalog; the helper must round-trip
        # it instead of treating empty as malformed
        response = PipeCatalog.response("sid", [])
        assert PipeCatalog.tools_from_response(response) == []

    def test_tools_from_response_rejects_missing_result(self) -> None:
        bare = PipeEnvelope(meta={"session_id": "sid"}, frame={"jsonrpc": "2.0", "id": 2})
        with pytest.raises(PipeProtocolError, match="result"):
            PipeCatalog.tools_from_response(bare)

    def test_tools_from_response_rejects_non_object_result(self) -> None:
        bogus = PipeEnvelope(meta={"session_id": "sid"}, frame={"jsonrpc": "2.0", "id": 2, "result": "nope"})
        with pytest.raises(PipeProtocolError, match="result"):
            PipeCatalog.tools_from_response(bogus)

    def test_tools_from_response_rejects_missing_tools(self) -> None:
        bogus = PipeEnvelope(meta={"session_id": "sid"}, frame={"jsonrpc": "2.0", "id": 2, "result": {}})
        with pytest.raises(PipeProtocolError, match="tools"):
            PipeCatalog.tools_from_response(bogus)

    def test_tools_from_response_rejects_non_list_tools(self) -> None:
        bogus = PipeEnvelope(meta={"session_id": "sid"}, frame={"jsonrpc": "2.0", "id": 2, "result": {"tools": {}}})
        with pytest.raises(PipeProtocolError, match="tools"):
            PipeCatalog.tools_from_response(bogus)


class TestPipeCatalogListener:
    """Verify the daemon-side :class:`PipeListener` intercepts :class:`PipeCatalog` requests."""

    def test_listener_dispatches_catalog_request_to_provider(self, socket_path: str) -> None:
        async def scenario() -> tuple[list[str], list[dict[str, Any]]]:
            tools = [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}]
            provider = _RecordingCatalogProvider(tools)
            # use an _EchoFrameHandler so any leak of the catalog request to
            # the FrameHandler dispatch path becomes a visible echo response
            # rather than a silent passthrough
            listener = PipeListener(frame_handler=_EchoFrameHandler(), catalog_provider=provider)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)

                # send the catalog request stamped with the connection's session_id
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                )
                await writer.drain()

                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                returned_tools = PipeCatalog.tools_from_response(response)
                return list(provider.calls), returned_tools
            finally:
                await listener.stop()

        provider_calls, returned_tools = asyncio.run(scenario())
        assert len(provider_calls) == 1, f"provider must be invoked exactly once, got {provider_calls!r}"
        assert returned_tools == [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}]

    def test_listener_invokes_provider_with_connection_session_id(self, socket_path: str) -> None:
        async def scenario() -> tuple[str, list[str]]:
            provider = _RecordingCatalogProvider([])
            listener = PipeListener(catalog_provider=provider)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                )
                await writer.drain()
                # consume the response so the writer isn't torn down before
                # the daemon flushes; the response value isn't asserted here
                await reader.readuntil(b"\n")
                await _drain_close(writer)
                return session_id, list(provider.calls)
            finally:
                await listener.stop()

        session_id, calls = asyncio.run(scenario())
        assert calls == [session_id]

    def test_listener_stamps_catalog_response_with_connection_session_id(self, socket_path: str) -> None:
        async def scenario() -> tuple[str, dict[str, Any]]:
            provider = _RecordingCatalogProvider([{"name": "noop"}])
            listener = PipeListener(catalog_provider=provider)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                )
                await writer.drain()
                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                return session_id, dict(response.meta)
            finally:
                await listener.stop()

        session_id, response_meta = asyncio.run(scenario())
        assert response_meta == {"session_id": session_id}

    def test_listener_does_not_invoke_frame_handler_for_catalog_request(self, socket_path: str) -> None:
        async def scenario() -> int:
            handler = _EchoFrameHandler()
            provider = _RecordingCatalogProvider([{"name": "alpha"}])
            listener = PipeListener(frame_handler=handler, catalog_provider=provider)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)

                # send catalog request first; the handler must NOT see it
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                )
                await writer.drain()
                await reader.readuntil(b"\n")  # drain the catalog response

                # send a normal frame so we can confirm the handler still
                # works for non-catalog traffic; the handler must see exactly
                # this one frame, not two
                writer.write(
                    PipeEnvelope(
                        meta={"session_id": session_id},
                        frame={"jsonrpc": "2.0", "id": 99, "method": "ping"},
                    ).to_bytes()
                )
                await writer.drain()
                await reader.readuntil(b"\n")  # drain the echo response

                await _drain_close(writer)
                return len(handler.calls)
            finally:
                await listener.stop()

        handler_calls = asyncio.run(scenario())
        assert handler_calls == 1, "FrameHandler must not see catalog requests"

    def test_listener_drops_catalog_envelope_with_wrong_session_id(self, socket_path: str) -> None:
        async def scenario() -> tuple[int, dict[str, Any]]:
            provider = _RecordingCatalogProvider([{"name": "alpha"}])
            listener = PipeListener(catalog_provider=provider)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)

                # impostor catalog request stamped with a wrong session_id;
                # the listener's pre-existing session_id check rejects this
                # envelope BEFORE the catalog intercept runs, so the
                # provider must not be invoked for the impostor at all
                writer.write(
                    PipeEnvelope(
                        meta={"session_id": "00000000000000000000000000000000"},
                        frame=PipeCatalog.request().frame,
                    ).to_bytes()
                )
                # follow with a real catalog request so we can read a single
                # response and confirm it's the legitimate one
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                )
                await writer.drain()

                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                return len(provider.calls), dict(response.meta)
            finally:
                await listener.stop()

        provider_calls, response_meta = asyncio.run(scenario())
        assert provider_calls == 1, "provider must NOT be invoked for impostor session_id"
        assert "session_id" in response_meta and response_meta["session_id"] != "00000000000000000000000000000000"

    def test_listener_default_catalog_provider_returns_empty(self, socket_path: str) -> None:
        async def scenario() -> list[dict[str, Any]]:
            # no catalog_provider supplied -- the null provider must answer
            # with an empty list rather than blocking the request
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                )
                await writer.drain()
                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                return PipeCatalog.tools_from_response(response)
            finally:
                await listener.stop()

        tools = asyncio.run(scenario())
        assert tools == []

    def test_listener_concurrent_catalog_requests_get_correct_session_ids(self, socket_path: str) -> None:
        async def scenario() -> list[tuple[str, dict[str, Any]]]:
            # the provider tags each tool with the calling session_id so we
            # can verify the listener routes each response to the correct
            # connection even under concurrent traffic
            class _SessionEchoCatalog(CatalogProvider):
                async def get_catalog(self, session_id: str) -> list[dict[str, Any]]:
                    return [{"name": session_id}]

            listener = PipeListener(catalog_provider=_SessionEchoCatalog())
            await listener.start(socket_path)
            try:

                async def one_client() -> tuple[str, dict[str, Any]]:
                    session_id, reader, writer = await _client_handshake(socket_path)
                    writer.write(
                        PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                    )
                    await writer.drain()
                    response_line = await reader.readuntil(b"\n")
                    response = PipeEnvelope.from_bytes(response_line)
                    await _drain_close(writer)
                    return session_id, dict(response.meta)

                return list(await asyncio.gather(*[one_client() for _ in range(8)]))
            finally:
                await listener.stop()

        results = asyncio.run(scenario())
        assert len({r[0] for r in results}) == 8, f"collided session_ids: {results}"
        for session_id, response_meta in results:
            assert response_meta == {"session_id": session_id}, f"crossover: handshake={session_id} response={response_meta}"

    def test_listener_continues_after_catalog_provider_raises(self, socket_path: str) -> None:
        async def scenario() -> tuple[int, dict[str, Any]]:
            # use a raising catalog provider; the first catalog request
            # produces no response, but the listener loop must keep running
            # so subsequent FrameHandler traffic still works
            provider = _RaisingCatalogProvider()
            handler = _EchoFrameHandler()
            listener = PipeListener(frame_handler=handler, catalog_provider=provider)
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)

                # send the doomed catalog request
                writer.write(
                    PipeEnvelope(meta={"session_id": session_id}, frame=PipeCatalog.request().frame).to_bytes()
                )
                await writer.drain()
                # provider raised -> no response was sent; we cannot read on
                # the catalog side. instead send a regular frame and confirm
                # the listener still answers it (handler-routed)
                writer.write(
                    PipeEnvelope(
                        meta={"session_id": session_id},
                        frame={"jsonrpc": "2.0", "id": 7, "method": "ping"},
                    ).to_bytes()
                )
                await writer.drain()
                response_line = await reader.readuntil(b"\n")
                response = PipeEnvelope.from_bytes(response_line)
                await _drain_close(writer)
                return len(provider.calls), dict(response.frame)
            finally:
                await listener.stop()

        provider_calls, response_frame = asyncio.run(scenario())
        assert provider_calls == 1
        assert response_frame["id"] == 7, "listener loop must survive a raising catalog provider"


class TestPipeCatalogClient:
    """Verify the pipe-side :func:`_fetch_catalog_on` against a real listener."""

    def test_fetch_catalog_returns_provider_list(self, socket_path: str) -> None:
        async def scenario() -> list[dict[str, Any]]:
            tools = [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}]
            listener = PipeListener(catalog_provider=_RecordingCatalogProvider(tools))
            await listener.start(socket_path)
            try:
                # open the daemon connection, complete the handshake, then
                # fetch the catalog over the same connection -- this mirrors
                # what _run_pipe_main does in production
                reader, writer = await asyncio.open_unix_connection(socket_path)
                try:
                    session_id = await _handshake_on(reader, writer)
                    return await _fetch_catalog_on(reader, writer, session_id)
                finally:
                    await _drain_close(writer)
            finally:
                await listener.stop()

        fetched = asyncio.run(scenario())
        assert fetched == [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}]

    def test_fetch_catalog_returns_empty_when_provider_is_default(self, socket_path: str) -> None:
        async def scenario() -> list[dict[str, Any]]:
            listener = PipeListener()
            await listener.start(socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                try:
                    session_id = await _handshake_on(reader, writer)
                    return await _fetch_catalog_on(reader, writer, session_id)
                finally:
                    await _drain_close(writer)
            finally:
                await listener.stop()

        assert asyncio.run(scenario()) == []

    def test_fetch_catalog_raises_on_session_id_mismatch(self) -> None:
        # drive _fetch_catalog_on against an in-memory pair so we can inject
        # a response whose meta.session_id differs from what the helper sent.
        # we cannot exercise this against a real listener because the
        # listener's pre-existing meta.session_id validation drops impostor
        # envelopes BEFORE the catalog intercept fires, so no response would
        # ever come back.
        async def scenario() -> None:
            client_reader, client_writer = await _make_memory_stream_pair()
            server_reader, server_writer = await _make_memory_stream_pair()

            sent_session_id = "1111111111111111111111111111111"
            stamped_session_id = "2222222222222222222222222222222"
            fetch_task = asyncio.create_task(
                _fetch_catalog_on(server_reader, client_writer, sent_session_id)
            )
            # consume the request the fetch sent
            try:
                _ = await client_reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            # respond with the WRONG session_id stamped on meta
            response = PipeCatalog.response(stamped_session_id, [])
            server_writer.write(response.to_bytes())
            await server_writer.drain()
            try:
                await fetch_task
            finally:
                client_writer.close()
                server_writer.close()

        with pytest.raises(PipeProtocolError, match="mismatch"):
            asyncio.run(scenario())

    def test_fetch_catalog_raises_on_malformed_response(self) -> None:
        # drive _fetch_catalog_on against an in-memory pair so we can inject
        # a malformed response without spinning up a real listener; the
        # helper must surface the protocol error rather than coercing it
        async def scenario() -> None:
            client_reader, client_writer = await _make_memory_stream_pair()
            server_reader, server_writer = await _make_memory_stream_pair()

            session_id = "deadbeefdeadbeefdeadbeefdeadbeef"
            # spawn the fetch as a task so we can write the malformed
            # response after it has sent its request
            fetch_task = asyncio.create_task(
                _fetch_catalog_on(server_reader, client_writer, session_id)
            )
            # consume the catalog request the fetch sent
            try:
                _ = await client_reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            # respond with a malformed envelope: result is missing tools
            malformed = PipeEnvelope(
                meta={"session_id": session_id},
                frame={"jsonrpc": "2.0", "id": PipeCatalog.REQUEST_ID, "result": {}},
            )
            server_writer.write(malformed.to_bytes())
            await server_writer.drain()
            try:
                await fetch_task
            finally:
                client_writer.close()
                server_writer.close()

        with pytest.raises(PipeProtocolError):
            asyncio.run(scenario())


class TestPipeForwarderToolsListIntercept:
    """Verify :func:`_run_forwarder` answers ``tools/list`` locally from its catalog argument."""

    def test_forwarder_answers_tools_list_locally_without_daemon_round_trip(self) -> None:
        async def scenario() -> tuple[dict[str, Any], int]:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            session_id = "deadbeefdeadbeefdeadbeefdeadbeef"
            catalog = [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}]

            forwarder = asyncio.create_task(
                _run_forwarder(
                    session_id, stdin_reader, stdout_writer, daemon_in_reader, daemon_writer, catalog
                )
            )
            try:
                # write a tools/list request with an id; the forwarder must
                # answer it on stdout WITHOUT writing any envelope to the
                # daemon socket
                request_frame = {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
                stdin_writer.write((json.dumps(request_frame) + "\n").encode("utf-8"))
                await stdin_writer.drain()

                response_line = await stdout_reader.readuntil(b"\n")
                response = json.loads(response_line.decode("utf-8").rstrip("\n"))

                # confirm nothing reached the daemon side; we set a short
                # deadline because if the forwarder mistakenly forwarded the
                # request we'd see an envelope here within milliseconds
                daemon_envelope_count = 0
                try:
                    await asyncio.wait_for(daemon_reader.readuntil(b"\n"), timeout=0.1)
                    daemon_envelope_count = 1
                except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                    daemon_envelope_count = 0

                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return response, daemon_envelope_count
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        response, daemon_envelope_count = asyncio.run(scenario())
        assert response == {
            "jsonrpc": "2.0",
            "id": 5,
            "result": {"tools": [{"name": "alpha", "description": "a"}, {"name": "beta", "description": "b"}]},
        }
        assert daemon_envelope_count == 0, "tools/list must NOT be round-tripped to the daemon"

    def test_forwarder_intercepts_tools_list_even_when_catalog_is_empty(self) -> None:
        async def scenario() -> dict[str, Any]:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            forwarder = asyncio.create_task(
                _run_forwarder("sid", stdin_reader, stdout_writer, daemon_in_reader, daemon_writer, [])
            )
            try:
                stdin_writer.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode("utf-8"))
                await stdin_writer.drain()
                response_line = await stdout_reader.readuntil(b"\n")
                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return json.loads(response_line.decode("utf-8").rstrip("\n"))
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        response = asyncio.run(scenario())
        assert response == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    def test_forwarder_passes_through_non_tools_list_methods_unchanged(self) -> None:
        async def scenario() -> dict[str, Any]:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            session_id = "abcdef0123456789abcdef0123456789"
            catalog = [{"name": "alpha"}]
            forwarder = asyncio.create_task(
                _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_in_reader, daemon_writer, catalog)
            )
            try:
                # tools/call must still be forwarded; only tools/list is
                # answered locally
                stdin_writer.write(
                    (json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}) + "\n").encode("utf-8")
                )
                await stdin_writer.drain()
                envelope_line = await daemon_reader.readuntil(b"\n")
                envelope = PipeEnvelope.from_bytes(envelope_line)
                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return dict(envelope.frame)
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        forwarded = asyncio.run(scenario())
        assert forwarded == {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}

    def test_forwarder_forwards_tools_list_notifications_to_daemon(self) -> None:
        # tools/list notifications (no ``id`` field) must NOT be answered
        # locally; per JSON-RPC notifications expect no response and the
        # daemon may still need to see them for bookkeeping
        async def scenario() -> dict[str, Any]:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            session_id = "fedcba9876543210fedcba9876543210"
            catalog = [{"name": "alpha"}]
            forwarder = asyncio.create_task(
                _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_in_reader, daemon_writer, catalog)
            )
            try:
                # no "id" key -> JSON-RPC notification
                stdin_writer.write((json.dumps({"jsonrpc": "2.0", "method": "tools/list"}) + "\n").encode("utf-8"))
                await stdin_writer.drain()
                envelope_line = await daemon_reader.readuntil(b"\n")
                envelope = PipeEnvelope.from_bytes(envelope_line)
                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return dict(envelope.frame)
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        forwarded = asyncio.run(scenario())
        assert forwarded == {"jsonrpc": "2.0", "method": "tools/list"}, "notifications must reach the daemon"

    def test_forwarder_legacy_signature_without_catalog_falls_back_to_empty_list(self) -> None:
        # tests written before T4 (T3-era) call _run_forwarder without the
        # catalog argument; the new signature defaults to None which the
        # forwarder treats as an empty catalog so existing test scaffolding
        # keeps compiling
        async def scenario() -> dict[str, Any]:
            stdin_reader, stdin_writer = await _make_memory_stream_pair()
            stdout_reader, stdout_writer = await _make_memory_stream_pair()
            daemon_reader, daemon_writer = await _make_memory_stream_pair()
            daemon_in_reader, daemon_in_writer = await _make_memory_stream_pair()

            forwarder = asyncio.create_task(
                _run_forwarder("sid", stdin_reader, stdout_writer, daemon_in_reader, daemon_writer)
            )
            try:
                stdin_writer.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode("utf-8"))
                await stdin_writer.drain()
                response_line = await stdout_reader.readuntil(b"\n")
                stdin_writer.close()
                daemon_in_writer.close()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await asyncio.wait_for(forwarder, timeout=2.0)
                return json.loads(response_line.decode("utf-8").rstrip("\n"))
            finally:
                if not forwarder.done():
                    forwarder.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await forwarder

        response = asyncio.run(scenario())
        assert response == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


class TestPipeCatalogEnd2End:
    """Full-stack T4: daemon listener with a real CatalogProvider + pipe forwarder answering tools/list locally."""

    def test_pipe_handshake_then_catalog_then_local_tools_list(self, socket_path: str) -> None:
        async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any], int]:
            # the daemon advertises a known catalog through the provider
            tools = [{"name": "foo", "description": "f"}, {"name": "bar", "description": "b"}]
            handler = _EchoFrameHandler()
            listener = PipeListener(frame_handler=handler, catalog_provider=_RecordingCatalogProvider(tools))
            await listener.start(socket_path)
            try:
                # pipe-side: open the daemon connection, run handshake +
                # catalog fetch, then drive _run_forwarder against it
                stdin_reader, stdin_writer = await _make_memory_stream_pair()
                stdout_reader, stdout_writer = await _make_memory_stream_pair()
                daemon_reader, daemon_writer = await asyncio.open_unix_connection(socket_path)
                session_id = await _handshake_on(daemon_reader, daemon_writer)
                fetched = await _fetch_catalog_on(daemon_reader, daemon_writer, session_id)

                forwarder = asyncio.create_task(
                    _run_forwarder(session_id, stdin_reader, stdout_writer, daemon_reader, daemon_writer, fetched)
                )
                try:
                    # upstream issues tools/list -- the forwarder must
                    # answer locally from the catalog WITHOUT touching the
                    # daemon (the FrameHandler must not see it)
                    stdin_writer.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode("utf-8"))
                    await stdin_writer.drain()
                    response_line = await stdout_reader.readuntil(b"\n")
                    response = json.loads(response_line.decode("utf-8").rstrip("\n"))

                    # also send a ping that DOES round-trip; this proves the
                    # forwarder still forwards non-tools/list traffic
                    stdin_writer.write((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n").encode("utf-8"))
                    await stdin_writer.drain()
                    ping_response_line = await stdout_reader.readuntil(b"\n")
                    ping_response = json.loads(ping_response_line.decode("utf-8").rstrip("\n"))

                    # let any in-flight echo land before we count handler calls
                    for _ in range(10):
                        if any(call[1].get("method") == "ping" for call in handler.calls):
                            break
                        await asyncio.sleep(0.01)
                    return fetched, response, len(handler.calls)
                finally:
                    stdin_writer.close()
                    daemon_writer.close()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        await daemon_writer.wait_closed()
                    if not forwarder.done():
                        forwarder.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await forwarder
            finally:
                await listener.stop()

        fetched, response, handler_calls = asyncio.run(scenario())
        assert fetched == [{"name": "foo", "description": "f"}, {"name": "bar", "description": "b"}]
        assert response == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "foo", "description": "f"}, {"name": "bar", "description": "b"}]},
        }
        # exactly one handler call -- the ping. tools/list MUST not have
        # reached the handler.
        assert handler_calls == 1, "FrameHandler must see only the ping, not tools/list"


async def _make_memory_stream_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Build an in-memory ``(reader, writer)`` pair for forwarder unit tests.

    Uses ``socket.socketpair`` to back the pair with real socket fds so the
    asyncio reader/writer machinery exercises the same code path as the
    production pipe; the upside vs. ``StreamReader.feed_data`` is faithful
    drain/EOF semantics.
    """
    import socket as _socket

    loop = asyncio.get_running_loop()
    sock_a, sock_b = _socket.socketpair()

    # the read side uses StreamReader + StreamReaderProtocol bound to sock_a
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    await loop.connect_accepted_socket(lambda: protocol, sock_a)

    # the write side wraps sock_b in a write-pipe transport so callers can
    # ``writer.write(...)`` and ``await writer.drain()`` symmetrically
    writer_transport, writer_protocol = await loop.connect_accepted_socket(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), sock_b
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)
    return reader, writer
