"""Two sessions of one agent, each holding a project of its own, each read their own tree (pg://serena/session/project/binding)."""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, _SESSION_KEY_VAR, SerenaAgent
from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.project import Project
from serena.tools.file_tools import ListDirTool, SearchForPatternTool

T = TypeVar("T")


def _as_session(session: str, operation: Callable[[], T]) -> T:
    """:return: the result of ``operation``, run under ``session``'s key, as a tool call of that session runs"""

    def bound() -> T:
        _SESSION_KEY_VAR.set(session)
        return operation()

    return contextvars.copy_context().run(bound)


def _project(root: Path, name: str, marker: str) -> Project:
    """
    :param root: the project root, created with one file holding ``marker``
    :param name: the project name
    :param marker: the text of the project's one file, named after it
    :return: a project with no languages, so activating it starts no language server
    """
    root.mkdir(parents=True)
    (root / f"{marker}.txt").write_text(f"{marker}\n", encoding="utf-8")
    config = ProjectConfig(project_name=name, language_servers=[], ignored_paths=[], ignore_all_files_in_gitignore=True, encoding="utf-8")
    return Project(project_root=str(root), project_config=config, serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    """A minimal agent holding no project."""
    serena_agent = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    yield serena_agent
    serena_agent.on_shutdown(timeout=0.5)


@pytest.fixture(autouse=True)
def _no_pipe_session() -> Iterator[None]:
    """Keeps the pipe transport's session id out of scope, so the session key alone identifies a session."""
    token = _PIPE_SESSION_ID_VAR.set(None)
    yield
    _PIPE_SESSION_ID_VAR.reset(token)


def test_two_sessions_activating_two_projects_each_read_their_own_tree(agent: SerenaAgent, tmp_path: Path) -> None:
    first = _project(tmp_path / "first", "first", "alpha")
    second = _project(tmp_path / "second", "second", "omega")
    _as_session("cc-session-1", lambda: agent._activate_project(first))
    _as_session("cc-session-2", lambda: agent._activate_project(second))
    list_dir = agent.get_tool(ListDirTool)
    search = agent.get_tool(SearchForPatternTool)

    # each session's listing and search see its own project's file and not the other's
    first_listing = _as_session("cc-session-1", lambda: list_dir.apply())
    second_listing = _as_session("cc-session-2", lambda: list_dir.apply())
    assert "alpha.txt" in first_listing and "omega.txt" not in first_listing, first_listing
    assert "omega.txt" in second_listing and "alpha.txt" not in second_listing, second_listing

    first_search = _as_session("cc-session-1", lambda: search.apply(substring_pattern="alpha|omega"))
    second_search = _as_session("cc-session-2", lambda: search.apply(substring_pattern="alpha|omega"))
    assert "alpha.txt" in first_search and "omega.txt" not in first_search, first_search
    assert "omega.txt" in second_search and "alpha.txt" not in second_search, second_search

    assert _as_session("cc-session-1", lambda: agent.get_active_project_or_raise().project_root) == first.project_root
    assert _as_session("cc-session-2", lambda: agent.get_active_project_or_raise().project_root) == second.project_root
