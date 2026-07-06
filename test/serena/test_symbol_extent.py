"""
Unit tests for :mod:`serena.symbol_extent` — the language-specific widening of
LSP symbol extents to the enclosing statement boundary.

These tests construct :class:`LanguageServerSymbol` instances from minimal symbol-root
dicts so the widening logic can be exercised without a running language server.
"""

from __future__ import annotations

from typing import Any

import pytest

from serena.symbol import LanguageServerSymbol, PositionInFile
from serena.symbol_extent import (
    GoSymbolExtentStrategy,
    IdentitySymbolExtentStrategy,
    PythonSymbolExtentStrategy,
    get_symbol_extent_strategy,
)
from solidlsp.lsp_protocol_handler.lsp_types import SymbolKind


def _make_symbol(
    name: str,
    kind: SymbolKind,
    name_line: int,
    lsp_end_line: int | None = None,
    lsp_end_col: int | None = None,
) -> LanguageServerSymbol:
    """
    Build a minimal :class:`LanguageServerSymbol` for strategy tests.

    :param name: the symbol's identifier.
    :param kind: the LSP :class:`SymbolKind`.
    :param name_line: the 0-based line on which the identifier is reported.
    :param lsp_end_line: the 0-based end line reported by the LSP (defaults to ``name_line``).
    :param lsp_end_col: the 0-based end column reported by the LSP (defaults to end of name).
    :return: a :class:`LanguageServerSymbol` wrapping a synthetic symbol-root dict.
    """
    end_line = name_line if lsp_end_line is None else lsp_end_line
    end_col = len(name) if lsp_end_col is None else lsp_end_col
    symbol_root: dict[str, Any] = {
        "name": name,
        "kind": int(kind),
        "selectionRange": {
            "start": {"line": name_line, "character": 0},
            "end": {"line": name_line, "character": len(name)},
        },
        "location": {
            "range": {
                "start": {"line": name_line, "character": 0},
                "end": {"line": end_line, "character": end_col},
            }
        },
    }
    return LanguageServerSymbol(symbol_root)


class TestGetSymbolExtentStrategy:
    """Dispatcher returns the right strategy based on file extension."""

    def test_python_file_yields_python_strategy(self) -> None:
        assert isinstance(get_symbol_extent_strategy("foo.py"), PythonSymbolExtentStrategy)

    def test_python_stub_file_yields_python_strategy(self) -> None:
        assert isinstance(get_symbol_extent_strategy("types.pyi"), PythonSymbolExtentStrategy)

    def test_non_python_file_yields_identity(self) -> None:
        assert isinstance(get_symbol_extent_strategy("foo.ts"), IdentitySymbolExtentStrategy)

    def test_extensionless_file_yields_identity(self) -> None:
        assert isinstance(get_symbol_extent_strategy("Makefile"), IdentitySymbolExtentStrategy)

    def test_go_file_yields_go_strategy(self) -> None:
        assert isinstance(get_symbol_extent_strategy("main.go"), GoSymbolExtentStrategy)


class TestGoStrategyWidensDeclarationStart:
    """
    ``GoSymbolExtentStrategy`` widens a gopls name-only ``var``/``const``/``type`` start
    back to the leading keyword so statement-level edits cover the whole declaration; it
    leaves grouped members, non-declarations and out-of-range positions untouched.
    """

    @staticmethod
    def _widen_start(file_text: str, name: str, name_line: int, name_col: int) -> PositionInFile:
        # widen an explicitly supplied (name-only) start position for a synthetic Go symbol
        strategy = GoSymbolExtentStrategy()
        symbol = _make_symbol(name, SymbolKind.Variable, name_line)
        return strategy.get_statement_start_position(symbol, file_text, PositionInFile(line=name_line, col=name_col))

    def test_var_start_widens_to_keyword(self) -> None:
        start = self._widen_start("package main\n\nvar Foo = 42\n", "Foo", 2, 4)
        assert (start.line, start.col) == (2, 0)

    def test_const_start_widens_to_keyword(self) -> None:
        start = self._widen_start('package main\n\nconst Bar = "x"\n', "Bar", 2, 6)
        assert (start.line, start.col) == (2, 0)

    def test_type_start_widens_to_keyword(self) -> None:
        start = self._widen_start("package main\n\ntype Thing struct {\n\tN int\n}\n", "Thing", 2, 5)
        assert (start.line, start.col) == (2, 0)

    def test_indented_declaration_widens_to_keyword_keeping_indent(self) -> None:
        # a var inside a func keeps its indentation; only the keyword is drawn into the extent
        start = self._widen_start("package main\n\nfunc f() {\n\tvar x = 1\n}\n", "x", 3, 5)
        assert (start.line, start.col) == (3, 1)

    def test_grouped_declaration_member_is_not_widened(self) -> None:
        # grouped block: the keyword sits on a prior line, so the member must be left untouched
        strategy = GoSymbolExtentStrategy()
        symbol = _make_symbol("Foo", SymbolKind.Variable, 3)
        lsp_start = PositionInFile(line=3, col=1)
        result = strategy.get_statement_start_position(symbol, "package main\n\nvar (\n\tFoo = 1\n)\n", lsp_start)
        assert result is lsp_start

    def test_non_declaration_start_is_not_widened(self) -> None:
        # a function name is preceded by ``func ``, not a var/const/type keyword
        strategy = GoSymbolExtentStrategy()
        symbol = _make_symbol("Helper", SymbolKind.Function, 2)
        lsp_start = PositionInFile(line=2, col=5)
        result = strategy.get_statement_start_position(symbol, "package main\n\nfunc Helper() {}\n", lsp_start)
        assert result is lsp_start

    def test_end_position_is_returned_unchanged(self) -> None:
        # gopls already reports the end at the value/body close; the strategy never moves it
        strategy = GoSymbolExtentStrategy()
        symbol = _make_symbol("Foo", SymbolKind.Variable, 2)
        lsp_end = PositionInFile(line=2, col=12)
        assert strategy.get_statement_end_position(symbol, "package main\n\nvar Foo = 42\n", lsp_end) is lsp_end

    def test_out_of_range_start_is_returned_unchanged(self) -> None:
        strategy = GoSymbolExtentStrategy()
        symbol = _make_symbol("Foo", SymbolKind.Variable, 99)
        lsp_start = PositionInFile(line=99, col=4)
        assert strategy.get_statement_start_position(symbol, "package main\n", lsp_start) is lsp_start


class TestIdentityStrategy:
    """Identity strategy is a pass-through for both positions."""

    def test_end_position_is_unchanged(self) -> None:
        strategy = IdentitySymbolExtentStrategy()
        symbol = _make_symbol("FOO", SymbolKind.Variable, 0)
        lsp_end = PositionInFile(line=0, col=3)
        assert strategy.get_statement_end_position(symbol, "FOO = 1\n", lsp_end) == lsp_end

    def test_start_position_is_unchanged(self) -> None:
        strategy = IdentitySymbolExtentStrategy()
        symbol = _make_symbol("FOO", SymbolKind.Variable, 0)
        lsp_start = PositionInFile(line=0, col=0)
        assert strategy.get_statement_start_position(symbol, "FOO = 1\n", lsp_start) == lsp_start


class TestPythonStrategyWidensVariableEnd:
    """Python strategy widens the end of Variable/Field symbols to the enclosing statement end."""

    def test_single_line_list_literal_widens_past_closing_bracket(self) -> None:
        """Module-level list literal: LSP reports name-only, strategy widens to end of statement."""
        text = "FOO = [1, 2, 3]\n"
        symbol = _make_symbol("FOO", SymbolKind.Variable, name_line=0)
        strategy = PythonSymbolExtentStrategy()
        narrow_end = PositionInFile(line=0, col=3)
        widened = strategy.get_statement_end_position(symbol, text, narrow_end)

        # end should point past the closing bracket on line 0
        assert widened.line == 0
        assert widened.col == 15  # len("FOO = [1, 2, 3]")

    def test_multiline_list_literal_widens_to_closing_bracket_line(self) -> None:
        """Multi-line list literal: widening must land on the closing bracket's line."""
        text = "FOO = [\n    1,\n    2,\n    3,\n]\n"
        symbol = _make_symbol("FOO", SymbolKind.Variable, name_line=0)
        strategy = PythonSymbolExtentStrategy()
        narrow_end = PositionInFile(line=0, col=3)
        widened = strategy.get_statement_end_position(symbol, text, narrow_end)

        # closing bracket is on line 4 (0-based); end col is 1 (past "]")
        assert widened.line == 4
        assert widened.col == 1

    def test_dataclass_field_widens_to_statement_end(self) -> None:
        """Dataclass field (AnnAssign inside class body) widens to the full annotation statement."""
        text = 'from dataclasses import dataclass\n\n@dataclass\nclass X:\n    a: int = 0\n    b: str = ""\n'
        # field `a` is reported at line 4 (0-based); name itself is a single character
        symbol = _make_symbol("a", SymbolKind.Field, name_line=4)
        strategy = PythonSymbolExtentStrategy()
        narrow_end = PositionInFile(line=4, col=5)  # LSP may end mid-annotation
        widened = strategy.get_statement_end_position(symbol, text, narrow_end)

        # widened to end of "    a: int = 0" (line 4, col 14)
        assert widened.line == 4
        assert widened.col == 14  # col_offset after "    a: int = 0"

    def test_aug_assign_is_widened(self) -> None:
        """AugAssign (e.g. ``x += 1``) is a valid statement target for widening."""
        text = "x = 0\nx += 1\n"
        # symbol `x` on line 1 (the AugAssign line)
        symbol = _make_symbol("x", SymbolKind.Variable, name_line=1)
        strategy = PythonSymbolExtentStrategy()
        narrow_end = PositionInFile(line=1, col=1)
        widened = strategy.get_statement_end_position(symbol, text, narrow_end)

        assert widened.line == 1
        assert widened.col == 6  # len("x += 1")


class TestPythonStrategyWidensVariableStart:
    """Python strategy widens the start to the statement's first line/col."""

    def test_start_is_at_statement_beginning(self) -> None:
        text = "FOO = [\n    1,\n]\n"
        symbol = _make_symbol("FOO", SymbolKind.Variable, name_line=0)
        strategy = PythonSymbolExtentStrategy()
        lsp_start = PositionInFile(line=0, col=0)
        widened = strategy.get_statement_start_position(symbol, text, lsp_start)

        assert widened.line == 0
        assert widened.col == 0


class TestPythonStrategyFallsBack:
    """When widening cannot succeed, the strategy returns the LSP position verbatim."""

    def test_non_variable_kind_returns_lsp_end(self) -> None:
        """Functions/classes should not be widened — their extents are already statement-level."""
        text = "def foo():\n    return 1\n"
        symbol = _make_symbol("foo", SymbolKind.Function, name_line=0)
        strategy = PythonSymbolExtentStrategy()
        lsp_end = PositionInFile(line=1, col=12)
        assert strategy.get_statement_end_position(symbol, text, lsp_end) == lsp_end

    def test_syntax_error_returns_lsp_end(self) -> None:
        """Unparseable file disables widening for this call."""
        text = "FOO = [1,\n"  # missing closing bracket — SyntaxError
        symbol = _make_symbol("FOO", SymbolKind.Variable, name_line=0)
        strategy = PythonSymbolExtentStrategy()
        lsp_end = PositionInFile(line=0, col=3)
        assert strategy.get_statement_end_position(symbol, text, lsp_end) == lsp_end

    def test_unmatched_name_returns_lsp_end(self) -> None:
        """A symbol whose name cannot be found in the AST yields the LSP position."""
        text = "OTHER = 1\n"
        symbol = _make_symbol("FOO", SymbolKind.Variable, name_line=0)
        strategy = PythonSymbolExtentStrategy()
        lsp_end = PositionInFile(line=0, col=3)
        assert strategy.get_statement_end_position(symbol, text, lsp_end) == lsp_end


class TestPythonStrategyTupleUnpacking:
    """Tuple unpacking assignments: each name is found via recursive walk of the target."""

    def test_tuple_unpacking_widens_statement(self) -> None:
        text = "x, y = 1, 2\n"
        symbol = _make_symbol("x", SymbolKind.Variable, name_line=0)
        strategy = PythonSymbolExtentStrategy()
        lsp_end = PositionInFile(line=0, col=1)
        widened = strategy.get_statement_end_position(symbol, text, lsp_end)

        # widened end should be past "x, y = 1, 2"
        assert widened.line == 0
        assert widened.col == 11


@pytest.mark.parametrize(
    "kind",
    [SymbolKind.Variable, SymbolKind.Constant, SymbolKind.Field, SymbolKind.Property],
)
def test_all_variable_like_kinds_are_widened(kind: SymbolKind) -> None:
    """Variable, Constant, Field, and Property kinds all get widened."""
    text = "FOO = [\n    1,\n]\n"
    symbol = _make_symbol("FOO", kind, name_line=0)
    strategy = PythonSymbolExtentStrategy()
    narrow_end = PositionInFile(line=0, col=3)
    widened = strategy.get_statement_end_position(symbol, text, narrow_end)

    assert widened.line == 2  # closing bracket line
    assert widened.col == 1
