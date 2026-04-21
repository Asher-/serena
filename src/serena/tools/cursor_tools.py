"""
Cursor-based code navigation tools.

These tools provide stateful, incremental navigation through code structure
using LSP graph edges (containment, references, call hierarchy, type hierarchy).
They also cover pattern-based symbol search (``cursor_find``) and symbol-level
editing (``cursor_replace_body``, ``cursor_insert_before``, ``cursor_insert_after``,
``cursor_rename``) — so the cursor is the full MCP-exposed interface for
LSP-addressable (symbol-level) activity.
"""

import difflib
from collections import defaultdict
from collections.abc import Sequence

from serena.cursor import EdgeType
from serena.tools import SUCCESS_RESULT
from serena.tools.tools_base import Tool, ToolMarkerSymbolicEdit, ToolMarkerSymbolicRead
from solidlsp.ls_types import SymbolKind


class CursorStartTool(Tool, ToolMarkerSymbolicRead):
    """
    Start a navigation cursor at a symbol for incremental code exploration.
    The cursor tracks your position and lets you navigate along code relationships
    (containment, references, calls, type hierarchy).
    """

    def apply(
        self,
        name_path: str,
        relative_path: str = "",
        cursor_id: str = "",
    ) -> str:
        """
        Start a new cursor at the specified symbol. Returns the symbol's neighborhood
        showing all reachable symbols via active edge types.

        :param name_path: name path of the symbol to start at (e.g. "MyClass/my_method").
            See find_symbol for name path pattern syntax.
        :param relative_path: optional file path to narrow the symbol search.
        :param cursor_id: optional explicit cursor ID. Auto-generated if empty.
        :return: the cursor view showing the symbol and its neighborhood.
        """
        manager = self.agent.get_cursor_manager()
        cid, _state = manager.start_cursor(
            name_path=name_path,
            relative_path=relative_path or None,
            cursor_id=cursor_id or None,
        )
        return manager.format_cursor_view(cid)


class CursorMoveTool(Tool, ToolMarkerSymbolicRead):
    """
    Move a navigation cursor to an adjacent symbol. The target should be
    visible in the cursor's current neighborhood (from cursor_start or cursor_look output).
    """

    def apply(
        self,
        cursor_id: str,
        target_name: str,
        target_relative_path: str = "",
    ) -> str:
        """
        Move the cursor to a neighboring symbol. Returns the new position's neighborhood.

        :param cursor_id: the ID of the cursor to move (shown in cursor output).
        :param target_name: name of the symbol to move to. Must be visible in the current neighborhood.
        :param target_relative_path: optional file path to disambiguate if multiple neighbors share the same name.
        :return: the updated cursor view at the new position.
        """
        manager = self.agent.get_cursor_manager()
        manager.move_cursor(
            cursor_id=cursor_id,
            target_name=target_name,
            target_relative_path=target_relative_path or None,
        )
        return manager.format_cursor_view(cursor_id)


class CursorLookTool(Tool, ToolMarkerSymbolicRead):
    """
    Look at the neighborhood of the cursor's current position without moving.
    Useful for re-examining the current position after changing edge type configuration.
    """

    def apply(self, cursor_id: str) -> str:
        """
        Show the current cursor position and its neighborhood.

        :param cursor_id: the ID of the cursor to look from.
        :return: the cursor view showing the symbol and its neighborhood.
        """
        manager = self.agent.get_cursor_manager()
        return manager.format_cursor_view(cursor_id)


class CursorConfigureTool(Tool, ToolMarkerSymbolicRead):
    """
    Configure which edge types a cursor follows and what information is shown.
    Edge types: contains, references, referenced-by, calls, called-by, inherits, inherited-by.
    """

    # noinspection PyDefaultArgument
    def apply(
        self,
        cursor_id: str,
        edge_types: list[str] = [],  # noqa: B006
        include_body: bool = False,
    ) -> str:
        """
        Configure the cursor's active edge types and display options.

        :param cursor_id: the ID of the cursor to configure.
        :param edge_types: list of edge type names to enable. If empty, all edge types are enabled.
            Valid values: contains, references, referenced-by, calls, called-by, inherits, inherited-by.
        :param include_body: whether to include the symbol's source code body in the cursor view.
        :return: confirmation of the new configuration and updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)

        if edge_types:
            valid_types: set[EdgeType] = set()
            for name in edge_types:
                try:
                    valid_types.add(EdgeType(name))
                except ValueError:
                    valid_names = [e.value for e in EdgeType]
                    raise ValueError(f"Unknown edge type '{name}'. Valid edge types: {valid_names}")
            state.active_edge_types = frozenset(valid_types)
        else:
            state.active_edge_types = frozenset(EdgeType)

        state.include_body = include_body

        return manager.format_cursor_view(cursor_id)


class CursorHistoryTool(Tool, ToolMarkerSymbolicRead):
    """
    Show the trail of symbols visited by a cursor, from start to current position.
    """

    def apply(self, cursor_id: str) -> str:
        """
        Show the navigation trail for a cursor.

        :param cursor_id: the ID of the cursor.
        :return: the formatted trail showing each visited location.
        """
        manager = self.agent.get_cursor_manager()
        return manager.format_trail(cursor_id)


class CursorCloseTool(Tool, ToolMarkerSymbolicRead):
    """
    Close a navigation cursor and free its resources.
    """

    def apply(self, cursor_id: str) -> str:
        """
        Close a cursor.

        :param cursor_id: the ID of the cursor to close.
        :return: confirmation that the cursor was closed.
        """
        manager = self.agent.get_cursor_manager()
        manager.close_cursor(cursor_id)
        return f"Cursor {cursor_id} closed."


class CursorFindTool(Tool, ToolMarkerSymbolicRead):
    """
    Search for symbols in the codebase by name path pattern (the multi-match variant of
    ``cursor_start``, which requires a unique match). If the search yields exactly one
    symbol a cursor is started there; otherwise the candidate list is returned so the
    caller can disambiguate and follow up with ``cursor_start``.
    """

    # noinspection PyDefaultArgument
    def apply(
        self,
        name_path_pattern: str,
        relative_path: str = "",
        depth: int = 0,
        include_body: bool = False,
        include_kinds: list[int] = [],  # noqa: B006
        exclude_kinds: list[int] = [],  # noqa: B006
        substring_matching: bool = False,
        max_matches: int = -1,
        cursor_id: str = "",
        max_answer_chars: int = -1,
    ) -> str:
        """
        Search for symbols matching a name path pattern.

        A name path is a path in the symbol tree *within a source file*.
        Examples: ``"method"`` (any symbol named ``method``), ``"MyClass/method"``
        (``method`` inside ``MyClass``), ``"/MyClass/method"`` (exact top-level path).
        Append ``[i]`` for a specific overload.

        If the pattern uniquely identifies a symbol, a cursor is started on it and the
        cursor view is returned. Otherwise, the list of candidate symbols is returned so
        you can refine the pattern or call ``cursor_start`` with a more specific one.

        :param name_path_pattern: name path matching pattern.
        :param relative_path: optional file or directory to restrict the search to.
        :param depth: depth up to which descendants shall be included for each match. Ignored
            when ``include_body=True``. Default 0.
        :param include_body: whether to include each match's source code. Use judiciously.
        :param include_kinds: LSP symbol kind integers to include (empty = all).
        :param exclude_kinds: LSP symbol kind integers to exclude. Takes precedence over ``include_kinds``.
        :param substring_matching: if True, the last element of the pattern is matched as a
            substring (e.g. ``"Foo/get"`` matches ``"Foo/getValue"``).
        :param max_matches: maximum permitted matches; -1 (default) means no limit.
        :param cursor_id: optional cursor ID to use when the match is unique. Auto-generated otherwise.
        :param max_answer_chars: maximum characters for the candidate-list output; -1 means use default.
        :return: a cursor view (unique match) or a JSON-formatted candidate list.
        """
        if include_body:
            depth = 0
        assert max_matches != 0, "max_matches must be > 0 or equal to -1."
        parsed_include_kinds: Sequence[SymbolKind] | None = [SymbolKind(k) for k in include_kinds] if include_kinds else None
        parsed_exclude_kinds: Sequence[SymbolKind] | None = [SymbolKind(k) for k in exclude_kinds] if exclude_kinds else None
        manager = self.agent.get_cursor_manager()
        symbols = manager.find_symbols(
            name_path_pattern,
            relative_path=relative_path or None,
            include_kinds=parsed_include_kinds,
            exclude_kinds=parsed_exclude_kinds,
            substring_matching=substring_matching,
        )
        n_matches = len(symbols)

        if n_matches == 0:
            return f"No symbols found matching '{name_path_pattern}'."

        if n_matches == 1:
            cid, _ = manager.register_cursor_at_symbol(symbols[0], cursor_id=cursor_id or None)
            view = manager.format_cursor_view(cid)
            return f"Found unique match; started cursor {cid}.\n\n{view}"

        def candidate_list_json() -> str:
            candidate_dicts = [
                s.to_dict(
                    kind=True,
                    name_path=True,
                    name=False,
                    relative_path=True,
                    body_location=True,
                    depth=depth,
                    body=include_body,
                    children_name=True,
                    children_name_path=False,
                )
                for s in symbols
            ]
            return self._to_json(candidate_dicts)

        if 0 < max_matches < n_matches:
            summary = f"Matched {n_matches}>{max_matches} symbols; refine your pattern or use cursor_start with a specific name path."
            rel_path_to_name_paths: defaultdict[str, list[str]] = defaultdict(list)
            for s in symbols:
                rel_path_to_name_paths[s.location.relative_path or "unknown"].append(s.get_name_path())
            return f"{summary}\n{self._to_json(rel_path_to_name_paths)}"

        def shortened_relative_path_to_name_paths() -> str:
            rel_path_to_name_paths: defaultdict[str, list[str]] = defaultdict(list)
            for s in symbols:
                rel_path_to_name_paths[s.location.relative_path or "unknown"].append(s.get_name_path())
            return f"Candidates (shortened):\n{self._to_json(rel_path_to_name_paths)}"

        result = f"Found {n_matches} matching symbols; pick one and call cursor_start on its name path.\n{candidate_list_json()}"
        return self._limit_length(result, max_answer_chars, shortened_result_factories=[shortened_relative_path_to_name_paths])


class CursorReplaceBodyTool(Tool, ToolMarkerSymbolicEdit):
    """
    Replace the body of the symbol at the cursor's current position.
    The cursor remains positioned on the same symbol (its stored location is refreshed).
    """

    def apply(self, cursor_id: str, body: str) -> str:
        """
        Replace the body of the symbol at the cursor's current position.

        The body is the full definition of the symbol in the programming language, including
        the signature line for functions. It does NOT include preceding docstrings, comments,
        or imports.

        :param cursor_id: the cursor whose current symbol to replace.
        :param body: the new body text.
        :return: confirmation and the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        name_path = state.current_symbol.get_name_path()
        relative_path = state.current_location.relative_path
        if relative_path is None:
            raise ValueError(f"Cursor {cursor_id} has no relative path; cannot perform edit.")

        # snapshot extent and file content before the edit so we can report a diff summary
        # and detect gross over-deletion (e.g. a ballooned symbol extent absorbing siblings)
        pre_start = state.current_symbol.get_body_start_position_or_raise()
        pre_end = state.current_symbol.get_body_end_position_or_raise()
        pre_content = self.project.read_file(relative_path)

        # execute the edit
        code_editor = self.create_code_editor()
        code_editor.replace_body(name_path, relative_file_path=relative_path, body=body)

        # post-edit sibling-loss safety net: if the number of lines removed is much larger than
        # the symbol's extent could account for, the extent was likely corrupted and absorbed
        # sibling material. Raise rather than silently report success — the file is in a
        # surprising state and the caller should inspect before continuing.
        post_content = self.project.read_file(relative_path)
        removed, added = self._count_diff_lines(pre_content, post_content)
        extent_lines = max(pre_end.line - pre_start.line + 1, 1)
        body_lines = len(body.splitlines()) + 1
        tolerated_removal = extent_lines + 5
        if removed > tolerated_removal and removed > extent_lines * 2:
            raise ValueError(
                f"cursor_replace_body on {name_path!r} in {relative_path!r} produced a "
                f"suspicious diff (-{removed}/+{added} lines) for a symbol whose extent "
                f"spans {extent_lines} lines with a replacement body of {body_lines} lines. "
                f"This typically indicates that the symbol's extent had silently expanded to "
                f"absorb sibling material, which is then deleted by the replacement. The edit "
                f"has already been applied — inspect the file and run `git checkout` to recover."
            )

        manager.reanchor_cursor(cursor_id)
        diff_summary = f"Diff: -{removed} / +{added} lines"
        return f"{SUCCESS_RESULT}\n{diff_summary}\n\n" + manager.format_cursor_view(cursor_id)

    @staticmethod
    def _count_diff_lines(before: str, after: str) -> tuple[int, int]:
        """
        Counts the number of removed and added lines in a unified diff between ``before``
        and ``after``.

        :param before: the original text
        :param after: the updated text
        :return: ``(removed_line_count, added_line_count)``
        """
        diff = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                n=0,
                lineterm="",
            )
        )
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        return removed, added


class CursorInsertBeforeTool(Tool, ToolMarkerSymbolicEdit):
    """
    Insert content immediately before the symbol at the cursor's current position.
    The cursor stays on the target symbol; its stored location is refreshed.
    """

    def apply(self, cursor_id: str, body: str) -> str:
        """
        Insert content before the symbol at the cursor's current position.

        Typical uses: insert a new class/function above the current one, or insert a new
        import statement before the first top-level symbol in a file.

        :param cursor_id: the cursor whose current symbol to insert before.
        :param body: the content to insert; it will be placed immediately before the line
            where the symbol is defined.
        :return: confirmation and the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        name_path = state.current_symbol.get_name_path()
        relative_path = state.current_location.relative_path
        if relative_path is None:
            raise ValueError(f"Cursor {cursor_id} has no relative path; cannot perform edit.")
        code_editor = self.create_code_editor()
        code_editor.insert_before_symbol(name_path, relative_file_path=relative_path, body=body)
        manager.reanchor_cursor(cursor_id)
        return f"{SUCCESS_RESULT}\n\n" + manager.format_cursor_view(cursor_id)


class CursorInsertAfterTool(Tool, ToolMarkerSymbolicEdit):
    """
    Insert content immediately after the symbol at the cursor's current position.
    The cursor stays on the target symbol; its stored location is refreshed.
    """

    def apply(self, cursor_id: str, body: str) -> str:
        """
        Insert content after the symbol at the cursor's current position.

        Typical use: add a new class, function, method, or variable assignment after
        an existing one.

        :param cursor_id: the cursor whose current symbol to insert after.
        :param body: the content to insert; it will be placed on the line following the
            end of the symbol's definition.
        :return: confirmation and the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        name_path = state.current_symbol.get_name_path()
        relative_path = state.current_location.relative_path
        if relative_path is None:
            raise ValueError(f"Cursor {cursor_id} has no relative path; cannot perform edit.")
        code_editor = self.create_code_editor()
        code_editor.insert_after_symbol(name_path, relative_file_path=relative_path, body=body)
        manager.reanchor_cursor(cursor_id)
        return f"{SUCCESS_RESULT}\n\n" + manager.format_cursor_view(cursor_id)

class CursorReplaceRangeTool(Tool, ToolMarkerSymbolicEdit):
    """
    Replace a non-symbolic line range in a file. Unlike ``cursor_replace_body`` and the
    ``cursor_insert_before`` / ``cursor_insert_after`` pair, this primitive does not
    address an LSP symbol — it operates directly on a ``[start_line, end_line]``
    (inclusive, 0-based) range of file lines. It is the escape hatch for editing
    regions that the language server does not surface as symbols: free-floating
    comment blocks, blank-line gaps between imports, imports themselves (on LSPs that
    do not expose them as symbols), license headers, and any content before the first
    declaration or after the last.

    The replacement ``body`` is inserted verbatim at the start of ``start_line``
    after the range has been deleted. If the caller intends the replacement to remain
    line-oriented, ``body`` should end with a newline.

    Note on the marker: this tool does not perform a *symbolic* edit — it mutates a
    raw line range. It is marked with ``ToolMarkerSymbolicEdit`` only because that is
    the existing edit marker already imported by this module and because the current
    project-server-required check only gates read-only tools; the distinction has no
    runtime effect for edit tools. A future cleanup pass may introduce a dedicated
    ``ToolMarkerFileLineEdit`` marker.
    """

    def apply(self, relative_path: str, start_line: int, end_line: int, body: str) -> str:
        """
        Replace the file's lines ``[start_line, end_line]`` (inclusive) with ``body``.

        Typical uses: delete a top-of-file comment block that sits above the first
        declaration (``body=""``); reorder a sequence of import statements; rewrite a
        ``///`` doc comment whose lines are not themselves an LSP symbol; rewrite any
        other non-symbolic region that ``cursor_replace_body`` cannot reach.

        :param relative_path: relative path to the file to edit.
        :param start_line: the 0-based index of the first line to replace (inclusive).
        :param end_line: the 0-based index of the last line to replace (inclusive).
            Must satisfy ``start_line <= end_line``. To replace a single line, pass
            ``start_line == end_line``.
        :param body: the replacement text. Inserted verbatim; the caller should supply
            a trailing newline to keep the file line-oriented. Pass an empty string to
            delete the range with no replacement.
        :return: a success confirmation and the diff summary.
        """
        # validate input before any filesystem work so callers see a clean error message
        if start_line < 0 or end_line < start_line:
            raise ValueError(
                f"cursor_replace_range: invalid range [{start_line}, {end_line}] "
                f"in {relative_path!r}; require 0 <= start_line <= end_line."
            )

        # snapshot content so we can report a line-diff summary after the edit
        pre_content = self.project.read_file(relative_path)

        # execute the edit via the filesystem-level code editor layer; the operation
        # bypasses the LSP's workspace-edit interface intentionally — workspace edits
        # are rejected for regions the server does not recognize as symbols
        code_editor = self.create_code_editor()
        code_editor.replace_lines(relative_path, start_line, end_line, body)

        # compute a diff summary for the return value so the caller can verify the edit
        # size against their intent (mirrors cursor_replace_body's diff-summary style)
        post_content = self.project.read_file(relative_path)
        removed, added = CursorReplaceBodyTool._count_diff_lines(pre_content, post_content)
        diff_summary = f"Diff: -{removed} / +{added} lines"
        return f"{SUCCESS_RESULT}\n{diff_summary}"


class CursorRenameTool(Tool, ToolMarkerSymbolicEdit):
    """
    Rename the symbol at the cursor's current position throughout the codebase using the
    language server's refactoring support. The cursor re-anchors to the renamed symbol.
    """

    def apply(self, cursor_id: str, new_name: str) -> str:
        """
        Rename the symbol at the cursor's current position.

        All references to the symbol are updated via the language server's rename refactoring.
        The cursor re-anchors to the renamed symbol at its new name.

        :param cursor_id: the cursor whose current symbol to rename.
        :param new_name: the new name.
        :return: the rename status message followed by the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        old_name_path = state.current_symbol.get_name_path()
        relative_path = state.current_location.relative_path
        if relative_path is None:
            raise ValueError(f"Cursor {cursor_id} has no relative path; cannot perform edit.")
        code_editor = self.create_ls_code_editor()
        status_message = code_editor.rename_symbol(old_name_path, relative_path=relative_path, new_name=new_name)

        # Re-anchor: the old name path's last segment is replaced by new_name
        parts = old_name_path.split("/")
        parts[-1] = new_name
        new_name_path = "/".join(parts)
        try:
            manager.reanchor_cursor(cursor_id, name_path=new_name_path, relative_path=relative_path)
            view = manager.format_cursor_view(cursor_id)
            return f"{status_message}\n\n{view}"
        except ValueError as e:
            return f"{status_message}\n\n(Cursor could not re-anchor to renamed symbol: {e})"


class CursorOverviewTool(Tool, ToolMarkerSymbolicRead):
    """
    Return an overview of the top-level symbols in a file by starting a cursor on the
    file's first top-level symbol with only the ``contains`` edge active. This covers the
    use case of the old ``get_symbols_overview`` tool in cursor-first form.
    """

    def apply(self, relative_path: str, cursor_id: str = "", max_answer_chars: int = -1) -> str:
        """
        Show the top-level symbols in a file as a cursor view.

        Internally this delegates to the language server symbol retriever to find top-level
        symbols and starts a cursor on the file's container with only the ``contains`` edge
        so the output is a flat listing of top-level symbols in the file.

        :param relative_path: relative path to the source file.
        :param cursor_id: optional cursor ID for the started cursor. Auto-generated otherwise.
        :param max_answer_chars: maximum characters for the returned output; -1 means use default.
        :return: the cursor view listing top-level symbols under ``contains``.
        """
        import os

        file_path = os.path.join(self.project.project_root, relative_path)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File {relative_path} does not exist in the project.")
        if os.path.isdir(file_path):
            raise ValueError(f"Expected a file path, but got a directory path: {relative_path}.")

        retriever = self.create_language_server_symbol_retriever()
        if not retriever.can_analyze_file(relative_path):
            raise ValueError(
                f"Cannot extract symbols from file {relative_path}. "
                f"Active languages: {[l.value for l in self.agent.get_active_lsp_languages()]}"
            )
        top_level = retriever.get_symbol_overview(relative_path).get(relative_path, [])
        if not top_level:
            return f"No top-level symbols found in {relative_path}."

        # Use the first top-level symbol as a foothold; show its siblings by reading
        # the overview directly rather than trying to force the cursor onto a synthetic file node.
        lines: list[str] = [f"Top-level symbols in {relative_path}:"]
        for sym in top_level:
            line = sym.line
            loc = f"{relative_path}:{line + 1}" if line is not None else relative_path
            lines.append(f"  {sym.name} ({sym.symbol_kind_name}) [{loc}]")
        lines.append("")
        lines.append("Use cursor_start with a name path to position on a specific symbol.")
        result = "\n".join(lines)
        return self._limit_length(result, max_answer_chars)
