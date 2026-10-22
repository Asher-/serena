"""unit tests for the release of the serena-go-bridge processes that structural Go cursors start.

a structural backend's helper process must stop once no live session's project or cursor manager holds that backend,
so that every serena-go-bridge process a daemon runs belongs to a backend a live session can reach. The tests wait on
the exit of each bridge process; the timeout only bounds how long a broken implementation can hang a test.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import LanguageBackend, ProjectConfig, SerenaConfig
from serena.cursor import StructuralCursorState
from serena.ls_manager import LanguageServerManager
from serena.project import Project
from solidlsp.ls_config import Language

_GATE_TIMEOUT = 10.0
"""the seconds a test waits on a bridge process's exit before failing; a bound on a hang, never the ordering under test"""

_BRIDGE_PROCESS_NAME = "serena-go-bridge"
_GO_FILE = "hello.go"


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    a = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False, log_level=logging.ERROR))
    yield a
    a.on_shutdown(timeout=0.1)


@pytest.fixture
def go_project(agent: SerenaAgent, tmp_path: Path) -> Project:
    """
    :return: a project holding ``hello.go`` and a language-server manager double, so the project never starts a
        language server
    """
    (tmp_path / _GO_FILE).write_text("package main\n\nfunc Hello() {}\n", encoding="utf-8")
    project_config = ProjectConfig(project_name="go_bridge_test", languages=[Language.GO], language_backend=LanguageBackend.LSP)
    project = Project(project_root=str(tmp_path), project_config=project_config, serena_config=agent.serena_config)
    project.language_server_manager = MagicMock(spec=LanguageServerManager)
    return project


def _bridge_pids() -> set[int]:
    """
    :return: the process ids of this process's serena-go-bridge children
    """
    return {child.pid for child in psutil.Process().children() if child.name() == _BRIDGE_PROCESS_NAME}


def _miss(*args: object, **kwargs: object) -> None:
    """
    raises a fresh error on every call, as a language server that knows no such symbol does; a single shared error
    instance would keep the traceback of its last raise, and with it the cursor manager that caught it, alive
    """
    raise ValueError("no language-server symbol")


def _open_go_cursor(agent: SerenaAgent, session_key: str, project: Project) -> None:
    """Activates ``project`` in the session ``session_key`` and opens a structural cursor on ``hello.go`` there."""

    def run() -> None:
        _SESSION_KEY_VAR.set(session_key)
        agent._activate_project(project, update_active_modes=False, update_active_tools=False)
        # no language server knows the symbol, so the cursor falls through to the Go structural backend
        with patch("serena.cursor.LanguageServerSymbolRetriever") as retriever_cls:
            retriever_cls.return_value.find_unique.side_effect = _miss
            _cursor_id, state = agent.get_cursor_manager().start_cursor("Hello", relative_path=_GO_FILE)
        assert isinstance(state, StructuralCursorState)

    contextvars.copy_context().run(run)


class TestEvictionStopsGoBridges:
    """verifies that evicting every session holding a Go backend stops the serena-go-bridge processes it started."""

    def test_bridges_of_evicted_sessions_are_stopped(self, agent: SerenaAgent, go_project: Project) -> None:
        bridges_before = _bridge_pids()
        _open_go_cursor(agent, "session-1", go_project)
        _open_go_cursor(agent, "session-2", go_project)
        started = [psutil.Process(pid) for pid in _bridge_pids() - bridges_before]
        assert started, "the structural Go cursors started no serena-go-bridge process"

        agent._evict_session_state("session-1")
        agent._evict_session_state("session-2")

        _gone, alive = psutil.wait_procs(started, timeout=_GATE_TIMEOUT)
        assert alive == [], f"serena-go-bridge processes still running after both sessions were evicted: {[p.pid for p in alive]}"
