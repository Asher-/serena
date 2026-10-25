"""Tests holding a separate session for every agent of a client.

These pin ``pg://serena/session/project/agent``: each call an agent issues -- a client's main conversation or any one
of its subagents -- runs against a project that agent itself activated, whatever project another agent of the same
client activates before the call arrives or while it runs (bug://serena/project/active/session/race).

Every subagent of one client reaches serena through one forwarded session id; what tells two of them apart is the
reserved argument ``_serena_agent_id`` a PreToolUse hook adds to each of the subagent's calls. A call without it is the
main conversation's.

Every ordering is established on a gate (:class:`threading.Event`), never by a clock: a wait with a timeout below only
guards the suite against a hang and fails the test if the guarded event never happens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaMCPFactory
from serena.project import Project
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject
from serena.util.session_project_roots import ExplicitProjectRootStore
from test.serena.test_session_project_binding import (
    _HANG_GUARD_SECONDS,
    _active,
    _Gate,
    _GatedActivationTool,
    _no_pipe_session,  # noqa: F401 -- autouse fixture, imported so it applies to this module
    _project_at,
    _RelativeWriteTool,
)
from test.serena.test_session_project_binding import (
    agent as _agent_fixture,
)
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx, _RequiresProjectTool

agent = _agent_fixture
"""the fixture of the binding tests, bound here under its own name so pytest serves it to this module's tests"""

_AGENT_ID_ARGUMENT = "_serena_agent_id"
"""the reserved argument that carries the calling subagent's agent id"""

_CLIENT_SESSION = "cc-session-1"
"""the one forwarded session id every agent of the client arrives under"""


class _ArgumentRecordingTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    A tool that records the keyword arguments its apply receives.
    """

    def __init__(self, agent: SerenaAgent) -> None:
        super().__init__(agent)
        self.received: dict[str, Any] | None = None

    def apply(self, relative_path: str = "", **kwargs: Any) -> str:
        """
        Records the arguments of the call.

        :param relative_path: a path the call names
        """
        self.received = {"relative_path": relative_path, **kwargs}
        return "recorded"


class _PublishedRelativeWriteTool(_RelativeWriteTool):
    """
    A relative-path write tool documented as a published MCP tool must be.
    """

    def apply(self, relative_path: str) -> str:
        """
        Writes a file at a path relative to the calling session's project.

        :param relative_path: the path of the file to write
        """
        return super().apply(relative_path)


class _RecordingActivationTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    An activation that binds a given project to the calling session through :meth:`SerenaAgent._activate_project`, which
    records it as that session's explicit activation in the store a restarted daemon restores from.
    """

    def __init__(self, agent: SerenaAgent, project: Project) -> None:
        super().__init__(agent)
        self._project = project

    def apply(self) -> str:
        self.agent._activate_project(self._project, update_active_modes=False, update_active_tools=False)
        return "activated"


def _make_roots(tmp_path: Path, *names: str) -> list[Path]:
    """
    :return: one new directory under ``tmp_path`` per name
    """
    roots = [tmp_path / name for name in names]
    for root in roots:
        root.mkdir()
    return roots


def _activatable_project_at(root: Path, name: str) -> MagicMock:
    """
    :param root: the project root
    :param name: the project's name
    :return: a stand-in project rooted at ``root`` that :meth:`SerenaAgent._activate_project` accepts
    """
    project = _project_at(root, name)
    project.project_name = name
    project.project_config.language_backend = None
    return project


class TestCallOfOneAgentIgnoresAnotherAgentsActivation:
    """pg://serena/session/project/agent"""

    def test_write_lands_under_own_project_after_other_agent_activates(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Agent A1 activates P1, agent A2 of the same client then activates P2, and A1's relative-path write lands under P1."""
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        project_1, project_2 = _project_at(root_1, "project-P1"), _project_at(root_2, "project-P2")

        # agent A1 activates P1, then agent A2 activates P2, both under the client's one forwarded session id
        for agent_id, project in (("A1", project_1), ("A2", project_2)):
            activation = _active(_GatedActivationTool(agent, project, _Gate.open()), monkeypatch)
            result = activation.apply_ex(
                mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **{_AGENT_ID_ARGUMENT: agent_id}
            )
            assert result == "activated", f"agent {agent_id}'s activation answered {result!r}"

        # agent A1 writes by relative path
        write = _active(_RelativeWriteTool(agent), monkeypatch)
        result = write.apply_ex(
            mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, relative_path="out.txt", **{_AGENT_ID_ARGUMENT: "A1"}
        )

        assert result == str(root_1), f"agent A1's write ran against {result!r}"
        assert (root_1 / "out.txt").is_file(), "agent A1's write did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "agent A1's write landed under agent A2's project P2"

    def test_write_lands_under_own_project_while_other_agent_activates(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Agent A1 holds P1; agent A2's activate_project(P2) is held in flight while A1's relative-path write runs to
        completion, and the write lands under P1.
        """
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        project_1, project_2 = _project_at(root_1, "project-P1"), _project_at(root_2, "project-P2")
        activation_1 = _active(_GatedActivationTool(agent, project_1, _Gate.open()), monkeypatch)
        result = activation_1.apply_ex(
            mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **{_AGENT_ID_ARGUMENT: "A1"}
        )
        assert result == "activated", f"agent A1's activation answered {result!r}"

        # hold agent A2's activation of P2 in flight
        gate = _Gate.closed()
        activation_2 = _active(_GatedActivationTool(agent, project_2, gate), monkeypatch)
        activation_2_task = activation_2._issue_call(
            False, True, _make_mcp_ctx(forwarded_header=_CLIENT_SESSION), **{_AGENT_ID_ARGUMENT: "A2"}
        )
        gate.await_entry()

        # agent A1 writes by relative path while agent A2's activation is in flight
        write = _active(_RelativeWriteTool(agent), monkeypatch)
        result = write.apply_ex(
            mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, relative_path="out.txt", **{_AGENT_ID_ARGUMENT: "A1"}
        )
        gate.release.set()
        assert activation_2_task.result(timeout=_HANG_GUARD_SECONDS) == "activated"

        assert result == str(root_1), f"agent A1's write ran against {result!r}"
        assert (root_1 / "out.txt").is_file(), "agent A1's write did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "agent A1's write landed under agent A2's project P2"


class TestCallWithoutAgentIdIsTheClientSession:
    """pg://serena/session/project/agent: the main conversation's calls carry no agent id"""

    def test_call_without_agent_id_is_answered_from_the_forwarded_session_slot(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The main conversation activates P0, agents A1 and A2 then activate P1 and P2, and the main conversation's
        relative-path write lands under P0, from the slot keyed on the forwarded session id alone.
        """
        root_0, root_1, root_2 = _make_roots(tmp_path, "P0", "P1", "P2")
        projects = [_project_at(root, f"project-{root.name}") for root in (root_0, root_1, root_2)]

        # the main conversation activates P0, then agents A1 and A2 activate P1 and P2
        for agent_arguments, project in (
            ({}, projects[0]),
            ({_AGENT_ID_ARGUMENT: "A1"}, projects[1]),
            ({_AGENT_ID_ARGUMENT: "A2"}, projects[2]),
        ):
            activation = _active(_GatedActivationTool(agent, project, _Gate.open()), monkeypatch)
            result = activation.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **agent_arguments)
            assert result == "activated", f"the activation of {project.project_name} answered {result!r}"

        # the main conversation writes by relative path
        write = _active(_RelativeWriteTool(agent), monkeypatch)
        result = write.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, relative_path="out.txt")

        assert result == str(root_0), f"the main conversation's write ran against {result!r}"
        assert (root_0 / "out.txt").is_file(), "the main conversation's write did not land under its own project P0"
        assert not (root_1 / "out.txt").exists() and not (root_2 / "out.txt").exists(), (
            "the main conversation's write landed under a subagent's project"
        )
        assert {_CLIENT_SESSION, f"{_CLIENT_SESSION}/agent/A1", f"{_CLIENT_SESSION}/agent/A2"} <= set(agent._active_projects_by_session)


class TestAgentIdArgumentIsReserved:
    """the reserved argument is published by no tool and reaches no tool's apply"""

    def test_agent_id_reaches_no_apply_and_no_schema(self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        A call carrying the agent id runs the tool's apply without it, and neither the tool's published schema nor that of
        any tool serena can publish lists it.
        """
        tool = _active(_ArgumentRecordingTool(agent), monkeypatch)

        result = tool.apply_ex(
            mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, relative_path="x", **{_AGENT_ID_ARGUMENT: "A1"}
        )

        assert result == "recorded", f"the call answered {result!r}"
        assert tool.received == {"relative_path": "x"}, f"apply received {tool.received!r}"
        assert _AGENT_ID_ARGUMENT not in SerenaMCPFactory.make_mcp_tool(tool).parameters["properties"]

        # no tool serena can publish lists the reserved argument in its schema
        publishing = [
            published.get_name_from_cls()
            for published in agent._all_tools.values()
            if _AGENT_ID_ARGUMENT in SerenaMCPFactory.make_mcp_tool(published).parameters["properties"]
        ]
        assert publishing == [], f"tools whose published schema lists the reserved argument: {publishing}"


class TestAgentIdThroughTheMcpTool:
    """pg://serena/session/project/agent through the MCP tool serena publishes, whose arguments FastMCP validates"""

    def test_write_through_the_mcp_tool_lands_under_own_agents_project(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Agents A1 and A2 activate P1 and P2, and A1's relative-path write issued through the published MCP tool, with
        the agent id among its raw arguments, lands under P1.
        """
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        project_1, project_2 = _project_at(root_1, "project-P1"), _project_at(root_2, "project-P2")
        for agent_id, project in (("A1", project_1), ("A2", project_2)):
            activation = _active(_GatedActivationTool(agent, project, _Gate.open()), monkeypatch)
            result = activation.apply_ex(
                mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **{_AGENT_ID_ARGUMENT: agent_id}
            )
            assert result == "activated", f"agent {agent_id}'s activation answered {result!r}"

        # agent A1 writes through the published MCP tool, the agent id among the raw arguments
        mcp_tool = SerenaMCPFactory.make_mcp_tool(_active(_PublishedRelativeWriteTool(agent), monkeypatch))
        result = asyncio.run(
            mcp_tool.run({"relative_path": "out.txt", _AGENT_ID_ARGUMENT: "A1"}, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION))
        )

        assert result == str(root_1), f"agent A1's write through the MCP tool ran against {result!r}"
        assert (root_1 / "out.txt").is_file(), "agent A1's write through the MCP tool did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "agent A1's write through the MCP tool landed under agent A2's project P2"


class TestAgentRootSurvivesDaemonRestart:
    """pg://serena/session/project/restoration for each agent of a client (pg://serena/session/project/agent)"""

    def test_restarted_daemon_restores_an_agents_root_for_that_agent_alone(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Agent A1 activates P1 and the daemon restarts. Under the client's one forwarded session id, the calls of the main
        conversation and of agent A2 are refused with nothing restored for them, and agent A1's call is answered from P1,
        restored for A1's session alone.
        """
        (root_1,) = _make_roots(tmp_path, "P1")
        store_path = str(tmp_path / "session_project_roots.json")

        # agent A1 activates P1, the activation recorded in the store a restarted daemon reads
        agent._explicit_project_root_store = ExplicitProjectRootStore(store_path)
        activation = _active(_RecordingActivationTool(agent, _activatable_project_at(root_1, "project-P1")), monkeypatch)
        result = activation.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **{_AGENT_ID_ARGUMENT: "A1"})
        assert result == "activated", f"agent A1's activation answered {result!r}"

        # the restart: a second agent, holding nothing in memory, over the same store
        restarted = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
        try:
            restarted.record_tool_usage = lambda *args, **kwargs: None  # type: ignore[method-assign]
            restarted._explicit_project_root_store = ExplicitProjectRootStore(store_path)
            assert restarted._explicit_project_roots_by_session == {}, "a restarted daemon starts with no in-memory record"

            restored: list[tuple[str, str | int | None]] = []

            def restore(root: str, **kwargs: Any) -> bool:
                restored.append((root, _SESSION_KEY_VAR.get(None)))
                restarted._active_project = _project_at(root, f"restored-{Path(root).name}")
                return True

            monkeypatch.setattr(restarted, "activate_project_from_path_or_name", restore)
            tool = _active(_RequiresProjectTool(restarted), monkeypatch)

            # the main conversation and agent A2 call first, then agent A1
            results: dict[str, str] = {}
            for caller, agent_arguments in (
                ("the main conversation", {}),
                ("agent A2", {_AGENT_ID_ARGUMENT: "A2"}),
                ("agent A1", {_AGENT_ID_ARGUMENT: "A1"}),
            ):
                results[caller] = tool.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **agent_arguments)
        finally:
            restarted.on_shutdown(timeout=0.5)

        for caller in ("the main conversation", "agent A2"):
            assert "No active project" in results[caller], f"{caller}'s call after the restart answered {results[caller]!r}"
        assert results["agent A1"] == _RequiresProjectTool.RAN, f"agent A1's call after the restart answered {results['agent A1']!r}"
        assert restored == [(str(root_1), f"{_CLIENT_SESSION}/agent/A1")], f"the restarted daemon restored {restored!r}"
