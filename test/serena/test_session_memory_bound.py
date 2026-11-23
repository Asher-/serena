"""
The memory that many sessions' cursor greps and finds retain on a trusted project whose links loop back into it stays
bounded: the document symbols kept alive number at most the project's files, and each session holds at most
:attr:`CursorManager.MAX_CURSORS` cursors, however many sessions run and however many cursors they start.
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.cursor import CursorManager
from serena.language_backend import BuiltinLanguageBackend
from serena.project import Project
from solidlsp.ls import DocumentSymbols
from solidlsp.ls_config import LanguageServerId

pytestmark = pytest.mark.python

_FILES = 6
"""the number of source files of the looped project"""

_SESSIONS = 4
"""the number of sessions that search the project"""

_ROUNDS = 8
"""the number of greps and finds each session runs"""


@pytest.fixture
def looped_project(tmp_path: Path) -> Iterator[Project]:
    """
    :return: a trusted Python project of :data:`_FILES` files whose package links back to the root and to itself, with
        a started language server
    """
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    for index in range(_FILES):
        (root / "pkg" / f"module_{index}.py").write_text(
            "".join(f"def function_{index}_{n}():\n    return {n}\n\n\n" for n in range(40)), encoding="utf-8"
        )
    try:
        (root / "pkg" / "loop").symlink_to(root, target_is_directory=True)
        (root / "pkg" / "self").symlink_to(root / "pkg", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    serena_config = SerenaConfig(
        gui_log_window=False,
        web_dashboard=False,
        log_level=logging.ERROR,
        language_backend=BuiltinLanguageBackend.LSP.get_instance(),
        trusted_project_path_patterns=[str(root)],
    )
    project_config = ProjectConfig(
        project_name="session_memory_bound",
        language_servers=[LanguageServerId.PYTHON],
        language_backend=BuiltinLanguageBackend.LSP.get_instance(),
    )
    project = Project(project_root=str(root), project_config=project_config, serena_config=serena_config)
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


def test_many_sessions_greps_and_finds_on_a_looped_project_retain_bounded_memory(looped_project: Project) -> None:
    sessions = [CursorManager(looped_project) for _ in range(_SESSIONS)]
    for _round in range(_ROUNDS):
        for session in sessions:
            groups, _ = session.find_pattern_with_enclosing_symbols("return")
            for symbol, _hits in groups:
                session.register_cursor_at_symbol(symbol)
            for symbol in session.find_symbols("function", substring_matching=True):
                session.register_cursor_at_symbol(symbol)

    assert _live_document_symbols() <= _FILES
    for session in sessions:
        assert len(session.list_cursors()) == CursorManager.MAX_CURSORS
