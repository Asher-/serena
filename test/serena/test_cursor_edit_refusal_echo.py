"""
A refusal of the cursor edit surface names the caller's relative path and name path exactly as sent.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.cursor import CursorManager
from serena.tools.cursor_tools import CursorReplaceBetweenTool, CursorReplaceRangeTool, CursorReplaceRangeVerifiedTool
from test.serena.test_cursor_plaintext import _force_lsp_miss, _manager
from test.serena.test_cursor_replace_range import _InMemoryCodeEditor

_QUOTED_PATH = "test_repo/it's_absent.py"
_QUOTED_ANCHOR = "Isn't"
_QUOTED_CONFIG = "it's/config.json"
_QUOTED_MEMBER = "don't"
_MODULE = "test_repo/_refusal_echo_sandbox.py"


@pytest.fixture
def anchor_module(python_serena_agent: SerenaAgent) -> Iterator[str]:
    """
    :return: the project-relative path of a module holding the top-level class ``Present``
    """
    path = Path(python_serena_agent.get_active_project_or_raise().project_root) / _MODULE
    path.write_text("class Present:\n    pass\n", encoding="utf-8")
    try:
        yield _MODULE
    finally:
        path.unlink()


def test_an_inverted_range_refusal_names_the_path_as_sent(python_serena_agent: SerenaAgent) -> None:
    tool = python_serena_agent.get_tool(CursorReplaceRangeTool)

    with pytest.raises(ValueError) as refusal:
        tool.apply(relative_path=_QUOTED_PATH, start_line=5, end_line=2, body="", expect_version="*")

    assert f"in '{_QUOTED_PATH}';" in str(refusal.value)


def test_an_inverted_verified_range_refusal_names_the_path_as_sent(python_serena_agent: SerenaAgent) -> None:
    tool = python_serena_agent.get_tool(CursorReplaceRangeVerifiedTool)

    with pytest.raises(ValueError) as refusal:
        tool.apply(relative_path=_QUOTED_PATH, start_line=5, end_line=2, expected_content="", body="", expect_version="*")

    assert f"in '{_QUOTED_PATH}';" in str(refusal.value)


def test_an_unresolved_anchor_refusal_names_the_anchor_and_path_as_sent(python_serena_agent: SerenaAgent, anchor_module: str) -> None:
    tool = python_serena_agent.get_tool(CursorReplaceBetweenTool)

    with pytest.raises(ValueError) as refusal:
        tool.apply(relative_path=anchor_module, before_symbol=_QUOTED_ANCHOR, after_symbol="Present", body="\n", expect_version="*")

    assert f"'{_QUOTED_ANCHOR}' did not resolve to any symbol in '{anchor_module}'." in str(refusal.value)


def test_a_start_line_beyond_the_file_names_the_path_as_sent() -> None:
    with pytest.raises(ValueError) as refusal:
        CursorReplaceRangeVerifiedTool._verify_expected(_QUOTED_PATH, "a\n", 5, 5, "x\n")

    assert f"in '{_QUOTED_PATH}'." in str(refusal.value)


def test_an_inverted_replace_lines_range_names_the_path_as_sent() -> None:
    editor = _InMemoryCodeEditor({_QUOTED_PATH: "a\nb\n"})

    with pytest.raises(ValueError) as refusal:
        editor.replace_lines(_QUOTED_PATH, start_line=3, end_line=2, content="")

    assert f"for '{_QUOTED_PATH}'" in str(refusal.value)


@pytest.fixture
def structural_cursor(tmp_path: Path) -> tuple[CursorManager, str, Path]:
    """
    :return: a cursor manager over ``tmp_path``, the id of a structural cursor on the member ``don't`` of
        ``it's/config.json``, and that file
    """
    config = tmp_path / "it's" / "config.json"
    config.parent.mkdir()
    config.write_text('{"don\'t": 1}\n', encoding="utf-8")
    manager = _manager(tmp_path)
    with _force_lsp_miss():
        cursor_id, _ = manager.start_cursor(_QUOTED_MEMBER, relative_path=_QUOTED_CONFIG)
    return manager, cursor_id, config


def test_an_lsp_only_operation_on_a_structural_cursor_names_its_path_and_member_as_sent(
    structural_cursor: tuple[CursorManager, str, Path],
) -> None:
    manager, cursor_id, _ = structural_cursor

    with pytest.raises(TypeError) as refusal:
        manager.get_lsp_cursor(cursor_id)

    assert f"at '{_QUOTED_CONFIG}':'{_QUOTED_MEMBER}';" in str(refusal.value)


def test_a_structural_cursor_that_cannot_reanchor_names_its_member_and_path_as_sent(
    structural_cursor: tuple[CursorManager, str, Path],
) -> None:
    manager, cursor_id, config = structural_cursor
    config.write_text('{"other": 1}\n', encoding="utf-8")

    with pytest.raises(ValueError) as refusal:
        manager.reanchor_cursor(cursor_id)

    assert f"path '{_QUOTED_MEMBER}' no longer resolves in '{_QUOTED_CONFIG}'" in str(refusal.value)


def test_a_structural_cursor_whose_member_moved_reanchors_on_it(structural_cursor: tuple[CursorManager, str, Path]) -> None:
    manager, cursor_id, config = structural_cursor
    config.write_text('{"first": 0,\n "don\'t": 2}\n', encoding="utf-8")

    state = manager.reanchor_cursor(cursor_id)

    assert state.name_path == _QUOTED_MEMBER
    assert manager.get_cursor(cursor_id).relative_path == _QUOTED_CONFIG
