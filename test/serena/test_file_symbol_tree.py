"""
The document-symbol tree of a file resolves the symbol that encloses a line: the innermost container or variable holding
the line's first non-whitespace column, else the narrowest root symbol covering the line.
"""

import pytest

from serena.cursor import _FileSymbolTree
from serena.util.text_utils import LineType, TextLine
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path

pytestmark = pytest.mark.python

_NESTED = "test_repo/nested.py"


def _line(line_number: int) -> TextLine:
    content = (get_repo_path(LanguageServerId.PYTHON) / _NESTED).read_text(encoding="utf-8").split("\n")[line_number]
    return TextLine(line_number=line_number, line_content=content, match_type=LineType.MATCH)


@pytest.mark.parametrize("language_server", [LanguageServerId.PYTHON], indirect=True)
@pytest.mark.parametrize(
    ("line_number", "expected_name_path"),
    [
        pytest.param(2, "OuterClass/NestedClass/find_me", id="a line held by a nested container"),
        pytest.param(4, "OuterClass", id="a blank line inside a root symbol"),
        pytest.param(15, "b", id="a single-line top-level symbol no container holds"),
    ],
)
def test_the_enclosing_symbol_of_a_line(language_server: SolidLanguageServer, line_number: int, expected_name_path: str) -> None:
    tree = _FileSymbolTree(language_server.request_document_symbols(_NESTED))

    symbol = tree.enclosing_symbol(_line(line_number))

    assert symbol is not None
    assert symbol.get_name_path() == expected_name_path
