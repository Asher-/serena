"""unit tests for the eviction of every per-session slot under a forwarded session id (pg://serena/session/eviction).

serena keys a tier-2 slot on the forwarded session id, optionally followed by ``/session/<id>`` and ``/agent/<id>``. When
the multiplexer reports that a forwarded session has ended, by posting /sessions/end to the streamable-http listener
``start-mcp-server`` builds, every slot whose key equals that id or begins with that id followed by ``/`` is evicted, and
no other slot is touched. The tests wait on the exit of each bridge process and on the stop of each language-server
manager; the timeout only bounds how long a broken implementation can hang a test.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import psutil
import pytest
from click.testing import CliRunner
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.cli import TopLevelCommands
from serena.config.serena_config import LanguageBackend, ProjectConfig, SerenaConfig
from serena.cursor import StructuralCursorState
from serena.ls_manager import LanguageServerManager
from serena.project import Project
from serena.tools import Tool
from serena.tools.tools_base import SESSION_ID_ARGUMENT
from solidlsp.ls_config import Language

_GATE_TIMEOUT = 10.0
"""the seconds a test waits on a bridge process's exit before failing; a bound on a hang, never the ordering under test"""

_BRIDGE_PROCESS_NAME = "serena-go-bridge"
_GO_FILE = "hello.go"

_FORWARDED = "f" * 64
"""the forwarded session id the multiplexer reports ended"""

_OTHER_FORWARDED = "e" * 64
"""a forwarded session id that stays live"""

_UNDER_FORWARDED = (_FORWARDED, f"{_FORWARDED}/session/S1", f"{_FORWARDED}/session/S1/agent/A1")
"""the slot keys that lie under the ended forwarded session id"""

_LIVE_KEY = f"{_OTHER_FORWARDED}/session/S2"
"""a slot key under a forwarded session id that stays live"""

_SESSION_END_PATH = "/sessions/end"
"""the path on which the multiplexer reports an ended forwarded session"""

_LISTENER_BASE_URL = "http://127.0.0.1:8000"
"""the URL the tests address the listener at, whose host the listener's DNS-rebinding protection admits"""

_MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
"""the headers of a request on the listener's streamable-http MCP endpoint"""

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "brain-mcp-multiplexer", "version": "0"}},
}
"""the request with which the multiplexer opens an MCP session with serena"""


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    a = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False, log_level=logging.ERROR))
    yield a
    a.on_shutdown(timeout=0.1)


@pytest.fixture
def make_go_project(agent: SerenaAgent, work_dir: Path) -> Callable[[str], Project]:
    """
    :return: a factory building, under its own directory, a project holding ``hello.go`` and a language-server manager
        double, so the project never starts a language server; the double's ``stopped`` event is set when it is stopped
    """

    def make(name: str) -> Project:
        root = work_dir / name
        root.mkdir()
        (root / _GO_FILE).write_text("package main\n\nfunc Hello() {}\n", encoding="utf-8")
        project_config = ProjectConfig(project_name=name, languages=[Language.GO], language_backend=LanguageBackend.LSP)
        project = Project(project_root=str(root), project_config=project_config, serena_config=agent.serena_config)
        manager = MagicMock(spec=LanguageServerManager)
        manager.stopped = threading.Event()
        manager.stop_all.side_effect = lambda *args, **kwargs: manager.stopped.set()
        project.language_server_manager = manager
        return project

    return make


@pytest.fixture
def listener(agent: SerenaAgent) -> FastMCP:
    """
    :return: the server ``start-mcp-server --transport streamable-http`` builds around ``agent``, with its run suppressed so
        a test serves the server's streamable-http app in process
    """
    server = FastMCP(name="Serena")
    root_handlers = list(logging.root.handlers)
    root_level = logging.root.level
    with (
        patch("serena.cli.SerenaMCPFactory") as factory_cls,
        patch("serena.util.watchdog.start_wedge_watchdog"),
        patch.object(server, "run") as run,
    ):
        factory_cls.return_value.agent = agent
        factory_cls.return_value.create_mcp_server.return_value = server
        result = CliRunner().invoke(TopLevelCommands.start_mcp_server, ["--transport", "streamable-http"])

    # the log handlers start-mcp-server installs on the root logger, removed so they do not outlive the test
    for handler in [handler for handler in logging.root.handlers if handler not in root_handlers]:
        logging.root.removeHandler(handler)
        handler.close()
    logging.root.setLevel(root_level)

    assert result.exit_code == 0, repr(result.exception)
    run.assert_called_once_with(transport="streamable-http")
    return server


def _bridge_pids() -> set[int]:
    """
    :return: the process ids of this process's serena-go-bridge children
    """
    return {child.pid for child in psutil.Process().children() if child.name() == _BRIDGE_PROCESS_NAME}


def _miss(*args: object, **kwargs: object) -> None:
    """
    Raises a fresh error on every call, as a language server that knows no such symbol does; a single shared error
    instance would keep the traceback of its last raise, and with it the cursor manager that caught it, alive
    """
    raise ValueError("no language-server symbol")


def _open_go_cursor(agent: SerenaAgent, session_key: str, project: Project) -> set[int]:
    """
    Activates ``project`` in the session ``session_key`` and opens a structural cursor on ``hello.go`` there.

    :return: the process ids of the serena-go-bridge processes the cursor started
    """
    bridges_before = _bridge_pids()

    def run() -> None:
        _SESSION_KEY_VAR.set(session_key)
        agent._activate_project(project, update_active_modes=False, update_active_tools=False)
        # no language server knows the symbol, so the cursor falls through to the Go structural backend
        with patch("serena.cursor.LanguageServerSymbolRetriever") as retriever_cls:
            retriever_cls.return_value.find_unique.side_effect = _miss
            _cursor_id, state = agent.get_cursor_manager().start_cursor("Hello", relative_path=_GO_FILE)
        assert isinstance(state, StructuralCursorState)

    contextvars.copy_context().run(run)
    started = _bridge_pids() - bridges_before
    assert started, f"the structural Go cursor of {session_key} started no serena-go-bridge process"
    return started


def _manager_double(project: Project) -> MagicMock:
    """
    :return: the language-server manager double ``make_go_project`` attached to ``project``
    """
    return cast(MagicMock, project.language_server_manager)


def _client(server: FastMCP) -> TestClient:
    """
    :return: a client of ``server``'s streamable-http app, addressing it at a host its DNS-rebinding protection admits
    """
    return TestClient(server.streamable_http_app(), base_url=_LISTENER_BASE_URL)


def _forwarded_call_context(forwarded_session_id: str) -> MagicMock:
    """
    :return: the MCP context of a tool call the multiplexer forwards on behalf of ``forwarded_session_id``
    """
    ctx = MagicMock()
    ctx.session.client_params = None
    ctx.request_context.request.headers = {"x-forwarded-mcp-session-id": forwarded_session_id}
    return ctx


class _ActiveProjectRootTool(Tool):
    """a tool that requires an active project and answers with the root of the project its call runs on"""

    def apply(self) -> str:
        return self.agent.get_active_project_or_raise().project_root


class TestEvictionUnderAForwardedSessionId:
    """verifies that ending a forwarded session evicts exactly the slots that lie under its id."""

    def test_slots_under_the_ended_id_are_evicted_and_their_bridges_stop(
        self, agent: SerenaAgent, make_go_project: Callable[[str], Project]
    ) -> None:
        ended_bridges: set[int] = set()
        for index, key in enumerate(_UNDER_FORWARDED):
            ended_bridges |= _open_go_cursor(agent, key, make_go_project(f"ended_{index}"))
        live_bridges = _open_go_cursor(agent, _LIVE_KEY, make_go_project("live"))

        # the bridge processes are taken before eviction, since one that has already exited can no longer be looked up
        ended = [psutil.Process(pid) for pid in ended_bridges]
        evicted = agent.evict_forwarded_session(_FORWARDED)

        assert sorted(evicted) == sorted(_UNDER_FORWARDED)
        for key in _UNDER_FORWARDED:
            assert key not in agent._cursor_managers_by_session
            assert key not in agent._active_projects_by_session
        _gone, alive = psutil.wait_procs(ended, timeout=_GATE_TIMEOUT)
        assert alive == [], f"serena-go-bridge processes still running after their slots were evicted: {[p.pid for p in alive]}"
        assert _LIVE_KEY in agent._cursor_managers_by_session
        assert _LIVE_KEY in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in live_bridges)

    def test_a_key_sharing_the_ids_characters_without_a_separator_is_kept(
        self, agent: SerenaAgent, make_go_project: Callable[[str], Project]
    ) -> None:
        lookalike = f"{_FORWARDED}0/session/S3"
        _open_go_cursor(agent, _FORWARDED, make_go_project("ended"))
        lookalike_bridges = _open_go_cursor(agent, lookalike, make_go_project("lookalike"))

        evicted = agent.evict_forwarded_session(_FORWARDED)

        assert evicted == [_FORWARDED]
        assert lookalike in agent._cursor_managers_by_session
        assert lookalike in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in lookalike_bridges)

    def test_the_durable_record_of_an_evicted_key_is_kept(self, agent: SerenaAgent, make_go_project: Callable[[str], Project]) -> None:
        key = f"{_FORWARDED}/session/S1"
        project = make_go_project("recorded")
        _open_go_cursor(agent, key, project)
        agent._explicit_project_root_store.set(key, project.project_root)

        agent.evict_forwarded_session(_FORWARDED)

        assert key not in agent._active_projects_by_session
        assert agent._explicit_project_root_store.get(key) == project.project_root


class TestSessionEndRoute:
    """verifies the /sessions/end route on the streamable-http listener ``start-mcp-server`` builds."""

    def test_ending_a_forwarded_session_evicts_its_slots_and_releases_their_resources(
        self, agent: SerenaAgent, listener: FastMCP, make_go_project: Callable[[str], Project]
    ) -> None:
        ended_projects = [make_go_project(f"ended_{index}") for index in range(len(_UNDER_FORWARDED))]
        ended_bridges: set[int] = set()
        for key, project in zip(_UNDER_FORWARDED, ended_projects, strict=True):
            ended_bridges |= _open_go_cursor(agent, key, project)
        live_project = make_go_project("live")
        live_bridges = _open_go_cursor(agent, _LIVE_KEY, live_project)

        # the managers and bridge processes are taken before eviction, which detaches the one and ends the other
        ended_managers = [_manager_double(project) for project in ended_projects]
        live_manager = _manager_double(live_project)
        ended = [psutil.Process(pid) for pid in ended_bridges]
        response = _client(listener).post(_SESSION_END_PATH, json={"forwarded_session_id": _FORWARDED})

        assert response.status_code == 200, response.text
        assert response.json() == {"evicted": sorted(_UNDER_FORWARDED)}
        for key in _UNDER_FORWARDED:
            assert key not in agent._cursor_managers_by_session
            assert key not in agent._active_projects_by_session
        _gone, alive = psutil.wait_procs(ended, timeout=_GATE_TIMEOUT)
        assert alive == [], f"serena-go-bridge processes still running after their slots were evicted: {[p.pid for p in alive]}"
        for manager in ended_managers:
            assert manager.stopped.wait(timeout=_GATE_TIMEOUT), "the manager of a project no remaining slot holds was never stopped"
        assert _LIVE_KEY in agent._cursor_managers_by_session
        assert agent._active_projects_by_session[_LIVE_KEY] is live_project
        assert all(psutil.pid_exists(pid) for pid in live_bridges)
        live_manager.stop_all.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        [b"{}", b'{"forwarded_session_id": ""}', b'{"forwarded_session_id": 7}', b'["forwarded_session_id"]', b"{"],
        ids=["absent", "empty", "not_a_string", "not_an_object", "not_json"],
    )
    def test_a_body_naming_no_forwarded_session_is_refused_and_evicts_nothing(
        self, agent: SerenaAgent, listener: FastMCP, make_go_project: Callable[[str], Project], body: bytes
    ) -> None:
        bridges = _open_go_cursor(agent, _FORWARDED, make_go_project("kept"))

        response = _client(listener).post(_SESSION_END_PATH, content=body, headers={"content-type": "application/json"})

        assert response.status_code == 400, response.text
        assert _FORWARDED in agent._cursor_managers_by_session
        assert _FORWARDED in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in bridges)

    def test_ending_a_forwarded_session_that_holds_no_slot_evicts_nothing(
        self, agent: SerenaAgent, listener: FastMCP, make_go_project: Callable[[str], Project]
    ) -> None:
        live_bridges = _open_go_cursor(agent, _LIVE_KEY, make_go_project("live"))

        response = _client(listener).post(_SESSION_END_PATH, json={"forwarded_session_id": _FORWARDED})

        assert response.status_code == 200, response.text
        assert response.json() == {"evicted": []}
        assert _LIVE_KEY in agent._cursor_managers_by_session
        assert _LIVE_KEY in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in live_bridges)

    def test_a_reconnection_between_the_multiplexer_and_serena_evicts_nothing(
        self, agent: SerenaAgent, listener: FastMCP, make_go_project: Callable[[str], Project]
    ) -> None:
        held_keys = (*_UNDER_FORWARDED, _LIVE_KEY)
        bridges: set[int] = set()
        for index, key in enumerate(held_keys):
            bridges |= _open_go_cursor(agent, key, make_go_project(f"held_{index}"))
        forwarded = {**_MCP_HEADERS, "x-forwarded-mcp-session-id": _FORWARDED}

        # the multiplexer ends its MCP session with serena and opens another, sending no session-end notification
        with _client(listener) as client:
            first = client.post("/mcp", headers=forwarded, json=_INITIALIZE)
            assert first.status_code == 200, first.text
            ended = client.delete("/mcp", headers={**forwarded, "mcp-session-id": first.headers["mcp-session-id"]})
            assert ended.status_code == 200, ended.text
            second = client.post("/mcp", headers=forwarded, json=_INITIALIZE)
            assert second.status_code == 200, second.text

        for key in held_keys:
            assert key in agent._cursor_managers_by_session
            assert key in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in bridges)

    def test_a_call_under_an_evicted_key_is_answered_from_its_recorded_project(
        self,
        agent: SerenaAgent,
        listener: FastMCP,
        make_go_project: Callable[[str], Project],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        key = f"{_FORWARDED}/session/S1"
        project = make_go_project("recorded")
        _open_go_cursor(agent, key, project)
        agent._explicit_project_root_store.set(key, project.project_root)
        manager = _manager_double(project)

        response = _client(listener).post(_SESSION_END_PATH, json={"forwarded_session_id": _FORWARDED})
        assert response.status_code == 200, response.text
        # the evicted slot's project is released before the call arrives, so the release cannot detach the manager
        # double the restore attaches
        assert manager.stopped.wait(timeout=_GATE_TIMEOUT), "the evicted slot's project was never released"

        # activating the recorded root yields the test's project, with a fresh manager double so it starts no language server
        def activate(root: str, **kwargs: object) -> bool:
            recorded = {project.project_root: project}[root]
            recorded.language_server_manager = MagicMock(spec=LanguageServerManager)
            return agent._activate_project(recorded, update_active_modes=False, update_active_tools=False, record_explicit=False)

        monkeypatch.setattr(agent, "activate_project_from_path_or_name", activate)
        monkeypatch.setattr(agent, "record_tool_usage", lambda *args, **kwargs: None)
        tool = _ActiveProjectRootTool(agent)
        monkeypatch.setattr(tool, "is_active", lambda: True)

        answer = tool.apply_ex(log_call=False, mcp_ctx=_forwarded_call_context(_FORWARDED), **{SESSION_ID_ARGUMENT: "S1"})

        assert answer == project.project_root


_CLAUDE_SESSION = "S1"
"""the Claude Code session id whose end the brain SessionEnd hook reports"""

_CARRYING_CLAUDE_SESSION = (
    f"{_FORWARDED}/session/{_CLAUDE_SESSION}",
    f"{_FORWARDED}/session/{_CLAUDE_SESSION}/agent/A1",
    f"{_OTHER_FORWARDED}/session/{_CLAUDE_SESSION}",
)
"""the slot keys that carry the ended Claude Code session id after ``/session/``, under either forwarded session id"""

_NOT_CARRYING_CLAUDE_SESSION = (f"{_FORWARDED}/session/S2", f"{_FORWARDED}/session/S10", _FORWARDED)
"""slot keys that do not carry the ended Claude Code session id: another session's, one whose session id begins with the
ended id's characters, and the bare forwarded session id"""


class TestEvictionOfAClaudeSession:
    """verifies that ending a Claude Code session evicts exactly the slots whose keys carry its id after ``/session/``."""

    def test_slots_carrying_the_ended_id_are_evicted_and_their_bridges_stop(
        self, agent: SerenaAgent, make_go_project: Callable[[str], Project]
    ) -> None:
        ended_bridges: set[int] = set()
        for index, key in enumerate(_CARRYING_CLAUDE_SESSION):
            ended_bridges |= _open_go_cursor(agent, key, make_go_project(f"ended_{index}"))
        kept_bridges: set[int] = set()
        for index, key in enumerate(_NOT_CARRYING_CLAUDE_SESSION):
            kept_bridges |= _open_go_cursor(agent, key, make_go_project(f"kept_{index}"))

        # the bridge processes are taken before eviction, since one that has already exited can no longer be looked up
        ended = [psutil.Process(pid) for pid in ended_bridges]
        evicted = agent.evict_claude_session(_CLAUDE_SESSION)

        assert sorted(evicted) == sorted(_CARRYING_CLAUDE_SESSION)
        for key in _CARRYING_CLAUDE_SESSION:
            assert key not in agent._cursor_managers_by_session
            assert key not in agent._active_projects_by_session
        _gone, alive = psutil.wait_procs(ended, timeout=_GATE_TIMEOUT)
        assert alive == [], f"serena-go-bridge processes still running after their slots were evicted: {[p.pid for p in alive]}"
        for key in _NOT_CARRYING_CLAUDE_SESSION:
            assert key in agent._cursor_managers_by_session
            assert key in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in kept_bridges)

    def test_ending_a_claude_session_that_holds_no_slot_evicts_nothing(
        self, agent: SerenaAgent, make_go_project: Callable[[str], Project]
    ) -> None:
        live_bridges = _open_go_cursor(agent, _LIVE_KEY, make_go_project("live"))

        evicted = agent.evict_claude_session(_CLAUDE_SESSION)

        assert evicted == []
        assert _LIVE_KEY in agent._cursor_managers_by_session
        assert _LIVE_KEY in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in live_bridges)

    def test_the_durable_record_of_an_evicted_key_is_kept(self, agent: SerenaAgent, make_go_project: Callable[[str], Project]) -> None:
        key = _CARRYING_CLAUDE_SESSION[0]
        project = make_go_project("recorded")
        _open_go_cursor(agent, key, project)
        agent._explicit_project_root_store.set(key, project.project_root)

        agent.evict_claude_session(_CLAUDE_SESSION)

        assert key not in agent._active_projects_by_session
        assert agent._explicit_project_root_store.get(key) == project.project_root


class TestSessionEndRouteForAClaudeSession:
    """verifies the answer of the /sessions/end route to a body naming a Claude Code session id."""

    def test_ending_a_claude_session_evicts_its_slots_and_releases_their_resources(
        self, agent: SerenaAgent, listener: FastMCP, make_go_project: Callable[[str], Project]
    ) -> None:
        ended_projects = [make_go_project(f"ended_{index}") for index in range(len(_CARRYING_CLAUDE_SESSION))]
        ended_bridges: set[int] = set()
        for key, project in zip(_CARRYING_CLAUDE_SESSION, ended_projects, strict=True):
            ended_bridges |= _open_go_cursor(agent, key, project)
        kept_projects = [make_go_project(f"kept_{index}") for index in range(len(_NOT_CARRYING_CLAUDE_SESSION))]
        kept_bridges: set[int] = set()
        for key, project in zip(_NOT_CARRYING_CLAUDE_SESSION, kept_projects, strict=True):
            kept_bridges |= _open_go_cursor(agent, key, project)

        # the managers and bridge processes are taken before eviction, which detaches the one and ends the other
        ended_managers = [_manager_double(project) for project in ended_projects]
        kept_managers = [_manager_double(project) for project in kept_projects]
        ended = [psutil.Process(pid) for pid in ended_bridges]
        response = _client(listener).post(_SESSION_END_PATH, json={"claude_session_id": _CLAUDE_SESSION})

        assert response.status_code == 200, response.text
        assert response.json() == {"evicted": sorted(_CARRYING_CLAUDE_SESSION)}
        for key in _CARRYING_CLAUDE_SESSION:
            assert key not in agent._cursor_managers_by_session
            assert key not in agent._active_projects_by_session
        _gone, alive = psutil.wait_procs(ended, timeout=_GATE_TIMEOUT)
        assert alive == [], f"serena-go-bridge processes still running after their slots were evicted: {[p.pid for p in alive]}"
        for manager in ended_managers:
            assert manager.stopped.wait(timeout=_GATE_TIMEOUT), "the manager of a project no remaining slot holds was never stopped"
        for key, project in zip(_NOT_CARRYING_CLAUDE_SESSION, kept_projects, strict=True):
            assert key in agent._cursor_managers_by_session
            assert agent._active_projects_by_session[key] is project
        assert all(psutil.pid_exists(pid) for pid in kept_bridges)
        for manager in kept_managers:
            manager.stop_all.assert_not_called()

    def test_a_call_under_an_evicted_key_is_answered_from_its_recorded_project(
        self,
        agent: SerenaAgent,
        listener: FastMCP,
        make_go_project: Callable[[str], Project],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        key = f"{_FORWARDED}/session/{_CLAUDE_SESSION}"
        project = make_go_project("recorded")
        _open_go_cursor(agent, key, project)
        agent._explicit_project_root_store.set(key, project.project_root)
        manager = _manager_double(project)

        response = _client(listener).post(_SESSION_END_PATH, json={"claude_session_id": _CLAUDE_SESSION})
        assert response.status_code == 200, response.text
        assert response.json() == {"evicted": [key]}
        # the evicted slot's project is released before the call arrives, so the release cannot detach the manager
        # double the restore attaches
        assert manager.stopped.wait(timeout=_GATE_TIMEOUT), "the evicted slot's project was never released"

        # activating the recorded root yields the test's project, with a fresh manager double so it starts no language server
        def activate(root: str, **kwargs: object) -> bool:
            recorded = {project.project_root: project}[root]
            recorded.language_server_manager = MagicMock(spec=LanguageServerManager)
            return agent._activate_project(recorded, update_active_modes=False, update_active_tools=False, record_explicit=False)

        monkeypatch.setattr(agent, "activate_project_from_path_or_name", activate)
        monkeypatch.setattr(agent, "record_tool_usage", lambda *args, **kwargs: None)
        tool = _ActiveProjectRootTool(agent)
        monkeypatch.setattr(tool, "is_active", lambda: True)

        answer = tool.apply_ex(log_call=False, mcp_ctx=_forwarded_call_context(_FORWARDED), **{SESSION_ID_ARGUMENT: _CLAUDE_SESSION})

        assert answer == project.project_root

    def test_a_body_naming_both_a_forwarded_and_a_claude_session_is_refused_and_evicts_nothing(
        self, agent: SerenaAgent, listener: FastMCP, make_go_project: Callable[[str], Project]
    ) -> None:
        # the key lies under the forwarded id and carries the Claude Code session id, so either reading alone would evict it
        key = f"{_FORWARDED}/session/{_CLAUDE_SESSION}"
        bridges = _open_go_cursor(agent, key, make_go_project("kept"))

        response = _client(listener).post(
            _SESSION_END_PATH, json={"forwarded_session_id": _FORWARDED, "claude_session_id": _CLAUDE_SESSION}
        )

        assert response.status_code == 400, response.text
        assert key in agent._cursor_managers_by_session
        assert key in agent._active_projects_by_session
        assert all(psutil.pid_exists(pid) for pid in bridges)
