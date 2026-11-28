"""
A cursor grep names each enclosing symbol by the symbol tree the symbol was found in, whatever parent links the build of
a language server's symbols left.
"""

from typing import cast

import pytest

from serena.agent import SerenaAgent
from solidlsp import ls_types
from solidlsp.ls import DocumentSymbols, LSPFileBuffer

pytestmark = pytest.mark.python


def test_a_grep_names_an_enclosing_symbol_by_the_tree_it_was_found_in(
    python_serena_agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = python_serena_agent
    language_server = agent.get_language_server_manager_or_raise().get_language_server("test_repo/nested.py")
    build = language_server._build_document_symbols_from_raw_symbols

    def build_with_a_stale_parent_link(relative_file_path: str, file_buffer: LSPFileBuffer) -> DocumentSymbols:
        # find_me links to a parent outside its tree, as the built symbols of some servers do
        symbols = build(relative_file_path, file_buffer=file_buffer)
        for symbol in symbols.iter_symbols():
            if symbol["name"] == "find_me":
                symbol["parent"] = cast(ls_types.UnifiedSymbolInformation, {**symbol["parent"], "name": "Stale", "parent": None})
        return symbols

    monkeypatch.setattr(language_server, "_build_document_symbols_from_raw_symbols", build_with_a_stale_parent_link)
    groups, _ = agent.execute_task(
        lambda: agent.get_cursor_manager().find_pattern_with_enclosing_symbols(r"\S", relative_path="test_repo/nested.py")
    )

    name_paths_by_line = {line.line_number: symbol.get_name_path() for symbol, hits in groups for hit in hits for line in hit.matched_lines}
    assert name_paths_by_line[2] == "OuterClass/NestedClass/find_me"
