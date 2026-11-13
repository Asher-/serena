"""Tests for what cursor_grep reports: its no-match answer, directory-scoped globs and hit blocks."""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from serena.agent import SerenaAgent
from serena.tools.cursor_tools import CursorGrepTool

_SANDBOX = "grep_report_sandbox"


@pytest.fixture
def sandbox(python_serena_agent: SerenaAgent) -> Iterator[Path]:
    root = Path(python_serena_agent.get_active_project_or_raise().project_root) / _SANDBOX
    (root / "sub").mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _grep(agent: SerenaAgent, **kwargs: Any) -> str:
    return agent.get_tool(CursorGrepTool).apply(because="test fixture: exercising the tool behaviour", max_answer_chars=1000000, **kwargs)


class TestNoMatchAnswer:
    def test_pattern_is_shown_exactly_as_sent(self, python_serena_agent: SerenaAgent) -> None:
        pattern = r"__no_such\(token__"

        result = _grep(python_serena_agent, substring_pattern=pattern, relative_path=os.path.join("test_repo", "services.py"))

        assert "No matches" in result
        assert pattern in result

    def test_a_file_outside_the_code_files_is_named_as_the_reason(self, python_serena_agent: SerenaAgent, sandbox: Path) -> None:
        (sandbox / "notes.md").write_text("needle\n", encoding="utf-8")

        with pytest.raises(ValueError, match="restrict_to_code_files"):
            _grep(python_serena_agent, substring_pattern="needle", relative_path=f"{_SANDBOX}/notes.md")


class TestDirectoryScopedGlobs:
    @pytest.fixture
    def markdown_tree(self, sandbox: Path) -> Path:
        (sandbox / "a.md").write_text("needle\n", encoding="utf-8")
        (sandbox / "sub" / "b.md").write_text("needle\n", encoding="utf-8")
        return sandbox

    def test_include_glob_matches_files_inside_the_directory(self, python_serena_agent: SerenaAgent, markdown_tree: Path) -> None:
        all_markdown = _grep(
            python_serena_agent, substring_pattern="needle", relative_path=_SANDBOX, paths_include_glob="*.md", restrict_to_code_files=False
        )
        nested_markdown = _grep(
            python_serena_agent,
            substring_pattern="needle",
            relative_path=_SANDBOX,
            paths_include_glob="sub/*.md",
            restrict_to_code_files=False,
        )

        assert f"{_SANDBOX}/a.md" in all_markdown
        assert f"{_SANDBOX}/sub/b.md" in nested_markdown

    def test_exclude_glob_drops_a_file_inside_the_directory(self, python_serena_agent: SerenaAgent, markdown_tree: Path) -> None:
        result = _grep(
            python_serena_agent,
            substring_pattern="needle",
            relative_path=_SANDBOX,
            paths_exclude_glob="sub/*",
            restrict_to_code_files=False,
        )

        assert f"{_SANDBOX}/a.md" in result
        assert f"{_SANDBOX}/sub/b.md" not in result


class TestHitBlocks:
    def test_two_matches_on_one_line_render_that_line_once(self, python_serena_agent: SerenaAgent, sandbox: Path) -> None:
        (sandbox / "c.md").write_text("before\nalpha beta\nafter\n", encoding="utf-8")

        result = _grep(
            python_serena_agent,
            substring_pattern="alpha|beta",
            relative_path=f"{_SANDBOX}/c.md",
            restrict_to_code_files=False,
            context_lines_before=1,
            context_lines_after=1,
        )

        assert result.count("alpha beta") == 1
        assert "1 hit(s)" in result
