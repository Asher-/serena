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

from serena.cursor import CursorManager, EdgeType, ReadRung, StructuralCursorState
from serena.symbol import LanguageServerSymbol
from serena.tools import SUCCESS_RESULT
from serena.tools.tools_base import Tool, ToolMarkerSymbolicEdit, ToolMarkerSymbolicRead
from serena.util.line_numbers import to_display_line, to_internal_line
from solidlsp.ls_types import SymbolKind


def _parse_edge_types(edge_types: list[str]) -> frozenset[EdgeType]:
    """
    Parse a list of edge type names into a frozenset of :class:`EdgeType`.

    :param edge_types: edge type names. Empty list yields an empty
        frozenset (no edges) — callers that want "all edges" must spell
        them out, since silently expanding empty to all reintroduces the
        per-symbol LSP cost the opt-in model is meant to avoid.
    :return: validated frozenset of edge types.
    :raises ValueError: when ``edge_types`` contains an unrecognised name.
    """
    parsed: set[EdgeType] = set()
    for name in edge_types:
        try:
            parsed.add(EdgeType(name))
        except ValueError:
            valid_names = [e.value for e in EdgeType]
            raise ValueError(f"Unknown edge type '{name}'. Valid edge types: {valid_names}")
    return frozenset(parsed)


class CursorStartTool(Tool, ToolMarkerSymbolicRead):
    """
    Start a navigation cursor at a symbol for incremental code exploration.
    The cursor tracks your position and lets you navigate along code relationships
    (containment, references, calls, type hierarchy).
    """

    # noinspection PyDefaultArgument
    # noinspection PyDefaultArgument
    def apply(
        self,
        name_path: str,
        because: str,
        relative_path: str = "",
        cursor_id: str = "",
        edge_types: list[str] = [],  # noqa: B006
    ) -> str:
        """
        Start a new cursor at the specified symbol and return its view.

        The cursor starts with **no edges resolved** by default -- only the
        symbol's position is shown. To see neighbors (children, references,
        callers, supertypes, etc.), pass ``edge_types`` here, or call
        ``cursor_configure`` afterwards. Resolving REFERENCES /
        REFERENCED_BY / CALLS via the language server can be a per-symbol
        cost of multiple minutes on large indexed projects, so the cursor
        only does what you explicitly asked for.

        ``because`` is required and articulates your **goal in
        understanding**: the semantic question you are trying to answer.
        Phrase it as the gap in your understanding the move closes, not
        a description of where you are navigating or a hypothesis about
        code structure. Examples:

        ✓ "to understand how session expiry interacts with rate limiting
           after a user reconnects"
        ✓ "to figure out which layer normalises the timestamp -- the
           ingest path or the renderer"
        ✗ "to find UserService.create_user" (mechanical)
        ✗ "I think the bug is in services.py" (hypothesis about code
           structure)
        ✗ "to look at the auth flow" (no semantic question)

        The reasoning is recorded on the cursor and rendered as ``why:
        <text>`` above the anchor on every subsequent projection so the
        trace shows your intent alongside the symbolic position.

        ``name_path`` accepts two grammars:

        1. **LSP symbol path** (e.g. ``"MyClass/my_method"``) -- the
           language-server-backed form used by ``find_symbol``. This
           resolves through the language server and supports the full
           LSP neighborhood.
        2. **Structural descent path** -- addresses a member inside a
           container literal (dict/list/object/array/mapping/sequence)
           that the language server does not surface as a symbol. When the
           LSP lookup misses and ``relative_path`` is provided, the cursor
           falls through to the file's structural backend.

        Structural path grammar differs slightly per backend:

        - **Python**: dict string keys appear as ``["key"]``; list indices
          as ``[N]``. Example: ``LAYER1_CLASSES/[7]/["source_file"]``.
        - **JSON / TOML / YAML**: object/table/mapping keys appear bare;
          sequence indices as ``[N]``. Example: ``members/existing``.

        :param name_path: name path of the symbol to start at (see grammar above).
        :param because: your **goal in understanding** for starting this
            cursor -- the semantic question this position lets you
            answer. Required: phrase as the gap in understanding the
            move closes, not a description of where you are going.
        :param relative_path: optional file path to narrow the symbol search.
            Required for the structural fallback (the backend is chosen by
            file extension).
        :param cursor_id: optional explicit cursor ID. Auto-generated if empty.
        :param edge_types: edges to resolve when rendering the
            neighborhood. Empty (default) renders only the cursor's
            position. Valid names: ``contains``, ``references``,
            ``referenced-by``, ``calls``, ``called-by``, ``inherits``,
            ``inherited-by``. Ignored for structural cursors.
        :return: a ``Started cursor <id>.`` line followed by the cursor's
            symbolic projection. The prefix mirrors :class:`CursorFindTool`'s
            output so callers can extract the cursor handle uniformly across
            both entry points.
        """
        parsed_edge_types = _parse_edge_types(edge_types) if edge_types else None
        manager = self.agent.get_cursor_manager()
        cid, state = manager.start_cursor(
            name_path=name_path,
            relative_path=relative_path or None,
            cursor_id=cursor_id or None,
            edge_types=parsed_edge_types,
        )
        # record the agent's stated goal so subsequent projections render it
        state.last_reasoning = because
        # prefix the projection with the assigned cursor id so callers can
        # parse the handle without inspecting the projection body
        return f"Started cursor {cid}.\n\n{manager.format_cursor_view(cid)}"

class CursorMoveTool(Tool, ToolMarkerSymbolicRead):
    """
    Move a navigation cursor to an adjacent symbol. The target should be
    visible in the cursor's current neighborhood (from cursor_start or cursor_look output).
    """

    def apply(
        self,
        cursor_id: str,
        target_name: str,
        because: str,
        target_relative_path: str = "",
    ) -> str:
        """
        Move the cursor to a neighboring symbol. Returns the new position's neighborhood.

        ``because`` is required and articulates your **goal in
        understanding**: the semantic question this hop lets you answer
        that the previous position did not. Phrase as the gap in your
        understanding the move closes -- not a description of where
        you are going or a hypothesis about what the target contains.
        Examples:

        ✓ "to confirm the rate limiter sees the same clock the auth
           middleware does"
        ✓ "to check whether this method is the only path that decrements
           the quota or whether there is a parallel one"
        ✗ "to look at create_user" (mechanical)
        ✗ "I think this is where validation happens" (hypothesis about
           code structure)

        The reasoning replaces the cursor's prior reasoning and is
        rendered as ``why: <text>`` above the anchor on every subsequent
        projection.

        :param cursor_id: the ID of the cursor to move (shown in cursor output).
        :param target_name: name of the symbol to move to. Must be visible in the current neighborhood.
        :param because: your **goal in understanding** for this hop --
            the semantic question this neighbor lets you answer. Required.
        :param target_relative_path: optional file path to disambiguate if multiple neighbors share the same name.
        :return: the updated cursor view at the new position.
        """
        manager = self.agent.get_cursor_manager()
        manager.move_cursor(
            cursor_id=cursor_id,
            target_name=target_name,
            target_relative_path=target_relative_path or None,
        )
        # update the cursor's reasoning to reflect the goal of this hop
        manager.get_cursor(cursor_id).last_reasoning = because
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
    # noinspection PyDefaultArgument
    def apply(
        self,
        cursor_id: str,
        edge_types: list[str] = [],  # noqa: B006
        include_body: bool = False,
        include_chain: bool = False,
        include_trail: bool = False,
        include_siblings: bool = False,
        include_gist: bool = False,
    ) -> str:
        """
        Configure the cursor's active edge types and projection toggles.

        The projection has six layers; only the **anchor** and **edge
        blocks** render unconditionally. Trail, chain, siblings, and gist
        are opt-in: a fresh cursor projects just its position so the
        agent can widen the view layer-by-layer when context warrants.
        Each ``include_*`` flag is replaced wholesale -- pass ``True``
        to render the layer, ``False`` (the default) to suppress it.

        :param cursor_id: the ID of the cursor to configure.
        :param edge_types: list of edge type names to make active. The
            previous set is replaced wholesale, so this both expands and
            contracts the configured edges. Empty list **clears** the
            active set (no neighbors will be resolved); spell out every
            edge you want when you want them all. Valid names:
            ``contains``, ``references``, ``referenced-by``, ``calls``,
            ``called-by``, ``inherits``, ``inherited-by``.
        :param include_body: when ``True``, the projection appends a
            ``--- body ---`` block with the symbol's full source.
        :param include_chain: when ``True``, the projection includes the
            ascending containment chain (immediate enclosing symbol up
            through the file).
        :param include_trail: when ``True``, the projection includes the
            last-N prior hops with a ``<- here`` marker on the current.
        :param include_siblings: when ``True``, the projection includes
            an inline list of peer names alongside the current symbol.
        :param include_gist: when ``True``, the projection includes a
            one-line extract of the symbol's body.
        :return: the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)

        # structural cursors carry no LSP edges; silently ignore edge_types so the
        # tool stays uniform across cursor kinds. The projection toggles still
        # apply -- structural views honour the same include_* flags.
        if isinstance(state, StructuralCursorState):
            state.include_body = include_body
            state.include_chain = include_chain
            state.include_trail = include_trail
            state.include_siblings = include_siblings
            state.include_gist = include_gist
            return manager.format_cursor_view(cursor_id)

        # Empty list explicitly clears the active set — see docstring. The
        # previous "empty == all edges" sugar is gone because each implicit
        # all-edges resolution can be a multi-minute LSP cost.
        state.active_edge_types = _parse_edge_types(edge_types)
        state.include_body = include_body
        state.include_chain = include_chain
        state.include_trail = include_trail
        state.include_siblings = include_siblings
        state.include_gist = include_gist

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

    This tool goes through the language server and therefore only resolves
    LSP-visible symbols (classes, functions, methods, top-level variables).
    Container-member paths surfaced by the structural backend — e.g.
    ``LAYER1_CLASSES/[7]/["source_file"]`` for a dict/list member inside a
    Python assignment, or a bare key inside a JSON/TOML/YAML document —
    are **not** searchable here. To reach a structural member, call
    :class:`CursorStartTool` directly with the full structural name path;
    it falls through to the structural backend when the LSP does not
    surface the symbol.
    """

    # noinspection PyDefaultArgument
    # noinspection PyDefaultArgument
    def apply(
        self,
        name_path_pattern: str,
        because: str,
        relative_path: str = "",
        depth: int = 0,
        include_body: bool = False,
        include_kinds: list[int] = [],  # noqa: B006
        exclude_kinds: list[int] = [],  # noqa: B006
        substring_matching: bool = False,
        max_matches: int = -1,
        cursor_id: str = "",
        edge_types: list[str] = [],  # noqa: B006
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

        ``because`` is required and articulates your **goal in
        understanding**: the semantic question the search lets you
        answer. Phrase as the gap in your understanding -- not a
        description of what you are searching for. Examples:

        ✓ "to find which subsystem owns timezone normalisation so I can
           reason about cross-tz aggregation"
        ✓ "to confirm whether quota enforcement happens at the API
           boundary or further inside the service"
        ✗ "to find UserService" (mechanical)
        ✗ "looking for the auth code" (no semantic question)

        When the search yields a unique match the reasoning is
        recorded on the started cursor and rendered above its anchor.
        For multi-match results the reasoning is included in the
        candidate-list header so the trace still carries intent.

        :param name_path_pattern: name path matching pattern.
        :param because: your **goal in understanding** for this search
            -- the semantic question the result lets you answer.
            Required.
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
        :param edge_types: edges to resolve when the match is unique and a
            cursor is started. Empty (default) renders only the cursor's
            position; pass an explicit list to opt in. Same valid names as
            :class:`CursorConfigureTool`. Has no effect when the search
            returns multiple candidates.
        :param max_answer_chars: maximum characters for the candidate-list output; -1 means use default.
        :return: a cursor view (unique match) or a JSON-formatted candidate list.
        """
        if include_body:
            depth = 0
        assert max_matches != 0, "max_matches must be > 0 or equal to -1."
        parsed_include_kinds: Sequence[SymbolKind] | None = [SymbolKind(k) for k in include_kinds] if include_kinds else None
        parsed_exclude_kinds: Sequence[SymbolKind] | None = [SymbolKind(k) for k in exclude_kinds] if exclude_kinds else None
        parsed_edge_types = _parse_edge_types(edge_types) if edge_types else None
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
            return f"why: {because}\n\nNo symbols found matching '{name_path_pattern}'."

        if n_matches == 1:
            cid, state = manager.register_cursor_at_symbol(
                symbols[0],
                cursor_id=cursor_id or None,
                edge_types=parsed_edge_types,
            )
            # record the agent's stated goal on the started cursor
            state.last_reasoning = because
            # honor include_body on the unique-match path: register_cursor_at_symbol creates
            # a cursor with include_body=False, so without this the documented include_body
            # flag was silently dropped for unique matches (the multi-candidate path below
            # already honors it via candidate_list_json)
            if include_body:
                state.include_body = True
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
            return f"why: {because}\n\n{summary}\n{self._to_json(rel_path_to_name_paths)}"

        def shortened_relative_path_to_name_paths() -> str:
            rel_path_to_name_paths: defaultdict[str, list[str]] = defaultdict(list)
            for s in symbols:
                rel_path_to_name_paths[s.location.relative_path or "unknown"].append(s.get_name_path())
            return f"Candidates (shortened):\n{self._to_json(rel_path_to_name_paths)}"

        result = f"why: {because}\n\nFound {n_matches} matching symbols; pick one and call cursor_start on its name path.\n{candidate_list_json()}"
        return self._limit_length(result, max_answer_chars, shortened_result_factories=[shortened_relative_path_to_name_paths])


class CursorGrepTool(Tool, ToolMarkerSymbolicRead):
    """
    Find a textual pattern and start a cursor at every enclosing symbol.

    cursor_grep is the cursor surface's regex search: for each hit that
    lives inside an LSP-addressable symbol it opens a fresh cursor at the
    enclosing symbol, so the agent can navigate each hit's neighborhood
    symbolically -- read the body, walk references, follow calls. Hits on
    non-symbol lines (comments, imports) are reported as a count; reach
    them through the cursor surface -- ``cursor_find``/``cursor_look`` to
    navigate to the region -- rather than treating them as out of reach.

    Use this whenever you want to locate or explore a pattern through the
    cursor. ``search_for_pattern`` remains available as a plain file-level
    reader for when you only need the matched lines.
    """

    def apply(
        self,
        substring_pattern: str,
        because: str,
        relative_path: str = "",
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        restrict_to_code_files: bool = True,
        max_matches: int = 20,
        context_lines_before: int = 0,
        context_lines_after: int = 0,
        max_answer_chars: int = -1,
    ) -> str:
        """
        Find a regex pattern and open one cursor per enclosing symbol.

        Pattern Matching Logic:
            For each match, the returned report contains the full lines where
            the substring pattern is found, optionally with context lines.
            The pattern is compiled with ``re.DOTALL``, so ``.`` matches
            newlines -- never put ``.*`` at the very beginning or end of the
            pattern, and prefer non-greedy quantifiers where possible.

        Cursor Creation Logic:
            For each match that falls inside an LSP-addressable symbol, a
            cursor is opened at the enclosing symbol. Multiple hits inside
            the same symbol collapse to a single cursor (one cursor per
            unique enclosing symbol). When the number of unique enclosing
            symbols exceeds ``max_matches``, only the first ``max_matches``
            symbols receive cursors; the rest are listed in a deferred
            section without cursor IDs. The agent's ``because`` is recorded
            on each opened cursor so subsequent projections render it.

        ``because`` is required and articulates your **goal in
        understanding** -- the semantic question the search lets you
        answer, not a description of what you are searching for.
        Examples:

        ✓ "to map every site that mutates the quota counter so I can
           reason about race conditions under concurrent withdrawal"
        ✓ "to find where the timezone fallback is applied so I can decide
           whether the bug lives in ingest or render"
        ✗ "to grep for self.users" (mechanical)
        ✗ "looking for ValueError" (no semantic question)

        :param substring_pattern: regular expression for a substring pattern
            to search for.
        :param because: your **goal in understanding** for this search --
            the semantic question the result lets you answer. Required.
            Recorded on every opened cursor.
        :param relative_path: only sub-paths of this path (relative to the
            project root) are searched. Pointing at a single file restricts
            the search to that file. Must exist.
        :param paths_include_glob: glob pattern restricting which files to
            include in the search. Empty disables include filtering.
        :param paths_exclude_glob: glob pattern excluding files; takes
            precedence over ``paths_include_glob``. Empty disables exclude.
        :param restrict_to_code_files: when ``True`` (the default), the
            search is confined to files an analyser can address symbolically
            -- the only files where enclosing-symbol grouping is meaningful.
        :param max_matches: maximum number of cursors to open (one per
            unique enclosing symbol). Additional groups beyond this cap are
            listed without cursor IDs. ``-1`` means no cap.
        :param context_lines_before: number of lines of context to include
            before each match in the report.
        :param context_lines_after: number of lines of context to include
            after each match in the report.
        :param max_answer_chars: maximum characters for the returned output;
            ``-1`` uses the configured default.
        :return: a multi-cursor report listing each opened cursor's anchor
            and hit count, plus any deferred symbols that would have
            received a cursor if not for ``max_matches``.
        """
        manager = self.agent.get_cursor_manager()
        groups, unsymboled = manager.find_pattern_with_enclosing_symbols(
            substring_pattern=substring_pattern,
            relative_path=relative_path or None,
            paths_include_glob=paths_include_glob,
            paths_exclude_glob=paths_exclude_glob,
            restrict_to_code_files=restrict_to_code_files,
            context_lines_before=context_lines_before,
            context_lines_after=context_lines_after,
        )

        n_symboled = sum(len(hits) for _, hits in groups)
        n_unsymboled = sum(len(hits) for _, hits in unsymboled)

        # no hits at all -- clean message rather than an empty header
        if not groups and not unsymboled:
            return f"why: {because}\n\nNo matches for {substring_pattern!r}."

        # cap cursor creation at max_matches; the remainder are listed
        # without cursors so the agent can decide whether to widen
        if max_matches < 0:
            opened_groups = groups
            deferred = []
        else:
            opened_groups = groups[:max_matches]
            deferred = groups[max_matches:]

        opened_cursors: list[tuple[str, LanguageServerSymbol, list[str]]] = []
        for sym, hits in opened_groups:
            cid, state = manager.register_cursor_at_symbol(sym)
            # record the agent's stated goal on every opened cursor
            state.last_reasoning = because
            opened_cursors.append((cid, sym, hits))

        # header: total hits split into symbol-anchored vs file-level, plus how
        # many cursors opened. Non-symbol hits are SURFACED below (with their
        # matched lines), never routed to another tool (spec-v2 §5.1/§5.3).
        header_parts = [
            f"Found {n_symboled + n_unsymboled} match(es): "
            f"{n_symboled} in {len(groups)} symbol(s), {n_unsymboled} on non-symbol lines.",
            f"Started {len(opened_cursors)} cursor(s)"
            + (f"; {len(deferred)} symbol(s) deferred." if deferred else "."),
        ]
        lines: list[str] = [f"why: {because}", "", " ".join(header_parts), ""]

        # per-cursor anchor + indented hit display strings
        for cid, _sym, hits in opened_cursors:
            view_lines = manager.format_cursor_view(cid).splitlines()
            # skip past the ``why: ...`` line to the actual ``@ ...`` anchor
            anchor = next((line for line in view_lines if line.startswith("@ ")), view_lines[0])
            lines.append(f"[{cid}]  {anchor}    {len(hits)} hit(s)")
            for hit in hits:
                for hit_line in hit.splitlines():
                    lines.append(f"    {hit_line}")
            lines.append("")

        # non-symbol hits: surfaced as file-level blocks (path + matched
        # line/number/context) so they are directly readable here -- the cursor
        # surface serves the read itself rather than deferring to another tool.
        if unsymboled:
            lines.append("-- non-symbol hits (no enclosing symbol; matched lines shown) --")
            for rel_path, hits in unsymboled:
                lines.append(f"  {rel_path}    {len(hits)} hit(s)")
                for hit in hits:
                    for hit_line in hit.splitlines():
                        lines.append(f"    {hit_line}")
            lines.append("")

        # deferred groups: just identifiers + counts so the agent can
        # rerun with a tighter pattern or a higher cap
        if deferred:
            lines.append("-- Deferred (no cursor opened; tighten the pattern or raise max_matches) --")
            for sym, hits in deferred:
                rel = sym.location.relative_path or "?"
                lines.append(
                    f"  @ {sym.get_name_path()} :{sym.symbol_kind_name}@{rel}    {len(hits)} hit(s)"
                )

        return self._limit_length("\n".join(lines).rstrip() + "\n", max_answer_chars)


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

        For structural cursors (container-member positions), ``body`` is the
        bare value expression that should replace the existing member's
        value; the member's key is preserved.

        :param cursor_id: the cursor whose current symbol to replace.
        :param body: the new body text.
        :return: confirmation and the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        # structural-cursor branch: dispatch to the container-member backend.
        # body is a bare value expression — the backend preserves the key half
        # of mapping-like containers.
        if isinstance(state, StructuralCursorState):
            before, after = manager.apply_container_edit(cursor_id, "replace", body)
            removed, added = self._count_diff_lines(before, after)
            diff_summary = f"Diff: -{removed} / +{added} lines"
            return self._reanchor_and_format(manager, cursor_id, diff_summary)

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

        diff_summary = f"Diff: -{removed} / +{added} lines"
        return self._reanchor_and_format(manager, cursor_id, diff_summary)

    @staticmethod
    def _reanchor_and_format(manager: CursorManager, cursor_id: str, diff_summary: str) -> str:
        """
        Re-anchor the cursor after an applied edit and render the result, tolerating a
        re-anchor failure.

        The edit has ALREADY been applied and saved by the time this runs. If
        re-anchoring then fails -- e.g. the body renamed the symbol so its old name path
        no longer resolves, or the new text is not yet resolvable by the language server
        -- that failure must NOT surface as a bare error that reads like the edit was a
        no-op. That was the non-atomic-corruption bug: the file was mutated but the caller
        saw only ``No symbol matching ...`` and treated it as "nothing happened". Instead,
        report success with the diff and a re-anchor note so the caller knows the edit
        landed.

        :param manager: the cursor manager owning ``cursor_id``.
        :param cursor_id: the cursor to re-anchor and render.
        :param diff_summary: the pre-computed ``Diff: -R / +A lines`` summary.
        :return: a success message with the cursor view, or a success message with a
            re-anchor note when re-anchoring fails.
        """
        try:
            manager.reanchor_cursor(cursor_id)
        except ValueError as e:
            return (
                f"{SUCCESS_RESULT}\n{diff_summary}\n\n"
                f"(edit applied; cursor {cursor_id} could not re-anchor afterward: {e})"
            )
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

        For structural cursors (container-member positions), ``body`` is a
        full member fragment: a ``"key": value`` pair for mapping-like
        containers (dict, object, mapping) or a bare value expression for
        sequence-like containers (list, array, sequence). The new member is
        inserted immediately before the cursor's anchor.

        To prepend at the container's head (no sibling anchor required),
        position the cursor on the container itself and use
        :class:`CursorInsertAtStartTool` instead.

        :param cursor_id: the cursor whose current symbol to insert before.
        :param body: the content to insert; it will be placed immediately before the line
            where the symbol is defined.
        :return: confirmation and the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        # structural-cursor branch: container-member insertion before the anchor
        if isinstance(state, StructuralCursorState):
            manager.apply_container_edit(cursor_id, "insert_before", body)
            manager.reanchor_cursor(cursor_id)
            return f"{SUCCESS_RESULT}\n\n" + manager.format_cursor_view(cursor_id)

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

        For structural cursors (container-member positions), ``body`` is a
        full member fragment: a ``"key": value`` pair for mapping-like
        containers (dict, object, mapping) or a bare value expression for
        sequence-like containers (list, array, sequence). The new member is
        inserted immediately after the cursor's anchor.

        To append at the container's tail (no sibling anchor required),
        position the cursor on the container itself and use
        :class:`CursorInsertAtEndTool` instead.

        :param cursor_id: the cursor whose current symbol to insert after.
        :param body: the content to insert; it will be placed on the line following the
            end of the symbol's definition.
        :return: confirmation and the updated cursor view.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        # structural-cursor branch: container-member insertion after the anchor
        if isinstance(state, StructuralCursorState):
            manager.apply_container_edit(cursor_id, "insert_after", body)
            manager.reanchor_cursor(cursor_id)
            return f"{SUCCESS_RESULT}\n\n" + manager.format_cursor_view(cursor_id)

        name_path = state.current_symbol.get_name_path()
        relative_path = state.current_location.relative_path
        if relative_path is None:
            raise ValueError(f"Cursor {cursor_id} has no relative path; cannot perform edit.")
        code_editor = self.create_code_editor()
        code_editor.insert_after_symbol(name_path, relative_file_path=relative_path, body=body)
        manager.reanchor_cursor(cursor_id)
        return f"{SUCCESS_RESULT}\n\n" + manager.format_cursor_view(cursor_id)


class CursorInsertAtStartTool(Tool, ToolMarkerSymbolicEdit):
    """
    Insert a member at the beginning of a container. Structural cursors only.

    The cursor must be positioned on a container (dict, list, object, array,
    mapping, sequence). The new member becomes the container's first entry.
    Calling this on an LSP cursor raises ``TypeError`` — symbol-level
    "prepend" is already expressible with :class:`CursorInsertBeforeTool`
    on the first child.
    """

    def apply(self, cursor_id: str, body: str) -> str:
        """
        Insert a member at the start of the container at the cursor's position.

        For mapping-like containers ``body`` is a full ``"key": value`` pair;
        for sequence-like containers it is a bare value expression. The
        cursor stays on the container.

        :param cursor_id: the structural cursor positioned on the container.
        :param body: the new member's source-text fragment.
        :return: confirmation and the updated cursor view.
        :raises TypeError: if ``cursor_id`` is an LSP cursor.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        if not isinstance(state, StructuralCursorState):
            raise TypeError(
                f"Cursor '{cursor_id}' is an LSP cursor; cursor_insert_at_start requires a structural cursor on a container.",
            )
        manager.apply_container_edit(cursor_id, "insert_start", body)
        manager.reanchor_cursor(cursor_id)
        return f"{SUCCESS_RESULT}\n\n" + manager.format_cursor_view(cursor_id)


class CursorInsertAtEndTool(Tool, ToolMarkerSymbolicEdit):
    """
    Insert a member at the end of a container. Structural cursors only.

    Mirror of :class:`CursorInsertAtStartTool`: the cursor must be on a
    container and the new member becomes its last entry. LSP cursors are
    rejected.
    """

    def apply(self, cursor_id: str, body: str) -> str:
        """
        Insert a member at the end of the container at the cursor's position.

        :param cursor_id: the structural cursor positioned on the container.
        :param body: the new member's source-text fragment.
        :return: confirmation and the updated cursor view.
        :raises TypeError: if ``cursor_id`` is an LSP cursor.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        if not isinstance(state, StructuralCursorState):
            raise TypeError(
                f"Cursor '{cursor_id}' is an LSP cursor; cursor_insert_at_end requires a structural cursor on a container.",
            )
        manager.apply_container_edit(cursor_id, "insert_end", body)
        manager.reanchor_cursor(cursor_id)
        return f"{SUCCESS_RESULT}\n\n" + manager.format_cursor_view(cursor_id)


class CursorRemoveMemberTool(Tool, ToolMarkerSymbolicEdit):
    """
    Remove the container member at the cursor's position. Structural cursors only.

    This is the container-member complement of :class:`SafeDeleteSymbol`:
    safe-delete runs an LSP reference check before deleting a symbol, while
    remove-member deletes a single member from a mapping or sequence
    literal where no such reference graph exists. Callers that need
    reference safety for LSP symbols should keep using :class:`SafeDeleteSymbol`.
    """

    def apply(self, cursor_id: str) -> str:
        """
        Remove the structural member the cursor is positioned on.

        After the removal the cursor is re-anchored at the parent container
        (the member's path is gone from the tree, so re-anchoring at the
        removed path would fail). Top-level removals close the cursor since
        no parent remains.

        :param cursor_id: the structural cursor identifying the member to
            remove.
        :return: confirmation and the updated cursor view, or a confirmation
            and close notice when the removed member was top-level.
        :raises TypeError: if ``cursor_id`` is an LSP cursor (symbol
            deletion is handled by :class:`SafeDeleteSymbol`).
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        if not isinstance(state, StructuralCursorState):
            raise TypeError(
                f"Cursor '{cursor_id}' is an LSP cursor; "
                "cursor_remove_member requires a structural cursor "
                "(use safe_delete_symbol for LSP symbols).",
            )

        # compute the parent container path BEFORE dispatching so we can
        # re-anchor after the removed path disappears from the index
        from serena.cursor import _split_name_path_segments

        segments = _split_name_path_segments(state.name_path)
        parent_path = "/".join(segments[:-1]) if len(segments) > 1 else ""

        manager.apply_container_edit(cursor_id, "remove", "")

        if not parent_path:
            # top-level member removed: there is no parent to re-anchor on;
            # close the cursor rather than carry a dangling path forward
            manager.close_cursor(cursor_id)
            return f"{SUCCESS_RESULT}\n\nCursor {cursor_id} closed: removed top-level member had no parent to re-anchor on."
        manager.reanchor_cursor(cursor_id, name_path=parent_path)
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
        :param start_line: the 1-based line number of the first line to replace (inclusive).
        :param end_line: the 1-based line number of the last line to replace (inclusive).
            Must satisfy ``start_line <= end_line``. To replace a single line, pass
            ``start_line == end_line``.
        :param body: the replacement text. The edit is line-oriented: when ``body``
            does not already end with a newline, the file's existing line terminator
            (``\\r\\n`` for CRLF files, ``\\n`` otherwise) is appended automatically
            so the next file line is never fused onto the body's final line. Pass
            an empty string to delete the range with no replacement.
        :return: a success confirmation and the diff summary.
        """
        # validate input before any filesystem work so callers see a clean error message
        if start_line < 1 or end_line < start_line:
            raise ValueError(
                f"cursor_replace_range: invalid range [{start_line}, {end_line}] in {relative_path!r}; require 1 <= start_line <= end_line."
            )

        # snapshot content so we can report a line-diff summary after the edit
        pre_content = self.project.read_file(relative_path)

        # execute the edit via the filesystem-level code editor layer; the operation
        # bypasses the LSP's workspace-edit interface intentionally — workspace edits
        # are rejected for regions the server does not recognize as symbols
        code_editor = self.create_code_editor()
        code_editor.replace_lines(relative_path, to_internal_line(start_line), to_internal_line(end_line), body)

        # compute a diff summary for the return value so the caller can verify the edit
        # size against their intent (mirrors cursor_replace_body's diff-summary style)
        post_content = self.project.read_file(relative_path)
        removed, added = CursorReplaceBodyTool._count_diff_lines(pre_content, post_content)
        diff_summary = f"Diff: -{removed} / +{added} lines"
        return f"{SUCCESS_RESULT}\n{diff_summary}"


class CursorReplaceRangeVerifiedTool(Tool, ToolMarkerSymbolicEdit):
    """
    Drift-safe variant of ``cursor_replace_range``: the caller supplies the text
    they expect to find at ``[start_line, end_line]``, and the edit aborts with a
    unified diff if the file has shifted since the caller last inspected it.

    Typical failure mode prevented: a preceding ``cursor_overview`` / ``cursor_look``
    is separated from the edit by intervening work that rewrote the file; the
    previously-correct ``start_line`` / ``end_line`` now point at the wrong content
    (a ``#endif``, a struct-closing ``}``, etc.); and ``cursor_replace_range`` would
    overwrite the shifted content blindly. With this tool the mismatch surfaces
    before any mutation, and the caller can re-resolve the range from a fresh
    overview.

    Note on the marker: this tool mutates a raw line range rather than an LSP
    symbol; see the note on ``CursorReplaceRangeTool``.
    """

    def apply(
        self,
        relative_path: str,
        start_line: int,
        end_line: int,
        expected_content: str,
        body: str,
    ) -> str:
        """
        Replace the file's lines ``[start_line, end_line]`` (inclusive) with
        ``body`` after verifying the current content of those lines matches
        ``expected_content``.

        :param relative_path: relative path to the file to edit.
        :param start_line: the 1-based line number of the first line to replace (inclusive).
        :param end_line: the 1-based line number of the last line to replace (inclusive).
            Must satisfy ``start_line <= end_line``.
        :param expected_content: the text the caller expects to find at
            ``[start_line, end_line]``. Compared line-by-line via
            ``str.splitlines()``, so a single trailing newline on either side is
            ignored and CRLF/LF line endings are treated as equivalent. On
            mismatch a ``ValueError`` with a unified diff is raised and the file
            is left unmodified.
        :param body: the replacement text. The edit is line-oriented: when ``body``
            does not already end with a newline, the file's existing line terminator
            (``\\r\\n`` for CRLF files, ``\\n`` otherwise) is appended automatically
            so the next file line is never fused onto the body's final line. Pass
            an empty string to delete the range with no replacement.
        :return: a success confirmation and the diff summary.
        """
        # validate input before any filesystem work so callers see a clean error message
        if start_line < 1 or end_line < start_line:
            raise ValueError(
                f"cursor_replace_range_verified: invalid range [{start_line}, {end_line}] "
                f"in {relative_path!r}; require 1 <= start_line <= end_line."
            )

        # convert the 1-based agent args to internal 0-based indices once at the tool
        # boundary; the drift check and the editor layer both operate 0-based
        internal_start = to_internal_line(start_line)
        internal_end = to_internal_line(end_line)

        # snapshot content for both drift verification and the post-edit diff summary
        pre_content = self.project.read_file(relative_path)

        # drift check: expected text must match what is currently at the range
        self._verify_expected(relative_path, pre_content, internal_start, internal_end, expected_content)

        # execute the edit via the filesystem-level code editor layer (same path as
        # cursor_replace_range so the two tools share a single mutation implementation)
        code_editor = self.create_code_editor()
        code_editor.replace_lines(relative_path, internal_start, internal_end, body)

        # compute a diff summary for the return value so the caller can verify the
        # edit size against their intent (mirrors cursor_replace_range)
        post_content = self.project.read_file(relative_path)
        removed, added = CursorReplaceBodyTool._count_diff_lines(pre_content, post_content)
        diff_summary = f"Diff: -{removed} / +{added} lines"
        return f"{SUCCESS_RESULT}\n{diff_summary}"

    @staticmethod
    def _verify_expected(
        relative_path: str,
        pre_content: str,
        start_line: int,
        end_line: int,
        expected_content: str,
    ) -> None:
        """
        Confirm that the file's lines ``[start_line, end_line]`` (inclusive) match
        ``expected_content`` line-by-line. Raise ``ValueError`` with a unified
        diff on mismatch; return silently on match.

        :param relative_path: relative path of the file being edited (for error text).
        :param pre_content: the file's full current content.
        :param start_line: inclusive 0-based start line index.
        :param end_line: inclusive 0-based end line index.
        :param expected_content: the text the caller expects at the range.
        """
        # extract the actual content at the requested line range
        all_lines = pre_content.splitlines(keepends=True)
        if start_line >= len(all_lines):
            raise ValueError(
                f"cursor_replace_range_verified: start_line={to_display_line(start_line)} is beyond the "
                f"file's line count ({len(all_lines)}) in {relative_path!r}."
            )
        actual_slice = all_lines[start_line : end_line + 1]
        actual_content = "".join(actual_slice)

        # normalise both sides via splitlines so trailing-newline and CRLF/LF
        # differences do not cause spurious drift errors
        actual_normalised = actual_content.splitlines()
        expected_normalised = expected_content.splitlines()
        if actual_normalised == expected_normalised:
            return

        # emit a compact unified diff so the caller can see exactly what shifted
        diff_lines = list(
            difflib.unified_diff(
                expected_normalised,
                actual_normalised,
                fromfile="expected",
                tofile=f"actual ({relative_path}:{to_display_line(start_line)}-{to_display_line(end_line)})",
                lineterm="",
            )
        )
        raise ValueError(
            "cursor_replace_range_verified: file drift detected; expected content does "
            "not match actual content at the requested range. Re-run cursor_overview / "
            "cursor_look to refresh your view of the file, then retry with the updated "
            "range and expected content.\n" + "\n".join(diff_lines)
        )


class CursorReplaceBetweenTool(Tool, ToolMarkerSymbolicEdit):
    """
    Replace the interstitial region between two anchor symbols with ``body``.

    Both anchors are resolved via the language server on every call — anchors
    are re-resolved rather than remembered — so the line range is always
    computed from the file's current structure. Even if the file has shifted
    between a prior overview and this edit, the anchor-based range is
    recomputed fresh and still targets the region the caller intends.

    Use this primitive for non-symbolic regions that sit between two nameable
    LSP symbols: preprocessor-directive blocks (``#if`` / ``#endif`` groups),
    detached comments, blank-line gaps, or free-floating ``///`` doc comments
    outside any symbol's extent. The interstitial range is computed as
    ``[end_of(before_symbol) + 1, start_of(after_symbol) - 1]`` (inclusive).

    When the caller already knows what text currently occupies the range,
    passing ``expected_content`` adds the same drift check as
    ``cursor_replace_range_verified``: the edit aborts with a unified diff if
    the actual content does not match.
    """

    def apply(
        self,
        relative_path: str,
        before_symbol: str,
        after_symbol: str,
        body: str,
        expected_content: str | None = None,
    ) -> str:
        """
        Replace the lines between ``before_symbol`` (exclusive) and ``after_symbol``
        (exclusive) with ``body``.

        :param relative_path: relative path to the file both anchors live in.
        :param before_symbol: LSP name path of the anchor that precedes the
            interstitial region (e.g. ``"MyClass/firstMethod"``).
        :param after_symbol: LSP name path of the anchor that follows the
            interstitial region. Must resolve to a symbol that starts strictly
            after ``before_symbol`` ends.
        :param body: the replacement text. The edit is line-oriented: when ``body``
            does not already end with a newline, the file's existing line terminator
            (``\\r\\n`` for CRLF files, ``\\n`` otherwise) is appended automatically
            so ``after_symbol``'s opening line is never fused onto the body's final
            line.
        :param expected_content: optional text the caller expects at the
            interstitial range, enabling a drift check identical to
            ``cursor_replace_range_verified``'s. Line-by-line comparison via
            ``str.splitlines()`` so trailing-newline and CRLF/LF differences are
            ignored.
        :return: a success confirmation, the diff summary, and the computed range.
        """
        # resolve both anchors on every call so drift is structurally eliminated
        before = self._resolve_unique_anchor("before_symbol", before_symbol, relative_path)
        after = self._resolve_unique_anchor("after_symbol", after_symbol, relative_path)

        # compute the inter-symbol line range from the fresh positions
        before_end = before.get_body_end_position_or_raise().line
        after_start = after.get_body_start_position_or_raise().line
        start_line = before_end + 1
        end_line = after_start - 1

        # reject misordered or touching anchors with a concrete explanation
        if start_line > end_line:
            raise ValueError(
                f"cursor_replace_between: no interstitial lines between "
                f"{before_symbol!r} (body ends at line {to_display_line(before_end)}) and "
                f"{after_symbol!r} (body starts at line {to_display_line(after_start)}); "
                f"computed range [{to_display_line(start_line)}, {to_display_line(end_line)}] is empty. "
                f"Use cursor_insert_after {before_symbol!r} or "
                f"cursor_insert_before {after_symbol!r} for adjacent symbols."
            )

        # snapshot and optionally drift-check before mutating
        pre_content = self.project.read_file(relative_path)
        if expected_content is not None:
            CursorReplaceRangeVerifiedTool._verify_expected(relative_path, pre_content, start_line, end_line, expected_content)

        # execute the edit via the filesystem-level code editor layer
        code_editor = self.create_code_editor()
        code_editor.replace_lines(relative_path, start_line, end_line, body)

        # compute a diff summary and include the computed range so the caller can
        # verify the anchors resolved to the lines they expected
        post_content = self.project.read_file(relative_path)
        removed, added = CursorReplaceBodyTool._count_diff_lines(pre_content, post_content)
        diff_summary = f"Diff: -{removed} / +{added} lines"
        return (
            f"{SUCCESS_RESULT}\n{diff_summary}\n(replaced lines [{to_display_line(start_line)}, {to_display_line(end_line)}] between {before_symbol!r} and {after_symbol!r})"
        )

    def _resolve_unique_anchor(self, role: str, name_path: str, relative_path: str) -> "LanguageServerSymbol":
        """
        Look up ``name_path`` on the language server and return the unique match.

        :param role: which anchor role is being resolved (for error messages:
            ``"before_symbol"`` or ``"after_symbol"``).
        :param name_path: the LSP name path to resolve.
        :param relative_path: the file to restrict the lookup to.
        :return: the single matched ``LanguageServerSymbol``.
        :raises ValueError: when the anchor fails to resolve uniquely.
        """
        manager = self.agent.get_cursor_manager()
        symbols = manager.find_symbols(name_path, relative_path=relative_path)
        if not symbols:
            raise ValueError(
                f"cursor_replace_between: {role} {name_path!r} did not resolve to any "
                f"symbol in {relative_path!r}. Verify the name path with cursor_overview."
            )
        if len(symbols) > 1:
            raise ValueError(
                f"cursor_replace_between: {role} {name_path!r} is ambiguous in "
                f"{relative_path!r} ({len(symbols)} matches). Disambiguate by using a "
                f"more specific name path."
            )
        return symbols[0]


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
        # rename is LSP-only; structural cursors cannot use the language server's
        # rename refactoring, so we fail fast via get_lsp_cursor.
        state = manager.get_lsp_cursor(cursor_id)
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

    def apply(self, relative_path: str, because: str, cursor_id: str = "", max_answer_chars: int = -1) -> str:
        """
        Show the top-level symbols in a file as a compact symbolic listing.

        Internally this delegates to the language server symbol retriever to find top-level
        symbols and renders each as a stable ``name :Kind@file:line:`` handle, mirroring the
        anchor format produced by ``format_cursor_view`` so callers see one unified
        symbolic projection across the cursor surface.

        ``because`` is required and articulates your **goal in
        understanding** for asking for this overview -- the semantic
        question the file's structure lets you answer. Phrase as the
        gap in your understanding, not what you expect the file to
        contain. Examples:

        ✓ "to learn what subsystems are co-located in services.py before
           deciding where to place the new throttle"
        ✓ "to confirm whether models.py defines its own validation or
           delegates to a shared utility"
        ✗ "to look at services.py" (no semantic question)
        ✗ "to find UserService" (use cursor_find)

        :param relative_path: relative path to the source file.
        :param because: your **goal in understanding** for asking for
            this listing -- the semantic question the file's structure
            lets you answer. Required.
        :param cursor_id: optional cursor ID for the started cursor. Auto-generated otherwise.
        :param max_answer_chars: maximum characters for the returned output; -1 means use default.
        :return: a compact listing of top-level symbols, one per line, prefixed with the agent's stated why.
        """
        import os

        file_path = os.path.join(self.project.project_root, relative_path)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File {relative_path} does not exist in the project.")
        if os.path.isdir(file_path):
            raise ValueError(f"Expected a file path, but got a directory path: {relative_path}.")

        manager = self.agent.get_cursor_manager()
        rung = manager.resolve_read_rung(relative_path)

        # Rung 1 -- LSP: the language server's top-level symbols (unchanged).
        if rung is ReadRung.LSP:
            retriever = self.create_language_server_symbol_retriever()
            top_level = retriever.get_symbol_overview(relative_path).get(relative_path, [])
            if not top_level:
                return f"why: {because}\n\nNo top-level symbols found in {relative_path}."
            # render each symbol as ``name :Kind@file:line:`` -- the same handle
            # shape used by format_cursor_view's anchor, so the agent treats
            # overview entries and cursor projections uniformly.
            lines: list[str] = [f"why: {because}", "", f"Top-level symbols in {relative_path}:"]
            for sym in top_level:
                line = sym.line
                # sym.line is the internal 0-based index; convert to the 1-based
                # cat -n number at this display boundary (spec-v2 §5.7)
                loc = f"{relative_path}:{to_display_line(line)}" if line is not None else relative_path
                kind = sym.symbol_kind_name
                if kind:
                    lines.append(f"  {sym.name} :{kind}@{loc}:")
                else:
                    lines.append(f"  {sym.name} @{loc}:")
            return self._limit_length("\n".join(lines), max_answer_chars)

        # Rung 2 -- structural: fall through to the structural backend so a
        # non-LSP file (yaml/json/toml/...) still lists its top-level nodes
        # instead of the old "Cannot extract symbols" dead-end (spec §5.1/§5.3).
        if rung is ReadRung.STRUCTURAL:
            structural = manager.structural_overview(relative_path)
            if not structural:
                return f"why: {because}\n\nNo top-level structural nodes found in {relative_path}."
            lines = [f"why: {because}", "", f"Top-level structural nodes in {relative_path}:"]
            for name_path, kind in structural:
                if kind:
                    lines.append(f"  {name_path} :{kind}@{relative_path}:")
                else:
                    lines.append(f"  {name_path} @{relative_path}:")
            return self._limit_length("\n".join(lines), max_answer_chars)

        # Rung 3 -- plaintext floor: no analyzer or structural backend claims
        # this file. NEVER raise; point at the read path that works today. A
        # line/size/encoding summary lands with the plaintext backend (T4).
        return (
            f"why: {because}\n\n"
            f"No symbol structure in {relative_path} "
            f"(no language server or structural backend for this file type). "
            f"Use cursor_grep to read its contents."
        )



class CursorNarrateTool(Tool, ToolMarkerSymbolicRead):
    """
    Record the agent's goal in understanding on a cursor without moving it.

    Use ``cursor_narrate`` to attach a fresh ``why`` to a cursor between
    navigation calls -- e.g. after looking at a position and forming a
    new hypothesis, or after the previous reasoning was answered and
    the next move's goal is different. The recorded text replaces the
    cursor's prior reasoning and is rendered as ``why: <text>`` above
    the anchor on every subsequent projection.

    The narration is a *semantic goal* -- the gap in your understanding
    you are now trying to close -- not a description of where the
    cursor is or a hypothesis about code structure. See
    :class:`CursorStartTool` for the same authoring discipline.
    """

    def apply(self, cursor_id: str, because: str) -> str:
        """
        Update the cursor's recorded ``why`` and return its updated view.

        :param cursor_id: the ID of the cursor whose reasoning to update.
        :param because: your **goal in understanding** at this cursor's
            current position -- the semantic question you are now trying
            to answer. Replaces any prior reasoning recorded on the
            cursor.

            ✓ "to confirm whether the rate limiter respects the same
               clock as auth"
            ✓ "to figure out whether ingest or render normalises tz"
            ✗ "I'm here looking at create_user" (mechanical)
            ✗ "I think this is the bug" (hypothesis about code structure)

        :return: the updated cursor view, with ``why: <text>`` rendered
            above the anchor.
        """
        manager = self.agent.get_cursor_manager()
        state = manager.get_cursor(cursor_id)
        state.last_reasoning = because
        return manager.format_cursor_view(cursor_id)
