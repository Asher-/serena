"""tests of the bound on the cursors a session's :class:`CursorManager` holds.

a manager holds at most :attr:`CursorManager.MAX_CURSORS` cursors; adding one more evicts the cursor used least recently,
and using an evicted cursor's id reports that it was evicted for the cap, however long ago that was.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.cursor import CursorManager
from serena.symbol import LanguageServerSymbol
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
    for _ in range(CursorManager.MAX_CURSORS * 10):
        _start(manager)

    with pytest.raises(ValueError, match="evicted"):
        manager.get_cursor(evicted)


def test_a_named_cursor_evicted_reports_its_eviction(manager: CursorManager) -> None:
    manager.register_cursor_at_symbol(_symbol(0), cursor_id="named")
    for _ in range(CursorManager.MAX_CURSORS):
        _start(manager)

    with pytest.raises(ValueError, match="evicted"):
        manager.get_cursor("named")


def test_using_an_id_no_cursor_had_reports_it_as_unknown_rather_than_evicted(manager: CursorManager) -> None:
    _start(manager)

    with pytest.raises(ValueError, match="No cursor with id 'never-started'"):
        manager.get_cursor("never-started")


def test_a_closed_cursor_frees_its_place_under_the_cap(manager: CursorManager) -> None:
    kept = _start(manager)
    for _ in range(CursorManager.MAX_CURSORS - 1):
        closed = _start(manager)
        manager.close_cursor(closed)

    for _ in range(CursorManager.MAX_CURSORS - 1):
        _start(manager)

    assert kept in manager.list_cursors()
