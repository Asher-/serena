"""Tests reloading a project whose project.yml changed while sessions hold its instance.

These pin the requirements of ``pg://serena/session/project/instance``
(bug://serena/agent/project/reload/shared/instance/shutdown):

* ``shutdown`` -- an instance stops its language servers only when no session, cursor manager or call in flight holds
  it, so a project.yml change one session picks up never leaves another session holding a shut-down instance;
* ``currency`` -- a session that activates a project is bound to an instance built from the project.yml on disk at that
  activation, whether or not it already holds an older instance of the same root;
* ``uniqueness`` -- at most one language-server manager runs over a project root at a time.

Every ordering is established on a gate (:class:`threading.Event`) or by the order of a log, never by a clock: a wait
with a timeout below only guards the suite against a hang and fails the test if the guarded event never happens.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from unittest.mock import MagicMock

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.constants import SERENA_MANAGED_DIR_NAME
from serena.ls_manager import LanguageServerFactory, LanguageServerManager
from serena.project import Project
from serena.tools.tools_base import Tool
from solidlsp.ls_config import Language
from test.serena.test_session_project_binding import _HANG_GUARD_SECONDS, _Gate
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx

T = TypeVar("T")

_SESSION_1 = "cc-session-1"
_SESSION_2 = "cc-session-2"

_EDITING_TOOL = "create_text_file"
"""a tool a read_only project excludes"""


@dataclass
class _StandInManager:
    """A language-server manager built by :class:`_ManagerFactory` in place of a real one."""

    project_root: str
    """the root of the project the manager was built for"""
    manager: MagicMock
    """the stand-in itself, handed to the project"""
    stopped: threading.Event
    """set once the manager has been stopped"""
    overlapped: bool
    """whether another manager of the same root was running when this one was built"""
    position: int
    """the position of the manager's construction in the factory's log"""


class _ManagerFactory:
    """
    A stand-in for :meth:`LanguageServerManager.from_languages` that starts no language server.

    It records every manager it builds, in order, in one log together with the moments a test marks, so a test reads
    the order of a manager's construction relative to those moments.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._log_length = 0
        self._managers: list[_StandInManager] = []

    def build(self, languages: list[Language], factory: LanguageServerFactory) -> LanguageServerManager:
        """Builds a stand-in manager for the project ``factory`` serves, in place of :meth:`LanguageServerManager.from_languages`."""
        with self._condition:
            running = [m for m in self._managers if m.project_root == factory.project_root and not m.stopped.is_set()]
            stopped = threading.Event()
            manager = MagicMock(spec=LanguageServerManager)
            manager.stop_all.side_effect = lambda *args, **kwargs: stopped.set()
            self._managers.append(
                _StandInManager(factory.project_root, manager, stopped, overlapped=bool(running), position=self._log_length)
            )
            self._log_length += 1
            self._condition.notify_all()
        return manager

    def mark(self) -> int:
        """
        :return: the position of this moment in the log, which every manager built after it follows
        """
        with self._condition:
            position = self._log_length
            self._log_length += 1
        return position

    def await_managers(self, root: Path, count: int) -> list[_StandInManager]:
        """
        :param root: a project root
        :param count: the number of managers to wait for
        :return: the first ``count`` managers built for ``root``, in order of construction
        """
        with self._condition:
            built = self._condition.wait_for(lambda: len(self._built_for(root)) >= count, timeout=_HANG_GUARD_SECONDS)
            assert built, f"fewer than {count} language-server managers were ever built for {root}"
            return self._built_for(root)[:count]

    def _built_for(self, root: Path) -> list[_StandInManager]:
        return [m for m in self._managers if m.project_root == str(root)]


class _Sessions:
    """Runs agent operations as one session or another, the way a tool call of that session runs them."""

    def __init__(self, agent: SerenaAgent) -> None:
        self._agent = agent

    def run(self, session: str, operation: Callable[[], T]) -> T:
        """
        :param session: the session key the operation runs under
        :param operation: the operation
        :return: the operation's result
        """

        def bound() -> T:
            _SESSION_KEY_VAR.set(session)
            return operation()

        return contextvars.copy_context().run(bound)

    def activate(self, session: str, root: Path) -> None:
        """Activates the project at ``root`` for ``session``, as an activate_project call of that session does."""
        self.run(session, lambda: self._agent.activate_project_from_path_or_name(str(root)))

    def held(self, session: str) -> Project | None:
        """:return: the project instance ``session`` holds"""
        return self._agent._active_projects_by_session.get(session)

    def tool_is_active(self, session: str, tool_name: str) -> bool:
        """:return: whether ``tool_name`` is active for ``session``"""
        return self.run(session, lambda: self._agent.tool_is_active(tool_name))


class _GatedCallTool(Tool):
    """A tool needing an active project that holds its call in flight on a gate and reports the project it ran against."""

    def __init__(self, agent: SerenaAgent, gate: _Gate) -> None:
        super().__init__(agent)
        self._gate = gate

    def apply(self) -> str:
        self._gate.pass_through()
        project = self.agent.get_active_project()
        assert project is not None
        return project.project_root


def _write_project(root: Path) -> Path:
    """
    :param root: the project root, created here
    :return: the resolved root, holding a minimal project.yml of a Python project
    """
    serena_dir = root / SERENA_MANAGED_DIR_NAME
    serena_dir.mkdir(parents=True)
    (serena_dir / ProjectConfig.SERENA_PROJECT_FILE).write_text(f"project_name: {root.name}\nlanguages:\n  - python\n", encoding="utf-8")
    return root.resolve()


def _change_project_yml(agent: SerenaAgent, root: Path, **changes: object) -> None:
    """
    Changes the project.yml at ``root`` on disk, as a user editing it does.

    :param agent: the agent whose configuration the project uses
    :param root: the project root
    :param changes: the configuration fields to change, with their new values
    """
    config = ProjectConfig.load(root, serena_config=agent.serena_config)
    for name, value in changes.items():
        setattr(config, name, value)
    config.save(str(root / SERENA_MANAGED_DIR_NAME / ProjectConfig.SERENA_PROJECT_FILE), save_project_local_yml=False)


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    """A minimal agent holding no project, its tool-usage recording stubbed out."""
    serena_agent = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False, log_level=logging.ERROR))
    serena_agent.record_tool_usage = lambda *args, **kwargs: None  # type: ignore[method-assign]
    yield serena_agent
    serena_agent.on_shutdown(timeout=0.1)


@pytest.fixture
def managers(monkeypatch: pytest.MonkeyPatch) -> _ManagerFactory:
    """The factory every project builds its language-server manager with, in place of real language servers."""
    factory = _ManagerFactory()
    monkeypatch.setattr(LanguageServerManager, "from_languages", staticmethod(factory.build))
    return factory


@pytest.fixture(autouse=True)
def _no_pipe_session() -> Iterator[None]:
    """Keeps the pipe transport's session id out of scope, so the session key alone identifies a session."""
    token = _PIPE_SESSION_ID_VAR.set(None)
    yield
    _PIPE_SESSION_ID_VAR.reset(token)


def _issue_gated_call(agent: SerenaAgent, session: str, gate: _Gate, monkeypatch: pytest.MonkeyPatch) -> Callable[[], str]:
    """
    Issues a call of ``session`` that holds its project on ``gate`` and returns once the call is in flight.

    :return: a function that waits for the call's result
    """
    tool = _GatedCallTool(agent, gate)
    monkeypatch.setattr(tool, "is_active", lambda: True)
    task = tool._issue_call(False, True, _make_mcp_ctx(forwarded_header=session))
    gate.await_entry()
    return lambda: task.result(timeout=_HANG_GUARD_SECONDS)


class TestReloadStopsAnInstanceOnlyWhenNothingHoldsIt:
    """pg://serena/session/project/instance/shutdown"""

    def test_reload_by_one_session_moves_the_other_onto_the_new_instance(
        self, agent: SerenaAgent, managers: _ManagerFactory, tmp_path: Path
    ) -> None:
        """
        Sessions 1 and 2 hold R; R's project.yml changes and session 1 activates R: session 2 holds the instance session 1
        holds, whose language servers keep running, and only the replaced instance's language servers are stopped.
        """
        root = _write_project(tmp_path / "R")
        sessions = _Sessions(agent)
        sessions.activate(_SESSION_1, root)
        sessions.activate(_SESSION_2, root)
        (old_manager,) = managers.await_managers(root, 1)

        _change_project_yml(agent, root, initial_prompt="changed")
        sessions.activate(_SESSION_1, root)

        assert sessions.held(_SESSION_2) is sessions.held(_SESSION_1), "session 2 was left holding the instance session 1's reload replaced"
        new_manager = managers.await_managers(root, 2)[1]
        assert old_manager.stopped.is_set(), "the replaced instance, which nothing holds, kept its language servers running"
        assert not new_manager.stopped.is_set(), "the language servers of the instance both sessions hold were stopped"

    def test_replaced_instance_stops_only_after_a_call_holding_it_ends(
        self, agent: SerenaAgent, managers: _ManagerFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Sessions 1 and 2 hold R and a call of session 2 is held in flight on a gate while R's project.yml changes and
        session 1 activates R: the replaced instance's language servers keep running until the call ends, and stop after.
        """
        root = _write_project(tmp_path / "R")
        sessions = _Sessions(agent)
        sessions.activate(_SESSION_1, root)
        sessions.activate(_SESSION_2, root)
        (old_manager,) = managers.await_managers(root, 1)
        gate = _Gate.closed()
        call_result = _issue_gated_call(agent, _SESSION_2, gate, monkeypatch)

        _change_project_yml(agent, root, initial_prompt="changed")
        sessions.activate(_SESSION_1, root)

        assert not old_manager.stopped.is_set(), "the replaced instance's language servers were stopped while a call of session 2 ran on it"
        gate.release.set()
        assert call_result() == str(root)
        assert old_manager.stopped.wait(_HANG_GUARD_SECONDS), (
            "the replaced instance's language servers were not stopped once no call held it"
        )

    def test_cursor_manager_over_the_replaced_instance_is_dropped(
        self, agent: SerenaAgent, managers: _ManagerFactory, tmp_path: Path
    ) -> None:
        """
        Session 2 has a cursor manager built over R's instance; R's project.yml changes and session 1 activates R: the
        cursor manager is dropped, so it neither serves the replaced instance nor keeps it running.
        """
        root = _write_project(tmp_path / "R")
        sessions = _Sessions(agent)
        sessions.activate(_SESSION_1, root)
        sessions.activate(_SESSION_2, root)
        (old_manager,) = managers.await_managers(root, 1)
        agent._cursor_managers_by_session[_SESSION_2] = MagicMock(project=sessions.held(_SESSION_2))

        _change_project_yml(agent, root, initial_prompt="changed")
        sessions.activate(_SESSION_1, root)

        assert _SESSION_2 not in agent._cursor_managers_by_session, "session 2 kept a cursor manager over the replaced instance"
        assert old_manager.stopped.wait(_HANG_GUARD_SECONDS), (
            "the replaced instance's language servers were not stopped once nothing held it"
        )


class TestActivationBindsTheCurrentInstance:
    """pg://serena/session/project/instance/currency"""

    def test_session_holding_the_replaced_instance_activates_the_root_and_gets_the_current_one(
        self, agent: SerenaAgent, managers: _ManagerFactory, tmp_path: Path
    ) -> None:
        """
        Sessions 1 and 2 hold R; R's project.yml changes, session 1 activates R and then session 2 activates R: session 2
        is bound to an instance built from the changed project.yml.
        """
        root = _write_project(tmp_path / "R")
        sessions = _Sessions(agent)
        sessions.activate(_SESSION_1, root)
        sessions.activate(_SESSION_2, root)

        _change_project_yml(agent, root, initial_prompt="changed")
        sessions.activate(_SESSION_1, root)
        sessions.activate(_SESSION_2, root)

        held = sessions.held(_SESSION_2)
        assert held is not None
        assert held.project_config.initial_prompt == "changed", (
            "session 2 is bound to an instance built from the project.yml before the change"
        )


class TestReloadDerivesToolsOfEveryMovedSession:
    """pg://serena/session/tool/derivation"""

    def test_session_moved_onto_a_read_only_instance_loses_editing_tools(
        self, agent: SerenaAgent, managers: _ManagerFactory, tmp_path: Path
    ) -> None:
        """
        Sessions 1 and 2 hold a writable R; R's project.yml becomes read_only and session 1 activates R: editing tools are
        inactive for session 2 too, whose project is now the read_only instance.
        """
        root = _write_project(tmp_path / "R")
        sessions = _Sessions(agent)
        sessions.activate(_SESSION_1, root)
        sessions.activate(_SESSION_2, root)
        assert sessions.tool_is_active(_SESSION_2, _EDITING_TOOL)

        _change_project_yml(agent, root, read_only=True)
        sessions.activate(_SESSION_1, root)

        assert not sessions.tool_is_active(_SESSION_1, _EDITING_TOOL), "session 1 kept editing tools on the read_only instance"
        assert not sessions.tool_is_active(_SESSION_2, _EDITING_TOOL), "session 2 kept the editing tools of the replaced writable instance"


class TestOneLanguageServerManagerPerRoot:
    """pg://serena/session/project/instance/uniqueness"""

    def test_new_instance_starts_its_manager_only_after_the_replaced_one_stops(
        self, agent: SerenaAgent, managers: _ManagerFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Session 2 holds R with a call held in flight on a gate, session 1 holds P and activates R after R's project.yml
        changed: no manager of R's new instance is built while the replaced instance's manager runs, and one is built
        once the gate is released.
        """
        root_r, root_p = _write_project(tmp_path / "R"), _write_project(tmp_path / "P")
        sessions = _Sessions(agent)
        sessions.activate(_SESSION_2, root_r)
        managers.await_managers(root_r, 1)
        sessions.activate(_SESSION_1, root_p)
        managers.await_managers(root_p, 1)
        gate = _Gate.closed()
        call_result = _issue_gated_call(agent, _SESSION_2, gate, monkeypatch)

        _change_project_yml(agent, root_r, initial_prompt="changed")
        sessions.activate(_SESSION_1, root_r)
        released_at = managers.mark()
        gate.release.set()
        assert call_result() == str(root_r)

        new_manager = managers.await_managers(root_r, 2)[1]
        assert not new_manager.overlapped, "a manager of R's new instance was built while the replaced instance's manager ran"
        assert new_manager.position > released_at, "a manager of R's new instance was built while a call held the replaced instance"
