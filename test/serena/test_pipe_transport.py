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
import os
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

from serena.cli import TopLevelCommands
from serena.daemon_pipe import PipeListener
from serena.pipe import PipeNotImplementedError, _handshake, _parse_unix_url, run_pipe_client
from serena.pipe_protocol import PipeEnvelope, PipeHandshake, PipeProtocolError


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

    def test_run_pipe_client_raises_t3_pending_after_successful_handshake(self) -> None:
        with patch("serena.pipe._handshake", new=AsyncMock(return_value="fake-session-id")):
            with pytest.raises(PipeNotImplementedError, match="T3"):
                run_pipe_client(daemon_url="unix:///tmp/whatever.sock")


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
