"""Tests that the line-range and container edit tools edit files of a project that runs no language server."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.project import Project
from serena.tools.cursor_tools import (
    CursorInsertAtEndTool,
    CursorInsertAtStartTool,
    CursorReplaceBetweenTool,
    CursorReplaceRangeTool,
    CursorReplaceRangeVerifiedTool,
    CursorStartTool,
)

_NOTES = "# Title\n\nalpha\n\n## Second\n\nbeta\n"
_SETTINGS = '{\n  "items": [\n    2\n  ]\n}\n'


@pytest.fixture
def markdown_project(tmp_path: Path) -> Iterator[tuple[SerenaAgent, Path]]:
    (tmp_path / "notes.md").write_text(_NOTES, encoding="utf-8")
    (tmp_path / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    project = Project(
        project_root=str(tmp_path),
        project_config=ProjectConfig(
            project_name="markdown_only",
            language_servers=[],
            ignored_paths=[],
            excluded_tools=[],
            read_only=False,
            ignore_all_files_in_gitignore=True,
            initial_prompt="",
            encoding="utf-8",
        ),
        serena_config=config,
    )
    config.projects = [RegisteredProject.from_project_instance(project)]
    agent = SerenaAgent(project="markdown_only", serena_config=config)
    agent.execute_task(lambda: None)
    try:
        yield agent, tmp_path
    finally:
        agent.on_shutdown(timeout=5)


def _apply(agent: SerenaAgent, tool_class: type, **kwargs: object) -> str:
    tool = agent.get_tool(tool_class)
    return agent.execute_task(lambda: tool.apply(**kwargs))


def test_replace_range_edits_the_file(markdown_project: tuple[SerenaAgent, Path]) -> None:
    agent, root = markdown_project

    _apply(agent, CursorReplaceRangeTool, relative_path="notes.md", start_line=3, end_line=3, body="ALPHA", expect_version="*")

    assert (root / "notes.md").read_text(encoding="utf-8") == _NOTES.replace("alpha", "ALPHA")


def test_replace_range_verified_edits_the_file(markdown_project: tuple[SerenaAgent, Path]) -> None:
    agent, root = markdown_project

    _apply(
        agent,
        CursorReplaceRangeVerifiedTool,
        relative_path="notes.md",
        start_line=7,
        end_line=7,
        expected_content="beta",
        body="BETA",
        expect_version="*",
    )

    assert (root / "notes.md").read_text(encoding="utf-8") == _NOTES.replace("beta", "BETA")


def test_replace_between_edits_the_file(markdown_project: tuple[SerenaAgent, Path]) -> None:
    agent, root = markdown_project

    _apply(
        agent,
        CursorReplaceBetweenTool,
        relative_path="notes.md",
        before_symbol="Title",
        after_symbol="Title/Second",
        body="\ngamma\n\n",
        expect_version="*",
    )

    assert (root / "notes.md").read_text(encoding="utf-8") == "# Title\n\ngamma\n\n## Second\n\nbeta\n"


def test_replace_between_an_absent_anchor_leaves_the_file_unchanged(markdown_project: tuple[SerenaAgent, Path]) -> None:
    agent, root = markdown_project

    with pytest.raises(Exception):
        _apply(
            agent,
            CursorReplaceBetweenTool,
            relative_path="notes.md",
            before_symbol="Title",
            after_symbol="Title/Missing",
            body="\ngamma\n\n",
            expect_version="*",
        )

    assert (root / "notes.md").read_text(encoding="utf-8") == _NOTES


@pytest.mark.parametrize(("tool_class", "expected_items"), [(CursorInsertAtStartTool, [1, 2]), (CursorInsertAtEndTool, [2, 3])])
def test_container_insert_edits_the_file(markdown_project: tuple[SerenaAgent, Path], tool_class: type, expected_items: list[int]) -> None:
    agent, root = markdown_project
    _apply(
        agent,
        CursorStartTool,
        because="test fixture: exercising the tool behaviour",
        name_path="items",
        relative_path="settings.json",
        cursor_id="items",
    )

    _apply(agent, tool_class, cursor_id="items", body="1" if tool_class is CursorInsertAtStartTool else "3", expect_version="*")

    assert json.loads((root / "settings.json").read_text(encoding="utf-8"))["items"] == expected_items
