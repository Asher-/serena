"""tests of the idle stop of a project's language servers by :class:`SerenaAgent`.

a project on which no tool call has run for ``language_server_idle_timeout`` seconds, and on which no call is in flight,
has its language servers stopped while its sessions keep holding it; the next call that asks for a server gets it
restarted. The idle stop runs on a thread of the agent, so the tests wait for it with a bounded poll.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.language_backend import BuiltinLanguageBackend
from serena.ls_manager import LanguageServerFactory, LanguageServerManager
from serena.project import Project
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from test.serena.test_tool_call_session_queue import _activated, _ForwardedMcpContext, _Gate, _GatedProbeTool

_IDLE_TIMEOUT = 0.2
"""the idle timeout, in seconds, of the agents whose idle stop the tests observe"""

_WAIT = 5.0
"""the seconds a test waits for the idle stop before failing; a bound on a hang, never the ordering under test"""

_QUIET = 1.0
"""the seconds a test lets pass to observe that no idle stop happens"""


class _RunningStateLanguageServerFactory(LanguageServerFactory):
    """
    A factory double that records each server it creates; each server is a mock that runs until it is stopped. While
    :attr:`stop_gate` is set, a stop of a server holds on that gate before the server stops running.
    """

    def __init__(self) -> None:
        # the base constructor is not invoked: a factory that creates mock servers needs none of its fields
        self.created: list[MagicMock] = []
        self.stop_gate: _Gate | None = None

    def create_language_server(self, language: LanguageServerId) -> SolidLanguageServer:  # type: ignore[override]
        server = MagicMock(spec=SolidLanguageServer)
        server.ls_id = language
        server.is_running.return_value = True
        server.is_ignored_path.return_value = False

        def stop(*arguments: Any, **keyword_arguments: Any) -> None:
            if self.stop_gate is not None:
                self.stop_gate.hold()
            server.is_running.return_value = False

        server.stop.side_effect = stop
        self.created.append(server)
        return server


class _ServerProbeTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """A tool whose call asks the active project's language server manager for a server and records the server it got."""

    def __init__(self, agent: SerenaAgent) -> None:
        super().__init__(agent)
        self.obtained: list[SolidLanguageServer] = []

    def apply(self) -> str:
        """
        Asks the active project's manager for the server of a Python file.

        :return: a fixed marker
        """
        self.obtained.append(self.project.get_language_server_manager_or_raise().get_language_server("main.py"))
        return "obtained"


class _ActivatingServerProbeTool(_ServerProbeTool):
    """A tool whose call activates a given project and then asks its language server manager for a server."""

    def __init__(self, agent: SerenaAgent, project: Project) -> None:
        super().__init__(agent)
        self._project_to_activate = project

    def apply(self) -> str:
        """
        Activates the project, then asks its manager for the server of a Python file.

        :return: a fixed marker
        """
        self.agent._activate_project(self._project_to_activate, update_active_modes=False, update_active_tools=False)
        return super().apply()


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


def _bind_project(agent: SerenaAgent, root: Path, factory: LanguageServerFactory, session_key: str | None = "session-1") -> Project:
    """
    Binds a project over ``root``, with a real language-server manager over ``factory``, as the active project of a
    session, as an activation by that session does.

    :param session_key: the session whose active project the project becomes; None binds it to the legacy slot, as an
        activation outside any session does
    :return: the bound project
    """
    root.mkdir()
    project_config = ProjectConfig(
        project_name=root.name, language_servers=[LanguageServerId.PYTHON], language_backend=BuiltinLanguageBackend.LSP.get_instance()
    )
    project = Project(project_root=str(root), project_config=project_config, serena_config=agent.serena_config)
    project.language_server_manager = LanguageServerManager.from_languages([LanguageServerId.PYTHON], factory, project=None)

    def run() -> None:
        if session_key is not None:
            _SESSION_KEY_VAR.set(session_key)
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
    factory: _RunningStateLanguageServerFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an idle timeout long enough that the call takes the project up before it passes, even on a loaded machine
    idle_timeout = 1.0
    agent = _agent(idle_timeout)
    gate = _Gate()
    call: threading.Thread | None = None
    try:
        _bind_project(agent, tmp_path / "project", factory)
        (server,) = factory.created
        held = _activated(_GatedProbeTool(agent, gate), monkeypatch)
        results: list[str] = []
        call = threading.Thread(target=lambda: results.append(held.apply_ex(mcp_ctx=_ForwardedMcpContext.for_session("session-1"))))
        call.start()
        assert gate.entered.wait(_WAIT)

        time.sleep(2 * idle_timeout)

        server.stop.assert_not_called()
    finally:
        gate.release.set()
        if call is not None:
            call.join(_WAIT)
        agent.on_shutdown(timeout=0.1)
    assert results == ["released"]


def test_a_call_after_an_idle_stop_gets_a_restarted_server(
    idle_agent: SerenaAgent, factory: _RunningStateLanguageServerFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_project(idle_agent, tmp_path / "project", factory)
    (server,) = factory.created
    assert _eventually(lambda: server.stop.called)
    probe = _activated(_ServerProbeTool(idle_agent), monkeypatch)

    result = probe.apply_ex(mcp_ctx=_ForwardedMcpContext.for_session("session-1"))

    assert result == "obtained", result
    (restarted,) = probe.obtained
    assert restarted.is_running()
    assert factory.created == [server, restarted]


def test_a_call_taking_up_a_project_during_its_idle_stop_gets_a_running_server(
    idle_agent: SerenaAgent, factory: _RunningStateLanguageServerFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A call of the session that takes the project up while the idle stop is stopping its server gets a server that is
    still running once the stop has finished.
    """
    stop_gate = _Gate()
    factory.stop_gate = stop_gate
    _bind_project(idle_agent, tmp_path / "project", factory)
    probe = _activated(_ServerProbeTool(idle_agent), monkeypatch)
    results: list[str] = []
    call = threading.Thread(target=lambda: results.append(probe.apply_ex(mcp_ctx=_ForwardedMcpContext.for_session("session-1"))))
    try:
        assert stop_gate.entered.wait(_WAIT)
        call.start()
        time.sleep(_IDLE_TIMEOUT)
    finally:
        stop_gate.release.set()
    call.join(_WAIT)
    stopped = factory.created[0]
    assert _eventually(lambda: not stopped.is_running())

    assert results == ["obtained"]
    (obtained,) = probe.obtained
    assert obtained.is_running()


def test_a_call_activating_a_project_during_its_idle_stop_gets_a_running_server(
    idle_agent: SerenaAgent, factory: _RunningStateLanguageServerFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A call of another session that activates the project while the idle stop is stopping its server gets a server that
    is still running once the stop has finished.
    """
    stop_gate = _Gate()
    factory.stop_gate = stop_gate
    project = _bind_project(idle_agent, tmp_path / "project", factory)
    probe = _activated(_ActivatingServerProbeTool(idle_agent, project), monkeypatch)
    results: list[str] = []
    call = threading.Thread(target=lambda: results.append(probe.apply_ex(mcp_ctx=_ForwardedMcpContext.for_session("session-2"))))
    try:
        assert stop_gate.entered.wait(_WAIT)
        call.start()
        time.sleep(_IDLE_TIMEOUT)
    finally:
        stop_gate.release.set()
    call.join(_WAIT)
    stopped = factory.created[0]
    assert _eventually(lambda: not stopped.is_running())

    assert results == ["obtained"]
    (obtained,) = probe.obtained
    assert obtained.is_running()


def test_on_shutdown_ends_the_idle_stop_thread() -> None:
    threads_before = set(threading.enumerate())
    agent = _agent(_IDLE_TIMEOUT)
    started = [thread for thread in threading.enumerate() if thread not in threads_before and thread.name == "LanguageServerIdleStop"]

    agent.on_shutdown(timeout=0.1)

    assert len(started) == 1
    assert not started[0].is_alive()


def test_a_call_outside_any_session_keeps_the_servers_of_the_legacy_slots_project_running_past_the_idle_timeout(
    idle_agent: SerenaAgent, factory: _RunningStateLanguageServerFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_project(idle_agent, tmp_path / "project", factory, session_key=None)
    (server,) = factory.created
    gate = _Gate()
    held = _activated(_GatedProbeTool(idle_agent, gate), monkeypatch)
    results: list[str] = []
    call = threading.Thread(target=lambda: results.append(held.apply_ex()))
    call.start()
    try:
        assert gate.entered.wait(_WAIT)
        time.sleep(_QUIET)
        server.stop.assert_not_called()
    finally:
        gate.release.set()
        call.join(_WAIT)
    assert results == ["released"]


def test_an_idle_timeout_of_zero_stops_no_server(factory: _RunningStateLanguageServerFactory, tmp_path: Path) -> None:
    agent = _agent(0)
    try:
        _bind_project(agent, tmp_path / "project", factory)
        (server,) = factory.created

        time.sleep(_QUIET)

        server.stop.assert_not_called()
    finally:
        agent.on_shutdown(timeout=0.1)
