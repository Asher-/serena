"""
A cursor grep names each enclosing symbol by the symbol tree the symbol was found in, whatever parent links a language
server leaves on the symbols.
"""

from collections.abc import Iterator
from typing import Any, cast

import pytest

from serena.agent import SerenaAgent
from solidlsp import ls_types
from solidlsp.ls import DocumentSymbols, SymbolTreeCopy

pytestmark = pytest.mark.python


def _iter_symbols(symbol: ls_types.UnifiedSymbolInformation) -> Iterator[ls_types.UnifiedSymbolInformation]:
    yield symbol
    for child in symbol.get("children", []):
        yield from _iter_symbols(child)


def test_a_grep_names_an_enclosing_symbol_by_the_tree_it_was_found_in(
    python_serena_agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = python_serena_agent
    language_server = agent.get_language_server_manager_or_raise().get_language_server("test_repo/nested.py")
    request_document_symbols = language_server.request_document_symbols

    def symbols_with_a_stale_parent_link(relative_file_path: str, *args: Any, **kwargs: Any) -> DocumentSymbols:
        # a tree of its own, whose find_me links to a parent outside the tree, as some servers' symbols do
        roots = [SymbolTreeCopy(root).root for root in request_document_symbols(relative_file_path, *args, **kwargs).root_symbols]
        for root in roots:
            for symbol in _iter_symbols(root):
                if symbol["name"] == "find_me":
                    symbol["parent"] = cast(ls_types.UnifiedSymbolInformation, {**symbol["parent"], "name": "Stale", "parent": None})
        return DocumentSymbols(roots)

    monkeypatch.setattr(language_server, "request_document_symbols", symbols_with_a_stale_parent_link)

    groups, _ = agent.execute_task(
        lambda: agent.get_cursor_manager().find_pattern_with_enclosing_symbols(r"\S", relative_path="test_repo/nested.py")
    )

    name_paths_by_line = {line.line_number: symbol.get_name_path() for symbol, hits in groups for hit in hits for line in hit.matched_lines}
    assert name_paths_by_line[2] == "OuterClass/NestedClass/find_me"
