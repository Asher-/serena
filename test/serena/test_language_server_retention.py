"""unit tests for the retention of a project's language servers by :class:`SerenaAgent`.

a project's language-server manager must be stopped once no session holds the project, and never while any session
holds it, so that the language servers a daemon runs are those of the projects its live sessions hold. An activation
decides before it returns; an eviction stops the project off the evicting thread, so those tests wait on a gate, whose
timeout only bounds how long a broken implementation can hang a test.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import LanguageBackend, ProjectConfig, SerenaConfig
from serena.ls_manager import LanguageServerManager
from serena.project import Project
from solidlsp.ls_config import Language

_GATE_TIMEOUT = 10.0
"""the seconds a test waits on a gate before failing; a bound on a hang, never the ordering under test"""


class _HeldProject(NamedTuple):
    """a project and the language-server manager double it holds, which sets ``stopped`` when stopped"""

    project: Project
    manager: MagicMock


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    a = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False, log_level=logging.ERROR))
    yield a
    a.on_shutdown(timeout=0.1)


def _held_project(agent: SerenaAgent, root: Path) -> _HeldProject:
    """
    :param agent: the agent whose configuration the project shares
    :param root: the project root, created here
    :return: a Python project over ``root`` holding a language-server manager double, so the project never starts a
        language server
    """
    root.mkdir()
    project_config = ProjectConfig(project_name=root.name, languages=[Language.PYTHON], language_backend=LanguageBackend.LSP)
    project = Project(project_root=str(root), project_config=project_config, serena_config=agent.serena_config)
    manager = MagicMock(spec=LanguageServerManager)
    manager.stopped = threading.Event()
    manager.stop_all.side_effect = lambda *args, **kwargs: manager.stopped.set()
    project.language_server_manager = manager
    return _HeldProject(project, manager)


@pytest.fixture
def project_a(agent: SerenaAgent, tmp_path: Path) -> _HeldProject:
    return _held_project(agent, tmp_path / "project_a")


@pytest.fixture
def project_b(agent: SerenaAgent, tmp_path: Path) -> _HeldProject:
    return _held_project(agent, tmp_path / "project_b")


def _activate(agent: SerenaAgent, session_key: str, held: _HeldProject) -> None:
    """Activates the project in the session ``session_key``, as an activate_project call from that session does."""

    def run() -> None:
        _SESSION_KEY_VAR.set(session_key)
        agent._activate_project(held.project, update_active_modes=False, update_active_tools=False)

    contextvars.copy_context().run(run)


class TestActivationStopsAProjectNoSessionHolds:
    """verifies that a session leaving a project stops its language servers only if no other session holds it."""

    def test_project_left_by_its_only_session_is_stopped(self, agent: SerenaAgent, project_a: _HeldProject, project_b: _HeldProject) -> None:
        _activate(agent, "session-1", project_a)
        _activate(agent, "session-2", project_b)

        # session 1, the only session holding A, moves to B
        _activate(agent, "session-1", project_b)

        # A's manager was stopped; B, held by both sessions, was not
        project_a.manager.stop_all.assert_called_once()
        project_b.manager.stop_all.assert_not_called()

    def test_project_still_held_by_another_session_is_not_stopped(
        self, agent: SerenaAgent, project_a: _HeldProject, project_b: _HeldProject
    ) -> None:
        _activate(agent, "session-1", project_a)
        _activate(agent, "session-2", project_a)

        # session 1 moves to B while session 2 still holds A: A's manager is not stopped
        _activate(agent, "session-1", project_b)
        project_a.manager.stop_all.assert_not_called()

        # session 2, now the only session holding A, moves to B: A's manager is stopped
        _activate(agent, "session-2", project_b)
        project_a.manager.stop_all.assert_called_once()


class TestEvictionStopsAProjectNoSessionHolds:
    """verifies that evicting the only session holding a project stops its language servers."""

    @pytest.mark.parametrize("evict", ["_evict_session_state", "evict_pipe_session"])
    def test_eviction_of_the_only_session_holding_a_project_stops_it(self, agent: SerenaAgent, project_a: _HeldProject, evict: str) -> None:
        _activate(agent, "session-1", project_a)

        getattr(agent, evict)("session-1")

        assert project_a.manager.stopped.wait(timeout=_GATE_TIMEOUT), "A's manager was never stopped"
        project_a.manager.stop_all.assert_called_once()

    def test_eviction_of_a_pipe_session_shuts_down_its_executor(self, agent: SerenaAgent, project_a: _HeldProject) -> None:
        _activate(agent, "session-1", project_a)
        executor = MagicMock()
        agent._task_executors_by_session["session-1"] = executor

        agent.evict_pipe_session("session-1")

        # the session's executor was shut down and dropped, and the project it held was still released
        executor.shutdown.assert_called_once()
        assert "session-1" not in agent._task_executors_by_session
        assert project_a.manager.stopped.wait(timeout=_GATE_TIMEOUT), "A's manager was never stopped"


class TestReleaseOfAProject:
    """verifies that releasing a project stops its language servers only if no session holds it."""

    def test_releasing_no_project_stops_nothing(self, agent: SerenaAgent, project_a: _HeldProject) -> None:
        _activate(agent, "session-1", project_a)

        agent._release_project_if_unheld(None)

        project_a.manager.stop_all.assert_not_called()
        assert project_a.project.language_server_manager is project_a.manager

    def test_releasing_a_project_a_session_holds_leaves_it_running(self, agent: SerenaAgent, project_a: _HeldProject) -> None:
        _activate(agent, "session-1", project_a)

        agent._release_project_if_unheld(project_a.project)

        project_a.manager.stop_all.assert_not_called()
        assert project_a.project.language_server_manager is project_a.manager

    def test_releasing_a_project_no_session_holds_stops_and_detaches_its_manager(
        self, agent: SerenaAgent, project_a: _HeldProject
    ) -> None:
        agent._release_project_if_unheld(project_a.project)

        project_a.manager.stop_all.assert_called_once()
        assert project_a.project.language_server_manager is None


class TestBackgroundReleaseOfAProject:
    """verifies that a background release runs the release on a thread of its own, and starts no thread for no project."""

    def test_a_project_is_released_on_a_thread_of_its_own(
        self, agent: SerenaAgent, project_a: _HeldProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        releasing_threads: list[str] = []
        released = threading.Event()

        def record_release(project: Project) -> None:
            releasing_threads.append(threading.current_thread().name)
            released.set()

        monkeypatch.setattr(agent, "_release_project_if_unheld", record_release)

        agent._release_project_if_unheld_in_background(project_a.project)

        assert released.wait(timeout=_GATE_TIMEOUT), "the project was never released"
        assert releasing_threads == [f"ReleaseProject[{project_a.project.project_name}]"]

    def test_no_project_starts_no_thread(self, agent: SerenaAgent) -> None:
        with patch.object(threading, "Thread") as thread_cls:
            agent._release_project_if_unheld_in_background(None)

        thread_cls.assert_not_called()
