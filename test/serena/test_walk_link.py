"""unit tests for the walks serena makes of a project's directory tree, over a tree holding a symbolic link to a directory.

a walk must enter no symbolic link to a directory, so that a link to the directory holding it, or to any ancestor of
it, adds no path to the walk and opens no directory beneath the link; and search_for_pattern and cursor_grep must name
each such link they did not enter. Every test builds a tree whose ``pkg/loop`` links to the tree's own root.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from serena.config.serena_config import LanguageBackend, ProjectConfig, SerenaConfig
from serena.cursor import CursorManager
from serena.project import Project
from serena.tools.cursor_tools import CursorGrepTool
from serena.tools.file_tools import SearchForPatternTool
from serena.util.file_system import find_all_non_ignored_files, scan_directory
from solidlsp.ls_config import Language
from solidlsp.structural.registry import StructuralBackendRegistry

_SOURCE_FILE = os.path.join("pkg", "module.py")
_LINK = os.path.join("pkg", "loop")


@pytest.fixture
def linked_tree(tmp_path: Path) -> Path:
    """
    :return: the root of a tree holding ``pkg/module.py``, which contains ``NEEDLE``, and ``pkg/loop``, a symbolic
        link to the root itself
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "module.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    os.symlink(tmp_path, tmp_path / _LINK, target_is_directory=True)
    return tmp_path


def _project(root: Path) -> Project:
    """
    :param root: the project root
    :return: a Python project over ``root`` whose language servers are never started
    """
    serena_config = SerenaConfig(
        gui_log_window=False,
        web_dashboard=False,
        log_level=logging.ERROR,
        language_backend=LanguageBackend.LSP,
    )
    project_config = ProjectConfig(project_name="walk_link_test", languages=[Language.PYTHON], language_backend=LanguageBackend.LSP)
    return Project(project_root=str(root), project_config=project_config, serena_config=serena_config)


class _NonLspBackend:
    """a language backend that is not the language server, so search_for_pattern groups no hit under a symbol."""

    def is_lsp(self) -> bool:
        return False


class _AgentStub:
    """
    the agent surface :class:`SearchForPatternTool` reads: the active project, the language backend, and the
    configuration that bounds the length of its answer.
    """

    def __init__(self, project: Project) -> None:
        self._project = project
        self.serena_config = project.serena_config

    def get_active_project_or_raise(self) -> Project:
        return self._project

    def get_language_backend(self) -> _NonLspBackend:
        return _NonLspBackend()


class _NoLanguageServerManager:
    """a language-server manager with no server suitable for any file, so cursor_grep anchors no hit to a symbol."""

    def has_suitable_ls_for_file(self, relative_file_path: str) -> bool:
        return False


class _CursorAgentStub(_AgentStub):
    """the agent surface :class:`CursorGrepTool` reads: a cursor manager over the project, and the answer bound."""

    def __init__(self, project: Project) -> None:
        super().__init__(project)
        project.language_server_manager = _NoLanguageServerManager()  # type: ignore[assignment]
        self._cursor_manager = CursorManager(project, structural_registry=StructuralBackendRegistry())

    def get_cursor_manager(self) -> CursorManager:
        return self._cursor_manager


def _is_beneath_link(relative_path: str) -> bool:
    """
    :param relative_path: a path relative to the tree's root
    :return: whether the path is the link or lies beneath it
    """
    return relative_path == _LINK or relative_path.startswith(_LINK + os.sep)


class TestWalkEntersNoLinkToADirectory:
    """verifies that each walk of a project tree returns the tree's files once and nothing reached through the link."""

    def test_scan_directory(self, linked_tree: Path) -> None:
        result = scan_directory(str(linked_tree), recursive=True, relative_to=str(linked_tree))

        assert result.files == [_SOURCE_FILE]
        assert result.directories == ["pkg"]

    def test_find_all_non_ignored_files(self, linked_tree: Path) -> None:
        files = [os.path.relpath(path, linked_tree) for path in find_all_non_ignored_files(str(linked_tree))]

        assert files == [_SOURCE_FILE]

    def test_gather_source_files(self, linked_tree: Path) -> None:
        files = _project(linked_tree).gather_source_files()

        assert files == [_SOURCE_FILE]
        assert not any(_is_beneath_link(path) for path in files)


class TestSearchForPatternNamesTheLink:
    """verifies that search_for_pattern returns the one match and names the link its walk did not enter."""

    @pytest.mark.parametrize("restrict_search_to_code_files", [False, True])
    def test_match_is_returned_once_and_link_is_named(self, linked_tree: Path, restrict_search_to_code_files: bool) -> None:
        tool = SearchForPatternTool(agent=_AgentStub(_project(linked_tree)))  # type: ignore[arg-type]

        result = tool.apply("NEEDLE", restrict_search_to_code_files=restrict_search_to_code_files)

        assert result.startswith("Found 1 matches across 0 symbols in 1 files.")
        assert _SOURCE_FILE in result
        assert _LINK in result
        assert os.path.join(_LINK, "pkg") not in result


class TestCursorGrepNamesTheLink:
    """verifies that cursor_grep returns the one match and names the link its walk did not enter."""

    @pytest.mark.parametrize("restrict_to_code_files", [False, True])
    def test_match_is_returned_once_and_link_is_named(self, linked_tree: Path, restrict_to_code_files: bool) -> None:
        tool = CursorGrepTool(agent=_CursorAgentStub(_project(linked_tree)))  # type: ignore[arg-type]

        result = tool.apply("NEEDLE", because="test", restrict_to_code_files=restrict_to_code_files)

        assert "Found 1 match(es): 0 in 0 symbol(s), 1 on non-symbol lines." in result
        assert _SOURCE_FILE in result
        assert _LINK in result
        assert os.path.join(_LINK, "pkg") not in result

    @pytest.mark.parametrize("restrict_to_code_files", [False, True])
    def test_link_is_named_when_nothing_matches(self, linked_tree: Path, restrict_to_code_files: bool) -> None:
        tool = CursorGrepTool(agent=_CursorAgentStub(_project(linked_tree)))  # type: ignore[arg-type]

        result = tool.apply("ABSENT", because="test", restrict_to_code_files=restrict_to_code_files)

        assert "No matches for 'ABSENT'." in result
        assert _LINK in result


class TestFindPatternReportsTheLink:
    """verifies that the cursor manager's pattern search finds the one match and reports the link its walk did not enter."""

    @pytest.mark.parametrize("restrict_to_code_files", [False, True])
    def test_match_is_found_once_and_link_is_reported(self, linked_tree: Path, restrict_to_code_files: bool) -> None:
        cursor_manager = _CursorAgentStub(_project(linked_tree)).get_cursor_manager()
        skipped_links: list[str] = []

        groups, unsymboled = cursor_manager.find_pattern_with_enclosing_symbols(
            "NEEDLE", restrict_to_code_files=restrict_to_code_files, skipped_links=skipped_links
        )

        assert groups == []
        assert [path for path, _hits in unsymboled] == [_SOURCE_FILE]
        assert skipped_links == [_LINK]
