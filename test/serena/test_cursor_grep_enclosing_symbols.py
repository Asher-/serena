"""
A cursor grep groups each hit under its enclosing symbol, and it asks the language server for each file's symbols once,
however many hits the file has.
"""

from typing import Any

import pytest

from serena.agent import SerenaAgent
from serena.cursor import MatchedConsecutiveLines
from serena.symbol import LanguageServerSymbol

pytestmark = pytest.mark.python

_Groups = list[tuple[LanguageServerSymbol, list[MatchedConsecutiveLines]]]
_Unsymboled = list[tuple[str, list[MatchedConsecutiveLines]]]


def _grep(agent: SerenaAgent, pattern: str, relative_path: str) -> tuple[_Groups, _Unsymboled]:
    return agent.execute_task(lambda: agent.get_cursor_manager().find_pattern_with_enclosing_symbols(pattern, relative_path=relative_path))


def _lines(hits: list[MatchedConsecutiveLines]) -> list[int]:
    return [line.line_number for hit in hits for line in hit.matched_lines]


@pytest.mark.parametrize(
    ("relative_path", "expected_groups", "expected_unsymboled"),
    [
        pytest.param(
            "test_repo/nested.py",
            [
                ("OuterClass", [0]),
                ("OuterClass/NestedClass", [1]),
                ("OuterClass/NestedClass/find_me", [2, 3]),
                ("OuterClass/nested_test", [5]),
                ("OuterClass/nested_test/WithinMethod", [6, 7]),
                ("OuterClass/nested_test/func_within_func", [9, 10]),
                ("OuterClass/nested_test/a", [12]),
                ("b", [15]),
            ],
            [],
            id="nested symbols and name lines",
        ),
        pytest.param(
            "test_repo/variables.py",
            [
                ("module_var", [13]),
                ("reassignable_module_var", [15]),
                ("typed_module_var", [19]),
                ("VariableContainer", [22, 23, 25, 29, 31]),
                ("VariableContainer/class_var", [26]),
                ("VariableContainer/reassignable_class_var", [28]),
                ("VariableContainer/typed_class_var", [32]),
                ("VariableContainer/__init__", [34, 35, 36, 37, 39, 40]),
                ("VariableContainer/modify_instance_var", [42, 43, 44, 45]),
                ("VariableContainer/use_module_var", [47, 48, 51]),
                ("VariableContainer/use_module_var/result", [49]),
                ("VariableContainer/use_module_var/other_result", [50]),
                ("VariableContainer/use_class_var", [53, 54, 57]),
                ("VariableContainer/use_class_var/result", [55]),
                ("VariableContainer/use_class_var/other_result", [56]),
                ("VariableDataclass", [60, 61, 62, 64, 71]),
                ("VariableDataclass/id", [65]),
                ("VariableDataclass/name", [66]),
                ("VariableDataclass/items", [67]),
                ("VariableDataclass/metadata", [68]),
                ("VariableDataclass/optional_value", [69]),
                ("VariableDataclass/status", [72]),
                ("use_module_variables", [75, 76, 79]),
                ("use_module_variables/result", [77]),
                ("use_module_variables/other_result", [78]),
                ("dataclass_instance", [83]),
                ("module_result", [87]),
                ("other_module_result", [88]),
                ("second_dataclass", [91]),
            ],
            [("test_repo/variables.py", [0, 1, 3, 4, 5, 6, 7, 8, 10, 12, 16, 18, 82, 84, 86, 90, 92])],
            id="hits outside any symbol",
        ),
        pytest.param(
            "test_repo/one_line_symbols.py",
            [("Marker", [5]), ("noop", [8])],
            [("test_repo/one_line_symbols.py", [0, 1, 2])],
            id="single-line top-level symbols",
        ),
    ],
)
def test_a_grep_groups_every_hit_under_its_enclosing_symbol(
    python_serena_agent: SerenaAgent,
    relative_path: str,
    expected_groups: list[tuple[str, list[int]]],
    expected_unsymboled: list[tuple[str, list[int]]],
) -> None:
    groups, unsymboled = _grep(python_serena_agent, r"\S", relative_path)

    assert [(symbol.get_name_path(), _lines(hits)) for symbol, hits in groups] == expected_groups
    assert [(path, _lines(hits)) for path, hits in unsymboled] == expected_unsymboled


def test_a_grep_requests_the_symbols_of_each_file_with_matches_once(
    python_serena_agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = python_serena_agent
    language_server = agent.get_language_server_manager_or_raise().get_language_server("test_repo/services.py")
    requested: list[str] = []
    request_document_symbols = language_server.request_document_symbols

    def counted_request_document_symbols(relative_file_path: str, *args: Any, **kwargs: Any) -> Any:
        requested.append(relative_file_path)
        return request_document_symbols(relative_file_path, *args, **kwargs)

    monkeypatch.setattr(language_server, "request_document_symbols", counted_request_document_symbols)

    groups, unsymboled = _grep(agent, r"^(import|from) |self", "test_repo")

    files_with_matches = {symbol.relative_path for symbol, _ in groups} | {path for path, _ in unsymboled}
    assert len(files_with_matches) > 1
    assert sorted(requested) == sorted(files_with_matches)
