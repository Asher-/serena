"""
Language-specific widening of symbol extents to the enclosing statement.

Motivation: several LSP servers report narrower-than-statement ranges for symbols.
Most importantly, the Python language servers (pyright, jedi-language-server) report
Variable/Constant/Field symbols with an extent that ends at the name identifier rather
than at the end of the containing assignment statement. Edit operations like
``insert_after_symbol`` take the LSP-reported end as the insertion anchor, so without
widening they land inside the RHS expression (e.g. inside a list literal, between
lines of a multi-line assignment). This module provides a :class:`SymbolExtentStrategy`
abstraction that normalises extents to the statement boundary for such cases.
"""

import ast
import logging
import os
from abc import ABC, abstractmethod

from serena.symbol import LanguageServerSymbol, PositionInFile
from solidlsp.lsp_protocol_handler.lsp_types import SymbolKind

log = logging.getLogger(__name__)


class SymbolExtentStrategy(ABC):
    """
    Computes statement-level start and end positions for a given symbol.

    Used by the code editor to widen LSP-reported extents to the enclosing statement
    boundary when the language server reports narrower extents than the caller expects
    for statement-level edits (e.g. insert_after, insert_before, replace_body).
    """

    @abstractmethod
    def get_statement_end_position(
        self, symbol: LanguageServerSymbol, file_text: str, lsp_end: PositionInFile
    ) -> PositionInFile:
        """
        Get the end position of the statement containing the given symbol.

        :param symbol: the symbol whose containing statement end is wanted.
        :param file_text: the current contents of the symbol's file.
        :param lsp_end: the LSP-reported end position of the symbol; returned unchanged
            when the strategy cannot improve on it.
        :return: the end position (0-based line/col) at which the containing statement ends.
        """

    @abstractmethod
    def get_statement_start_position(
        self, symbol: LanguageServerSymbol, file_text: str, lsp_start: PositionInFile
    ) -> PositionInFile:
        """
        Get the start position of the statement containing the given symbol.

        :param symbol: the symbol whose containing statement start is wanted.
        :param file_text: the current contents of the symbol's file.
        :param lsp_start: the LSP-reported start position of the symbol; returned unchanged
            when the strategy cannot improve on it.
        :return: the start position (0-based line/col) at which the containing statement begins.
        """


class IdentitySymbolExtentStrategy(SymbolExtentStrategy):
    """
    Strategy that does not alter LSP-reported positions.

    Used for languages whose LSP servers already report statement-level extents, or for
    which no statement-widening implementation exists yet.
    """

    def get_statement_end_position(
        self, symbol: LanguageServerSymbol, file_text: str, lsp_end: PositionInFile
    ) -> PositionInFile:
        return lsp_end

    def get_statement_start_position(
        self, symbol: LanguageServerSymbol, file_text: str, lsp_start: PositionInFile
    ) -> PositionInFile:
        return lsp_start


class PythonSymbolExtentStrategy(SymbolExtentStrategy):
    """
    Strategy that widens Python Variable/Constant/Field/Property extents to the
    enclosing ``ast`` statement.

    Uses :mod:`ast` on the current file text to find the narrowest assignment-like
    statement (``Assign``, ``AnnAssign``, ``AugAssign``) that both contains the
    symbol's identifier line and assigns to the symbol's name. Falls back to the
    LSP-reported positions when parsing fails, when the symbol is not a variable-like
    kind, or when no matching statement can be located.
    """

    # variable-like kinds for which LSP Python servers typically report name-only extents
    _VARIABLE_KINDS: frozenset[SymbolKind] = frozenset(
        {SymbolKind.Variable, SymbolKind.Constant, SymbolKind.Field, SymbolKind.Property}
    )

    def get_statement_end_position(
        self, symbol: LanguageServerSymbol, file_text: str, lsp_end: PositionInFile
    ) -> PositionInFile:
        # restrict widening to variable-like symbols — other kinds already get statement-level extents
        if symbol.symbol_kind not in self._VARIABLE_KINDS:
            return lsp_end

        node = self._find_containing_statement(symbol, file_text)
        if node is None or node.end_lineno is None or node.end_col_offset is None:
            return lsp_end

        # ast uses 1-based lines and byte-based columns; LSP/PositionInFile use 0-based
        # (character-based). Column is only consumed at the end of a line for statement
        # termination, so byte vs. code-unit mismatch is irrelevant for downstream line-only
        # callers.
        return PositionInFile(line=node.end_lineno - 1, col=node.end_col_offset)

    def get_statement_start_position(
        self, symbol: LanguageServerSymbol, file_text: str, lsp_start: PositionInFile
    ) -> PositionInFile:
        if symbol.symbol_kind not in self._VARIABLE_KINDS:
            return lsp_start

        node = self._find_containing_statement(symbol, file_text)
        if node is None:
            return lsp_start

        return PositionInFile(line=node.lineno - 1, col=node.col_offset)

    def _find_containing_statement(
        self, symbol: LanguageServerSymbol, file_text: str
    ) -> ast.Assign | ast.AnnAssign | ast.AugAssign | None:
        """
        Find the narrowest assignment-like AST node containing the symbol.

        :param symbol: the symbol to locate.
        :param file_text: the current file contents.
        :return: the narrowest matching :class:`ast.Assign`, :class:`ast.AnnAssign` or
            :class:`ast.AugAssign` node, or ``None`` if parsing fails or no match is found.
        """
        # fail-safe parse: any syntax issue disables widening for this call
        try:
            tree = ast.parse(file_text)
        except SyntaxError as e:
            log.debug("ast.parse failed while widening symbol extent for %s: %s", symbol, e)
            return None

        name = symbol.name
        symbol_line = symbol.line
        if symbol_line is None:
            return None
        target_line_1based = symbol_line + 1

        # search for the narrowest assignment node that contains the symbol's identifier line
        # and assigns to the symbol's name — narrowness protects us from outer scopes wrapping
        # the real target (e.g. class body containing an inner function that assigns the same name)
        best: ast.Assign | ast.AnnAssign | ast.AugAssign | None = None
        best_width: int | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                continue
            if node.end_lineno is None:
                continue
            if not (node.lineno <= target_line_1based <= node.end_lineno):
                continue
            if not self._node_assigns_name(node, name):
                continue

            width = node.end_lineno - node.lineno
            if best is None or (best_width is not None and width < best_width):
                best = node
                best_width = width

        return best

    @staticmethod
    def _node_assigns_name(
        node: ast.Assign | ast.AnnAssign | ast.AugAssign, name: str
    ) -> bool:
        """
        Check whether the given assignment node binds the given name anywhere in its targets.

        :param node: the assignment node to inspect.
        :param name: the bare identifier to look for.
        :return: ``True`` iff any target (or nested tuple/list target) binds ``name``.
        """
        if isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets: list[ast.expr] = [node.target]
        else:
            targets = list(node.targets)

        # walk each target subtree to handle tuple/list unpacking and starred targets
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name) and sub.id == name:
                    return True
        return False


def get_symbol_extent_strategy(relative_path: str) -> SymbolExtentStrategy:
    """
    Pick the appropriate :class:`SymbolExtentStrategy` for the file at ``relative_path``.

    :param relative_path: the file path relative to the project root; dispatch is by file
        extension only, so callers only need to pass the path used by the LSP.
    :return: a Python-aware strategy for ``.py``/``.pyi`` files, an identity strategy otherwise.
    """
    _, ext = os.path.splitext(relative_path)
    if ext.lower() in (".py", ".pyi"):
        return PythonSymbolExtentStrategy()
    return IdentitySymbolExtentStrategy()
