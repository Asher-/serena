"""
The document symbols a language server returns have parent links that lead to the root of their own tree, whatever links
the build of the symbols left.
"""

from typing import cast

import pytest

from solidlsp import ls_types
from solidlsp.ls import DocumentSymbols, LSPFileBuffer
from solidlsp.ls_config import LanguageServerId
from test.conftest import start_default_ls_context

pytestmark = pytest.mark.python


def _ancestor_names(symbol: ls_types.UnifiedSymbolInformation) -> list[str]:
    names = []
    parent = symbol.get("parent")
    while parent is not None:
        names.append(parent["name"])
        parent = parent.get("parent")
    return names


def test_symbols_built_with_a_parent_link_outside_their_tree_are_returned_linked_to_their_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with start_default_ls_context(LanguageServerId.PYTHON) as language_server:
        build = language_server._build_document_symbols_from_raw_symbols

        def build_with_a_stale_parent_link(relative_file_path: str, file_buffer: LSPFileBuffer) -> DocumentSymbols:
            # find_me links to a parent outside its tree, as the built symbols of some servers do
            symbols = build(relative_file_path, file_buffer=file_buffer)
            for symbol in symbols.iter_symbols():
                if symbol["name"] == "find_me":
                    symbol["parent"] = cast(ls_types.UnifiedSymbolInformation, {**symbol["parent"], "name": "Stale", "parent": None})
            return symbols

        monkeypatch.setattr(language_server, "_build_document_symbols_from_raw_symbols", build_with_a_stale_parent_link)
        symbols = language_server.request_document_symbols("test_repo/nested.py")

    find_me = next(symbol for symbol in symbols.iter_symbols() if symbol["name"] == "find_me")
    assert _ancestor_names(find_me) == ["NestedClass", "OuterClass"]
