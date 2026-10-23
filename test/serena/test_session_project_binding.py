"""Tests binding each call of a session to the project that session holds.

These pin the requirements of ``pg://serena/session/project``:

* ``order`` -- a call runs against the project its session holds when the call *starts*, so a call a session
  issues while its own ``activate_project`` is in flight runs against the project that activation binds
  (bug://serena/project/activation/session/isolation);
* ``separation`` -- a call of one session runs against a project that session activated, whatever another
  session activates before or while the call runs (bug://serena/project/active/session/race).

Every ordering is established on a gate (:class:`threading.Event`), never by a clock: a wait with a timeout
below only guards the suite against a hang and fails the test if the guarded event never happens.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx

_HANG_GUARD_SECONDS = 30.0
"""the longest any guarded event may take before the test fails instead of hanging"""


@dataclass
class _Gate:
    """
    A pair of events through which a test holds a tool call in flight.

    The call sets :attr:`entered` once it is running and then waits for :attr:`release`, so a test that has
    waited for :attr:`entered` knows the call is in flight and stays in flight until it sets :attr:`release`.
    """

    entered: threading.Event
    release: threading.Event

    @classmethod
    def closed(cls) -> "_Gate":
        return cls(entered=threading.Event(), release=threading.Event())

    @classmethod
    def open(cls) -> "_Gate":
        gate = cls.closed()
        gate.release.set()
        return gate

    def pass_through(self) -> None:
        """Marks the call as running and blocks it until the gate is released."""
        self.entered.set()
        assert self.release.wait(_HANG_GUARD_SECONDS), "the gate was never released"

    def await_entry(self) -> None:
        """Blocks the test until the gated call is running."""
        assert self.entered.wait(_HANG_GUARD_SECONDS), "the gated call never started"


class _GatedActivationTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    An activation that binds a given project to the calling session's slot once its gate is released.

    It assigns through :attr:`SerenaAgent._active_project`, the setter every real activation ends in, so the
    project lands in the per-session slot of the session the call is bound to.
    """

    def __init__(self, agent: SerenaAgent, project: Project, gate: _Gate) -> None:
        super().__init__(agent)
        self._project = project
        self._gate = gate

    def apply(self) -> str:
        self._gate.pass_through()
        self.agent._active_project = self._project
        return "activated"


class _ActiveRootTool(Tool):
    """A tool that needs an active project and answers with the root of the project its call runs against."""

    def apply(self) -> str:
        project = self.agent.get_active_project()
        assert project is not None
        return project.project_root


class _RelativeWriteTool(Tool):
    """A tool that needs an active project and writes a file at a path relative to that project's root."""

    def apply(self, relative_path: str) -> str:
        project = self.agent.get_active_project()
        assert project is not None
        Path(project.project_root, relative_path).write_text("written by its session\n")
        return project.project_root


def _project_at(root: Path | str, name: str) -> MagicMock:
    """
    :param root: the project root
    :param name: a name for the stand-in, shown in assertion messages
    :return: a stand-in project rooted at ``root``
    """
    project = MagicMock(name=name)
    project.project_root = str(root)
    return project


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    """A minimal agent holding no project, its tool-usage recording stubbed out."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    serena_agent = SerenaAgent(serena_config=config)
    serena_agent.record_tool_usage = lambda *args, **kwargs: None  # type: ignore[method-assign]
    yield serena_agent
    serena_agent.on_shutdown(timeout=0.5)


@pytest.fixture(autouse=True)
def _no_pipe_session() -> Iterator[None]:
    """Keeps the pipe transport's session id out of scope, so the forwarded session id keys every call."""
    token = _PIPE_SESSION_ID_VAR.set(None)
    yield
    _PIPE_SESSION_ID_VAR.reset(token)


def _active(tool: Tool, monkeypatch: pytest.MonkeyPatch) -> Tool:
    """
    :param tool: a tool of the test
    :param monkeypatch: the test's monkeypatch fixture
    :return: the tool, reported active whatever the agent's active tool set
    """
    monkeypatch.setattr(tool, "is_active", lambda: True)
    return tool


class TestCallRunsAgainstProjectHeldWhenItStarts:
    """pg://serena/session/project/order"""

    def test_call_queued_behind_activation_runs_against_activated_project(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A session holding X issues activate_project(A) and, while that activation is in flight, a call that
        resolves its project: the call runs after the activation on the session's executor and must run against
        A, the project its session holds when it starts -- not X, the project held when it arrived.
        """
        session = "cc-session-order"
        project_x = _project_at("/tmp/serena-order-X", "project-X")
        project_a = _project_at("/tmp/serena-order-A", "project-A")
        agent._active_projects_by_session[session] = project_x
        gate = _Gate.closed()
        activation = _active(_GatedActivationTool(agent, project_a, gate), monkeypatch)
        probe = _active(_ActiveRootTool(agent), monkeypatch)

        # hold the session's activation in flight, then issue the session's next call behind it
        activation_task = activation._issue_call(False, True, _make_mcp_ctx(forwarded_header=session))
        gate.await_entry()
        probe_task = probe._issue_call(False, True, _make_mcp_ctx(forwarded_header=session))

        # let the activation finish; the queued call then starts
        gate.release.set()
        assert activation_task.result(timeout=_HANG_GUARD_SECONDS) == "activated"
        root = probe_task.result(timeout=_HANG_GUARD_SECONDS)

        assert root == project_a.project_root, (
            f"a call issued after its session's activate_project ran against {root!r}, not the activated project {project_a.project_root!r}"
        )


class TestCallOfOneSessionIgnoresAnotherSessionsActivation:
    """pg://serena/session/project/separation"""

    def test_write_lands_under_own_project_after_other_session_activates(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Session 1 activates A, session 2 then activates B, and session 1's relative-path write lands under A."""
        root_a, root_b = tmp_path / "A", tmp_path / "B"
        root_a.mkdir()
        root_b.mkdir()
        project_a, project_b = _project_at(root_a, "project-A"), _project_at(root_b, "project-B")

        # session 1 activates A, then session 2 activates B
        for session, project in (("cc-session-1", project_a), ("cc-session-2", project_b)):
            activation = _active(_GatedActivationTool(agent, project, _Gate.open()), monkeypatch)
            assert activation.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=session), log_call=False) == "activated"

        # session 1 writes by relative path
        write = _active(_RelativeWriteTool(agent), monkeypatch)
        result = write.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header="cc-session-1"), log_call=False, relative_path="out.txt")

        assert result == str(root_a), f"session 1's write ran against {result!r}"
        assert (root_a / "out.txt").is_file(), "session 1's write did not land under its own project A"
        assert not (root_b / "out.txt").exists(), "session 1's write landed under session 2's project B"

    def test_write_lands_under_own_project_while_other_session_activates(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Session 1 holds A; session 2's activate_project(B) is held in flight while session 1's relative-path
        write runs to completion, and the write lands under A.
        """
        root_a, root_b = tmp_path / "A", tmp_path / "B"
        root_a.mkdir()
        root_b.mkdir()
        project_a, project_b = _project_at(root_a, "project-A"), _project_at(root_b, "project-B")
        activation_a = _active(_GatedActivationTool(agent, project_a, _Gate.open()), monkeypatch)
        assert activation_a.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header="cc-session-1"), log_call=False) == "activated"

        # hold session 2's activation of B in flight
        gate = _Gate.closed()
        activation_b = _active(_GatedActivationTool(agent, project_b, gate), monkeypatch)
        activation_b_task = activation_b._issue_call(False, True, _make_mcp_ctx(forwarded_header="cc-session-2"))
        gate.await_entry()

        # session 1 writes by relative path while session 2's activation is in flight
        write = _active(_RelativeWriteTool(agent), monkeypatch)
        result = write.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header="cc-session-1"), log_call=False, relative_path="out.txt")
        gate.release.set()
        assert activation_b_task.result(timeout=_HANG_GUARD_SECONDS) == "activated"

        assert result == str(root_a), f"session 1's write ran against {result!r}"
        assert (root_a / "out.txt").is_file(), "session 1's write did not land under its own project A"
        assert not (root_b / "out.txt").exists(), "session 1's write landed under session 2's project B"
