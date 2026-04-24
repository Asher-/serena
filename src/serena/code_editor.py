import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Reversible
from contextlib import contextmanager
from typing import Generic, TypeVar, cast

from serena.jetbrains.jetbrains_plugin_client import JetBrainsPluginClient
from serena.symbol import JetBrainsSymbol, LanguageServerSymbol, LanguageServerSymbolRetriever, PositionInFile, Symbol
from solidlsp import SolidLanguageServer, ls_types
from solidlsp.ls import LSPFileBuffer
from solidlsp.ls_utils import PathUtils, TextUtils

from .project import Project

log = logging.getLogger(__name__)
TSymbol = TypeVar("TSymbol", bound=Symbol)


class CodeEditor(Generic[TSymbol], ABC):
    def __init__(self, project: Project) -> None:
        self.project_root = project.project_root
        self.encoding = project.project_config.encoding
        self.newline = project.line_ending.newline_str

    class EditedFile(ABC):
        def __init__(self, relative_path: str) -> None:
            self.relative_path = relative_path

        @abstractmethod
        def get_contents(self) -> str:
            """
            :return: the contents of the file.
            """

        @abstractmethod
        def set_contents(self, contents: str) -> None:
            """
            Fully resets the contents of the file.

            :param contents: the new contents
            """

        @abstractmethod
        def delete_text_between_positions(self, start_pos: PositionInFile, end_pos: PositionInFile) -> None:
            pass

        @abstractmethod
        def insert_text_at_position(self, pos: PositionInFile, text: str) -> None:
            pass

        def text_between_positions(self, start_pos: PositionInFile, end_pos: PositionInFile) -> str:
            """
            Returns the text of the file between ``start_pos`` (inclusive) and
            ``end_pos`` (exclusive), computed against the current in-memory contents
            of the edited file.

            :param start_pos: the inclusive start position
            :param end_pos: the exclusive end position
            :return: the text between the two positions
            """
            contents = self.get_contents()
            start_idx = TextUtils.get_index_from_line_col(contents, start_pos.line, start_pos.col)
            end_idx = TextUtils.get_index_from_line_col(contents, end_pos.line, end_pos.col)
            return contents[start_idx:end_idx]

    @contextmanager
    def _open_file_context(self, relative_path: str) -> Iterator["CodeEditor.EditedFile"]:
        """
        Context manager for opening a file
        """
        raise NotImplementedError("This method must be overridden for each subclass")

    def read_file(self, relative_path: str) -> str:
        """
        Reads the content of a file.

        :param relative_path: the relative path of the file to read
        :return: the content of the file
        """
        with self._open_file_context(relative_path) as file:
            return file.get_contents()

    @contextmanager
    def edited_file_context(self, relative_path: str) -> Iterator["CodeEditor.EditedFile"]:
        """
        Context manager for editing a file.
        """
        with self._open_file_context(relative_path) as edited_file:
            yield edited_file
            # save the file
            self._save_edited_file(edited_file)

    def _save_edited_file(self, edited_file: "CodeEditor.EditedFile") -> None:
        abs_path = os.path.join(self.project_root, edited_file.relative_path)
        new_contents = edited_file.get_contents()
        with open(abs_path, "w", encoding=self.encoding, newline=self.newline) as f:
            f.write(new_contents)

    @abstractmethod
    def _find_unique_symbol(self, name_path: str, relative_file_path: str) -> TSymbol:
        """
        Finds the unique symbol with the given name in the given file.
        If no such symbol exists, raises a ValueError.

        :param name_path: the name path
        :param relative_file_path: the relative path of the file in which to search for the symbol.
        :return: the unique symbol
        """

    def _get_statement_end_position(self, symbol: TSymbol, edited_file: "CodeEditor.EditedFile") -> PositionInFile:
        """
        Get the end position for statement-level edit operations on the given symbol.

        Default implementation returns the LSP-reported body end position unchanged.
        Subclasses targeting language servers that report narrower-than-statement extents
        (e.g. Python variable symbols) should override to widen to the enclosing statement.

        :param symbol: the symbol the caller is about to edit.
        :param edited_file: the edited file context, providing access to current file text.
        :return: the 0-based end position.
        """
        return symbol.get_body_end_position_or_raise()

    def _get_statement_start_position(self, symbol: TSymbol, edited_file: "CodeEditor.EditedFile") -> PositionInFile:
        """
        Get the start position for statement-level edit operations on the given symbol.

        Symmetric to :meth:`_get_statement_end_position`; default returns the LSP-reported
        body start position unchanged.

        :param symbol: the symbol the caller is about to edit.
        :param edited_file: the edited file context, providing access to current file text.
        :return: the 0-based start position.
        """
        return symbol.get_body_start_position_or_raise()

    def replace_body(self, name_path: str, relative_file_path: str, body: str) -> None:
        """
        Replaces the body of the symbol with the given name_path in the given file.

        :param name_path: the name path of the symbol to replace.
        :param relative_file_path: the relative path of the file in which the symbol is defined.
        :param body: the new body
        """
        symbol = self._find_unique_symbol(name_path, relative_file_path)

        with self.edited_file_context(relative_file_path) as edited_file:
            # widen the LSP-reported extent to the enclosing statement boundaries when the
            # language server reports narrower extents (e.g. Python Variable symbols where
            # the extent ends at the identifier rather than the full assignment)
            start_pos = self._get_statement_start_position(symbol, edited_file)
            end_pos = self._get_statement_end_position(symbol, edited_file)

            # preserve the whitespace envelope of the original extent so that languages whose
            # symbol extent includes a trailing newline (e.g. markdown headings, where the extent
            # ends at line N+1 col 0) do not get that newline destroyed by body.strip(). For
            # Python/Swift/C++ where extents are tight, leading/trailing are empty and the strip
            # dominates, preserving the historical behaviour.
            original = edited_file.text_between_positions(start_pos, end_pos)
            leading = original[: len(original) - len(original.lstrip())]
            trailing = original[len(original.rstrip()) :]
            stripped_body = body.strip()
            framed_body = leading + stripped_body + trailing

            edited_file.delete_text_between_positions(start_pos, end_pos)
            edited_file.insert_text_at_position(start_pos, framed_body)

    @staticmethod
    def _count_leading_newlines(text: Iterable) -> int:
        cnt = 0
        for c in text:
            if c == "\n":
                cnt += 1
            elif c == "\r":
                continue
            else:
                break
        return cnt

    @classmethod
    def _count_trailing_newlines(cls, text: Reversible) -> int:
        return cls._count_leading_newlines(reversed(text))

    def insert_after_symbol(self, name_path: str, relative_file_path: str, body: str) -> None:
        """
        Inserts content after the symbol with the given name in the given file.
        """
        symbol = self._find_unique_symbol(name_path, relative_file_path)

        # make sure body always ends with at least one newline
        if not body.endswith("\n"):
            body += "\n"

        # make sure a suitable number of leading empty lines is used (at least 0/1 depending on the symbol type,
        # otherwise as many as the caller wanted to insert)
        original_leading_newlines = self._count_leading_newlines(body)
        body = body.lstrip("\r\n")
        min_empty_lines = 0
        if symbol.is_neighbouring_definition_separated_by_empty_line():
            min_empty_lines = 1
        num_leading_empty_lines = max(min_empty_lines, original_leading_newlines)
        if num_leading_empty_lines:
            body = ("\n" * num_leading_empty_lines) + body

        # make sure the one line break succeeding the original symbol, which we repurposed as prefix via
        # `line += 1`, is replaced
        body = body.rstrip("\r\n") + "\n"

        with self.edited_file_context(relative_file_path) as edited_file:
            # widen the LSP-reported extent to the enclosing statement end when the language server
            # reports narrower extents (e.g. Python Variable symbols) — prevents insertion inside
            # the RHS of multi-line assignments
            pos = self._get_statement_end_position(symbol, edited_file)

            # start at the beginning of the next line
            col = 0
            line = pos.line + 1

            edited_file.insert_text_at_position(PositionInFile(line, col), body)

    def insert_before_symbol(self, name_path: str, relative_file_path: str, body: str) -> None:
        """
        Inserts content before the symbol with the given name in the given file.
        """
        symbol = self._find_unique_symbol(name_path, relative_file_path)

        original_trailing_empty_lines = self._count_trailing_newlines(body) - 1

        # ensure eol is present at end
        body = body.rstrip() + "\n"

        # add suitable number of trailing empty lines after the body (at least 0/1 depending on the symbol type,
        # otherwise as many as the caller wanted to insert)
        min_trailing_empty_lines = 0
        if symbol.is_neighbouring_definition_separated_by_empty_line():
            min_trailing_empty_lines = 1
        num_trailing_newlines = max(min_trailing_empty_lines, original_trailing_empty_lines)
        body += "\n" * num_trailing_newlines

        # apply edit
        with self.edited_file_context(relative_file_path) as edited_file:
            # widen the LSP-reported extent to the enclosing statement start when the language
            # server reports narrower extents (e.g. Python Variable symbols where the start may
            # fall on a continuation line of a multi-line assignment)
            symbol_start_pos = self._get_statement_start_position(symbol, edited_file)

            # insert position is the start of line where the symbol is defined
            line = symbol_start_pos.line
            col = 0

            edited_file.insert_text_at_position(PositionInFile(line=line, col=col), body)

    def insert_at_line(self, relative_path: str, line: int, content: str) -> None:
        """
        Inserts content at the given line in the given file.

        :param relative_path: the relative path of the file in which to insert content
        :param line: the 0-based index of the line to insert content at
        :param content: the content to insert
        """
        with self.edited_file_context(relative_path) as edited_file:
            edited_file.insert_text_at_position(PositionInFile(line, 0), content)

    def delete_lines(self, relative_path: str, start_line: int, end_line: int) -> None:
        """
        Deletes lines in the given file.

        :param relative_path: the relative path of the file in which to delete lines
        :param start_line: the 0-based index of the first line to delete (inclusive)
        :param end_line: the 0-based index of the last line to delete (inclusive)
        """
        start_col = 0
        end_line_for_delete = end_line + 1
        end_col = 0
        with self.edited_file_context(relative_path) as edited_file:
            start_pos = PositionInFile(line=start_line, col=start_col)
            end_pos = PositionInFile(line=end_line_for_delete, col=end_col)
            edited_file.delete_text_between_positions(start_pos, end_pos)

    def replace_lines(self, relative_path: str, start_line: int, end_line: int, content: str) -> None:
        """
        Replaces a range of lines in the given file with the given content.

        The operation deletes lines ``[start_line, end_line]`` (inclusive, 0-based) and
        inserts ``content`` at the position where those lines began. This is the file-level
        counterpart to :py:meth:`replace_body` — it does not consult the language server
        and can therefore mutate regions outside any LSP symbol extent (e.g. free-floating
        comment blocks, blank-line gaps between imports, or regions before the first
        declaration in a file).

        The body is inserted verbatim. If the caller intends the replacement to remain
        line-oriented, ``content`` should end with a newline; otherwise the line that
        previously followed ``end_line`` will be joined onto the final line of ``content``.

        :param relative_path: the relative path of the file to edit
        :param start_line: the 0-based index of the first line to replace (inclusive)
        :param end_line: the 0-based index of the last line to replace (inclusive)
        :param content: the text to insert in place of the deleted range
        """
        # validate the range; start > end is a programmer error
        if start_line < 0 or end_line < start_line:
            raise ValueError(f"Invalid replace_lines range [{start_line}, {end_line}] for {relative_path!r}")

        # perform the delete-then-insert pair inside a single edited_file_context so the
        # file is saved exactly once; the delete spans [start_line, end_line+1) line-starts
        # and the insert happens at the freed position (start_line, 0)
        with self.edited_file_context(relative_path) as edited_file:
            delete_start = PositionInFile(line=start_line, col=0)
            delete_end = PositionInFile(line=end_line + 1, col=0)
            edited_file.delete_text_between_positions(delete_start, delete_end)
            if content:
                edited_file.insert_text_at_position(PositionInFile(line=start_line, col=0), content)

    def delete_symbol(self, name_path: str, relative_file_path: str) -> None:
        """
        Deletes the symbol with the given name in the given file.
        """
        symbol = self._find_unique_symbol(name_path, relative_file_path)
        start_pos = symbol.get_body_start_position_or_raise()
        end_pos = symbol.get_body_end_position_or_raise()
        with self.edited_file_context(relative_file_path) as edited_file:
            edited_file.delete_text_between_positions(start_pos, end_pos)

    @abstractmethod
    def rename_symbol(self, name_path: str, relative_path: str, new_name: str) -> str:
        pass


class LanguageServerCodeEditor(CodeEditor[LanguageServerSymbol]):
    def __init__(self, symbol_retriever: LanguageServerSymbolRetriever):
        super().__init__(project=symbol_retriever.project)
        self._symbol_retriever = symbol_retriever

    def _get_language_server(self, relative_path: str) -> SolidLanguageServer:
        return self._symbol_retriever.get_language_server(relative_path)

    class EditedFile(CodeEditor.EditedFile):
        def __init__(self, lang_server: SolidLanguageServer, relative_path: str, file_buffer: LSPFileBuffer):
            super().__init__(relative_path)
            self._lang_server = lang_server
            self._file_buffer = file_buffer

        def get_contents(self) -> str:
            return self._file_buffer.contents

        def set_contents(self, contents: str) -> None:
            self._file_buffer.contents = contents

        def delete_text_between_positions(self, start_pos: PositionInFile, end_pos: PositionInFile) -> None:
            self._lang_server.delete_text_between_positions(self.relative_path, start_pos.to_lsp_position(), end_pos.to_lsp_position())

        def insert_text_at_position(self, pos: PositionInFile, text: str) -> None:
            self._lang_server.insert_text_at_position(self.relative_path, pos.line, pos.col, text)

        def apply_text_edits(self, text_edits: list[ls_types.TextEdit]) -> None:
            return self._lang_server.apply_text_edits_to_file(self.relative_path, text_edits)

    @contextmanager
    def _open_file_context(self, relative_path: str) -> Iterator["CodeEditor.EditedFile"]:
        lang_server = self._get_language_server(relative_path)
        with lang_server.open_file(relative_path) as file_buffer:
            yield self.EditedFile(lang_server, relative_path, file_buffer)

    def _get_code_file_content(self, relative_path: str) -> str:
        """Get the content of a file using the language server."""
        lang_server = self._get_language_server(relative_path)
        return lang_server.language_server.retrieve_full_file_content(relative_path)

    def _find_unique_symbol(self, name_path: str, relative_file_path: str) -> LanguageServerSymbol:
        return self._symbol_retriever.find_unique(name_path, within_relative_path=relative_file_path)

    def _get_statement_end_position(self, symbol: LanguageServerSymbol, edited_file: "CodeEditor.EditedFile") -> PositionInFile:
        # route through the per-language :class:`SymbolExtentStrategy` so Python variable
        # extents (reported as name-only by pyright/jedi) get widened to the enclosing
        # assignment statement's end. Other languages fall through to the identity strategy.
        # imported lazily to avoid coupling the module graph at import time.
        from serena.symbol_extent import get_symbol_extent_strategy

        lsp_end = symbol.get_body_end_position_or_raise()
        strategy = get_symbol_extent_strategy(edited_file.relative_path)
        return strategy.get_statement_end_position(symbol, edited_file.get_contents(), lsp_end)

    def _get_statement_start_position(self, symbol: LanguageServerSymbol, edited_file: "CodeEditor.EditedFile") -> PositionInFile:
        # imported lazily to avoid coupling the module graph at import time.
        from serena.symbol_extent import get_symbol_extent_strategy

        lsp_start = symbol.get_body_start_position_or_raise()
        strategy = get_symbol_extent_strategy(edited_file.relative_path)
        return strategy.get_statement_start_position(symbol, edited_file.get_contents(), lsp_start)

    def _relative_path_from_uri(self, uri: str) -> str:
        return os.path.relpath(PathUtils.uri_to_path(uri), self.project_root)

    class EditOperation(ABC):
        @abstractmethod
        def apply(self) -> None:
            pass

    class EditOperationFileTextEdits(EditOperation):
        def __init__(self, code_editor: "LanguageServerCodeEditor", file_uri: str, text_edits: list[ls_types.TextEdit]):
            self._code_editor = code_editor
            self._relative_path = code_editor._relative_path_from_uri(file_uri)
            self._text_edits = text_edits

        def apply(self) -> None:
            with self._code_editor.edited_file_context(self._relative_path) as edited_file:
                edited_file = cast(LanguageServerCodeEditor.EditedFile, edited_file)
                edited_file.apply_text_edits(self._text_edits)

    class EditOperationRenameFile(EditOperation):
        def __init__(self, code_editor: "LanguageServerCodeEditor", old_uri: str, new_uri: str):
            self._code_editor = code_editor
            self._old_relative_path = code_editor._relative_path_from_uri(old_uri)
            self._new_relative_path = code_editor._relative_path_from_uri(new_uri)

        def apply(self) -> None:
            old_abs_path = os.path.join(self._code_editor.project_root, self._old_relative_path)
            new_abs_path = os.path.join(self._code_editor.project_root, self._new_relative_path)
            os.rename(old_abs_path, new_abs_path)

    def _workspace_edit_to_edit_operations(self, workspace_edit: ls_types.WorkspaceEdit) -> list["LanguageServerCodeEditor.EditOperation"]:
        operations: list[LanguageServerCodeEditor.EditOperation] = []

        if "changes" in workspace_edit:
            for uri, edits in workspace_edit["changes"].items():
                operations.append(self.EditOperationFileTextEdits(self, uri, edits))

        if "documentChanges" in workspace_edit:
            for change in workspace_edit["documentChanges"]:
                if "textDocument" in change and "edits" in change:
                    operations.append(self.EditOperationFileTextEdits(self, change["textDocument"]["uri"], change["edits"]))
                elif "kind" in change:
                    if change["kind"] == "rename":
                        operations.append(self.EditOperationRenameFile(self, change["oldUri"], change["newUri"]))
                    else:
                        raise ValueError(f"Unhandled document change kind: {change}; Please report to Serena developers.")
                else:
                    raise ValueError(f"Unhandled document change format: {change}; Please report to Serena developers.")

        return operations

    def _apply_workspace_edit(self, workspace_edit: ls_types.WorkspaceEdit) -> int:
        """
        Applies a WorkspaceEdit

        :param workspace_edit: the edit to apply
        :return: number of edit operations applied
        """
        operations = self._workspace_edit_to_edit_operations(workspace_edit)
        for operation in operations:
            operation.apply()
        return len(operations)

    def rename_symbol(self, name_path: str, relative_path: str, new_name: str) -> str:
        """
        Renames a symbol, file, or directory throughout the codebase.

        :param name_path: the name path of the symbol to rename
        :param relative_path: the relative path of the file containing the symbol.
        :param new_name: the new name
        :return: a status message
        """
        symbol = self._find_unique_symbol(name_path, relative_path)
        if not symbol.location.has_position_in_file():
            raise ValueError(f"Symbol '{name_path}' does not have a valid position in file for renaming")

        # After has_position_in_file check, line and column are guaranteed to be non-None
        assert symbol.location.line is not None
        assert symbol.location.column is not None

        lang_server = self._get_language_server(relative_path)
        rename_result = lang_server.request_rename_symbol_edit(
            relative_file_path=relative_path, line=symbol.location.line, column=symbol.location.column, new_name=new_name
        )
        if rename_result is None:
            raise ValueError(
                f"Language server for {lang_server.language_id} returned no rename edits for symbol '{name_path}'. "
                f"The symbol might not support renaming."
            )
        num_changes = self._apply_workspace_edit(rename_result)

        if num_changes == 0:
            raise ValueError(
                f"Renaming symbol '{name_path}' to '{new_name}' resulted in no changes being applied; renaming may not be supported."
            )

        msg = f"Successfully renamed '{name_path}' to '{new_name}' ({num_changes} changes applied)"
        return msg


class JetBrainsCodeEditor(CodeEditor[JetBrainsSymbol]):
    def __init__(self, project: Project) -> None:
        self._project = project
        super().__init__(project)

    class EditedFile(CodeEditor.EditedFile):
        def __init__(self, relative_path: str, project: Project):
            super().__init__(relative_path)
            path = os.path.join(project.project_root, relative_path)
            log.info("Editing file: %s", path)
            with open(path, encoding=project.project_config.encoding) as f:
                self._content = f.read()

        def get_contents(self) -> str:
            return self._content

        def set_contents(self, contents: str) -> None:
            self._content = contents

        def delete_text_between_positions(self, start_pos: PositionInFile, end_pos: PositionInFile) -> None:
            self._content, _ = TextUtils.delete_text_between_positions(
                self._content, start_pos.line, start_pos.col, end_pos.line, end_pos.col
            )

        def insert_text_at_position(self, pos: PositionInFile, text: str) -> None:
            self._content, _, _ = TextUtils.insert_text_at_position(self._content, pos.line, pos.col, text)

    @contextmanager
    def _open_file_context(self, relative_path: str) -> Iterator["CodeEditor.EditedFile"]:
        yield self.EditedFile(relative_path, self._project)

    def _save_edited_file(self, edited_file: "CodeEditor.EditedFile") -> None:
        super()._save_edited_file(edited_file)
        with JetBrainsPluginClient.from_project(self._project) as client:
            client.refresh_file(edited_file.relative_path)

    def _find_unique_symbol(self, name_path: str, relative_file_path: str) -> JetBrainsSymbol:
        with JetBrainsPluginClient.from_project(self._project) as client:
            result = client.find_symbol(name_path, relative_path=relative_file_path, include_body=False, depth=0, include_location=True)
            symbols = result["symbols"]
            if not symbols:
                raise ValueError(f"No symbol with name {name_path} found in file {relative_file_path}")
            if len(symbols) > 1:
                raise ValueError(
                    f"Found multiple {len(symbols)} symbols with name {name_path} in file {relative_file_path}: "
                    + json.dumps(symbols, indent=2)
                )
            return JetBrainsSymbol(symbols[0], self._project)

    def rename_symbol(
        self,
        name_path: str | None,
        relative_path: str,
        new_name: str,
        rename_in_comments: bool = False,
        rename_in_text_occurrences: bool = False,
    ) -> str:
        """
        Renames a code symbol, file, or directory throughout the codebase.

        :param name_path: the name path of the symbol to rename. Set to None for renaming a file or directory.
        :param relative_path: if `name_path` is passed, the relative path of the file containing the symbol.
            Otherwise, the path to the directory or file to rename.
        :param new_name: the new name
        :param rename_in_comments: whether to rename occurrences of the symbol in comments
        :param rename_in_text_occurrences: whether to rename occurrences of the symbol in text
        :return: a status message
        """
        with JetBrainsPluginClient.from_project(self._project) as client:
            client.rename_symbol(
                name_path=name_path,
                relative_path=relative_path,
                new_name=new_name,
                rename_in_comments=rename_in_comments,
                rename_in_text_occurrences=rename_in_text_occurrences,
            )
            return "Success"
