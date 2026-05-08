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
import contextvars
import json
import os
import threading
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from serena.agent import _MCP_CALL_IN_FLIGHT, _PIPE_SESSION_ID_VAR, _SESSION_KEY_VAR, SerenaAgent
from serena.cli import TopLevelCommands
from serena.config.serena_config import SerenaConfig
from serena.daemon_pipe import CatalogProvider, FrameHandler, PipeListener
from serena.pipe import _fetch_catalog_on, _handshake, _handshake_on, _parse_unix_url, _run_forwarder, run_pipe_client
from serena.pipe_protocol import PipeCatalog, PipeEnvelope, PipeHandshake, PipeProtocolError
from serena.project import Project


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


class TestPipeListenerDisconnectHandlers:
    """T6: PipeListener.add_disconnect_handler — eviction-on-pipe-close hook.

    The listener fires registered disconnect handlers after a pipe connection's
    forwarder loop exits (whether by pipe-side EOF, transport error, or
    listener.stop()), passing the connection's daemon-allocated ``session_id``.
    The hook is the substrate for SerenaAgent.evict_pipe_session — when a pipe
    forwarder process exits, the daemon must drop that session_id's per-session
    state (active project, cursor manager) so the next connection on a fresh
    pipe doesn't inherit stale entries.
    """

    def test_disconnect_handler_fires_on_pipe_close(self, socket_path: str) -> None:
        async def scenario() -> tuple[str, list[str]]:
            recorded: list[str] = []
            listener = PipeListener()
            listener.add_disconnect_handler(lambda sid: recorded.append(sid))
            await listener.start(socket_path)
            try:
                session_id, reader, writer = await _client_handshake(socket_path)
                await _drain_close(writer)
                # the daemon-side handler runs on its own asyncio Task, so we
                # poll briefly (mirrors TestPipeFrameForwarder.test_listener_loop_exits_on_pipe_close)
                for _ in range(50):
                    if recorded:
                        break
                    await asyncio.sleep(0.01)
                return session_id, list(recorded)
            finally:
                await listener.stop()

        session_id, recorded = asyncio.run(scenario())
        assert recorded == [session_id], f"disconnect handler must fire with {session_id}; got {recorded}"

    def test_multiple_disconnect_handlers_all_fire_in_order(self, socket_path: str) -> None:
        async def scenario() -> list[tuple[int, str]]:
            recorded: list[tuple[int, str]] = []
            listener = PipeListener()
            listener.add_disconnect_handler(lambda sid: recorded.append((1, sid)))
            listener.add_disconnect_handler(lambda sid: recorded.append((2, sid)))
            listener.add_disconnect_handler(lambda sid: recorded.append((3, sid)))
            await listener.start(socket_path)
            try:
                _, _, writer = await _client_handshake(socket_path)
                await _drain_close(writer)
                for _ in range(50):
                    if len(recorded) >= 3:
                        break
                    await asyncio.sleep(0.01)
                return list(recorded)
            finally:
                await listener.stop()

        recorded = asyncio.run(scenario())
        assert len(recorded) == 3, f"all 3 handlers must fire; got {recorded}"
        # registration order is preserved -- callers that register multiple
        # eviction hooks (e.g. agent + telemetry) rely on deterministic ordering
        assert [pos for pos, _ in recorded] == [1, 2, 3]
        # all handlers must see the same session_id
        sids = {sid for _, sid in recorded}
        assert len(sids) == 1, f"all handlers must see the same session_id; got {sids}"

    def test_async_disconnect_handler_supported(self, socket_path: str) -> None:
        async def scenario() -> tuple[str, list[str]]:
            recorded: list[str] = []

            async def async_handler(sid: str) -> None:
                # tiny await so the test exercises the awaitable branch, not
                # just an async-def that immediately returns
                await asyncio.sleep(0)
                recorded.append(sid)

            listener = PipeListener()
            listener.add_disconnect_handler(async_handler)
            await listener.start(socket_path)
            try:
                session_id, _, writer = await _client_handshake(socket_path)
                await _drain_close(writer)
                for _ in range(50):
                    if recorded:
                        break
                    await asyncio.sleep(0.01)
                return session_id, list(recorded)
            finally:
                await listener.stop()

        session_id, recorded = asyncio.run(scenario())
        assert recorded == [session_id]

    def test_disconnect_handler_exception_does_not_block_other_handlers(self, socket_path: str) -> None:
        async def scenario() -> list[str]:
            recorded: list[str] = []

            def bad_handler(sid: str) -> None:
                raise RuntimeError("intentional disconnect-handler failure")

            listener = PipeListener()
            listener.add_disconnect_handler(bad_handler)
            listener.add_disconnect_handler(lambda sid: recorded.append(sid))
            await listener.start(socket_path)
            try:
                _, _, writer = await _client_handshake(socket_path)
                await _drain_close(writer)
                for _ in range(50):
                    if recorded:
                        break
                    await asyncio.sleep(0.01)
                return list(recorded)
            finally:
                await listener.stop()

        recorded = asyncio.run(scenario())
        assert len(recorded) == 1, "second handler must run despite first one raising"

    def test_disconnect_handler_does_not_fire_on_handshake_failure(self, socket_path: str) -> None:
        # if the first envelope is malformed (no handshake), no session_id is
        # ever allocated; disconnect handlers MUST NOT fire because there is
        # nothing for them to evict
        async def scenario() -> list[str]:
            recorded: list[str] = []
            listener = PipeListener()
            listener.add_disconnect_handler(lambda sid: recorded.append(sid))
            await listener.start(socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                bogus = PipeEnvelope(meta={}, frame={"jsonrpc": "2.0", "method": "tools/call", "id": 1})
                writer.write(bogus.to_bytes())
                await writer.drain()
                # listener will close the connection; consume the EOF so the
                # daemon side completes its teardown before we assert
                _ = await reader.read()
                await _drain_close(writer)
                # give the daemon a beat to fire any (incorrect) handlers
                await asyncio.sleep(0.05)
                return list(recorded)
            finally:
                await listener.stop()

        recorded = asyncio.run(scenario())
        assert recorded == [], "no disconnect handler should fire when handshake never completed"

    def test_disconnect_handler_fires_on_listener_stop(self, socket_path: str) -> None:
        # listener.stop() force-closes connections; the per-connection
        # forwarder unwinds via its finally block, so disconnect handlers
        # must fire for every connection that was active at stop() time
        recorded: list[str] = []

        async def scenario() -> list[str]:
            listener = PipeListener()
            listener.add_disconnect_handler(lambda sid: recorded.append(sid))
            await listener.start(socket_path)
            session_ids: list[str] = []
            for _ in range(3):
                sid, _, _ = await _client_handshake(socket_path)
                session_ids.append(sid)
            # stop() while connections are still open -- handlers fire during
            # the stop() teardown
            await listener.stop()
            return session_ids

        session_ids = asyncio.run(scenario())
        # wait for any pending async work to settle before asserting
        assert sorted(recorded) == sorted(session_ids)

    def test_disconnect_handler_receives_correct_session_id_per_connection(self, socket_path: str) -> None:
        # under concurrent traffic each disconnect handler invocation must
        # carry the session_id of the connection that closed -- not, e.g.,
        # the most-recently-registered connection's session_id
        async def scenario() -> tuple[set[str], set[str]]:
            recorded: list[str] = []
            listener = PipeListener()
            listener.add_disconnect_handler(lambda sid: recorded.append(sid))
            await listener.start(socket_path)
            try:

                async def one_client() -> str:
                    sid, _, writer = await _client_handshake(socket_path)
                    await _drain_close(writer)
                    return sid

                expected = set(await asyncio.gather(*[one_client() for _ in range(10)]))
                # poll for all evictions to land
                for _ in range(100):
                    if len(recorded) >= 10:
                        break
                    await asyncio.sleep(0.01)
                return expected, set(recorded)
            finally:
                await listener.stop()

        expected, observed = asyncio.run(scenario())
        assert observed == expected, f"each handler invocation must match its connection's session_id; expected={expected} observed={observed}"


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


@pytest.fixture
def isolation_agent() -> SerenaAgent:
    """Build a minimal :class:`SerenaAgent` with no active project, used for isolation tests that
    simulate parallel pipe siblings sharing a single daemon agent."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    return SerenaAgent(serena_config=config)


def _pipe_project_stub(name: str) -> Project:
    """Return a :class:`Project`-typed mock that the daemon's per-session routing layer can store
    and retrieve by ``project_root`` / ``project_name`` identity."""
    project = MagicMock(spec=Project)
    project.project_name = name
    project.project_root = f"/tmp/{name}"
    return project


class TestParallelSiblingWorkerIsolation:
    """T8: parallel-sibling-worker isolation across N pipe sessions sharing one daemon.

    Each pipe instance is identified by a stable handshake-asserted UUID held in
    ``_PIPE_SESSION_ID_VAR``. With N pipes running cursor-style tools concurrently,
    every pipe MUST see only its own active project across many iterations -- never
    a sibling's, never the legacy slot's. The legacy single-slot
    ``_legacy_active_project`` is seeded with a recognisable sentinel BEFORE the
    threads start; if it ever leaks into any pipe sibling's view the test fails,
    which is the IRONCLAD zero-crossover constraint inherited from
    ``plan://Serena:serena/serena-mcp-pipe-redesign``.

    Mirrors :meth:`TestPerSessionActiveProject.test_two_concurrent_clients_never_cross`
    in :mod:`test.serena.test_per_session_active_project` but uses ``str`` UUID keys
    (the pipe-asserted form, also stamped into ``_PIPE_SESSION_ID_VAR`` for parity
    with :meth:`Tool.apply_ex`'s pipe path) in place of ``int`` ``id()``-derived keys
    (the direct-stdio form). Both flavours land in the same
    ``_active_projects_by_session`` dict (typed ``dict[str | int, Project]``), so the
    isolation property must hold indistinguishably across keying flavours.
    """

    def test_two_pipe_siblings_each_see_only_own_project_across_100_iterations(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """Two pipe siblings (str UUID keys) reading interleaved must each see exactly
        their own project; the legacy slot's sentinel must never leak."""
        # set up two sibling pipe sessions, each with its own project
        project_a = _pipe_project_stub("pipe-client-a-project")
        project_b = _pipe_project_stub("pipe-client-b-project")
        # seed the legacy slot with a recognisable sentinel; if it ever leaks the test fails
        sentinel = _pipe_project_stub("legacy-sentinel-must-never-leak")
        isolation_agent._legacy_active_project = sentinel

        observations_a: list[Project | None] = []
        observations_b: list[Project | None] = []

        session_id_a = uuid.uuid4().hex
        session_id_b = uuid.uuid4().hex

        def pipe_client(
            pipe_session_id: str, project: Project, observations: list[Project | None]
        ) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                # mirror Tool.apply_ex's pipe-session ContextVar binding (both vars set on pipe path)
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                isolation_agent._active_project = project
                for _ in range(100):
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        # interleave two pipe sessions concurrently
        thread_a = threading.Thread(
            target=pipe_client, args=(session_id_a, project_a, observations_a)
        )
        thread_b = threading.Thread(
            target=pipe_client, args=(session_id_b, project_b, observations_b)
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        # each sibling sees only its own project across every iteration
        assert all(p is project_a for p in observations_a), (
            f"Pipe sibling A observed something other than its own project: distinct ids = "
            f"{set(id(p) for p in observations_a)}"
        )
        assert all(p is project_b for p in observations_b), (
            f"Pipe sibling B observed something other than its own project: distinct ids = "
            f"{set(id(p) for p in observations_b)}"
        )
        # the legacy slot's sentinel never leaked into either sibling's view
        assert sentinel not in observations_a and sentinel not in observations_b, (
            "Legacy slot sentinel leaked into a pipe sibling's view (IRONCLAD violation)"
        )

    def test_eight_pipe_siblings_each_see_only_own_project_across_100_iterations(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """Stress: eight concurrent pipe siblings each see only their own project; sentinel never leaks."""
        sentinel = _pipe_project_stub("legacy-sentinel-must-never-leak")
        isolation_agent._legacy_active_project = sentinel

        # build N=8 sibling pipe sessions with distinct UUIDs and distinct projects
        n = 8
        siblings: list[tuple[str, Project, list[Project | None]]] = [
            (uuid.uuid4().hex, _pipe_project_stub(f"pipe-client-{i}-project"), []) for i in range(n)
        ]

        def pipe_client(
            pipe_session_id: str, project: Project, observations: list[Project | None]
        ) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                isolation_agent._active_project = project
                for _ in range(100):
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        threads = [threading.Thread(target=pipe_client, args=args) for args in siblings]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # each sibling saw only its own project; nobody saw the sentinel; nobody saw a peer's project
        all_projects = {project for _, project, _ in siblings}
        for session_id, project, observations in siblings:
            assert all(p is project for p in observations), (
                f"Pipe sibling {session_id[:8]} observed something other than its own project: "
                f"distinct ids = {set(id(p) for p in observations)}"
            )
            assert sentinel not in observations, (
                f"Legacy slot sentinel leaked into pipe sibling {session_id[:8]}'s view"
            )
            for other in all_projects - {project}:
                assert other not in observations, (
                    f"A peer's project leaked into pipe sibling {session_id[:8]}'s view"
                )

    def test_legacy_sentinel_never_leaks_through_concurrent_pipe_reads(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """Sustain read pressure on the IRONCLAD guard: 4 siblings x 250 reads each, with the legacy
        slot rewritten to a fresh sentinel mid-run. Neither sentinel value ever leaks."""
        sentinel_v1 = _pipe_project_stub("legacy-sentinel-v1")
        sentinel_v2 = _pipe_project_stub("legacy-sentinel-v2")
        isolation_agent._legacy_active_project = sentinel_v1

        n = 4
        siblings: list[tuple[str, Project, list[Project | None]]] = [
            (uuid.uuid4().hex, _pipe_project_stub(f"pipe-client-{i}-project"), []) for i in range(n)
        ]

        # gate so all sibling threads start their read loops at roughly the same time
        ready = threading.Barrier(n + 1)

        def pipe_client(
            pipe_session_id: str, project: Project, observations: list[Project | None]
        ) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                isolation_agent._active_project = project
                ready.wait()
                for _ in range(250):
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        threads = [threading.Thread(target=pipe_client, args=args) for args in siblings]
        for t in threads:
            t.start()
        ready.wait()
        # rewrite the legacy slot mid-flight; the IRONCLAD guard MUST keep this out of pipe siblings
        isolation_agent._legacy_active_project = sentinel_v2
        for t in threads:
            t.join(timeout=15)

        for session_id, project, observations in siblings:
            assert all(p is project for p in observations), (
                f"Pipe sibling {session_id[:8]} drifted from its own project under read pressure"
            )
            assert sentinel_v1 not in observations and sentinel_v2 not in observations, (
                f"A legacy slot sentinel (v1 or v2) leaked into pipe sibling {session_id[:8]}'s view"
            )

    def test_pipe_sibling_writes_isolate_in_per_session_dict(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """The per-session dict accumulates one entry per pipe session, each keyed by its UUID.
        Direct dict-state assertion, complementing the read-side assertions above."""
        n = 5
        siblings: list[tuple[str, Project]] = [
            (uuid.uuid4().hex, _pipe_project_stub(f"pipe-client-{i}-project")) for i in range(n)
        ]

        def pipe_writer(pipe_session_id: str, project: Project) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                isolation_agent._active_project = project

            ctx.run(run)

        threads = [threading.Thread(target=pipe_writer, args=args) for args in siblings]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # every sibling's UUID is now a key in the per-session dict mapping to its own project,
        # and no UUID's slot was clobbered by another sibling's write
        for pipe_session_id, project in siblings:
            assert pipe_session_id in isolation_agent._active_projects_by_session, (
                f"Pipe sibling {pipe_session_id[:8]} did not land its project in the per-session dict"
            )
            assert isolation_agent._active_projects_by_session[pipe_session_id] is project, (
                f"Pipe sibling {pipe_session_id[:8]}'s slot was clobbered by another sibling's write"
            )

    def test_concurrent_pipe_writes_do_not_clobber_each_other(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """Sustained write contention modelling concurrent cursor_replace_range calls: each sibling
        rewrites its slot 50 times, reading back after each write. Final per-sibling sequence equals
        each sibling's own write sequence -- no cross-pollution from peers."""
        n = 6
        siblings: list[tuple[str, list[Project], list[Project | None]]] = []
        for i in range(n):
            session_id = uuid.uuid4().hex
            project_seq = [_pipe_project_stub(f"pipe-{i}-rev-{r}") for r in range(50)]
            observations: list[Project | None] = []
            siblings.append((session_id, project_seq, observations))

        def pipe_churn(
            pipe_session_id: str,
            project_seq: list[Project],
            observations: list[Project | None],
        ) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                for project in project_seq:
                    isolation_agent._active_project = project
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        threads = [threading.Thread(target=pipe_churn, args=args) for args in siblings]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # every sibling's observed sequence equals its written sequence in order (no cross-pollution
        # from peers' writes -- each sibling's own _SESSION_KEY_VAR steers writes to its own slot)
        for session_id, project_seq, observations in siblings:
            assert observations == project_seq, (
                f"Pipe sibling {session_id[:8]} observed values that diverged from its own write "
                f"sequence (possible cross-pollination from a sibling's write)"
            )


class TestConsistentResultsInParallelBatch:
    """T9: consistent-results invariant across N parallel tool calls within ONE pipe client.

    Distinct from :class:`TestParallelSiblingWorkerIsolation`: T8 asserts isolation across N
    siblings (N pipe sessions, each with its own ``_PIPE_SESSION_ID_VAR``). T9 asserts
    consistency *within* ONE client's parallel fan-out (one ``_PIPE_SESSION_ID_VAR`` shared
    across N parallel tool calls).

    The pipe path's invariant is documented in :meth:`Tool.apply_ex`: when N parallel tool
    calls fan out from one pipe client, each call inherits the same
    ``(_PIPE_SESSION_ID_VAR, _SESSION_KEY_VAR)`` from the dispatch context, so the
    per-session dict resolves to one project across all N calls. There must be no churn
    between calls in one client's batch -- every call in the batch reflects the same active
    project, even when sibling sessions are concurrently writing to *their own* slots and
    even when the legacy slot is being rewritten in flight.

    Each thread copies its own ContextVar context (mirroring how :meth:`Tool.apply_ex`
    builds a fresh per-call context) but seeds the *same* ``pipe_session_id`` -- so the
    shared ``_active_projects_by_session`` dict is the convergence point across the
    parallel calls.
    """

    def test_two_parallel_calls_in_one_pipe_session_see_same_project_across_100_iterations(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """Two parallel reads under one pipe ``session_id`` must both see exactly the
        project the pipe client activated; the legacy slot's sentinel must never leak."""
        # ONE pipe client, ONE handshake-asserted session_id shared across the parallel batch
        pipe_session_id = uuid.uuid4().hex
        client_project = _pipe_project_stub("one-pipe-client-project")
        sentinel = _pipe_project_stub("legacy-sentinel-must-never-leak")
        isolation_agent._legacy_active_project = sentinel

        # write-once: activate the project under the pipe session key (mirrors a single
        # ``activate_project`` call landing the project in ``_active_projects_by_session``
        # before the parallel-batch fan-out begins)
        write_ctx = contextvars.copy_context()

        def write_once() -> None:
            _MCP_CALL_IN_FLIGHT.set(True)
            _PIPE_SESSION_ID_VAR.set(pipe_session_id)
            _SESSION_KEY_VAR.set(pipe_session_id)
            isolation_agent._active_project = client_project

        write_ctx.run(write_once)

        observations_a: list[Project | None] = []
        observations_b: list[Project | None] = []

        def parallel_read(observations: list[Project | None]) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                # each parallel tool call binds the SAME pipe_session_id (T9's defining
                # invariant) but uses its own freshly-copied ContextVar context (mirroring
                # ``Tool.apply_ex``'s per-call context)
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                for _ in range(100):
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        thread_a = threading.Thread(target=parallel_read, args=(observations_a,))
        thread_b = threading.Thread(target=parallel_read, args=(observations_b,))
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        # both parallel readers within the same pipe session see the same project across every iteration
        assert all(p is client_project for p in observations_a), (
            f"Parallel reader A within one pipe session drifted: distinct ids = "
            f"{set(id(p) for p in observations_a)}"
        )
        assert all(p is client_project for p in observations_b), (
            f"Parallel reader B within one pipe session drifted: distinct ids = "
            f"{set(id(p) for p in observations_b)}"
        )
        assert sentinel not in observations_a and sentinel not in observations_b, (
            "Legacy slot sentinel leaked into a parallel batch within one pipe session "
            "(IRONCLAD violation)"
        )

    def test_eight_parallel_calls_in_one_pipe_session_see_same_project_across_100_iterations(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """Stress: eight parallel reads within one pipe session each see exactly the
        client's project; sentinel never leaks."""
        sentinel = _pipe_project_stub("legacy-sentinel-must-never-leak")
        isolation_agent._legacy_active_project = sentinel

        pipe_session_id = uuid.uuid4().hex
        client_project = _pipe_project_stub("one-pipe-client-project")

        # write-once under the pipe session key
        write_ctx = contextvars.copy_context()

        def write_once() -> None:
            _MCP_CALL_IN_FLIGHT.set(True)
            _PIPE_SESSION_ID_VAR.set(pipe_session_id)
            _SESSION_KEY_VAR.set(pipe_session_id)
            isolation_agent._active_project = client_project

        write_ctx.run(write_once)

        n = 8
        observations_per_call: list[list[Project | None]] = [[] for _ in range(n)]

        def parallel_read(observations: list[Project | None]) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                for _ in range(100):
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        threads = [
            threading.Thread(target=parallel_read, args=(observations_per_call[i],))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # every call in the batch saw the client's project; nobody saw the sentinel
        for i, observations in enumerate(observations_per_call):
            assert all(p is client_project for p in observations), (
                f"Parallel call {i} within one pipe session drifted: "
                f"distinct ids = {set(id(p) for p in observations)}"
            )
            assert sentinel not in observations, (
                f"Legacy slot sentinel leaked into parallel call {i} within one pipe session"
            )

    def test_one_pipe_session_view_unaffected_by_concurrent_sibling_session_writes(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """The most-T9-specific invariant: while one pipe client is fanning out a parallel
        read-batch, SIBLING pipe sessions are concurrently activating their own (different)
        projects on their own slots. The first client's batch must reflect the same
        project across every call, with no rotation onto a peer's project mid-flight."""
        # the pipe client of interest: one session_id with a fan-out of 4 parallel reads
        client_pipe_session_id = uuid.uuid4().hex
        client_project = _pipe_project_stub("client-of-interest-project")

        write_ctx = contextvars.copy_context()

        def write_client_project_once() -> None:
            _MCP_CALL_IN_FLIGHT.set(True)
            _PIPE_SESSION_ID_VAR.set(client_pipe_session_id)
            _SESSION_KEY_VAR.set(client_pipe_session_id)
            isolation_agent._active_project = client_project

        write_ctx.run(write_client_project_once)

        # 4 sibling pipe sessions, each with its own session_id and its own project sequence,
        # each firing 50 activate-style writes in parallel
        n_siblings = 4
        siblings: list[tuple[str, list[Project]]] = [
            (uuid.uuid4().hex, [_pipe_project_stub(f"sibling-{i}-rev-{r}") for r in range(50)])
            for i in range(n_siblings)
        ]

        # gate: client readers AND sibling writers all start their loops together so the
        # client's reads happen WHILE sibling writes are landing
        n_client_readers = 4
        ready = threading.Barrier(n_siblings + n_client_readers + 1)

        observations_per_call: list[list[Project | None]] = [
            [] for _ in range(n_client_readers)
        ]

        def client_parallel_read(observations: list[Project | None]) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(client_pipe_session_id)
                _SESSION_KEY_VAR.set(client_pipe_session_id)
                ready.wait()
                for _ in range(250):
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        def sibling_writer(pipe_session_id: str, project_seq: list[Project]) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                ready.wait()
                for project in project_seq:
                    isolation_agent._active_project = project

            ctx.run(run)

        client_threads = [
            threading.Thread(target=client_parallel_read, args=(observations_per_call[i],))
            for i in range(n_client_readers)
        ]
        sibling_threads = [
            threading.Thread(target=sibling_writer, args=args) for args in siblings
        ]
        for t in client_threads + sibling_threads:
            t.start()
        ready.wait()
        for t in client_threads + sibling_threads:
            t.join(timeout=15)

        # every observation across the 4 client parallel reads stayed pinned to the client's
        # project; no sibling project rotated in mid-flight
        all_sibling_projects: set[Project] = set()
        for _, project_seq in siblings:
            all_sibling_projects.update(project_seq)
        for i, observations in enumerate(observations_per_call):
            assert all(p is client_project for p in observations), (
                f"Client parallel reader {i} drifted onto a non-client project mid-batch: "
                f"distinct ids = {set(id(p) for p in observations)}"
            )
            for sibling in all_sibling_projects:
                assert sibling not in observations, (
                    f"A sibling session's project leaked into the client's parallel batch "
                    f"(reader {i})"
                )

    def test_parallel_writes_within_one_pipe_session_share_one_dict_slot(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """N parallel writes within one pipe session all key into the same per-session
        dict slot: the dict ends up with exactly ONE entry under the shared session_id,
        holding one of the N candidate projects (whichever happens to write last by Python
        dict overwrite semantics)."""
        # shared session_id across N parallel writers (the parallel-batch counterpart of
        # ``test_pipe_sibling_writes_isolate_in_per_session_dict``)
        pipe_session_id = uuid.uuid4().hex
        n = 6
        candidate_projects = [_pipe_project_stub(f"candidate-{i}-project") for i in range(n)]

        def parallel_writer(project: Project) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                isolation_agent._active_project = project

            ctx.run(run)

        threads = [
            threading.Thread(target=parallel_writer, args=(p,)) for p in candidate_projects
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # exactly ONE dict entry under the shared pipe session_id; no fanned-out write
        # leaked into a different session's slot
        assert pipe_session_id in isolation_agent._active_projects_by_session, (
            "Parallel writes within one pipe session did not land in the per-session dict"
        )
        assert isolation_agent._active_projects_by_session[pipe_session_id] in candidate_projects, (
            "Parallel writes within one pipe session landed something other than one of "
            "the N candidate projects (suggests a peer-key leak)"
        )
        # no SIDE-EFFECT entries materialised under a peer key (we never bound a sibling
        # session_id, so the dict must hold only this one entry)
        assert len(isolation_agent._active_projects_by_session) == 1, (
            f"Parallel writes within one pipe session created multiple dict entries; "
            f"expected 1, got {len(isolation_agent._active_projects_by_session)}"
        )

    def test_no_legacy_leak_during_one_pipe_session_parallel_reads_with_sentinel_rewrites(
        self, isolation_agent: SerenaAgent
    ) -> None:
        """Sustain pressure on the IRONCLAD guard *within* one pipe session: 4 parallel
        readers under one session_id reading 250x each, with the legacy slot rewritten
        between sentinel values mid-flight. Neither sentinel ever surfaces in the batch."""
        sentinel_v1 = _pipe_project_stub("legacy-sentinel-v1")
        sentinel_v2 = _pipe_project_stub("legacy-sentinel-v2")
        isolation_agent._legacy_active_project = sentinel_v1

        pipe_session_id = uuid.uuid4().hex
        client_project = _pipe_project_stub("one-pipe-client-project")

        write_ctx = contextvars.copy_context()

        def write_client_project_once() -> None:
            _MCP_CALL_IN_FLIGHT.set(True)
            _PIPE_SESSION_ID_VAR.set(pipe_session_id)
            _SESSION_KEY_VAR.set(pipe_session_id)
            isolation_agent._active_project = client_project

        write_ctx.run(write_client_project_once)

        n_readers = 4
        observations_per_call: list[list[Project | None]] = [[] for _ in range(n_readers)]
        ready = threading.Barrier(n_readers + 1)

        def parallel_read(observations: list[Project | None]) -> None:
            ctx = contextvars.copy_context()

            def run() -> None:
                _MCP_CALL_IN_FLIGHT.set(True)
                _PIPE_SESSION_ID_VAR.set(pipe_session_id)
                _SESSION_KEY_VAR.set(pipe_session_id)
                ready.wait()
                for _ in range(250):
                    observations.append(isolation_agent.get_active_project())

            ctx.run(run)

        threads = [
            threading.Thread(target=parallel_read, args=(observations_per_call[i],))
            for i in range(n_readers)
        ]
        for t in threads:
            t.start()
        ready.wait()
        # rotate the legacy slot mid-flight; the IRONCLAD guard MUST keep both sentinels
        # out of the parallel-batch's view
        isolation_agent._legacy_active_project = sentinel_v2
        for t in threads:
            t.join(timeout=15)

        for i, observations in enumerate(observations_per_call):
            assert all(p is client_project for p in observations), (
                f"Parallel reader {i} within one pipe session drifted under read pressure"
            )
            assert sentinel_v1 not in observations and sentinel_v2 not in observations, (
                f"A legacy slot sentinel (v1 or v2) leaked into parallel reader {i}'s view "
                f"within one pipe session"
            )
