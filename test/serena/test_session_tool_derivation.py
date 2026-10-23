"""Tests deriving each session's active tools from the project that session holds.

These pin ``pg://serena/session/tool/derivation``: the tools active for a session follow from the daemon's
configuration and that session's project alone, so another session's project -- its ``read_only`` setting or
its tool exclusions -- never enables or disables a tool for it (bug://serena/agent/tool/set/session/sharing).
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.project import Project

T = TypeVar("T")

_EDITING_TOOL = "create_text_file"
"""a tool a read_only project excludes"""

_EXCLUDABLE_TOOL = "find_file"
"""a tool a project's ``excluded_tools`` names in the exclusion test"""


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

    def activate(self, session: str, project: Project) -> None:
        """Activates ``project`` for ``session`` through the agent's activation path."""
        self.run(session, lambda: self._agent._activate_project(project))

    def tool_is_active(self, session: str, tool_name: str) -> bool:
        """:return: whether ``tool_name`` is active for ``session``"""
        return self.run(session, lambda: self._agent.tool_is_active(tool_name))


def _project(root: Path, name: str, read_only: bool = False, excluded_tools: list[str] | None = None) -> Project:
    """
    :param root: the project root, created if absent
    :param name: the project name
    :param read_only: whether the project is read-only
    :param excluded_tools: the tools the project excludes
    :return: a project with no languages, so activating it starts no language server
    """
    root.mkdir(parents=True, exist_ok=True)
    config = ProjectConfig(
        project_name=name,
        languages=[],
        ignored_paths=[],
        excluded_tools=excluded_tools or [],
        read_only=read_only,
        ignore_all_files_in_gitignore=True,
        initial_prompt="",
        encoding="utf-8",
    )
    return Project(project_root=str(root), project_config=config, serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    """A minimal agent holding no project."""
    serena_agent = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    yield serena_agent
    serena_agent.on_shutdown(timeout=0.5)


@pytest.fixture(autouse=True)
def _no_pipe_session() -> Iterator[None]:
    """Keeps the pipe transport's session id out of scope, so the session key alone identifies a session."""
    token = _PIPE_SESSION_ID_VAR.set(None)
    yield
    _PIPE_SESSION_ID_VAR.reset(token)


class TestToolsDeriveFromOwnSessionProject:
    """pg://serena/session/tool/derivation"""

    def test_read_only_project_of_another_session_leaves_editing_tools_active(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """Session 1 holds a writable project; session 2's read_only project disables editing for session 2 alone."""
        sessions = _Sessions(agent)
        sessions.activate("cc-session-1", _project(tmp_path / "writable", "writable"))
        sessions.activate("cc-session-2", _project(tmp_path / "read-only", "read-only", read_only=True))

        assert sessions.tool_is_active("cc-session-1", _EDITING_TOOL), "session 2's read_only project disabled editing for session 1"
        assert not sessions.tool_is_active("cc-session-2", _EDITING_TOOL), "session 2's read_only project left editing active for it"

    def test_writable_project_of_another_session_leaves_read_only_session_without_editing(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """Session 1 holds a read_only project; session 2's writable project enables editing for session 2 alone."""
        sessions = _Sessions(agent)
        sessions.activate("cc-session-1", _project(tmp_path / "read-only", "read-only", read_only=True))
        sessions.activate("cc-session-2", _project(tmp_path / "writable", "writable"))

        assert not sessions.tool_is_active("cc-session-1", _EDITING_TOOL), "session 2's writable project enabled editing for session 1"
        assert sessions.tool_is_active("cc-session-2", _EDITING_TOOL), "session 2's writable project left editing inactive for it"

    def test_tool_exclusion_of_another_sessions_project_leaves_tool_active(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """Session 2's project excludes a tool; that tool stays active for session 1."""
        sessions = _Sessions(agent)
        sessions.activate("cc-session-1", _project(tmp_path / "plain", "plain"))
        sessions.activate("cc-session-2", _project(tmp_path / "excluding", "excluding", excluded_tools=[_EXCLUDABLE_TOOL]))

        assert sessions.tool_is_active("cc-session-1", _EXCLUDABLE_TOOL), "session 2's tool exclusion disabled the tool for session 1"
        assert not sessions.tool_is_active("cc-session-2", _EXCLUDABLE_TOOL), "session 2's tool exclusion left the tool active for it"
