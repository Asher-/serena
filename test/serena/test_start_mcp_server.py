"""unit tests for the routes of ``serena start-mcp-server`` (:meth:`TopLevelCommands.start_mcp_server`).

Each test invokes the command with the daemon it builds replaced by doubles, so no server, language server, watchdog or
pipe forwarder starts, and with ``HOME`` under the test's own directory, so the operator log the command opens lies there
too. The tests take the arms of the command that no other test takes: the setting aside of an oversized operator log,
the registration of the SIGUSR1 fault handler, the resolution of ``--project-from-cwd``, the refusal of a pipe project that
is not a directory, the registration of the session-end route on the daemon's server (pg://serena/session/eviction), the
warning on a positional project argument, and the serving of the server beside a pipe listener.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner, Result
from mcp.server.fastmcp import FastMCP

from serena.cli import TopLevelCommands
from serena.config.serena_config import SerenaPaths
from serena.mcp import SessionEndRoute

_OPERATOR_LOG_CEILING = 100 * 1024 * 1024
"""the size in bytes above which start-mcp-server sets a pre-existing operator log aside as legacy"""

_OPERATOR_LOG_DIR = Path("Library") / "Logs" / "serena"
"""the directory of the operator log, relative to the home directory"""

_SESSION_END_PATH = "/sessions/end"
"""the path of the session-end route on the daemon's HTTP listener"""


@dataclass
class _Daemon:
    """the doubles standing in for the daemon ``start-mcp-server`` builds and for the pipe forwarder it hands off to"""

    home: Path
    """the home directory the command runs under"""

    factory_cls: MagicMock
    """the double of :class:`serena.mcp.SerenaMCPFactory`, whose instance builds :attr:`server`"""

    server: MagicMock
    """the double of the FastMCP server the factory builds, whose serving methods return at once"""

    run_pipe_client: MagicMock
    """the double of :func:`serena.pipe.run_pipe_client`, the pipe forwarder's entry point"""

    build_pipe_listener: MagicMock
    """the double of :func:`serena.mcp.build_pipe_listener`, which builds :attr:`pipe_listener`"""

    pipe_listener: MagicMock
    """the double of the daemon-side pipe listener, whose start and stop return at once"""

    @property
    def operator_log_dir(self) -> Path:
        """
        :return: the directory the command keeps its operator log in
        """
        return self.home / _OPERATOR_LOG_DIR

    @property
    def activated_project(self) -> str | None:
        """
        :return: the project the command handed the factory to activate at startup
        """
        self.factory_cls.assert_called_once()
        return self.factory_cls.call_args.kwargs["project"]

    def invoke(self, *args: str) -> Result:
        """
        Invokes ``start-mcp-server`` with ``args``.

        :param args: the command-line arguments
        :return: the result of the invocation
        """
        return CliRunner().invoke(TopLevelCommands.start_mcp_server, list(args))


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Daemon]:
    """
    :return: the doubles of the daemon, in place for the duration of the test, with ``HOME`` under ``tmp_path``
    """
    # the home the operator log is opened under, and the log-path record the command sets, restored after the test
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(SerenaPaths(), "last_returned_log_file_path", SerenaPaths().last_returned_log_file_path)
    root_handlers = list(logging.root.handlers)
    root_level = logging.root.level

    # the doubles of the daemon's construction and of the pipe forwarder
    server = MagicMock(spec=FastMCP)
    pipe_listener = MagicMock()
    pipe_listener.start = AsyncMock()
    pipe_listener.stop = AsyncMock()
    with (
        patch("serena.cli.SerenaMCPFactory") as factory_cls,
        patch("serena.util.watchdog.start_wedge_watchdog"),
        patch("serena.pipe.run_pipe_client") as run_pipe_client,
        patch("serena.mcp.build_pipe_listener", return_value=pipe_listener) as build_pipe_listener,
    ):
        factory_cls.return_value.create_mcp_server.return_value = server
        yield _Daemon(
            home=home,
            factory_cls=factory_cls,
            server=server,
            run_pipe_client=run_pipe_client,
            build_pipe_listener=build_pipe_listener,
            pipe_listener=pipe_listener,
        )

    # the log handlers the command installs on the root logger, removed so they do not outlive the test
    for handler in [handler for handler in logging.root.handlers if handler not in root_handlers]:
        logging.root.removeHandler(handler)
        handler.close()
    logging.root.setLevel(root_level)


class TestOperatorLog:
    """verifies that start-mcp-server sets aside a pre-existing operator log only when it exceeds the rotation ceiling."""

    def test_an_operator_log_over_the_ceiling_is_set_aside_as_legacy(self, daemon: _Daemon) -> None:
        daemon.operator_log_dir.mkdir(parents=True)
        oversized = daemon.operator_log_dir / "serena.log"
        with open(oversized, "wb") as log_file:
            log_file.truncate(_OPERATOR_LOG_CEILING + 1)

        result = daemon.invoke("--transport", "streamable-http")

        assert result.exit_code == 0, repr(result.exception)
        assert (daemon.operator_log_dir / "serena.log.legacy").stat().st_size == _OPERATOR_LOG_CEILING + 1
        assert oversized.stat().st_size < _OPERATOR_LOG_CEILING

    def test_an_operator_log_within_the_ceiling_is_kept(self, daemon: _Daemon) -> None:
        daemon.operator_log_dir.mkdir(parents=True)
        kept = daemon.operator_log_dir / "serena.log"
        kept.write_text("an earlier run\n", encoding="utf-8")

        result = daemon.invoke("--transport", "streamable-http")

        assert result.exit_code == 0, repr(result.exception)
        assert not (daemon.operator_log_dir / "serena.log.legacy").exists()
        assert kept.read_text(encoding="utf-8").startswith("an earlier run\n")


class TestFaultHandler:
    """verifies that start-mcp-server registers the SIGUSR1 traceback dump when stderr allows it, and starts without it otherwise."""

    def test_the_fault_handler_is_registered_for_sigusr1(self, daemon: _Daemon) -> None:
        with patch("serena.cli.faulthandler.register") as register:
            result = daemon.invoke("--transport", "streamable-http")

        assert result.exit_code == 0, repr(result.exception)
        register.assert_called_once()
        assert register.call_args.args == (signal.SIGUSR1,)
        assert register.call_args.kwargs["all_threads"] is True
        assert register.call_args.kwargs["chain"] is False

    def test_a_stderr_the_fault_handler_cannot_use_is_skipped(self, daemon: _Daemon) -> None:
        with patch("serena.cli.faulthandler.register", side_effect=OSError("stderr has no file descriptor")) as register:
            result = daemon.invoke("--transport", "streamable-http")

        assert result.exit_code == 0, repr(result.exception)
        register.assert_called_once()
        daemon.server.run.assert_called_once_with(transport="streamable-http")


class TestProjectFromCwd:
    """verifies the project start-mcp-server activates when asked to find it from the working directory."""

    def test_the_project_found_from_the_working_directory_is_activated(
        self, daemon: _Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = tmp_path / "found"
        (project_dir / ".serena").mkdir(parents=True)
        (project_dir / ".serena" / "project.yml").write_text("", encoding="utf-8")
        monkeypatch.chdir(project_dir)

        result = daemon.invoke("--transport", "streamable-http", "--project-from-cwd")

        assert result.exit_code == 0, repr(result.exception)
        assert daemon.activated_project == str(project_dir.resolve())

    def test_no_project_found_from_the_working_directory_activates_none(self, daemon: _Daemon) -> None:
        with patch("serena.cli.find_project_root", return_value=None):
            result = daemon.invoke("--transport", "streamable-http", "--project-from-cwd")

        assert result.exit_code == 0, repr(result.exception)
        assert daemon.activated_project is None


class TestPipeForwarder:
    """verifies the refusal of a pipe forwarder whose project is not a directory."""

    def test_a_pipe_project_that_is_not_a_directory_is_refused(self, daemon: _Daemon, tmp_path: Path) -> None:
        not_a_directory = tmp_path / "project.txt"
        not_a_directory.write_text("", encoding="utf-8")

        result = daemon.invoke("--transport", "pipe", "--project", str(not_a_directory))

        assert result.exit_code == 2, result.output
        assert "requires a valid project directory" in result.output
        daemon.run_pipe_client.assert_not_called()
        daemon.factory_cls.assert_not_called()


class TestDaemonServer:
    """verifies the server start-mcp-server builds for a daemon transport and serves without a pipe listener."""

    def test_the_server_serves_the_session_end_route_of_the_factorys_agent(self, daemon: _Daemon) -> None:
        result = daemon.invoke("--transport", "streamable-http")

        assert result.exit_code == 0, repr(result.exception)
        daemon.server.custom_route.assert_called_once_with(_SESSION_END_PATH, methods=["POST"])
        handler = daemon.server.custom_route.return_value.call_args.args[0]
        assert isinstance(handler.__self__, SessionEndRoute)
        assert handler.__self__._agent is daemon.factory_cls.return_value.agent
        daemon.server.run.assert_called_once_with(transport="streamable-http")
        daemon.build_pipe_listener.assert_not_called()

    def test_a_positional_project_argument_is_activated_with_a_deprecation_warning(
        self, daemon: _Daemon, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        project_dir = tmp_path / "positional"
        project_dir.mkdir()

        with caplog.at_level(logging.WARNING):
            result = daemon.invoke("--transport", "streamable-http", str(project_dir))

        assert result.exit_code == 0, repr(result.exception)
        assert daemon.activated_project == str(project_dir.resolve())
        assert "Positional project arg is deprecated" in caplog.text


class TestPipeListener:
    """verifies that, given a pipe socket path, start-mcp-server serves its transport beside a pipe listener."""

    @pytest.mark.parametrize(
        ("transport", "serve"),
        [("stdio", "run_stdio_async"), ("sse", "run_sse_async"), ("streamable-http", "run_streamable_http_async")],
    )
    def test_the_transport_is_served_beside_a_pipe_listener(
        self, daemon: _Daemon, tmp_path: Path, transport: str, serve: str
    ) -> None:
        socket_path = str(tmp_path / "daemon.sock")

        result = daemon.invoke("--transport", transport, "--pipe-socket-path", socket_path)

        assert result.exit_code == 0, repr(result.exception)
        daemon.build_pipe_listener.assert_called_once_with(daemon.factory_cls.return_value.agent, openai_tool_compatible=False)
        daemon.pipe_listener.start.assert_awaited_once_with(socket_path)
        getattr(daemon.server, serve).assert_awaited_once_with()
        daemon.pipe_listener.stop.assert_awaited_once_with()
        daemon.server.run.assert_not_called()

    def test_a_transport_the_pipe_listener_cannot_serve_beside_is_refused_and_the_listener_stopped(
        self, daemon: _Daemon, tmp_path: Path
    ) -> None:
        socket_path = str(tmp_path / "daemon.sock")

        # the command's own function, past the click choice that admits no such transport; outside click's runner stderr
        # may have a descriptor, so the fault handler is patched out rather than registered in the test process
        with (
            patch("serena.cli.faulthandler.register"),
            pytest.raises(ValueError, match="Unsupported transport with --pipe-socket-path"),
        ):
            TopLevelCommands.start_mcp_server.callback(
                project=None,
                project_file_arg=None,
                project_from_cwd=False,
                context="agent",
                modes=(),
                language_backend=None,
                transport="websocket",
                daemon_url="unix:///tmp/serena-daemon.sock",
                pipe_socket_path=socket_path,
                host="127.0.0.1",
                port=8000,
                enable_web_dashboard=None,
                open_web_dashboard=None,
                enable_gui_log_window=None,
                log_level=None,
                trace_lsp_communication=None,
                tool_timeout=None,
            )

        daemon.pipe_listener.start.assert_awaited_once_with(socket_path)
        daemon.pipe_listener.stop.assert_awaited_once_with()
        daemon.server.run.assert_not_called()
