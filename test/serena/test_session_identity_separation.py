"""Tests holding a separate session for every Claude Code session that reaches serena through one client connection.

These pin ``pg://serena/session/project/separation`` across session identifiers: each call of a Claude Code session runs
against a project that session itself activated, whatever project another session activates before the call arrives or
while it runs (bug://serena/project/activation/session/sharing).

The Claude desktop app sends the main-thread calls of more than one of its sessions through one connection, so they all
arrive under one forwarded session id; what tells two of them apart is the reserved argument ``_serena_session_id`` a
PreToolUse hook adds to each call, carrying the calling session's own session_id. A subagent's call carries its parent
session's identifier together with its own ``_serena_agent_id``.

Every ordering is established on a gate (:class:`threading.Event`), never by a clock: a wait with a timeout below only
guards the suite against a hang and fails the test if the guarded event never happens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaMCPFactory
from serena.util.session_project_roots import ExplicitProjectRootStore
from test.serena.test_session_agent_separation import (
    _activatable_project_at,
    _ArgumentRecordingTool,
    _make_roots,
    _PublishedRelativeWriteTool,
    _RecordingActivationTool,
)
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

_SESSION_ID_ARGUMENT = "_serena_session_id"
"""the reserved argument that carries the calling Claude Code session's session_id"""

_AGENT_ID_ARGUMENT = "_serena_agent_id"
"""the reserved argument that carries the calling subagent's agent id"""

_CLIENT_SESSION = "cc-session-1"
"""the one forwarded session id every session of the client arrives under"""


def _activate(agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, project: Any, **caller_arguments: str) -> None:
    """
    Activates a project for one caller under the client's forwarded session id, through an ungated activation.

    :param agent: the agent the call is issued to
    :param monkeypatch: the test's monkeypatch fixture
    :param project: the project to activate
    :param caller_arguments: the reserved arguments that identify the caller
    """
    activation = _active(_GatedActivationTool(agent, project, _Gate.open()), monkeypatch)
    result = activation.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **caller_arguments)
    assert result == "activated", f"the activation of {project.project_name} for {caller_arguments!r} answered {result!r}"


def _write(agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, **caller_arguments: str) -> str:
    """
    Writes ``out.txt`` by relative path for one caller under the client's forwarded session id.

    :param agent: the agent the call is issued to
    :param monkeypatch: the test's monkeypatch fixture
    :param caller_arguments: the reserved arguments that identify the caller
    :return: the root the write ran against
    """
    write = _active(_RelativeWriteTool(agent), monkeypatch)
    return write.apply_ex(
        mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, relative_path="out.txt", **caller_arguments
    )


class TestCallOfOneSessionIgnoresAnotherSessionsActivation:
    """pg://serena/session/project/separation across sessions that share one forwarded session id"""

    def test_write_lands_under_own_project_after_other_session_activates(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Session S1 activates P1, session S2 then activates P2, and S1's relative-path write lands under P1."""
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")

        # session S1 activates P1, then session S2 activates P2, both under the client's one forwarded session id
        _activate(agent, monkeypatch, _project_at(root_1, "project-P1"), **{_SESSION_ID_ARGUMENT: "S1"})
        _activate(agent, monkeypatch, _project_at(root_2, "project-P2"), **{_SESSION_ID_ARGUMENT: "S2"})

        # session S1 writes by relative path
        result = _write(agent, monkeypatch, **{_SESSION_ID_ARGUMENT: "S1"})

        assert result == str(root_1), f"session S1's write ran against {result!r}"
        assert (root_1 / "out.txt").is_file(), "session S1's write did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "session S1's write landed under session S2's project P2"

    def test_write_lands_under_own_project_while_other_session_activates(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Session S1 holds P1; session S2's activate_project(P2) is held in flight while S1's relative-path write runs to
        completion, and the write lands under P1.
        """
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        _activate(agent, monkeypatch, _project_at(root_1, "project-P1"), **{_SESSION_ID_ARGUMENT: "S1"})

        # hold session S2's activation of P2 in flight
        gate = _Gate.closed()
        activation_2 = _active(_GatedActivationTool(agent, _project_at(root_2, "project-P2"), gate), monkeypatch)
        activation_2_task = activation_2._issue_call(
            False, True, _make_mcp_ctx(forwarded_header=_CLIENT_SESSION), **{_SESSION_ID_ARGUMENT: "S2"}
        )
        gate.await_entry()

        # session S1 writes by relative path while session S2's activation is in flight
        result = _write(agent, monkeypatch, **{_SESSION_ID_ARGUMENT: "S1"})
        gate.release.set()
        assert activation_2_task.result(timeout=_HANG_GUARD_SECONDS) == "activated"

        assert result == str(root_1), f"session S1's write ran against {result!r}"
        assert (root_1 / "out.txt").is_file(), "session S1's write did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "session S1's write landed under session S2's project P2"


class TestSubagentOfASessionIsItsOwnSession:
    """pg://serena/session/project/agent within one Claude Code session"""

    def test_subagent_activation_leaves_its_parent_sessions_project(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Session S1 activates P1, its subagent A1 then activates P2, and S1's relative-path write lands under P1."""
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")

        # session S1 activates P1, then its subagent A1 activates P2
        _activate(agent, monkeypatch, _project_at(root_1, "project-P1"), **{_SESSION_ID_ARGUMENT: "S1"})
        _activate(agent, monkeypatch, _project_at(root_2, "project-P2"), **{_SESSION_ID_ARGUMENT: "S1", _AGENT_ID_ARGUMENT: "A1"})

        # session S1 writes by relative path
        result = _write(agent, monkeypatch, **{_SESSION_ID_ARGUMENT: "S1"})

        assert result == str(root_1), f"session S1's write ran against {result!r}"
        assert not (root_2 / "out.txt").exists(), "session S1's write landed under its subagent's project P2"
        assert {f"{_CLIENT_SESSION}/session/S1", f"{_CLIENT_SESSION}/session/S1/agent/A1"} <= set(agent._active_projects_by_session)


class TestCallWithoutIdentifiersIsTheClientSession:
    """the session key of a call that carries neither reserved argument, or only the agent id, is as before"""

    def test_call_without_identifiers_is_answered_from_the_forwarded_session_slot(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A call carrying no identifier activates P0, sessions S1 and S2 then activate P1 and P2, and a relative-path write
        carrying no identifier lands under P0, from the slot keyed on the forwarded session id alone.
        """
        root_0, root_1, root_2 = _make_roots(tmp_path, "P0", "P1", "P2")

        # a call without identifiers activates P0, then sessions S1 and S2 activate P1 and P2
        _activate(agent, monkeypatch, _project_at(root_0, "project-P0"))
        _activate(agent, monkeypatch, _project_at(root_1, "project-P1"), **{_SESSION_ID_ARGUMENT: "S1"})
        _activate(agent, monkeypatch, _project_at(root_2, "project-P2"), **{_SESSION_ID_ARGUMENT: "S2"})

        # a call without identifiers writes by relative path
        result = _write(agent, monkeypatch)

        assert result == str(root_0), f"the write without identifiers ran against {result!r}"
        assert not (root_1 / "out.txt").exists() and not (root_2 / "out.txt").exists(), (
            "the write without identifiers landed under a session's project"
        )
        assert {_CLIENT_SESSION, f"{_CLIENT_SESSION}/session/S1", f"{_CLIENT_SESSION}/session/S2"} <= set(agent._active_projects_by_session)

    def test_call_with_only_an_agent_id_keeps_its_agent_key(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A call carrying only the agent id A1 is keyed ``<forwarded session id>/agent/A1``."""
        (root_1,) = _make_roots(tmp_path, "P1")

        _activate(agent, monkeypatch, _project_at(root_1, "project-P1"), **{_AGENT_ID_ARGUMENT: "A1"})

        assert agent._active_projects_by_session[f"{_CLIENT_SESSION}/agent/A1"].project_root == str(root_1)


class TestSessionIdArgumentIsReserved:
    """the reserved argument is published by no tool and reaches no tool's apply"""

    def test_session_id_reaches_no_apply_and_no_schema(self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        A call carrying the session id runs the tool's apply without it, and no tool serena can publish lists it in its
        schema.
        """
        tool = _active(_ArgumentRecordingTool(agent), monkeypatch)

        result = tool.apply_ex(
            mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, relative_path="x", **{_SESSION_ID_ARGUMENT: "S1"}
        )

        assert result == "recorded", f"the call answered {result!r}"
        assert tool.received == {"relative_path": "x"}, f"apply received {tool.received!r}"

        # no tool serena can publish lists the reserved argument in its schema
        publishing = [
            published.get_name_from_cls()
            for published in agent._all_tools.values()
            if _SESSION_ID_ARGUMENT in SerenaMCPFactory.make_mcp_tool(published).parameters["properties"]
        ]
        assert publishing == [], f"tools whose published schema lists the reserved argument: {publishing}"


class TestSessionIdThroughTheMcpTool:
    """pg://serena/session/project/separation through the MCP tool serena publishes, whose arguments FastMCP validates"""

    def test_write_through_the_mcp_tool_lands_under_own_sessions_project(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Sessions S1 and S2 activate P1 and P2, and S1's relative-path write issued through the published MCP tool, with
        the session id among its raw arguments, lands under P1.
        """
        root_1, root_2 = _make_roots(tmp_path, "P1", "P2")
        _activate(agent, monkeypatch, _project_at(root_1, "project-P1"), **{_SESSION_ID_ARGUMENT: "S1"})
        _activate(agent, monkeypatch, _project_at(root_2, "project-P2"), **{_SESSION_ID_ARGUMENT: "S2"})

        # session S1 writes through the published MCP tool, the session id among the raw arguments
        mcp_tool = SerenaMCPFactory.make_mcp_tool(_active(_PublishedRelativeWriteTool(agent), monkeypatch))
        result = asyncio.run(
            mcp_tool.run({"relative_path": "out.txt", _SESSION_ID_ARGUMENT: "S1"}, context=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION))
        )

        assert result == str(root_1), f"session S1's write through the MCP tool ran against {result!r}"
        assert (root_1 / "out.txt").is_file(), "session S1's write through the MCP tool did not land under its own project P1"
        assert not (root_2 / "out.txt").exists(), "session S1's write through the MCP tool landed under session S2's project P2"


class TestSessionRootSurvivesDaemonRestart:
    """pg://serena/session/project/restoration for each Claude Code session of a client"""

    def test_restarted_daemon_restores_a_sessions_root_for_that_session_alone(
        self, agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Session S1 activates P1 and the daemon restarts. Under the client's one forwarded session id, the calls of session
        S2 and of a caller without identifiers are refused with nothing restored for them, and session S1's call is
        answered from P1, restored for S1's session alone.
        """
        (root_1,) = _make_roots(tmp_path, "P1")
        store_path = str(tmp_path / "session_project_roots.json")

        # session S1 activates P1, the activation recorded in the store a restarted daemon reads
        agent._explicit_project_root_store = ExplicitProjectRootStore(store_path)
        activation = _active(_RecordingActivationTool(agent, _activatable_project_at(root_1, "project-P1")), monkeypatch)
        result = activation.apply_ex(
            mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **{_SESSION_ID_ARGUMENT: "S1"}
        )
        assert result == "activated", f"session S1's activation answered {result!r}"

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

            # a caller without identifiers and session S2 call first, then session S1
            results: dict[str, str] = {}
            for caller, caller_arguments in (
                ("the caller without identifiers", {}),
                ("session S2", {_SESSION_ID_ARGUMENT: "S2"}),
                ("session S1", {_SESSION_ID_ARGUMENT: "S1"}),
            ):
                results[caller] = tool.apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=_CLIENT_SESSION), log_call=False, **caller_arguments)
        finally:
            restarted.on_shutdown(timeout=0.5)

        for caller in ("the caller without identifiers", "session S2"):
            assert "No active project" in results[caller], f"{caller}'s call after the restart answered {results[caller]!r}"
        assert results["session S1"] == _RequiresProjectTool.RAN, f"session S1's call after the restart answered {results['session S1']!r}"
        assert restored == [(str(root_1), f"{_CLIENT_SESSION}/session/S1")], f"the restarted daemon restored {restored!r}"
