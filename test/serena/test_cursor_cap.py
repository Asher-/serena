"""tests of the bound on the cursors a session's :class:`CursorManager` holds.

a manager holds at most :attr:`CursorManager.MAX_CURSORS` cursors; starting one more evicts the cursor used least
recently, and using an evicted cursor's id reports that it was evicted for the cap.
"""

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from serena.cursor import CursorManager


@pytest.fixture
def manager(tmp_path: Path) -> CursorManager:
    """
    :return: a manager over a stub project holding ``notes.txt``, a file no language server claims
    """
    (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
    return CursorManager(project)


@pytest.fixture(autouse=True)
def lsp_miss():
    """Makes every language-server lookup miss, so each started cursor is a whole-file cursor on ``notes.txt``"""
    retriever = MagicMock()
    retriever.find_unique.side_effect = ValueError("no symbol")
    with patch.object(CursorManager, "_retriever", new_callable=PropertyMock, return_value=retriever):
        yield


def _start(manager: CursorManager) -> str:
    """
    :return: the id of a newly started cursor
    """
    cursor_id, _state = manager.start_cursor("notes.txt", relative_path="notes.txt")
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
