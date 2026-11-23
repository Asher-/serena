"""tests of the bound on the cursors a session's :class:`CursorManager` holds.

a manager holds at most :attr:`CursorManager.MAX_CURSORS` cursors; adding one more evicts the cursor used least recently.
Using an id the manager generated that names no held cursor reports that the cursor was closed or evicted, however long
ago that was; any other id that names no held cursor is reported as an unknown or evicted cursor, and the manager
remembers nothing of evicted ids.
"""

import re
import tracemalloc
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.cursor import CursorManager
from serena.symbol import LanguageServerSymbol
from serena.tools.cursor_tools import CursorGrepTool
from serena.util.text_utils import LineType, MatchedConsecutiveLines, TextLine
from solidlsp import ls_types


@pytest.fixture
def manager(tmp_path: Path) -> CursorManager:
    """
    :return: a manager over a stub project
    """
    project = MagicMock()
    project.project_root = str(tmp_path)
    return CursorManager(project)


def _symbol(line: int) -> LanguageServerSymbol:
    """
    :param line: the line of the symbol
    :return: a function symbol of ``module.py``, as a language server reports one
    """
    position_range = {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 1}}
    return LanguageServerSymbol(
        ls_types.UnifiedSymbolInformation(  # type: ignore[typeddict-item]
            name=f"function_{line}",
            kind=ls_types.SymbolKind.Function,
            range=position_range,
            selectionRange=position_range,
            location={"uri": "file:///module.py", "range": position_range, "relativePath": "module.py", "absolutePath": "/module.py"},
            children=[],
        )
    )


def _start(manager: CursorManager) -> str:
    """
    :return: the id of a newly registered cursor
    """
    cursor_id, _state = manager.register_cursor_at_symbol(_symbol(len(manager.list_cursors())))
    return cursor_id


def test_a_manager_holds_at_most_the_cap_however_many_cursors_are_started(manager: CursorManager) -> None:
    for _ in range(CursorManager.MAX_CURSORS + 50):
        _start(manager)

    assert len(manager.list_cursors()) == CursorManager.MAX_CURSORS


def test_starting_a_cursor_beyond_the_cap_evicts_the_least_recently_used_one(manager: CursorManager) -> None:
    first = _start(manager)
    second = _start(manager)
    for _ in range(CursorManager.MAX_CURSORS - 2):
        _start(manager)
    manager.get_cursor(first)

    newest = _start(manager)

    assert first in manager.list_cursors()
    assert newest in manager.list_cursors()
    assert second not in manager.list_cursors()


def test_using_an_evicted_cursor_reports_its_eviction_for_the_cap(manager: CursorManager) -> None:
    evicted = _start(manager)
    for _ in range(CursorManager.MAX_CURSORS):
        _start(manager)

    with pytest.raises(ValueError, match="evicted"):
        manager.get_cursor(evicted)


def test_a_cursor_evicted_long_ago_still_reports_its_eviction(manager: CursorManager) -> None:
    evicted = _start(manager)
    for _ in range(CursorManager.MAX_CURSORS * 2):
        _start(manager)

    with pytest.raises(ValueError, match="was closed or evicted"):
        manager.get_cursor(evicted)


def test_a_named_cursor_evicted_reports_an_unknown_or_evicted_cursor(manager: CursorManager) -> None:
    manager.register_cursor_at_symbol(_symbol(0), cursor_id="named")
    for _ in range(CursorManager.MAX_CURSORS):
        _start(manager)

    with pytest.raises(ValueError, match="unknown or evicted cursor"):
        manager.get_cursor("named")


def test_using_an_id_no_cursor_had_reports_an_unknown_or_evicted_cursor(manager: CursorManager) -> None:
    _start(manager)

    with pytest.raises(ValueError, match="No cursor with id 'never-started': unknown or evicted cursor"):
        manager.get_cursor("never-started")


@pytest.mark.parametrize("cursor_id", ["c10000000", "c" + "9" * 30], ids=["large", "overlong"])
def test_evicting_a_cursor_named_like_a_generated_one_allocates_nothing_for_its_number(manager: CursorManager, cursor_id: str) -> None:
    manager.register_cursor_at_symbol(_symbol(0), cursor_id=cursor_id)
    for _ in range(CursorManager.MAX_CURSORS - 1):
        _start(manager)

    # the start that evicts the named cursor
    tracemalloc.start()
    try:
        _start(manager)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 256 * 1024
    with pytest.raises(ValueError, match="unknown or evicted cursor"):
        manager.get_cursor(cursor_id)


def test_an_id_with_a_leading_zero_is_not_the_generated_id_of_its_number(manager: CursorManager) -> None:
    manager.register_cursor_at_symbol(_symbol(0), cursor_id="c01")
    for index in range(CursorManager.MAX_CURSORS):
        manager.register_cursor_at_symbol(_symbol(index + 1), cursor_id=f"named-{index}")

    # no cursor was ever generated, so neither id is one the manager generated
    with pytest.raises(ValueError, match="No cursor with id 'c1': unknown or evicted cursor"):
        manager.get_cursor("c1")
    with pytest.raises(ValueError, match="No cursor with id 'c01': unknown or evicted cursor"):
        manager.get_cursor("c01")


@pytest.mark.parametrize("uncapped", [True, False], ids=["uncapped", "above_the_cap"])
def test_a_grep_over_more_symbols_than_the_cap_opens_cursors_up_to_the_cap_and_keeps_every_one(
    manager: CursorManager, uncapped: bool
) -> None:
    # a grep, uncapped or capped above the cursors a session holds, whose matches lie in one more symbol than that
    max_matches = -1 if uncapped else CursorManager.MAX_CURSORS + 1
    groups = [
        (_symbol(line), [MatchedConsecutiveLines(lines=[TextLine(line_number=line, line_content="x", match_type=LineType.MATCH)])])
        for line in range(CursorManager.MAX_CURSORS + 1)
    ]
    manager.find_pattern_with_enclosing_symbols = lambda *args, **kwargs: (groups, [])  # type: ignore[method-assign]
    tool = object.__new__(CursorGrepTool)
    tool.agent = MagicMock()
    tool.agent.get_cursor_manager.return_value = manager

    result = tool.apply(substring_pattern="x", because="to see the cap", max_matches=max_matches, max_answer_chars=10**8)

    assert f"Started {CursorManager.MAX_CURSORS} cursor(s); 1 symbol(s) deferred." in result
    opened = re.findall(r"^\[(c\d+)\]", result, re.MULTILINE)
    assert len(opened) == CursorManager.MAX_CURSORS
    assert set(opened) == set(manager.list_cursors())


def test_a_closed_cursor_frees_its_place_under_the_cap(manager: CursorManager) -> None:
    kept = _start(manager)
    for _ in range(CursorManager.MAX_CURSORS - 1):
        closed = _start(manager)
        manager.close_cursor(closed)

    for _ in range(CursorManager.MAX_CURSORS - 1):
        _start(manager)

    assert kept in manager.list_cursors()
