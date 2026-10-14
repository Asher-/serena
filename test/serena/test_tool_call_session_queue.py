"""
Tests of the tool-call task queue: the calls of each session run on a queue of that session's own, so a call of one
session is never delayed by a running task of another, and a call that exceeds its deadline is answered with a typed
timeout state naming the tool and the deadline while the next call of its session still runs.

Ordering is fixed by gates, never by the clock: a held call waits on its gate's ``release`` until the test sets it,
and every assertion about what was answered first is made while that gate is still unreleased. A deadline appears
only as the subject under test (the timeout cases) or as the bound on how long a failing run waits for an answer
that the fix makes immediate.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.mcp import SerenaMCPFactory
from serena.project import Project
from serena.tools.cursor_tools import CursorFindTool
from serena.tools.file_tools import StatTool
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject
from solidlsp.ls_config import Language

TTool = TypeVar("TTool", bound=Tool)


@dataclass
class _Gate:
    """
    A pair of events fixing the order of a held call: ``entered`` is set once the call's body runs, and the body then
    waits on ``release`` until the test sets it.
    """

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def hold(self) -> None:
        """Signals that the held body runs, then waits until the test releases it."""
        self.entered.set()
        self.release.wait()


@dataclass
class _ForwardedRequest:
    """The inbound HTTP request double carrying the multiplexer's forwarded headers."""

    headers: dict[str, str]


@dataclass
class _ForwardedRequestContext:
    """The FastMCP request-context double through which ``Tool.apply_ex`` reads the forwarded headers."""

    request: _ForwardedRequest


@dataclass
class _ForwardedMcpContext:
    """
    An ``mcp_ctx`` double for a call the multiplexer forwards on behalf of one client session: its
    ``X-Forwarded-Mcp-Session-Id`` is the session key ``Tool.apply_ex`` derives.
    """

    request_context: _ForwardedRequestContext
    session: Any = None

    @classmethod
    def for_session(cls, session_id: str) -> _ForwardedMcpContext:
        """
        :param session_id: the forwarded client session id
        :return: the context of a call forwarded on behalf of that session
        """
        return cls(_ForwardedRequestContext(_ForwardedRequest({"x-forwarded-mcp-session-id": session_id})))


class _GatedProbeTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """A tool whose call holds on a gate until the test releases it."""

    def __init__(self, agent: SerenaAgent, gate: _Gate) -> None:
        super().__init__(agent)
        self._gate = gate

    def apply(self) -> str:
        """
        Holds on the gate.

        :return: a fixed marker, once released
        """
        self._gate.hold()
        return "released"


class _ImmediateProbeTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """A tool whose call completes at once."""

    def apply(self) -> str:
        """
        Completes at once.

        :return: a fixed marker
        """
        return "ran"


class _ActivatingProbeTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """
    A tool whose call activates a project for its session, as ``activate_project`` and the self-heal do, and then waits
    for the project's language server manager, as the activation message does.
    """

    def __init__(self, agent: SerenaAgent, project: Project) -> None:
        super().__init__(agent)
        self._project = project

    def apply(self) -> str:
        """
        Activates the project and waits for its language server manager.

        :return: a fixed marker, once the language server manager is ready
        """
        self.agent._activate_project(self._project, update_active_modes=False, update_active_tools=False, record_explicit=False)
        self._project._lsm_ready_event.wait()
        return "language server ready"


class _GatedLanguageServer:
    """A language-server double whose symbol-tree request holds on a gate, as a server that never answers does."""

    def __init__(self, gate: _Gate) -> None:
        self._gate = gate

    def request_full_symbol_tree(self, within_relative_path: str | None = None) -> list:
        self._gate.hold()
        return []


class _SingleLanguageServerManager:
    """A language-server-manager double serving one language server."""

    def __init__(self, language_server: object) -> None:
        self._language_server = language_server

    def iter_language_servers(self) -> Iterator[object]:
        yield self._language_server

    def save_all_caches(self) -> None:
        pass

    def stop_all(self, save_cache: bool = False, timeout: float = 2.0) -> None:
        pass


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[SerenaAgent]:
    """A minimal agent with no active project, whose tool-usage recording is disabled."""
    a = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    monkeypatch.setattr(a, "record_tool_usage", lambda *args, **kwargs: None)
    yield a
    a.on_shutdown(timeout=0.5)


@pytest.fixture
def gate() -> Iterator[_Gate]:
    """A gate released at teardown: a held task's thread is not a daemon thread, so an unreleased one would keep the test process alive."""
    g = _Gate()
    yield g
    g.release.set()


def _activated(tool: TTool, monkeypatch: pytest.MonkeyPatch) -> TTool:
    """Marks the tool active, so its call runs its body rather than answering with the not-active error."""
    monkeypatch.setattr(tool, "is_active", lambda: True)
    return tool


def _project(root: Path, agent: SerenaAgent) -> Project:
    """
    :param root: the project root
    :param agent: the agent whose configuration the project uses
    :return: a project over the root, with no language server manager started
    """
    return Project(
        project_root=str(root),
        project_config=ProjectConfig(project_name=root.name, languages=[Language.PYTHON]),
        serena_config=agent.serena_config,
    )


def _bind_project(agent: SerenaAgent, session_id: str, root: Path) -> Project:
    """Binds a project over the root as the active project of the session, as an activation by that session does."""
    project = _project(root, agent)
    agent._active_projects_by_session[session_id] = project
    return project


def test_a_call_of_one_session_is_answered_while_a_task_of_another_session_is_held(
    agent: SerenaAgent, gate: _Gate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    While a call of session A holds on its gate, ``stat`` for session B is answered from B's own queue. The failing
    shape is one queue shared by every session: B's task waits behind A's held task and B is answered with a timeout.
    The calls are made through ``Tool.apply_ex`` on threads, as the pipe transport makes them.
    """
    agent.serena_config.tool_timeout = 10.0
    held = _activated(_GatedProbeTool(agent, gate), monkeypatch)
    stat = _activated(StatTool(agent), monkeypatch)
    (tmp_path / "probe.txt").write_text("probe\n", encoding="utf-8")
    _bind_project(agent, "session-b", tmp_path)

    held_results: list[str] = []
    held_call = threading.Thread(
        target=lambda: held_results.append(held.apply_ex(mcp_ctx=_ForwardedMcpContext.for_session("session-a")))
    )
    held_call.start()
    gate.entered.wait()

    stat_result = stat.apply_ex(relative_path="probe.txt", mcp_ctx=_ForwardedMcpContext.for_session("session-b"))
    released_before_stat_answered = gate.release.is_set()
    gate.release.set()
    held_call.join()

    assert not released_before_stat_answered
    assert stat_result.startswith("probe.txt: file"), stat_result
    assert held_results == ["released"]


def test_an_mcp_call_of_one_session_is_answered_while_a_task_of_another_session_is_held(
    agent: SerenaAgent, gate: _Gate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The same ordering through the MCP tools the server registers, on one event loop, as the streamable-HTTP transport
    runs them. Besides the shared queue, the failing shape includes a tool function that blocks the event loop while
    it waits, so that no request of any other session is served until the held call is answered.
    """
    agent.serena_config.tool_timeout = 10.0
    held = SerenaMCPFactory.make_mcp_tool(_activated(_GatedProbeTool(agent, gate), monkeypatch))
    stat = SerenaMCPFactory.make_mcp_tool(_activated(StatTool(agent), monkeypatch))
    (tmp_path / "probe.txt").write_text("probe\n", encoding="utf-8")
    _bind_project(agent, "session-b", tmp_path)

    async def scenario() -> tuple[str, bool, str]:
        held_call = asyncio.create_task(held.run({}, context=_ForwardedMcpContext.for_session("session-a")))  # type: ignore[arg-type]
        await asyncio.to_thread(gate.entered.wait)
        stat_result = await stat.run({"relative_path": "probe.txt"}, context=_ForwardedMcpContext.for_session("session-b"))  # type: ignore[arg-type]
        released_before_stat_answered = gate.release.is_set()
        gate.release.set()
        return stat_result, released_before_stat_answered, await held_call

    stat_result, released_before_stat_answered, held_result = asyncio.run(scenario())

    assert not released_before_stat_answered
    assert stat_result.startswith("probe.txt: file"), stat_result
    assert held_result == "released"


def test_a_call_past_its_deadline_returns_the_typed_timeout_and_the_next_call_of_its_session_runs(
    agent: SerenaAgent, gate: _Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A call of session A that holds on its gate past the deadline is answered with the typed timeout state naming the
    tool and the deadline, and the next call of session A runs to completion while the first is still held: the
    abandoned task no longer occupies the session's queue.
    """
    held = _activated(_GatedProbeTool(agent, gate), monkeypatch)
    immediate = _activated(_ImmediateProbeTool(agent), monkeypatch)
    session_a = _ForwardedMcpContext.for_session("session-a")

    agent.serena_config.tool_timeout = 2.0
    timed_out = held.apply_ex(mcp_ctx=session_a)
    held_at_deadline = gate.entered.is_set() and not gate.release.is_set()

    agent.serena_config.tool_timeout = 10.0
    next_result = immediate.apply_ex(mcp_ctx=session_a)
    released_before_next_answered = gate.release.is_set()

    assert held_at_deadline
    assert timed_out == "Error: tool 'gated_probe' timed out: it did not complete within its deadline of 2 s."
    assert next_result == "ran"
    assert not released_before_next_answered


def test_cursor_find_without_a_relative_path_returns_the_typed_timeout_while_the_language_server_is_held(
    agent: SerenaAgent, gate: _Gate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``cursor_find`` with no ``relative_path`` asks every language server for its full symbol tree. Against a language
    server that never answers, the call is answered at the deadline with the typed timeout state, not with an untyped
    ``TimeoutError`` that names neither the tool nor the deadline.
    """
    project = _bind_project(agent, "session-a", tmp_path)
    project.language_server_manager = _SingleLanguageServerManager(_GatedLanguageServer(gate))  # type: ignore[assignment]
    find = _activated(CursorFindTool(agent), monkeypatch)

    agent.serena_config.tool_timeout = 5.0
    result = find.apply_ex(
        name_path_pattern="anything", because="to observe the deadline", mcp_ctx=_ForwardedMcpContext.for_session("session-a")
    )

    assert gate.entered.is_set() and not gate.release.is_set()
    assert result == "Error: tool 'cursor_find' timed out: it did not complete within its deadline of 5 s."


def test_an_activation_within_a_session_call_does_not_queue_language_server_startup_behind_that_call(
    agent: SerenaAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An activation made inside a call of session A (``activate_project``, or the self-heal before a tool body) starts
    the project's language server manager without queueing it behind that call, so the call can wait for the manager,
    as the activation message does, and is answered before its deadline. Queued on the session's own queue, the start
    would wait for the very call that waits for it, and the call would be answered with a timeout.
    """
    project = _project(tmp_path, agent)
    monkeypatch.setattr(project, "create_language_server_manager", project._lsm_ready_event.set)
    activating = _activated(_ActivatingProbeTool(agent, project), monkeypatch)

    agent.serena_config.tool_timeout = 10.0
    result = activating.apply_ex(mcp_ctx=_ForwardedMcpContext.for_session("session-a"))

    assert result == "language server ready"
