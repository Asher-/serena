# SPDX-License-Identifier: GPL-3.0-or-later
"""
The implementation of operations on the project's files.
"""

import os
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from serena.tools import CreateTextFileTool, FindFileTool, ListDirTool, ReadFileTool, SearchForPatternTool
from serena.util.file_lifecycle import AtomicFileWriter, FilesystemLifecycle
from serena.util.text_utils import MatchedConsecutiveLines
from solidlsp.ls_utils import TextUtils

from ..facade import FacadeApi, ReferencedType, facade_method
from ..representable import Renderer, RepresentableViaRenderer

if TYPE_CHECKING:
    from serena.agent import SerenaAgent


class FileContent(RepresentableViaRenderer):
    """
    The content of a file (or of a range of its lines): `text` (the joined lines) and `lines`.
    """

    def __init__(self, lines: list[str], renderer: "FileContentRenderer"):
        """
        :param lines: the lines (without line breaks)
        :param renderer: the renderer to use for representing the content
        """
        super().__init__(renderer)
        self.lines = lines

    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class FileContentRenderer(Renderer[FileContent]):
    def render(self, obj: FileContent) -> str:
        return self._limit_length(obj.text)


class DirectoryListing(RepresentableViaRenderer):
    """
    The entries of a directory: `dirs` and `files` (relative paths).
    """

    def __init__(self, dirs: list[str], files: list[str], renderer: "DirectoryListingRenderer"):
        """
        :param dirs: the relative paths of the directories
        :param files: the relative paths of the files
        :param renderer: the renderer to use for representing the listing
        """
        super().__init__(renderer)
        self.dirs = dirs
        self.files = files

    dirs: list[str]
    files: list[str]


class DirectoryListingRenderer(Renderer[DirectoryListing]):
    def render(self, obj: DirectoryListing) -> str:
        return self._limit_length(self._to_json({"dirs": obj.dirs, "files": obj.files}))


class PatternMatches(RepresentableViaRenderer):
    """
    The matches of a pattern search (`MatchedConsecutiveLines`).
    """

    def __init__(self, matches: list[MatchedConsecutiveLines], renderer: "PatternMatchesRenderer"):
        """
        :param matches: the matches
        :param renderer: the renderer to use for representing the matches
        """
        super().__init__(renderer)
        self.matches = matches

    matches: list[MatchedConsecutiveLines]

    def __len__(self) -> int:
        return len(self.matches)

    def matches_by_file_(self) -> dict[str, list[MatchedConsecutiveLines]]:
        result: defaultdict[str, list[MatchedConsecutiveLines]] = defaultdict(list)
        for match in self.matches:
            assert match.source_file_path is not None
            result[match.source_file_path].append(match)
        return result


class PatternMatchesRenderer(Renderer[PatternMatches]):
    """
    Renders matches as a mapping from file paths to matched line blocks (with context), falling back to progressively
    shorter representations (first lines, truncated first lines, line numbers, per-file counts, a summary) if the
    length limit is exceeded.
    """

    _TEXT_TRUNCATE = 60

    def render(self, obj: PatternMatches) -> str:
        matches_by_file = obj.matches_by_file_()
        file_to_matches = {path: [m.to_display_string() for m in matches] for path, matches in matches_by_file.items()}

        # capture lightweight match data for shortening before serialization
        match_lines_by_file = {
            path: [{"line": m.matched_lines[0].line_number, "text": m.matched_lines[0].line_content.strip()} for m in matches]
            for path, matches in matches_by_file.items()
        }

        # shortened result closures, from least to most aggressive shortening
        def render_first_lines(truncate: bool) -> str:
            """Render each match's first line, either in full or truncated to a fixed length."""

            def entry_text(text: str) -> str:
                if truncate and len(text) > self._TEXT_TRUNCATE:
                    return text[: self._TEXT_TRUNCATE] + "..."
                return text

            compact = {
                path: [{"line": m["line"], "text": entry_text(str(m["text"]))} for m in lines]
                for path, lines in match_lines_by_file.items()
            }
            if truncate:
                header = (
                    f"Matched lines (text over {self._TEXT_TRUNCATE} chars is truncated, marked with a trailing '...'); "
                    "use read_file with the line numbers for full content:"
                )
            else:
                header = "Matched lines per file; use read_file with the line numbers for surrounding context:"
            return f"{header}\n{self._to_json(compact)}"

        def make_first_lines_full() -> str:
            return render_first_lines(truncate=False)

        def make_first_lines_truncated() -> str:
            return render_first_lines(truncate=True)

        def make_line_numbers_only() -> str:
            numbers = {path: [m["line"] for m in lines] for path, lines in match_lines_by_file.items()}
            return f"Match lines per file:\n{self._to_json(numbers)}"

        def make_per_file_counts() -> str:
            counts = {path: len(lines) for path, lines in match_lines_by_file.items()}
            return f"Match counts per file:\n{self._to_json(counts)}"

        def make_summary() -> str:
            return f"Found {len(obj)} matches in {len(match_lines_by_file)} files."

        return self._limit_length(
            self._to_json(file_to_matches),
            shortened_result_factories=[
                make_first_lines_full,
                make_first_lines_truncated,
                make_line_numbers_only,
                make_per_file_counts,
                make_summary,
            ],
        )


class EnclosingSymbolHits:
    """
    The matches of a search, grouped by the symbol that encloses each, or by the file for a match outside any symbol.
    Each ``(name_path, relative_path)`` of a group is a valid argument for ``cursor_start``.
    """

    Group = tuple[str, str | None, str | None]
    """the file, the name path and the kind of a group; the latter two are None for a match outside any symbol"""

    def __init__(self, skipped_links: list[str]) -> None:
        """
        :param skipped_links: the symbolic links to directories the search did not enter
        """
        self._skipped_links = skipped_links
        self._grouped: dict[EnclosingSymbolHits.Group, list[str]] = {}
        self._group_order: list[EnclosingSymbolHits.Group] = []
        self._file_order: list[str] = []

    def add(self, rel: str, name_path: str | None, kind: str | None, hit: str) -> None:
        """
        :param rel: the file of the match
        :param name_path: the name path of the enclosing symbol, if any
        :param kind: the kind of the enclosing symbol, if any
        :param hit: the display string of the match
        """
        if rel not in self._file_order:
            self._file_order.append(rel)
        key = (rel, name_path, kind)
        if key not in self._grouped:
            self._grouped[key] = []
            self._group_order.append(key)
        self._grouped[key].append(hit)

    def render_summary(self) -> str:
        """
        :return: the header, counting the matches, the symbol-resident groups and the touched files
        """
        n_matches = sum(len(v) for v in self._grouped.values())
        n_symbols = sum(1 for k in self._grouped if k[1] is not None)
        header = f"Found {n_matches} matches across {n_symbols} symbols in {len(self._file_order)} files."
        if self._skipped_links:
            header += f" Symbolic links to directories not entered: {', '.join(self._skipped_links)}."
        return header

    @staticmethod
    def _render_hit(hit: str) -> list[str]:
        # indent every line of the hit so multi-line context blocks stay readable
        return [f"    {sub}" for sub in hit.splitlines()] or ["    "]

    def render_full(self) -> str:
        """
        :return: the groups clustered by file in discovery order, symbol blocks first, then file-level blocks
        """
        lines = [self.render_summary(), ""]
        for rel in self._file_order:
            sym_groups = [k for k in self._group_order if k[0] == rel and k[1] is not None]
            file_groups = [k for k in self._group_order if k[0] == rel and k[1] is None]
            for key in sym_groups:
                _, name_path, kind = key
                lines.append(f"@ {name_path} ({kind}) [{rel}]")
                lines.append("  hits:")
                for hit in self._grouped[key]:
                    lines.extend(self._render_hit(hit))
                lines.append("")
            for key in file_groups:
                lines.append(f"@ {rel} [{rel}]")
                lines.append("  hits:")
                for hit in self._grouped[key]:
                    lines.extend(self._render_hit(hit))
                lines.append("  (use cursor_overview to navigate)")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def render_symbol_list(self) -> str:
        """
        :return: the symbol and file identifiers only, with no hit bodies
        """
        lines = [self.render_summary(), ""]
        for rel in self._file_order:
            for key in [k for k in self._group_order if k[0] == rel]:
                _, name_path, kind = key
                count = len(self._grouped[key])
                if name_path is not None:
                    lines.append(f"@ {name_path} ({kind}) [{rel}] - {count} matches")
                else:
                    lines.append(f"@ {rel} [{rel}] - {count} matches (use cursor_overview to navigate)")
        return "\n".join(lines)

    def render_per_file_counts(self) -> str:
        """
        :return: one line per file
        """
        lines = [self.render_summary(), ""]
        per_file: dict[str, int] = {}
        for (rel, _, _), hits in self._grouped.items():
            per_file[rel] = per_file.get(rel, 0) + len(hits)
        for rel in self._file_order:
            lines.append(f"{rel}: {per_file[rel]} matches")
        return "\n".join(lines)


class FsApi(FacadeApi):
    def __init__(self, agent: "SerenaAgent") -> None:
        super().__init__(
            agent,
            name="fs",
            description="the project's files as units (as opposed to their content, see `edit`)",
            types=[
                ReferencedType(
                    MatchedConsecutiveLines, members=["source_file_path", "matched_lines", "start_line", "end_line", "to_display_string"]
                ),
            ],
        )

    @facade_method(corresponding_tool=ReadFileTool)
    def read_file(self, relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> FileContent:
        """
        Reads the given file or a range of its lines.

        :param relative_path: the relative path to the file to read
        :param start_line: the 0-based index of the first line to be retrieved, negative values count from the end of the file.
        :param end_line: the 0-based index of the last line to be retrieved (inclusive). If None, read until the end of the file.
        :return: the content
        """
        project = self._get_project()
        project.validate_relative_path(relative_path)

        # read lines, using the same (LSP-compliant) notion of line breaks as the line-based editing operations
        lines = TextUtils.split_lines(project.read_file(relative_path))

        start_idx, end_idx = TextUtils.line_slice_indices(start_line, end_line, len(lines))
        lines = lines[start_idx:end_idx]

        return FileContent(lines, FileContentRenderer(self._agent, max_answer_chars))

    @facade_method(can_edit=True, corresponding_tool=CreateTextFileTool)
    def create_text_file(self, relative_path: str, content: str) -> str:
        """
        Writes a new file or overwrites an existing file with the given content.

        :param relative_path: the relative path to the file to create
        :param content: the (appropriately encoded) content to write to the file
        :return: a message indicating success
        """
        project = self._get_project()
        project_root = Path(project.project_root)
        # the destination is resolved up front, so that the path which is validated is the path which is written
        abs_path = (project_root / relative_path).resolve()
        will_overwrite_existing = abs_path.exists()

        # validate the destination path; the validation resolves symlinks, thus covering abs_path
        project.validate_relative_path(relative_path)

        # write the file
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        AtomicFileWriter().write_text(str(abs_path), content, project.project_config.encoding, project.line_ending.newline_str, None)
        answer = f"File created: {relative_path}."
        if will_overwrite_existing:
            answer += " Overwrote existing file."
        return answer

    @facade_method(corresponding_tool=ListDirTool)
    def list_dir(
        self, relative_path: str, recursive: bool, skip_ignored_files: bool = False, max_answer_chars: int = -1
    ) -> DirectoryListing:
        """
        Lists files and directories in the given directory (optionally with recursion).

        :param relative_path: the relative path to the directory to list; pass "." to scan the project root
        :param recursive: whether to scan subdirectories recursively
        :param skip_ignored_files: whether to skip files and directories that are ignored
        :return: the listing
        """
        project = self._get_project()
        if not project.relative_path_exists(relative_path):
            raise FileNotFoundError(f"Directory not found: {relative_path} (check if the path is correct relative to the project root)")
        project.validate_relative_path(relative_path)

        scan_result = project.scan_directory(relative_path, recursive=recursive, skip_ignored_paths=skip_ignored_files)
        return DirectoryListing(scan_result.directories, scan_result.files, DirectoryListingRenderer(self._agent, max_answer_chars))

    @facade_method(corresponding_tool=FindFileTool)
    def find_file(self, file_mask: str, relative_path: str, skip_ignored_files: bool = False) -> list[str]:
        """
        Finds files matching the given file mask within the given relative path.

        :param file_mask: the filename or file mask (using the wildcards * or ?) to search for
        :param relative_path: the relative path to the directory to search in; pass "." to scan the project root
        :param skip_ignored_files: whether to skip ignored files/directories
        :return: the relative paths of the matching files
        """
        project = self._get_project()
        project.validate_relative_path(relative_path)

        scan_result = project.scan_directory(
            relative_path,
            recursive=True,
            skip_ignored_paths=skip_ignored_files,
            is_matching_file=lambda e: fnmatch(e.name, file_mask),
        )
        return scan_result.files

    @facade_method(corresponding_tool=SearchForPatternTool)
    def search_for_pattern(
        self,
        substring_pattern: str,
        context_lines_before: int = 0,
        context_lines_after: int = 0,
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        relative_path: str = "",
        restrict_search_to_code_files: bool = False,
        skip_ignored_files: bool = True,
        multiline: bool = True,
        max_answer_chars: int = -1,
    ) -> PatternMatches:
        """
        Searches for a regex pattern across project files, returning whole matched lines (plus optional context).
        Prefer symbolic operations if you know which symbols you are looking for!

        :param substring_pattern: regular expression to search for.
        :param context_lines_before: number of context lines to include before each match.
        :param context_lines_after: number of context lines to include after each match.
        :param paths_include_glob: optional glob (relative to project root, e.g. ``"src/**/*.ts"``) restricting which files are searched.
        :param paths_exclude_glob: optional glob to exclude files; takes precedence over `paths_include_glob`.
        :param relative_path: restricts the search to this file or subdirectory of the project root
        :param restrict_search_to_code_files: whether to search only (non-ignored) files containing analyzable code symbols
            (useful when looking for class/method definitions); otherwise also search non-code files.
        :param skip_ignored_files: whether to skip ignored sub-paths (default: True)
        :param multiline: whether to apply multi-line matching (default: True), enabling the flags re.DOTALL and re.MULTILINE
        :return: the matches, rendered as a mapping from file paths to matched consecutive lines (0-based line numbers)
        """
        project = self._get_project()
        relative_path = relative_path.strip()
        if relative_path:
            project.validate_relative_path(relative_path)

        matches = project.search_project_files_for_pattern(
            pattern=substring_pattern,
            relative_path=relative_path,
            context_lines_before=context_lines_before,
            context_lines_after=context_lines_after,
            paths_include_glob=paths_include_glob.strip(),
            paths_exclude_glob=paths_exclude_glob.strip(),
            multiline=multiline,
            code_files_only=restrict_search_to_code_files,
            skip_ignored_files=skip_ignored_files,
        )
        return PatternMatches(matches, PatternMatchesRenderer(self._agent, max_answer_chars))

    def lifecycle(self) -> FilesystemLifecycle:
        """
        :return: the lifecycle service of the active project, whose results are typed states (not found, stale, path escape)
            and whose writes are atomic
        """
        project = self._get_project()
        return FilesystemLifecycle(project.project_root, is_ignored=project.is_ignored_path)

    def search_by_enclosing_symbol(
        self,
        substring_pattern: str,
        relative_path: str = "",
        context_lines_before: int = 0,
        context_lines_after: int = 0,
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        code_files_only: bool = False,
    ) -> EnclosingSymbolHits:
        """
        Searches the project for a pattern, grouping the matches by the symbol that encloses each.

        :param substring_pattern: the regular expression to search for
        :param relative_path: the path to search under, relative to the project root; it must exist
        :param context_lines_before: the number of lines of context before each match
        :param context_lines_after: the number of lines of context after each match
        :param paths_include_glob: a glob of the files to include; empty includes all
        :param paths_exclude_glob: a glob of the files to exclude, which takes precedence over the include glob; empty excludes none
        :param code_files_only: whether to search only files an analyser can address symbolically
        :return: the grouped matches
        """
        from serena.symbol import LanguageServerSymbol, LanguageServerSymbolRetriever

        # locate and validate the search root
        project = self._get_project()
        if not os.path.exists(os.path.join(project.project_root, relative_path)):
            raise FileNotFoundError(f"Relative path {relative_path} does not exist.")

        # run the regex pass, collecting the links not entered
        skipped_links: list[str] = []
        matches = project.search_project_files_for_pattern(
            pattern=substring_pattern,
            relative_path=relative_path,
            context_lines_before=context_lines_before,
            context_lines_after=context_lines_after,
            paths_include_glob=paths_include_glob.strip(),
            paths_exclude_glob=paths_exclude_glob.strip(),
            code_files_only=code_files_only,
            skipped_links=skipped_links,
        )

        # opt into the LSP retriever only when the project's backend supports it
        retriever = LanguageServerSymbolRetriever(project) if self._agent.get_language_backend().is_lsp() else None

        # for each hit, ask the LSP for the enclosing symbol
        hits = EnclosingSymbolHits(skipped_links)
        for match in matches:
            assert match.source_file_path is not None
            rel = match.source_file_path
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
            hits.add(rel, name_path, kind, match.to_display_string())
        return hits
