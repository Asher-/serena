"""
The memory that sessions' cursor greps and finds retain on a trusted project whose links loop back into it stays
bounded: the document symbols they keep alive number at most the project's files, and each session holds at most
:attr:`CursorManager.MAX_CURSORS` cursors. An idle stop of the project's servers releases the symbols it served, and the
next call is served again.
"""

from __future__ import annotations

import contextvars
import gc
import logging
import time
import weakref
from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.cursor import CursorManager
from serena.language_backend import BuiltinLanguageBackend
from serena.project import Project
from solidlsp.ls import DocumentSymbols
from solidlsp.ls_config import LanguageServerId

pytestmark = pytest.mark.python

_FILES = 3
"""the number of source files of the looped project"""

_IDLE_TIMEOUT = 0.5
"""the language-server idle timeout, in seconds, of the agent whose idle stop is observed"""

_WAIT = 30.0
"""the seconds a test waits for the idle stop before failing; a bound on a hang, never the ordering under test"""


def _looped_tree(root: Path) -> Path:
    """
    :return: ``root``, holding :data:`_FILES` Python files in ``pkg``, which links back to the root and to itself
    """
    (root / "pkg").mkdir(parents=True)
    for index in range(_FILES):
        (root / "pkg" / f"module_{index}.py").write_text(f"def function_{index}():\n    return {index}\n", encoding="utf-8")
    try:
        (root / "pkg" / "loop").symlink_to(root, target_is_directory=True)
        (root / "pkg" / "self").symlink_to(root / "pkg", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")
    return root


def _project(root: Path, serena_config: SerenaConfig) -> Project:
    """
    :return: a trusted Python project over ``root``
    """
    project_config = ProjectConfig(
        project_name=root.name,
        language_servers=[LanguageServerId.PYTHON],
        language_backend=BuiltinLanguageBackend.LSP.get_instance(),
    )
    return Project(project_root=str(root), project_config=project_config, serena_config=serena_config)


def _config(root: Path, idle_timeout: float = 0.0) -> SerenaConfig:
    """
    :return: a configuration trusting ``root``, with the given language-server idle timeout
    """
    return SerenaConfig(
        gui_log_window=False,
        web_dashboard=False,
        log_level=logging.ERROR,
        language_backend=BuiltinLanguageBackend.LSP.get_instance(),
        trusted_project_path_patterns=[str(root)],
        language_server_idle_timeout=idle_timeout,
    )


@pytest.fixture
def looped_project(tmp_path: Path) -> Iterator[Project]:
    """
    :return: a trusted looped project with a started language server
    """
    root = _looped_tree(tmp_path / "project")
    project = _project(root, _config(root))
    manager = project.create_language_server_manager()
    try:
        yield project
    finally:
        manager.stop_all()


def _live_document_symbols() -> int:
    """
    :return: the number of document-symbol collections alive in the process
    """
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, DocumentSymbols))


def test_sessions_greps_and_finds_on_a_looped_project_keep_at_most_its_files_symbols_alive(looped_project: Project) -> None:
    before = _live_document_symbols()

    sessions = [CursorManager(looped_project) for _ in range(2)]
    for _round in range(2):
        for session in sessions:
            groups, _ = session.find_pattern_with_enclosing_symbols("return")
            for symbol, _hits in groups:
                session.register_cursor_at_symbol(symbol)
            for symbol in session.find_symbols("function", substring_matching=True):
                session.register_cursor_at_symbol(symbol)

    assert _live_document_symbols() - before <= _FILES
    for session in sessions:
        assert 0 < len(session.list_cursors()) <= CursorManager.MAX_CURSORS
        closed = session.list_cursors()[0]
        assert session.get_cursor(closed).cursor_id == closed
        session.close_cursor(closed)
        assert closed not in session.list_cursors()


def test_an_idle_stop_releases_the_symbols_its_servers_served_and_the_next_call_is_served_again(tmp_path: Path) -> None:
    root = _looped_tree(tmp_path / "project")
    agent = SerenaAgent(serena_config=_config(root, idle_timeout=_IDLE_TIMEOUT))
    project = _project(root, agent.serena_config)
    manager = project.create_language_server_manager()
    try:
        # the project held by a session, whose call is served symbols
        def activate() -> None:
            _SESSION_KEY_VAR.set("session-1")
            agent._activate_project(project, update_active_modes=False, update_active_tools=False)

        contextvars.copy_context().run(activate)
        server = manager.get_language_server("pkg/module_0.py")
        served = weakref.ref(server.request_document_symbols("pkg/module_0.py"))

        # the idle stop
        deadline = time.monotonic() + _WAIT
        while time.monotonic() < deadline and served() is not None:
            time.sleep(0.1)
            gc.collect()

        assert served() is None
        names = [
            symbol["name"]
            for symbol in manager.get_language_server("pkg/module_0.py").request_document_symbols("pkg/module_0.py").root_symbols
        ]
        assert names == ["function_0"]
    finally:
        manager.stop_all()
        agent.on_shutdown(timeout=1.0)
