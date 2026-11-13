"""
A refusal of the cursor edit surface names the caller's relative path and name path exactly as sent.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.tools.cursor_tools import CursorReplaceBetweenTool, CursorReplaceRangeTool, CursorReplaceRangeVerifiedTool

_QUOTED_PATH = "test_repo/it's_absent.py"
_QUOTED_ANCHOR = "Isn't"
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
