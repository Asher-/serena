"""
File and file system-related tools, specifically for
  * listing directory contents
  * reading files
  * creating files
  * editing at the file level
"""

import os
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

from serena.tools import SUCCESS_RESULT, EditedFileContext, Tool, ToolMarkerCanEdit, ToolMarkerOptional
from serena.util.file_system import scan_directory
from serena.symbol import LanguageServerSymbol
from serena.util.text_utils import ContentReplacer, search_files


class ReadFileTool(Tool):
    """
    Reads a file within the project directory.
    """

    def apply(self, relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> str:
        """
        Reads the given file or a chunk of it. Generally, symbolic operations
        like find_symbol or find_referencing_symbols should be preferred if you know which symbols you are looking for.

        :param relative_path: the relative path to the file to read
        :param start_line: the 0-based index of the first line to be retrieved.
        :param end_line: the 0-based index of the last line to be retrieved (inclusive). If None, read until the end of the file.
        :param max_answer_chars: if the file (chunk) is longer than this number of characters,
            no content will be returned. Don't adjust unless there is really no other way to get the content
            required for the task.
        :return: the full text of the file at the given relative path
        """
        self.project.validate_relative_path(relative_path, require_not_ignored=True)

        result = self.project.read_file(relative_path)
        result_lines = result.splitlines()
        if end_line is None:
            result_lines = result_lines[start_line:]
        else:
            result_lines = result_lines[start_line : end_line + 1]
        result = "\n".join(result_lines)

        return self._limit_length(result, max_answer_chars)


class CreateTextFileTool(Tool, ToolMarkerCanEdit):
    """
    Creates/overwrites a file in the project directory.
    """

    def apply(self, relative_path: str, content: str) -> str:
        """
        Write a new file or overwrite an existing file.

        :param relative_path: the relative path to the file to create
        :param content: the (appropriately encoded) content to write to the file
        :return: a message indicating success or failure
        """
        project_root = self.get_project_root()
        abs_path = (Path(project_root) / relative_path).resolve()
        will_overwrite_existing = abs_path.exists()

        if will_overwrite_existing:
            self.project.validate_relative_path(relative_path, require_not_ignored=True)
        else:
            assert abs_path.is_relative_to(self.get_project_root()), (
                f"Cannot create file outside of the project directory, got {relative_path=}"
            )

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding=self.project.project_config.encoding, newline=self.project.line_ending.newline_str)
        answer = f"File created: {relative_path}."
        if will_overwrite_existing:
            answer += " Overwrote existing file."
        return answer


class ListDirTool(Tool):
    """
    Lists files and directories in the given directory (optionally with recursion).
    """

    def apply(self, relative_path: str, recursive: bool, skip_ignored_files: bool = False, max_answer_chars: int = -1) -> str:
        """
        Lists files and directories in the given directory (optionally with recursion).

        :param relative_path: the relative path to the directory to list; pass "." to scan the project root
        :param recursive: whether to scan subdirectories recursively
        :param skip_ignored_files: whether to skip files and directories that are ignored
        :param max_answer_chars: if the output is longer than this number of characters,
            no content will be returned. -1 means the default value from the config will be used.
            Don't adjust unless there is really no other way to get the content required for the task.
        :return: a JSON object with the names of directories and files within the given directory
        """
        # Check if the directory exists before validation
        if not self.project.relative_path_exists(relative_path):
            error_info = {
                "error": f"Directory not found: {relative_path}",
                "project_root": self.get_project_root(),
                "hint": "Check if the path is correct relative to the project root",
            }
            return self._to_json(error_info)

        self.project.validate_relative_path(relative_path, require_not_ignored=skip_ignored_files)

        dirs, files = scan_directory(
            os.path.join(self.get_project_root(), relative_path),
            relative_to=self.get_project_root(),
            recursive=recursive,
            is_ignored_dir=self.project.is_ignored_path if skip_ignored_files else None,
            is_ignored_file=self.project.is_ignored_path if skip_ignored_files else None,
        )

        result = self._to_json({"dirs": dirs, "files": files})
        return self._limit_length(result, max_answer_chars)


class FindFileTool(Tool):
    """
    Finds files in the given relative paths
    """

    def apply(self, file_mask: str, relative_path: str) -> str:
        """
        Finds non-gitignored files matching the given file mask within the given relative path

        :param file_mask: the filename or file mask (using the wildcards * or ?) to search for
        :param relative_path: the relative path to the directory to search in; pass "." to scan the project root
        :return: a JSON object with the list of matching files
        """
        self.project.validate_relative_path(relative_path, require_not_ignored=True)

        dir_to_scan = os.path.join(self.get_project_root(), relative_path)

        # find the files by ignoring everything that doesn't match
        def is_ignored_file(abs_path: str) -> bool:
            if self.project.is_ignored_path(abs_path):
                return True
            filename = os.path.basename(abs_path)
            return not fnmatch(filename, file_mask)

        _dirs, files = scan_directory(
            path=dir_to_scan,
            recursive=True,
            is_ignored_dir=self.project.is_ignored_path,
            is_ignored_file=is_ignored_file,
            relative_to=self.get_project_root(),
        )

        result = self._to_json({"files": files})
        return result


class ReplaceContentTool(Tool, ToolMarkerCanEdit):
    """
    Replaces content in a file (optionally using regular expressions).
    """

    def apply(
        self,
        relative_path: str,
        needle: str,
        repl: str,
        mode: Literal["literal", "regex"],
        allow_multiple_occurrences: bool = False,
    ) -> str:
        r"""
        Replaces one or more occurrences of a given pattern in a file with new content.

        This is the preferred way to replace content in a file whenever the symbol-level
        tools are not appropriate.

        VERY IMPORTANT: The "regex" mode allows very large sections of code to be replaced without fully quoting them!
        Use a regex of the form "beginning.*?end-of-text-to-be-replaced" to be faster and more economical!
        ALWAYS try to use wildcards to avoid specifying the exact content to be replaced,
        especially if it spans several lines. Note that you cannot make mistakes, because if the regex should match
        multiple occurrences while you disabled `allow_multiple_occurrences`, an error will be returned, and you can retry
        with a revised regex.
        Therefore, using regex mode with suitable wildcards is usually the best choice!

        :param relative_path: the relative path to the file
        :param needle: the string or regex pattern to search for.
            If `mode` is "literal", this string will be matched exactly.
            If `mode` is "regex", this string will be treated as a regular expression (syntax of Python's `re` module,
            with flags DOTALL and MULTILINE enabled).
        :param repl: the replacement string (verbatim).
            If mode is "regex", the string can contain backreferences to matched groups in the needle regex,
            specified using the syntax $!1, $!2, etc. for groups 1, 2, etc.
        :param mode: either "literal" or "regex", specifying how the `needle` parameter is to be interpreted.
        :param allow_multiple_occurrences: whether to allow matching and replacing multiple occurrences.
            If false and multiple occurrences are found, an error will be returned
        """
        return self.replace_content(
            relative_path, needle, repl, mode=mode, allow_multiple_occurrences=allow_multiple_occurrences, require_not_ignored=True
        )

    def replace_content(
        self,
        relative_path: str,
        needle: str,
        repl: str,
        mode: Literal["literal", "regex"],
        allow_multiple_occurrences: bool = False,
        require_not_ignored: bool = True,
    ) -> str:
        """
        Performs the replacement, with additional options not exposed in the tool.
        This function can be used internally by other tools.
        """
        self.project.validate_relative_path(relative_path, require_not_ignored=require_not_ignored)
        with EditedFileContext(relative_path, self.create_code_editor()) as context:
            original_content = context.get_original_content()
            replacer = ContentReplacer(mode=mode, allow_multiple_occurrences=allow_multiple_occurrences)
            updated_content = replacer.replace(original_content, needle, repl)
            context.set_updated_content(updated_content)
        return SUCCESS_RESULT


class DeleteLinesTool(Tool, ToolMarkerCanEdit, ToolMarkerOptional):
    """
    Deletes a range of lines within a file.
    """

    def apply(
        self,
        relative_path: str,
        start_line: int,
        end_line: int,
    ) -> str:
        """
        Deletes the given lines in the file.
        Requires that the same range of lines was previously read using the `read_file` tool to verify correctness
        of the operation.

        :param relative_path: the relative path to the file
        :param start_line: the 0-based index of the first line to be deleted
        :param end_line: the 0-based index of the last line to be deleted
        """
        code_editor = self.create_code_editor()
        code_editor.delete_lines(relative_path, start_line, end_line)
        return SUCCESS_RESULT


class ReplaceLinesTool(Tool, ToolMarkerCanEdit, ToolMarkerOptional):
    """
    Replaces a range of lines within a file with new content.
    """

    def apply(
        self,
        relative_path: str,
        start_line: int,
        end_line: int,
        content: str,
    ) -> str:
        """
        Replaces the given range of lines in the given file.
        Requires that the same range of lines was previously read using the `read_file` tool to verify correctness
        of the operation.

        :param relative_path: the relative path to the file
        :param start_line: the 0-based index of the first line to be deleted
        :param end_line: the 0-based index of the last line to be deleted
        :param content: the content to insert
        """
        if not content.endswith("\n"):
            content += "\n"
        result = self.agent.get_tool(DeleteLinesTool).apply(relative_path, start_line, end_line)
        if result != SUCCESS_RESULT:
            return result
        self.agent.get_tool(InsertAtLineTool).apply(relative_path, start_line, content)
        return SUCCESS_RESULT


class InsertAtLineTool(Tool, ToolMarkerCanEdit, ToolMarkerOptional):
    """
    Inserts content at a given line in a file.
    """

    def apply(
        self,
        relative_path: str,
        line: int,
        content: str,
    ) -> str:
        """
        Inserts the given content at the given line in the file, pushing existing content of the line down.
        In general, symbolic insert operations like insert_after_symbol or insert_before_symbol should be preferred if you know which
        symbol you are looking for.
        However, this can also be useful for small targeted edits of the body of a longer symbol (without replacing the entire body).

        :param relative_path: the relative path to the file
        :param line: the 0-based index of the line to insert content at
        :param content: the content to be inserted
        """
        if not content.endswith("\n"):
            content += "\n"
        code_editor = self.create_code_editor()
        code_editor.insert_at_line(relative_path, line, content)
        return SUCCESS_RESULT


class SearchForPatternTool(Tool):
    """
    Performs a symbol-aware regex search of the project. Hits are grouped by
    their enclosing symbol (when one exists in the LSP geography) so each group
    is a ``cursor_start`` target via its ``name_path`` and ``relative_path``;
    hits without an enclosing symbol are listed under their file with a hint
    pointing at ``cursor_overview``.
    """

    def apply(
        self,
        substring_pattern: str,
        context_lines_before: int = 0,
        context_lines_after: int = 0,
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        relative_path: str = "",
        restrict_search_to_code_files: bool = False,
        max_answer_chars: int = -1,
    ) -> str:
        """
        Offers a flexible regex search across the project, grouping hits under
        the symbol that encloses each match so the result reads as a navigable
        cursor view rather than a flat data dump. Symbolic operations like
        ``cursor_find`` should still be preferred when you already know which
        symbols you are looking for.

        Pattern Matching Logic:
            For each match, the returned result will contain the full lines where
            the substring pattern is found, optionally with context lines. The
            pattern is compiled with ``re.DOTALL``, so ``.`` matches newlines —
            never put ``.*`` at the very beginning or end of the pattern, and
            prefer non-greedy quantifiers where possible.

        File Selection Logic:
            ``restrict_search_to_code_files=True`` confines the search to files
            an analyser can address symbolically (the only files for which
            grouping by enclosing symbol is meaningful). Otherwise all
            non-ignored files are searched and matches in non-analysable files
            appear under file-level blocks. ``relative_path`` and the include /
            exclude globs further restrict the search; globs are matched against
            paths relative to the project root.

        Output:
            A header line is followed by per-symbol blocks of the form
            ``@ <name_path> (<Kind>) [<relative_path>]`` with indented hits, and
            file-level blocks ``@ <relative_path>`` for hits outside any
            analysed symbol. Each ``(name_path, relative_path)`` pair is a
            valid argument for ``cursor_start``; no cursors are opened by this
            tool. When the result is too long, it shortens progressively to a
            symbol/file list, then per-file counts, then the header alone.

        :param substring_pattern: regular expression for a substring pattern to search for
        :param context_lines_before: number of lines of context to include before each match
        :param context_lines_after: number of lines of context to include after each match
        :param paths_include_glob: optional glob pattern specifying files to include in the search.
            Matches against relative file paths from the project root (e.g., ``"*.py"``,
            ``"src/**/*.ts"``). Supports standard glob patterns and brace expansion.
            If empty, all non-ignored files are included.
        :param paths_exclude_glob: optional glob pattern specifying files to exclude from the
            search. Takes precedence over ``paths_include_glob``. If empty, no files are
            excluded.
        :param relative_path: only sub-paths of this path (relative to the project root) are
            searched. Pointing at a single file restricts the search to that file. Must exist.
        :param restrict_search_to_code_files: whether to restrict the search to files that an
            analyser can address symbolically. Set to ``True`` if you are only interested in
            symbol-resident matches; ``False`` (the default) lets matches in HTML, YAML, etc.
            appear under file-level blocks.
        :param max_answer_chars: maximum characters for the returned output; ``-1`` uses the
            configured default. Tighten the query rather than raising this.
        :return: a plain-text cursor-style listing — never JSON.
        """

        # locate and validate the search root
        abs_path = os.path.join(self.get_project_root(), relative_path)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Relative path {relative_path} does not exist.")

        # run the regex pass via either the source-restricted or whole-tree path
        if restrict_search_to_code_files:
            matches = self.project.search_source_files_for_pattern(
                pattern=substring_pattern,
                relative_path=relative_path,
                context_lines_before=context_lines_before,
                context_lines_after=context_lines_after,
                paths_include_glob=paths_include_glob.strip(),
                paths_exclude_glob=paths_exclude_glob.strip(),
            )
        else:
            if os.path.isfile(abs_path):
                rel_paths_to_search = [relative_path]
            else:
                _dirs, rel_paths_to_search = scan_directory(
                    path=abs_path,
                    recursive=True,
                    is_ignored_dir=self.project.is_ignored_path,
                    is_ignored_file=self.project.is_ignored_path,
                    relative_to=self.get_project_root(),
                )
            matches = search_files(
                rel_paths_to_search,
                substring_pattern,
                context_lines_before=context_lines_before,
                context_lines_after=context_lines_after,
                file_reader=self.project.read_file,
                root_path=self.get_project_root(),
                paths_include_glob=paths_include_glob,
                paths_exclude_glob=paths_exclude_glob,
            )

        # opt into the LSP retriever only when the project's backend supports it
        retriever = None
        if self.agent.get_language_backend().is_lsp():
            retriever = self.create_language_server_symbol_retriever()

        # for each hit, ask the LSP for the enclosing symbol; group by (file, name_path, kind)
        Group = tuple[str, str | None, str | None]
        grouped: dict[Group, list[str]] = {}
        group_order: list[Group] = []
        file_order: list[str] = []
        seen_files: set[str] = set()
        for match in matches:
            assert match.source_file_path is not None
            rel = match.source_file_path
            if rel not in seen_files:
                seen_files.add(rel)
                file_order.append(rel)
            line = match.matched_lines[0].line_number
            name_path: str | None = None
            kind: str | None = None
            if retriever is not None and retriever.can_analyze_file(rel):
                try:
                    ls = retriever.get_language_server(rel)
                    sym_dict = ls.request_containing_symbol(rel, line, 0, strict=False)
                    if sym_dict is not None:
                        sym = LanguageServerSymbol(sym_dict)
                        name_path = sym.get_name_path()
                        kind = sym.symbol_kind_name
                except Exception:
                    pass
            key: Group = (rel, name_path, kind)
            if key not in grouped:
                grouped[key] = []
                group_order.append(key)
            grouped[key].append(match.to_display_string())

        # header counts the matches, the symbol-resident groups, and the touched files
        n_matches = sum(len(v) for v in grouped.values())
        n_symbols = sum(1 for k in grouped if k[1] is not None)
        n_files = len(file_order)
        header = f"Found {n_matches} matches across {n_symbols} symbols in {n_files} files."

        def render_hit(hit: str) -> list[str]:
            # indent every line of the hit so multi-line context blocks stay readable
            return [f"    {sub}" for sub in hit.splitlines()] or ["    "]

        def make_full() -> str:
            # cluster groups by file in discovery order; symbol blocks first, then file-level
            lines = [header, ""]
            for rel in file_order:
                sym_groups = [k for k in group_order if k[0] == rel and k[1] is not None]
                file_groups = [k for k in group_order if k[0] == rel and k[1] is None]
                for key in sym_groups:
                    _, name_path, kind = key
                    lines.append(f"@ {name_path} ({kind}) [{rel}]")
                    lines.append("  hits:")
                    for hit in grouped[key]:
                        lines.extend(render_hit(hit))
                    lines.append("")
                for key in file_groups:
                    lines.append(f"@ {rel} [{rel}]")
                    lines.append("  hits:")
                    for hit in grouped[key]:
                        lines.extend(render_hit(hit))
                    lines.append("  (use cursor_overview to navigate)")
                    lines.append("")
            return "\n".join(lines).rstrip() + "\n"

        def make_symbol_list() -> str:
            # symbol/file identifiers only, no hit bodies
            lines = [header, ""]
            for rel in file_order:
                for key in [k for k in group_order if k[0] == rel]:
                    _, name_path, kind = key
                    count = len(grouped[key])
                    if name_path is not None:
                        lines.append(f"@ {name_path} ({kind}) [{rel}] - {count} matches")
                    else:
                        lines.append(f"@ {rel} [{rel}] - {count} matches (use cursor_overview to navigate)")
            return "\n".join(lines)

        def make_per_file_counts() -> str:
            # collapse to one line per file
            lines = [header, ""]
            per_file: dict[str, int] = {}
            for (rel, _, _), hits in grouped.items():
                per_file[rel] = per_file.get(rel, 0) + len(hits)
            for rel in file_order:
                lines.append(f"{rel}: {per_file[rel]} matches")
            return "\n".join(lines)

        def make_summary() -> str:
            return header

        return self._limit_length(
            make_full(),
            max_answer_chars,
            shortened_result_factories=[make_symbol_list, make_per_file_counts, make_summary],
        )