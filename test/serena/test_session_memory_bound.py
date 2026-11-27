"""
The memory that sessions' cursor greps and finds retain on a trusted project whose links loop back into it stays
bounded: the document symbols they keep alive number at most the project's files, and each session holds at most
:attr:`CursorManager.MAX_CURSORS` cursors. An idle stop of the project's servers releases the symbols it served, and the
next call is served again; stopping a server whose process has ended saves its symbols and releases them as well.
"""

from __future__ import annotations

import gc
import logging
import os
import signal
import threading
import weakref
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.cursor import CursorManager
from serena.language_backend import BuiltinLanguageBackend
from serena.project import Project
from serena.tools.config_tools import GetCurrentConfigTool
from solidlsp.ls import DocumentSymbols, SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from solidlsp.util.symbol_store import SymbolStore

pytestmark = pytest.mark.python

_FILES = 1
"""the number of source files of the looped project"""

_SESSIONS = 2
"""the number of sessions working on the looped project, the fewest that share its servers"""

_ROUNDS = 2
"""the number of rounds of greps and finds each session makes, the fewest that repeat one"""

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

    sessions = [CursorManager(looped_project) for _ in range(_SESSIONS)]
    for _round in range(_ROUNDS):
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


def _idle_stopping_agent(root: Path, stopped: threading.Event) -> SerenaAgent:
    """
    :param root: the root of the project the agent activates, which this configures to use its Python language server
    :param stopped: the event set after each idle-stop attempt for a project, whether it stopped the project's servers,
        found it holding no language server manager, or its stop_all raised
    :return: an agent on the project whose servers are stopped after :data:`_IDLE_TIMEOUT` seconds without a tool call
    """
    # the project's own configuration, naming its Python language server, which activation by path reads
    (root / ".serena").mkdir(exist_ok=True)
    (root / ".serena" / "project.yml").write_text(
        f"project_name: {root.name}\nlanguage_servers:\n- {LanguageServerId.PYTHON.get_key()}\nlanguage_backend: LSP\n", encoding="utf-8"
    )
    return SerenaAgent(
        project=str(root),
        serena_config=_config(root, idle_timeout=_IDLE_TIMEOUT),
        on_language_servers_idle_stopped=lambda _project: stopped.set(),
    )


def test_an_idle_stop_releases_the_symbols_its_servers_served_and_the_next_call_is_served_again(tmp_path: Path) -> None:
    root = _looped_tree(tmp_path / "project")
    stopped = threading.Event()
    agent = _idle_stopping_agent(root, stopped)
    project = agent.get_active_project()
    assert project is not None
    manager = project.create_language_server_manager()
    try:
        # the idle stop of the newly activated project, after which it is not stopped again until it is used
        assert stopped.wait(_WAIT)
        stopped.clear()

        # symbols served by the restarted server, then a tool call on the project, from whose end the next idle stop counts
        server = manager.get_language_server("pkg/module_0.py")
        served = weakref.ref(server.request_document_symbols("pkg/module_0.py"))
        agent.get_tool(GetCurrentConfigTool).apply_ex(log_call=False)

        assert stopped.wait(_WAIT)
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


def _saved_root_symbol_names(server: SolidLanguageServer, relative_path: str) -> list[str] | None:
    """
    :param server: a language server, whose raw document symbols are read as it saved them
    :param relative_path: the relative path of the file
    :return: the names of the file's raw root symbols that a new store on the server's cache file reads, or None if it
        reads no entry of the file
    """
    store = SymbolStore(
        server.cache_dir / server.RAW_DOCUMENT_SYMBOL_STORE_FILENAME, server._raw_document_symbols_cache_version(), server._cache_loader
    )
    try:
        row = store.get(relative_path)
    finally:
        store.close()
    return None if row is None else [symbol["name"] for symbol in row[1]]


def test_an_idle_stop_of_a_crashed_server_saves_its_symbols_and_keeps_none_of_them_alive(tmp_path: Path) -> None:
    root = _looped_tree(tmp_path / "project")
    stopped = threading.Event()
    agent = _idle_stopping_agent(root, stopped)
    project = agent.get_active_project()
    assert project is not None
    manager = project.create_language_server_manager()
    try:
        # the idle stop of the newly activated project, after which it is not stopped again until it is used
        assert stopped.wait(_WAIT)
        stopped.clear()

        # symbols served by the restarted server, whose cache on disk is then removed
        server = manager.get_language_server("pkg/module_0.py")
        served = weakref.ref(server.request_document_symbols("pkg/module_0.py"))
        saved = server.cache_dir / server.RAW_DOCUMENT_SYMBOL_STORE_FILENAME
        saved.unlink(missing_ok=True)

        # the server's process ends on its own, then a tool call on the project, from whose end the next idle stop counts
        pid = server.process_id()
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        psutil.Process(pid).wait(timeout=_WAIT)
        agent.get_tool(GetCurrentConfigTool).apply_ex(log_call=False)

        assert stopped.wait(_WAIT)
        gc.collect()
        assert _saved_root_symbol_names(server, "pkg/module_0.py") == ["function_0"]
        assert served() is None
    finally:
        manager.stop_all()
        agent.on_shutdown(timeout=1.0)


def test_removing_the_language_of_a_crashed_server_saves_its_symbols(looped_project: Project) -> None:
    manager = looped_project.create_language_server_manager()
    server = manager.get_language_server("pkg/module_0.py")
    server.request_document_symbols("pkg/module_0.py")
    saved = server.cache_dir / server.RAW_DOCUMENT_SYMBOL_STORE_FILENAME
    saved.unlink(missing_ok=True)

    # the server's process ends on its own
    pid = server.process_id()
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    psutil.Process(pid).wait(timeout=_WAIT)

    manager.remove_language_server(LanguageServerId.PYTHON, save_cache=True)

    assert _saved_root_symbol_names(server, "pkg/module_0.py") == ["function_0"]


def test_restarting_the_language_of_a_crashed_server_saves_its_symbols_and_holds_a_running_replacement(
    looped_project: Project,
) -> None:
    manager = looped_project.create_language_server_manager()
    server = manager.get_language_server("pkg/module_0.py")
    server.request_document_symbols("pkg/module_0.py")
    saved = server.cache_dir / server.RAW_DOCUMENT_SYMBOL_STORE_FILENAME
    saved.unlink(missing_ok=True)

    # the server's process ends on its own
    pid = server.process_id()
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    psutil.Process(pid).wait(timeout=_WAIT)

    try:
        replacement = manager.restart_language_server(LanguageServerId.PYTHON)

        assert saved.is_file()
        assert replacement is not server
        assert replacement.is_running()
        assert list(manager.iter_language_servers()) == [replacement]
    finally:
        manager.stop_all()


def test_a_language_removed_and_added_again_is_served_by_a_running_server_and_a_second_addition_is_refused(
    looped_project: Project,
) -> None:
    manager = looped_project.create_language_server_manager()
    removed = manager.get_language_server("pkg/module_0.py")
    manager.remove_language_server(LanguageServerId.PYTHON)

    try:
        added = manager.add_language_server(LanguageServerId.PYTHON)

        assert added is not removed
        assert added.is_running()
        assert manager.get_language_server("pkg/module_0.py") is added
        with pytest.raises(ValueError):
            manager.add_language_server(LanguageServerId.PYTHON)
        assert list(manager.iter_language_servers()) == [added]
    finally:
        manager.stop_all()
