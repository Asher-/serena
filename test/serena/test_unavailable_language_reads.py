"""unit tests for reads of a file whose language server is unavailable.

each read either answers the file on a lower rung and names the unavailable language and its startup failure, or
raises the error naming them. Every test runs over a project whose python server runs and whose go server failed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.cursor import CursorManager
from serena.language_backend import BuiltinLanguageBackend
from serena.ls_manager import LanguageServerManager, LanguageUnavailableError
from serena.project import Project
from serena.repl.api.fs_api import FsApi
from serena.repl.api.lsp_api import LspApi
from serena.tools.cursor_tools import CursorGrepTool, CursorOverviewTool
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from solidlsp.structural.registry import StructuralBackendRegistry

_GO_FILE = "main.go"
_GO_FAILURE = "gopls missing"
_NOTES_FILE = "notes.txt"


class _LspBackend:
    """the language-server backend."""

    def is_lsp(self) -> bool:
        return True


class _AgentStub:
    """the agent surface the read tools and facade APIs read: the active project, its cursor manager, the backend."""

    def __init__(self, project: Project) -> None:
        self._project = project
        self.serena_config = project.serena_config
        self._cursor_manager = CursorManager(project, structural_registry=StructuralBackendRegistry())

    def get_active_project_or_raise(self) -> Project:
        return self._project

    def get_cursor_manager(self) -> CursorManager:
        return self._cursor_manager

    def get_language_backend(self) -> _LspBackend:
        return _LspBackend()

    def get_active_language_server_ids(self) -> list[LanguageServerId]:
        return [LanguageServerId.PYTHON]


@pytest.fixture
def agent(tmp_path: Path) -> _AgentStub:
    """
    :return: an agent over a project holding ``main.go`` and ``module.py``, whose python server runs and whose go
        server failed to start
    """
    (tmp_path / _GO_FILE).write_text("package main\n\nfunc Needle() {}\n", encoding="utf-8")
    (tmp_path / "module.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    (tmp_path / _NOTES_FILE).write_text("Needle in the notes\n", encoding="utf-8")
    serena_config = SerenaConfig(
        gui_log_window=False,
        web_dashboard=False,
        log_level=logging.ERROR,
        language_backend=BuiltinLanguageBackend.LSP.get_instance(),
    )
    project = Project(
        project_root=str(tmp_path),
        project_config=ProjectConfig(
            project_name="unavailable_language_reads",
            language_servers=[LanguageServerId.PYTHON, LanguageServerId.GO],
            language_backend=BuiltinLanguageBackend.LSP.get_instance(),
        ),
        serena_config=serena_config,
    )
    python = MagicMock(spec=SolidLanguageServer)
    python.ls_id = LanguageServerId.PYTHON
    python.is_running.return_value = True
    python.is_ignored_path.side_effect = lambda path, ignore_unsupported_files=False: not path.endswith(".py")
    project.language_server_manager = LanguageServerManager(
        {LanguageServerId.PYTHON: python}, unavailable_languages={LanguageServerId.GO: RuntimeError(_GO_FAILURE)}
    )
    return _AgentStub(project)


def test_cursor_overview_names_the_unavailable_language(agent: _AgentStub) -> None:
    result = CursorOverviewTool(agent=agent).apply(relative_path=_GO_FILE, because="to read the go file")  # type: ignore[arg-type]

    assert LanguageServerId.GO.get_key() in result
    assert _GO_FAILURE in result


def test_cursor_overview_of_a_file_of_no_language_is_its_plaintext_view(agent: _AgentStub) -> None:
    result = CursorOverviewTool(agent=agent).apply(relative_path=_NOTES_FILE, because="to read the notes")  # type: ignore[arg-type]

    assert result.endswith(f"\n\n{_NOTES_FILE}: 1 line, 20 bytes, utf-8, LF, trailing newline")


def test_cursor_grep_names_the_unavailable_language(agent: _AgentStub) -> None:
    result = CursorGrepTool(agent=agent).apply(  # type: ignore[arg-type]
        substring_pattern="Needle", because="to find the go function", restrict_to_code_files=False
    )

    assert "Needle" in result
    assert f"{LanguageServerId.GO.get_key()}: {_GO_FAILURE}" in result


def test_search_by_enclosing_symbol_names_the_unavailable_language(agent: _AgentStub) -> None:
    hits = FsApi(agent).search_by_enclosing_symbol("Needle")  # type: ignore[arg-type]

    summary = hits.render_summary()
    assert f"{LanguageServerId.GO.get_key()}: {_GO_FAILURE}" in summary


def test_symbols_overview_raises_naming_the_unavailable_language(agent: _AgentStub) -> None:
    with pytest.raises(LanguageUnavailableError) as exc_info:
        LspApi(agent).get_symbols_overview(_GO_FILE)  # type: ignore[arg-type]

    assert exc_info.value.language == LanguageServerId.GO
    assert _GO_FAILURE in str(exc_info.value)


def test_symbols_overview_of_a_file_of_no_language_is_refused(agent: _AgentStub) -> None:
    with pytest.raises(ValueError, match=f"Cannot extract symbols from file {_NOTES_FILE}"):
        LspApi(agent).get_symbols_overview(_NOTES_FILE)  # type: ignore[arg-type]
