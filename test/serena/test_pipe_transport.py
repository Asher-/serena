"""Pipe transport regression tests.

This module is the canonical home for tests against the per-client stdio pipe
process described in :mod:`serena.pipe` and the daemon-side handshake-asserted
session pinning. T1's CLI scaffolding tests live here together with the deeper
transport regressions added in T7-T10 (cursor lifetime, parallel-sibling
isolation, batch consistency, restart resilience).
"""

from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from serena.cli import TopLevelCommands


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
        transport_param = next(
            param for param in TopLevelCommands.start_mcp_server.params if param.name == "transport"
        )
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
