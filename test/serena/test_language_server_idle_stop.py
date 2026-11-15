"""tests of the idle stop of a project's language servers by :class:`SerenaAgent`.

a project on which no tool call has run for ``language_server_idle_timeout`` seconds, and on which no call is in flight,
has its language servers stopped while its sessions keep holding it; the next call that asks for a server gets it
restarted. The idle stop runs on a thread of the agent, so the tests wait for it with a bounded poll.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.language_backend import BuiltinLanguageBackend
from serena.ls_manager import LanguageServerFactory, LanguageServerManager
from serena.project import Project
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId

_IDLE_TIMEOUT = 0.2
"""the idle timeout, in seconds, of the agents whose idle stop the tests observe"""

_WAIT = 5.0
"""the seconds a test waits for the idle stop before failing; a bound on a hang, never the ordering under test"""

_QUIET = 1.0
"""the seconds a test lets pass to observe that no idle stop happens"""


class _RunningStateLanguageServerFactory(LanguageServerFactory):
    """A factory double that records each server it creates; each server is a mock that runs until it is stopped."""

    def __init__(self) -> None:
        # the base constructor is deliberately not invoked: it validates paths, which a mock server never reads
        self.created: list[MagicMock] = []

    def create_language_server(self, language: LanguageServerId) -> SolidLanguageServer:  # type: ignore[override]
        server = MagicMock(spec=SolidLanguageServer)
        server.ls_id = language
        server.is_running.return_value = True
        server.is_ignored_path.return_value = False
        server.stop.side_effect = lambda *args, **kwargs: setattr(server.is_running, "return_value", False)
        self.created.append(server)
        return server


def _agent(idle_timeout: float) -> SerenaAgent:
    """
    :param idle_timeout: the agent's language-server idle timeout, in seconds
    :return: a minimal agent with that idle timeout
    """
    return SerenaAgent(
        serena_config=SerenaConfig(
            gui_log_window=False, web_dashboard=False, log_level=logging.ERROR, language_server_idle_timeout=idle_timeout
        )
    )


@pytest.fixture
def idle_agent() -> Iterator[SerenaAgent]:
    agent = _agent(_IDLE_TIMEOUT)
    yield agent
    agent.on_shutdown(timeout=0.1)


@pytest.fixture
def factory() -> _RunningStateLanguageServerFactory:
    return _RunningStateLanguageServerFactory()


def _bind_project(agent: SerenaAgent, root: Path, factory: LanguageServerFactory) -> Project:
    """
    Binds a project over ``root``, with a real language-server manager over ``factory``, as the active project of the
    session ``session-1``, as an activation by that session does.

    :return: the bound project
    """
    root.mkdir()
    project_config = ProjectConfig(
        project_name=root.name, language_servers=[LanguageServerId.PYTHON], language_backend=BuiltinLanguageBackend.LSP.get_instance()
    )
    project = Project(project_root=str(root), project_config=project_config, serena_config=agent.serena_config)
    project.language_server_manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON], factory, project=None)

    def run() -> None:
        _SESSION_KEY_VAR.set("session-1")
        agent._activate_project(project, update_active_modes=False, update_active_tools=False)

    contextvars.copy_context().run(run)
    return project


def _eventually(condition: Callable[[], bool]) -> bool:
    """
    :param condition: the condition to wait for
    :return: whether the condition held within :data:`_WAIT` seconds
    """
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_the_servers_of_a_project_no_call_has_used_for_the_idle_timeout_are_stopped_and_the_project_stays_held(
    idle_agent: SerenaAgent, factory: _RunningStateLanguageServerFactory, tmp_path: Path
) -> None:
    project = _bind_project(idle_agent, tmp_path / "project", factory)
    manager = project.language_server_manager
    (server,) = factory.created

    assert _eventually(lambda: server.stop.called)
    assert project.language_server_manager is manager
    assert idle_agent._active_projects_by_session["session-1"] is project


def test_a_call_in_flight_on_a_project_keeps_its_servers_running_past_the_idle_timeout(
    idle_agent: SerenaAgent, factory: _RunningStateLanguageServerFactory, tmp_path: Path
) -> None:
    project = _bind_project(idle_agent, tmp_path / "project", factory)
    (server,) = factory.created

    call_hold = idle_agent._pin_session_project_for_call("session-1")
    try:
        time.sleep(_QUIET)
        server.stop.assert_not_called()
    finally:
        idle_agent._unpin_project_for_call(call_hold)
    assert project.language_server_manager is not None


def test_a_call_after_an_idle_stop_gets_a_restarted_server(
    idle_agent: SerenaAgent, factory: _RunningStateLanguageServerFactory, tmp_path: Path
) -> None:
    project = _bind_project(idle_agent, tmp_path / "project", factory)
    (server,) = factory.created
    assert _eventually(lambda: server.stop.called)

    call_hold = idle_agent._pin_session_project_for_call("session-1")
    try:
        restarted = project.get_language_server_manager_or_raise().get_language_server("main.py")
    finally:
        idle_agent._unpin_project_for_call(call_hold)

    assert restarted.is_running()
    assert factory.created == [server, restarted]


def test_an_idle_timeout_of_zero_stops_no_server(factory: _RunningStateLanguageServerFactory, tmp_path: Path) -> None:
    agent = _agent(0)
    try:
        _bind_project(agent, tmp_path / "project", factory)
        (server,) = factory.created

        time.sleep(_QUIET)

        server.stop.assert_not_called()
    finally:
        agent.on_shutdown(timeout=0.1)
