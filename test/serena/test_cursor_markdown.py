"""
Integration tests for cursor tools against a Markdown project (M4 LSP).

Exercises ``CursorOverviewTool``, ``CursorStartTool``, and ``CursorConfigureTool``
through ``SerenaAgent.get_tool()`` against the Markdown test repository,
covering the full agent path from project activation through the
``SerenaMarkdownLanguageServer`` ``documentSymbol`` handler.
"""

from collections.abc import Iterator

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.project import Project
from serena.tools.cursor_tools import CursorConfigureTool, CursorOverviewTool, CursorStartTool
from solidlsp.ls_config import Language
from test.conftest import get_repo_path

pytestmark = pytest.mark.markdown


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def markdown_serena_agent() -> Iterator[SerenaAgent]:
    """SerenaAgent configured for the Markdown test repo with Language.MARKDOWN active."""
    # build a minimal SerenaConfig so the agent does not try to launch the dashboard or GUI
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    repo_path = get_repo_path(Language.MARKDOWN)

    # construct the project explicitly so we control the language list
    project = Project(
        project_root=str(repo_path),
        project_config=ProjectConfig(
            project_name="test_repo_markdown",
            languages=[Language.MARKDOWN],
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

    # the agent must run a no-op task to materialize the language server manager
    agent = SerenaAgent(project="test_repo_markdown", serena_config=config)
    agent.execute_task(lambda: None)
    try:
        yield agent
    finally:
        agent.on_shutdown(timeout=5)


# ===========================================================================
# Cursor overview / navigation tests on Markdown
# ===========================================================================


class TestMarkdownCursorOverview:
    """Test cursor_overview against the Markdown LSP via the agent path."""

    def test_overview_lists_top_level_heading(self, markdown_serena_agent: SerenaAgent) -> None:
        """cursor_overview returns the top-level heading on a Markdown file."""
        # CursorOverviewTool only emits top-level symbols; nested headings are
        # reachable via cursor_start name_path segments.
        overview_tool = markdown_serena_agent.get_tool(CursorOverviewTool)
        result = overview_tool.apply(because="test fixture: exercising the tool behaviour", relative_path="README.md")
        assert "Top-level symbols in README.md" in result
        # overview entries share the symbolic-projection ``name :Kind@file:line:`` shape
        assert "Test Repository :Namespace@README.md:1:" in result

    def test_overview_missing_file_raises(self, markdown_serena_agent: SerenaAgent) -> None:
        """cursor_overview raises FileNotFoundError for a missing Markdown file."""
        # mirrors the Python-side test for parity across language backends
        overview_tool = markdown_serena_agent.get_tool(CursorOverviewTool)
        with pytest.raises(FileNotFoundError):
            overview_tool.apply(because="test fixture: exercising the tool behaviour", relative_path="does/not/exist.md")


class TestMarkdownCursorBody:
    """Test that cursor body extraction returns the heading section text."""

    def test_cursor_start_with_body_returns_section_text(self, markdown_serena_agent: SerenaAgent) -> None:
        """cursor_start + cursor_configure(include_body=true) returns the section body."""
        # cursor_start positions on the nested heading via its name_path segments
        start_tool = markdown_serena_agent.get_tool(CursorStartTool)
        configure_tool = markdown_serena_agent.get_tool(CursorConfigureTool)

        start_result = start_tool.apply(
            because="test fixture: exercising the tool behaviour",
            relative_path="README.md",
            name_path="Test Repository/Overview",
            cursor_id="md-overview",
        )
        assert "Overview" in start_result
        # the new symbolic projection glues kind into ``:Kind@file:line:`` form
        assert ":Namespace@" in start_result

        # body is sourced from the M4 backend via _MdSymbolRef.body_range,
        # routed through the LSP server's documentSymbol range
        body_result = configure_tool.apply(cursor_id="md-overview", include_body=True)
        assert "--- body ---" in body_result
        assert "## Overview" in body_result
        assert "This repository contains sample markdown files" in body_result
