"""tests of the bound on the cursors a session's :class:`CursorManager` holds.

a manager holds at most :attr:`CursorManager.MAX_CURSORS` cursors; adding one more evicts the cursor used least recently.
Using an id the manager generated that names no held cursor reports that the cursor was closed or evicted, however long
ago that was; any other id that names no held cursor is reported as an unknown or evicted cursor, and the manager
remembers nothing of evicted ids.
"""

import logging
import re
import tracemalloc
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.cursor import CursorManager
from serena.language_backend import BuiltinLanguageBackend
from serena.symbol import LanguageServerSymbol
from serena.tools.cursor_tools import CursorGrepTool
from solidlsp import ls_types
from solidlsp.ls_config import LanguageServerId


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


@pytest.mark.python
@pytest.mark.parametrize("uncapped", [True, False], ids=["uncapped", "above_the_cap"])
def test_a_grep_over_more_symbols_than_the_cap_opens_cursors_up_to_the_cap_and_keeps_every_one(tmp_path: Path, uncapped: bool) -> None:
    # a Python project whose matches lie in one more function than the cursors a session holds
    root = tmp_path / "project"
    (root / ".serena").mkdir(parents=True)
    (root / ".serena" / "project.yml").write_text(
        f"project_name: {root.name}\nlanguage_servers:\n- {LanguageServerId.PYTHON.get_key()}\nlanguage_backend: LSP\n", encoding="utf-8"
    )
    (root / "module.py").write_text(
        "".join(f"def function_{index}():\n    return 'needle'\n\n\n" for index in range(CursorManager.MAX_CURSORS + 1)), encoding="utf-8"
    )
    agent = SerenaAgent(
        project=str(root),
        serena_config=SerenaConfig(
            gui_log_window=False, web_dashboard=False, log_level=logging.ERROR, language_backend=BuiltinLanguageBackend.LSP.get_instance()
        ),
    )
    try:
        # a grep uncapped, or capped above the cursors a session holds
        max_matches = -1 if uncapped else CursorManager.MAX_CURSORS + 1

        result = agent.get_tool(CursorGrepTool).apply(
            substring_pattern="needle", because="to see the cap", max_matches=max_matches, max_answer_chars=10**8
        )

        assert f"Started {CursorManager.MAX_CURSORS} cursor(s); 1 symbol(s) deferred." in result
        opened = re.findall(r"^\[(c\d+)\]", result, re.MULTILINE)
        assert len(opened) == CursorManager.MAX_CURSORS
        assert set(opened) == set(agent.get_cursor_manager().list_cursors())

        # a further grep, whose one cursor evicts the cursor used least recently, the first this grep opened
        agent.get_tool(CursorGrepTool).apply(substring_pattern=r"def function_1\(", because="to see the eviction", max_answer_chars=10**8)
        with pytest.raises(ValueError, match="was closed or evicted"):
            agent.get_cursor_manager().get_cursor(opened[0])
    finally:
        agent.on_shutdown(timeout=1.0)


def test_a_closed_cursor_frees_its_place_under_the_cap(manager: CursorManager) -> None:
    kept = _start(manager)
    for _ in range(CursorManager.MAX_CURSORS - 1):
        closed = _start(manager)
        manager.close_cursor(closed)

    for _ in range(CursorManager.MAX_CURSORS - 1):
        _start(manager)

    assert kept in manager.list_cursors()
