"""Tests that the REPL's enclosing-symbol search credits each match to its own enclosing symbol."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.repl.api.fs_api import FsApi

_MODULE = "attribution_sandbox.py"


@pytest.fixture
def adjacent_functions(python_serena_agent: SerenaAgent) -> Iterator[str]:
    path = Path(python_serena_agent.get_active_project_or_raise().project_root) / _MODULE
    path.write_text("def first():\n    return 'needle'\n\n\ndef second():\n    return 'needle'\n", encoding="utf-8")
    try:
        yield _MODULE
    finally:
        path.unlink()


def test_matches_in_adjacent_symbols_with_overlapping_windows_are_each_credited(
    python_serena_agent: SerenaAgent, adjacent_functions: str
) -> None:
    hits = FsApi(python_serena_agent).search_by_enclosing_symbol(
        "needle", relative_path=adjacent_functions, context_lines_before=2, context_lines_after=2
    )

    symbols = hits.render_symbol_list()
    assert "@ first " in symbols
    assert "@ second " in symbols


@pytest.fixture
def adjacent_matches(python_serena_agent: SerenaAgent) -> Iterator[str]:
    path = Path(python_serena_agent.get_active_project_or_raise().project_root) / _MODULE
    path.write_text("def counted():\n    a = 'needle'\n    b = 'needle'\n    return a + b\n", encoding="utf-8")
    try:
        yield _MODULE
    finally:
        path.unlink()


def test_matches_on_adjacent_lines_of_one_symbol_are_each_counted(python_serena_agent: SerenaAgent, adjacent_matches: str) -> None:
    hits = FsApi(python_serena_agent).search_by_enclosing_symbol(
        "needle", relative_path=adjacent_matches, context_lines_before=1, context_lines_after=1
    )

    assert hits.render_summary().startswith("Found 2 matches across 1 symbols in 1 files.")
    symbols = hits.render_symbol_list()
    assert "@ counted " in symbols
    assert " - 2 matches" in symbols


def test_full_and_per_file_views_show_and_count_the_matched_lines_of_a_symbol(
    python_serena_agent: SerenaAgent, adjacent_matches: str
) -> None:
    hits = FsApi(python_serena_agent).search_by_enclosing_symbol(
        "needle", relative_path=adjacent_matches, context_lines_before=1, context_lines_after=1
    )

    full = hits.render_full()
    assert "@ counted " in full
    assert "a = 'needle'" in full and "b = 'needle'" in full
    assert hits.render_per_file_counts().splitlines()[-1] == f"{adjacent_matches}: 2 matches"


@pytest.fixture
def module_level_match(python_serena_agent: SerenaAgent) -> Iterator[str]:
    path = Path(python_serena_agent.get_active_project_or_raise().project_root) / _MODULE
    path.write_text("# needle\n\n\ndef unrelated():\n    return 1\n", encoding="utf-8")
    try:
        yield _MODULE
    finally:
        path.unlink()


def test_a_match_outside_any_symbol_renders_as_a_file_block(python_serena_agent: SerenaAgent, module_level_match: str) -> None:
    hits = FsApi(python_serena_agent).search_by_enclosing_symbol("needle", relative_path=module_level_match)

    full = hits.render_full()
    assert f"@ {module_level_match} [{module_level_match}]" in full
    assert "# needle" in full
    assert "(use cursor_overview to navigate)" in full
    assert f"@ {module_level_match} [{module_level_match}] - 1 matches (use cursor_overview to navigate)" in hits.render_symbol_list()
    assert hits.render_summary() == "Found 1 matches across 0 symbols in 1 files."
