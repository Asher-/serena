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
from serena.tools.file_tools import CreateTextFileTool
from test.serena.test_session_project_binding import _HANG_GUARD_SECONDS
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx

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

    def test_modes_set_by_another_session_leave_the_tools_a_session_derives_unchanged(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """Session 2 sets its modes; session 1, activating a project afterwards, derives its tools without session 2's modes."""
        sessions = _Sessions(agent)
        sessions.activate("cc-session-1", _project(tmp_path / "plain", "plain"))
        tools_before = sessions.run("cc-session-1", agent.get_active_tool_names)

        sessions.run("cc-session-2", lambda: agent.set_modes(["no-memories"]))
        sessions.activate("cc-session-1", _project(tmp_path / "plain-2", "plain-2"))

        assert sessions.run("cc-session-2", agent.get_active_tool_names) != tools_before, "session 2's modes left its own tools unchanged"
        assert sessions.run("cc-session-1", agent.get_active_tool_names) == tools_before, (
            "session 2's modes changed the tools session 1 derives"
        )

    def test_session_holding_no_project_is_answered_from_the_configuration(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """Session 1 holds a read_only project; session 2, holding none, has the editing tools the Serena configuration gives."""
        sessions = _Sessions(agent)
        sessions.activate("cc-session-1", _project(tmp_path / "read-only", "read-only", read_only=True))

        assert sessions.tool_is_active("cc-session-2", _EDITING_TOOL), "a session holding no project was answered from another's"

    def test_session_holding_no_project_is_answered_with_the_modes_of_the_configuration(self, agent: SerenaAgent) -> None:
        """Session 1 sets its modes; session 2, which has activated nothing, has the modes the Serena configuration gives."""
        sessions = _Sessions(agent)
        configured = sorted(mode.name for mode in agent._config_active_modes.get_modes())
        sessions.run("cc-session-1", lambda: agent.set_modes(["no-memories"]))

        assert sessions.run("cc-session-1", lambda: sorted(mode.name for mode in agent.get_active_modes())) != configured, (
            "session 1's set_modes left its own modes unchanged"
        )
        assert sessions.run("cc-session-2", lambda: sorted(mode.name for mode in agent.get_active_modes())) == configured, (
            "a session holding no project was answered with another session's modes"
        )

    def test_caller_outside_any_session_is_answered_from_the_single_slot(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """A caller outside any session activates a read_only project; a session holding a writable project keeps editing."""
        sessions = _Sessions(agent)
        sessions.activate("cc-session-1", _project(tmp_path / "writable", "writable"))
        agent._activate_project(_project(tmp_path / "read-only", "read-only", read_only=True))

        assert not agent.tool_is_active(_EDITING_TOOL), "the caller outside any session was not answered from its own read_only project"
        assert sessions.tool_is_active("cc-session-1", _EDITING_TOOL), "the single slot's read_only project disabled editing for session 1"


class TestToolCallsAnsweredFromOwnSessionTools:
    """pg://serena/session/tool/derivation"""

    def test_editing_call_of_each_session_is_admitted_by_its_own_project(self, agent: SerenaAgent, tmp_path: Path) -> None:
        """
        Through the tool-call path, session 1's create_text_file call writes into its writable project, while session 2's,
        whose project is read_only, is refused as inactive and writes nothing.
        """
        writable = _project(tmp_path / "writable", "writable")
        read_only = _project(tmp_path / "read-only", "read-only", read_only=True)
        sessions = _Sessions(agent)
        sessions.activate("cc-session-1", writable)
        sessions.activate("cc-session-2", read_only)
        tool = CreateTextFileTool(agent)

        written = tool._issue_call(False, True, _make_mcp_ctx(forwarded_header="cc-session-1"), relative_path="new.txt", content="x")
        refused = tool._issue_call(False, True, _make_mcp_ctx(forwarded_header="cc-session-2"), relative_path="new.txt", content="x")

        written_result = written.result(timeout=_HANG_GUARD_SECONDS)
        assert (Path(writable.project_root) / "new.txt").read_text() == "x", written_result
        refused_result = refused.result(timeout=_HANG_GUARD_SECONDS)
        assert refused_result.startswith(f"Error: Tool '{_EDITING_TOOL}' is not active"), refused_result
        assert not (Path(read_only.project_root) / "new.txt").exists(), "session 2's refused call wrote into its read_only project"
