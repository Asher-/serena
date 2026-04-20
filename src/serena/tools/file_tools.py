"""
File and file system-related tools, specifically for
  * listing directory contents
  * reading files
  * creating files
  * editing at the file level
"""

import os

from serena.symbol import LanguageServerSymbol
from serena.tools import Tool
from serena.util.file_system import scan_directory
from serena.util.text_utils import search_files


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
