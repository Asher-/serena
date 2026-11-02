"""
File and file system-related tools, specifically for
  * listing directory contents
  * reading files
  * creating files
  * editing at the file level
"""
# SPDX-License-Identifier: GPL-3.0-or-later

from typing import TYPE_CHECKING, Literal, cast

from serena.tools import EditingToolWithDiagnostics, Tool, ToolMarkerCanEdit, ToolMarkerOptional
from serena.util.file_lifecycle import FilesystemLifecycle

if TYPE_CHECKING:
    from serena.repl.api.edit_api import EditApi
    from serena.repl.api.fs_api import FsApi


class EditApiMixin:
    """
    Mixin for tools which delegate to the editing API.
    The API is imported locally, since the API module refers to the tools (as corresponding tools).
    """

    def _api(self) -> "EditApi":
        from serena.repl.api.edit_api import EditApi

        tool = cast(Tool, cast(object, self))
        return EditApi(tool.agent)


class FsApiMixin:
    """
    Mixin for tools which delegate to the file system API.
    The API is imported locally, since the API module refers to the tools (as corresponding tools).
    """

    def _api(self) -> "FsApi":
        from serena.repl.api.fs_api import FsApi

        tool = cast(Tool, cast(object, self))
        return FsApi(tool.agent)


class ReadFileTool(Tool, ToolMarkerOptional, FsApiMixin):
    """
    Reads a file within the project directory.
    """

    def apply(self, relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> str:
        """
        Reads the given file or a chunk of it.

        :param relative_path: the relative path to the file to read
        :param start_line: the 0-based index of the first line to be retrieved, negative values count from the end of the file.
        :param end_line: the 0-based index of the last line to be retrieved (inclusive). If None, read until the end of the file.
        :param max_answer_chars: if the file (chunk) is longer than this number of characters,
            no content will be returned. Don't adjust unless there is really no other way to get the content
            required for the task.
        :return: the full text of the file at the given relative path
        """
        return self._api().read_file(relative_path, start_line, end_line, max_answer_chars).represent()


class CreateTextFileTool(Tool, ToolMarkerCanEdit, FsApiMixin):
    """
    Creates or (with explicit intent) overwrites a text file at a project-relative path, writing the
    exact content given. Path-addressed rather than symbol-anchored, so it can bootstrap a brand-new
    file and write empty / zero-byte content -- neither of which a cursor or symbol edit can do, since
    an empty file has no symbol to anchor to. The write is atomic (temp + ``os.replace``): a crash or
    error never leaves a half-written file.
    """

    def apply(self, relative_path: str, content: str = "", overwrite: bool = False, expect_version: str = "") -> str:
        """
        Create, or with explicit intent overwrite, a UTF-8 text file with the given content.

        Creating a new file needs nothing beyond the path and content. Overwriting an existing file
        requires ``overwrite=True`` -- without it the call is refused and the original bytes are left
        intact (no silent clobber). For a safe overwrite, pass ``expect_version`` (the ``version`` a
        prior ``stat`` reported): if the file changed under you the write is refused with a typed stale
        state carrying the current content; omit ``expect_version`` to overwrite unconditionally.
        Parent directories are created as needed; ``content`` may be empty (a genuine zero-byte file).

        :param relative_path: path of the file to write, relative to the project root
        :param content: UTF-8 text to write; defaults to "" (writes a zero-byte file)
        :param overwrite: set True to replace an existing file; without it, an existing path is refused
        :param expect_version: the file's expected content version (from ``stat``) for a safe
            compare-and-swap overwrite; empty overwrites unconditionally
        :return: a confirmation, a refusal (already exists), or a typed stale state
        """
        # mutate through the lifecycle service (read-root == write-root)
        return self._api().lifecycle().create(relative_path, content, overwrite=overwrite, expect_version=expect_version).render()


class ListDirTool(Tool, FsApiMixin):
    """
    Lists the entries of a project directory as a typed listing (name, kind, size) -- the reflex
    replacement for raw ``ls``. Gitignored and hidden (dot-prefixed) entries are skipped by default so
    the listing matches what the project treats as source; the opt-outs surface them explicitly.
    Symlinks are reported with their target and never followed.
    """

    def apply(
        self,
        relative_path: str = ".",
        recursive: bool = False,
        max_entries: int = 1000,
        include_ignored: bool = False,
        include_hidden: bool = False,
    ) -> str:
        """
        List a directory's entries, relative to the project root.

        :param relative_path: the directory to list, relative to the project root (default: the root)
        :param recursive: whether to descend into sub-directories (symlinked directories are never followed)
        :param max_entries: maximum entries to return; the listing declares when it was truncated
        :param include_ignored: set True to include gitignored entries (skipped by default)
        :param include_hidden: set True to include hidden dot-prefixed entries (skipped by default)
        :return: a typed listing, or a typed not-found / not-a-directory state (never an exception)
        """
        return self._api().lifecycle().list_dir(
            relative_path,
            recursive=recursive,
            max_entries=max_entries,
            include_ignored=include_ignored,
            include_hidden=include_hidden,
        ).render()


class FindFileTool(Tool, FsApiMixin):
    """
    Finds files whose PATH matches a glob or substring -- the reflex replacement for raw ``find``. This
    matches paths, not contents; to search file contents use ``search_for_pattern``. Gitignored and
    hidden files are skipped and symlinked directories are not traversed.
    """

    def apply(
        self, pattern: str, relative_path: str = ".", max_results: int = 1000, include_ignored: bool = False
    ) -> str:
        """
        Find files by path pattern under a directory.

        :param pattern: a glob (e.g. ``"*.py"``, matched against the relative path and base name) or,
            when it has no glob metacharacters, a path substring
        :param relative_path: the directory to search under, relative to the project root (default: root)
        :param max_results: maximum matches to return; the result declares when it was truncated
        :param include_ignored: set True to include gitignored files (skipped by default)
        :return: a typed list of matching project-relative paths
        """
        return self._api().lifecycle().find_file(
            pattern, relative_path=relative_path, max_results=max_results, include_ignored=include_ignored
        ).render()


class ReplaceContentTool(EditingToolWithDiagnostics, ToolMarkerOptional, EditApiMixin):
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

        VERY IMPORTANT: The "regex" mode allows very large sections of code to be replaced WITHOUT
        quoting them fully: use a needle of the form "beginning.*?end-of-text-to-be-replaced" with
        wildcards instead of pasting the exact original text — shorter, cheaper, and you cannot make
        mistakes, because an ambiguous match returns an error you can refine, so wildcards are safe.
        Prefer regex mode with suitable wildcards for long multi-line replacements; use the
        symbol-level editors when replacing a whole method/class.

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
        with self.diagnostics_context(relative_path) as diagnostics_context:
            return diagnostics_context.format_result(
                self._api().replace_content(relative_path, needle, repl, mode, allow_multiple_occurrences=allow_multiple_occurrences)
            )


class ReplaceInFilesTool(EditingToolWithDiagnostics, ToolMarkerOptional, EditApiMixin):
    """
    Replaces occurrences of a pattern across multiple files, with dry-run preview and per-occurrence selection.
    """

    def apply(
        self,
        needle: str,
        repl: str,
        mode: Literal["literal", "regex"],
        relative_path: str = "",
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        dry_run: bool = False,
        occurrence_ids: list[str] | None = None,
        expected_count: int = -1,
        max_answer_chars: int = -1,
    ) -> str:
        r"""
        Replaces occurrences of a pattern across multiple files in ONE call.

        This is the preferred tool for repeated small edits (renames, import swaps, annotation changes,
        path prefixes) spanning several files or many places in one file: one call with a SHORT pattern
        replaces many single-file replacements with long disambiguating needles.

        Recommended protocol whenever there is ANY risk of unintended replacements:
        1. Call with dry_run=True: every prospective change is returned as a minimal line diff with an
           occurrence id; nothing is modified.
        2. Call again with dry_run=False, passing the ids you want in occurrence_ids (omit it to apply
           all). You pick the desired replacements from the list - no counting, no needle-crafting.

        For clearly unambiguous bulk replacements you may skip the dry run; pass expected_count as a
        guard. If the actual number of matches differs, NOTHING is changed and the diff list is
        returned, so a failed guard costs one call and gives you the dry-run output to select from.

        :param needle: the string (mode "literal") or regular expression (mode "regex"; Python `re`
            syntax with DOTALL and MULTILINE) to search for
        :param repl: the replacement string. In regex mode, backreferences to matched groups can be
            specified as $!1, $!2, etc.
        :param mode: either "literal" or "regex", specifying how `needle` is to be interpreted
        :param relative_path: only consider this file or directory (default: the whole project)
        :param paths_include_glob: optional glob (relative to the project root, e.g. "src/**/*.java")
            restricting which files are considered
        :param paths_exclude_glob: optional glob of files to exclude; takes precedence over the include glob
        :param dry_run: if True, do not modify anything; return the prospective changes as a list of
            diffs with occurrence ids
        :param occurrence_ids: optional list of occurrence ids (obtained from a dry run) to which the
            replacement is restricted; if any id is unknown or stale, NOTHING is changed. If omitted,
            all occurrences are replaced.
        :param expected_count: optional guard for calls without occurrence_ids: the number of
            occurrences you expect to be replaced. If the actual count differs, nothing is changed and
            the list of prospective changes is returned. -1 disables the guard.
        :param max_answer_chars: if the output exceeds this many characters, a shortened version is
            returned. -1 uses the configured default.
        :return: in a dry run, the prospective changes; otherwise a summary of the applied replacements
        """
        api = self._api()
        if dry_run:
            return api.replace_in_files(
                needle, repl, mode, relative_path, paths_include_glob, paths_exclude_glob, dry_run=True, max_answer_chars=max_answer_chars
            ).represent()
        with self.diagnostics_context() as diagnostics_context:
            result = api.replace_in_files(
                needle,
                repl,
                mode,
                relative_path,
                paths_include_glob,
                paths_exclude_glob,
                occurrence_ids=occurrence_ids,
                expected_count=expected_count,
                max_answer_chars=max_answer_chars,
            )
            assert isinstance(result, str)
            return diagnostics_context.format_result(result)


class DeleteLinesTool(EditingToolWithDiagnostics, ToolMarkerOptional, EditApiMixin):
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
        with self.diagnostics_context(relative_path) as diagnostics_context:
            return diagnostics_context.format_result(self._api().delete_lines(relative_path, start_line, end_line))


class ReplaceLinesTool(EditingToolWithDiagnostics, ToolMarkerOptional, EditApiMixin):
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
        with self.diagnostics_context(relative_path) as diagnostics_context:
            return diagnostics_context.format_result(self._api().replace_lines(relative_path, start_line, end_line, content))


class InsertAtLineTool(EditingToolWithDiagnostics, ToolMarkerOptional, EditApiMixin):
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
        with self.diagnostics_context(relative_path) as diagnostics_context:
            return diagnostics_context.format_result(self._api().insert_at_line(relative_path, line, content))


class SearchForPatternTool(Tool, FsApiMixin):
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
        hits = self._api().search_by_enclosing_symbol(
            substring_pattern,
            relative_path=relative_path,
            context_lines_before=context_lines_before,
            context_lines_after=context_lines_after,
            paths_include_glob=paths_include_glob,
            paths_exclude_glob=paths_exclude_glob,
            code_files_only=restrict_search_to_code_files,
        )
        return self._limit_length(
            hits.render_full(),
            max_answer_chars,
            shortened_result_factories=[hits.render_symbol_list, hits.render_per_file_counts, hits.render_summary],
        )


class StatTool(Tool):
    """
    Reports a path's metadata -- kind, size, permissions, and, for a regular file, its encoding,
    line-ending style, trailing-newline state, and content version -- the reflex replacement for raw
    ``stat``. The content version is the token ``create`` / ``delete_file`` / ``rename_file`` accept as
    ``expect_version`` for a safe compare-and-swap write. Symlinks are described without following.
    """

    def apply(self, relative_path: str) -> str:
        """
        Describe a file, directory, or symlink.

        :param relative_path: the path to describe, relative to the project root
        :return: a typed metadata line, or a typed not-found state (never an exception)
        """
        service = FilesystemLifecycle(self.get_project_root(), is_ignored=self.project.is_ignored_path)
        return service.stat(relative_path).render()


class DeleteFileTool(Tool, ToolMarkerCanEdit):
    """
    Deletes a file -- the reflex replacement for raw ``rm``. Deleting a missing path is a no-op success
    (idempotent). For a safe delete, pass ``expect_version`` (from ``stat``): if the content changed
    under you the delete is refused; omit it to delete unconditionally.
    """

    def apply(self, relative_path: str, expect_version: str = "") -> str:
        """
        Delete a file at a project-relative path.

        :param relative_path: the file to delete, relative to the project root
        :param expect_version: the file's expected content version (from ``stat``) for a safe
            compare-and-swap delete; empty deletes unconditionally
        :return: a confirmation, an idempotent no-op notice, or a typed stale state
        """
        service = FilesystemLifecycle(self.get_project_root(), is_ignored=self.project.is_ignored_path)
        return service.delete(relative_path, expect_version=expect_version).render()


class RenameFileTool(Tool, ToolMarkerCanEdit):
    """
    Renames or moves a file -- one verb for both -- the reflex replacement for raw ``mv``. Missing
    destination parent directories are created. For a safe move, pass ``expect_version`` (from ``stat``
    of the source); omit it to move unconditionally. This is a plain move; reference-aware companion
    edits are not performed.
    """

    def apply(self, source: str, destination: str, expect_version: str = "") -> str:
        """
        Rename or move a file within the project.

        :param source: the existing path, relative to the project root
        :param destination: the target path, relative to the project root (parents are created)
        :param expect_version: the source's expected content version (from ``stat``) for a safe
            compare-and-swap move; empty moves unconditionally
        :return: a confirmation, a refusal, or a typed stale state
        """
        service = FilesystemLifecycle(self.get_project_root(), is_ignored=self.project.is_ignored_path)
        return service.rename(source, destination, expect_version=expect_version).render()
