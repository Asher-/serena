"""
Cursor-based code navigation for Serena.

Provides a stateful cursor that can be positioned on a symbol and moved along LSP graph edges
(contains, references, calls, type hierarchy) for incremental exploration.
"""

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from serena.project import Project
from serena.symbol import LanguageServerSymbol, LanguageServerSymbolLocation, LanguageServerSymbolRetriever
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_utils import PathUtils
from solidlsp.lsp_protocol_handler.lsp_types import (
    CallHierarchyItem,
    SymbolKind,
    TypeHierarchyItem,
)
from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.kinds import KindName
from solidlsp.structural.registry import StructuralBackendRegistry, default_structural_backend_registry

log = logging.getLogger(__name__)


class EdgeType(Enum):
    """Types of edges available for cursor navigation."""

    CONTAINS = "contains"
    REFERENCES = "references"
    REFERENCED_BY = "referenced-by"
    CALLS = "calls"
    CALLED_BY = "called-by"
    INHERITS = "inherits"
    INHERITED_BY = "inherited-by"


ALL_EDGE_TYPES = frozenset(EdgeType)

# A new cursor starts with no edges resolved. Resolving REFERENCES /
# REFERENCED_BY / CALLS / etc. is a per-symbol LSP cost that can be
# minutes on large indexed projects (SourceKit-LSP on iina, e.g.), so the
# agent must opt in to the edges it wants — either at start time via the
# ``edge_types`` parameter on ``cursor_start`` / ``cursor_find``, or after
# the fact via ``cursor_configure``.
DEFAULT_EDGE_TYPES: frozenset[EdgeType] = frozenset()

# operations that require the cursor to be positioned on a container node
_CONTAINER_POSITIONED_OPERATIONS: frozenset[str] = frozenset({"insert_start", "insert_end"})
# operations that require the cursor to be positioned on a container's member
_MEMBER_ANCHORED_OPERATIONS: frozenset[str] = frozenset({"insert_before", "insert_after", "replace", "remove"})
# kinds that guarantee the cursor addresses a non-container leaf. Only the
# kinds on this list can be pre-flagged without inspecting the parsed tree:
#
# * ``container_member`` — Python's kind for a dict/list member whose value is
#   *not* itself a dict or list (the walk re-yields container-valued members
#   with kind ``container``, overwriting the cache entry; so a cached
#   ``container_member`` is definitionally a scalar-valued member).
# * ``string`` / ``number`` / ``boolean`` / ``null`` — JSON array-item scalar
#   kinds emitted by ``_value_kind``.
# * ``scalar`` — the TOML / YAML catch-all for array-item scalars.
#
# JSON ``member``, TOML ``pair``, YAML ``pair`` are deliberately absent: the
# walk tags every mapping member with those kinds regardless of whether the
# value is scalar or compound, so they are not a reliable pre-flag signal.
_STRUCTURAL_SCALAR_KINDS: frozenset[str] = frozenset({"container_member", "string", "number", "boolean", "null", "scalar"})


@dataclass
class NeighborSymbol:
    """A symbol reachable from the current cursor position via a specific edge type."""

    name: str
    kind: str
    relative_path: str | None
    line: int | None
    column: int | None
    edge_type: EdgeType
    detail: str | None = None

    @property
    def location_str(self) -> str:
        if self.relative_path and self.line is not None:
            return f"{self.relative_path}:{self.line + 1}"
        elif self.relative_path:
            return self.relative_path
        return "?"

    def format_compact(self) -> str:
        parts = [self.name]
        if self.kind:
            parts.append(f"({self.kind})")
        parts.append(f"[{self.location_str}]")
        if self.detail:
            parts.append(f"— {self.detail}")
        return " ".join(parts)


@dataclass
class CursorState:
    """The state of a single navigation cursor."""

    cursor_id: str
    current_symbol: LanguageServerSymbol
    current_location: LanguageServerSymbolLocation
    trail: list[LanguageServerSymbolLocation] = field(default_factory=list)
    active_edge_types: frozenset[EdgeType] = DEFAULT_EDGE_TYPES
    include_body: bool = False

    def record_move(self, new_symbol: LanguageServerSymbol, new_location: LanguageServerSymbolLocation) -> None:
        """Record moving the cursor to a new symbol."""
        self.trail.append(self.current_location)
        self.current_symbol = new_symbol
        self.current_location = new_location


@dataclass
class StructuralCursorState:
    """The state of a cursor positioned on a structural (non-LSP) node.

    Used when a name path resolves to a container-literal member (dict entry,
    list item, object member, array item, mapping pair, sequence item) that
    the language server does not surface as a symbol. The cursor carries a
    canonical name path (as emitted by the backend's ``walk_nodes``) plus the
    file's relative path; edit tools re-resolve against a fresh parse on each
    call so handles stay consistent with on-disk content.

    :ivar cursor_id: the cursor's handle.
    :ivar relative_path: POSIX-style project-relative path of the file whose
        structural backend owns this cursor. Required because structural
        lookups route through the backend registry by file extension.
    :ivar name_path: canonical name path identifying the addressed node. For
        Python, dict keys appear as ``["key"]`` and list indices as ``[N]``;
        for JSON/TOML/YAML keys appear bare and indices as ``[N]``.
    :ivar kind: the :type:`~solidlsp.structural.kinds.KindName` of the resolved
        node captured at ``cursor_start`` time for display.
    :ivar trail: prior canonical name paths visited by this cursor.
    :ivar include_body: when ``True``, ``format_cursor_view`` appends the
        addressed node's serialized source (a ``--- body ---`` block) to the
        rendered view. Default ``False``. Mirrors :class:`CursorState` so
        :class:`~serena.tools.cursor_tools.CursorConfigureTool` can toggle
        the same display option across LSP and structural cursors.
    """

    cursor_id: str
    relative_path: str
    name_path: str
    kind: KindName
    trail: list[str] = field(default_factory=list)
    include_body: bool = False


AnyCursorState = CursorState | StructuralCursorState


@dataclass(frozen=True)
class StructuralResolution:
    """Result of a structural name-path resolution against a file.

    Returned by :meth:`CursorManager.resolve_structural_name_path` when a name
    path (including synthetic ``parent/<kind>#<index>`` paths) matches an
    addressable node in the file's structural backend.

    :ivar name_path: canonical name path as emitted by the backend's
        :meth:`~solidlsp.structural.base.StructuralLanguage.walk_nodes`.
    :ivar kind: the :type:`~solidlsp.structural.kinds.KindName` of the
        resolved node.
    :ivar node: opaque backend-owned AST handle for the node. Treat this as a
        value: do not inspect backend-internal attributes.
    """

    name_path: str
    kind: KindName
    node: Any


@dataclass
class _StructuralNodeCacheEntry:
    """Per-file cache of a structural walk result.

    :ivar mtime_ns: modification time of the source file when the cache was
        populated; used as the invalidation key.
    :ivar nodes_by_path: map from ``name_path`` to ``(kind, node)`` covering
        every node yielded by the backend's ``walk_nodes`` for the file.
    """

    mtime_ns: int
    nodes_by_path: dict[str, tuple[KindName, Any]]


def _split_name_path_segments(name_path: str) -> list[str]:
    """Split a structural name path into segments on bracket-depth-0 slashes.

    Canonical name paths may embed ``/`` inside ``[...]`` brackets (e.g. a
    Python dict key ``"a/b"`` appears as ``["a/b"]``); splitting on the raw
    ``/`` character would corrupt such keys. This helper walks the path and
    breaks only at ``/`` characters whose surrounding bracket depth is zero,
    yielding the canonical atoms used by ``walk_nodes``.

    :param name_path: a structural name path emitted by a backend's
        ``walk_nodes``. May be empty.
    :return: list of segment strings in the order they appear. An empty
        input yields ``[""]`` (the caller usually wants to treat this as
        the root container; callers filter as needed).
    """
    segments: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(name_path):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "/" and depth == 0:
            segments.append(name_path[start:i])
            start = i + 1
    segments.append(name_path[start:])
    return segments


def _parent_name_path(name_path: str) -> str | None:
    """Return the enclosing container's name path, or ``None`` for top-level paths.

    Uses :func:`_split_name_path_segments` so keys embedding ``/`` inside
    ``[...]`` brackets are not accidentally split on their inner slash.

    :param name_path: a canonical structural name path.
    :return: the prefix formed by dropping the final segment, joined with
        ``/``; ``None`` when the input has zero or one segments (i.e. no
        parent container exists for ``insert_before`` / ``insert_after`` /
        ``replace`` / ``remove`` to anchor against).
    """
    segments = _split_name_path_segments(name_path)
    if len(segments) <= 1:
        return None
    return "/".join(segments[:-1])


class CursorManager:
    """
    Manages cursor state and resolves LSP graph edges for navigation.

    Each cursor is identified by a string ID and tracks its current symbol,
    trail of visited symbols, and configured edge types.
    """

    def __init__(
        self,
        project: Project,
        structural_registry: StructuralBackendRegistry | None = None,
    ) -> None:
        """Construct the manager with an optional structural backend registry.

        :param project: the project whose files the manager navigates.
        :param structural_registry: registry used by
            :meth:`resolve_structural_name_path` to look up per-file structural
            backends. Defaults to :func:`default_structural_backend_registry`
            so production callers do not need to configure routing. Tests may
            pass a custom registry to isolate from backend imports.
        """
        self._project = project
        self._cursors: dict[str, AnyCursorState] = {}
        self._next_cursor_id = 1
        # lazy default so test overrides and production both go through one path
        self._structural_registry = structural_registry if structural_registry is not None else default_structural_backend_registry()
        # per-file cache of structural walk_nodes output; keyed by relative path
        self._structural_nodes_cache: dict[str, _StructuralNodeCacheEntry] = {}

    @property
    def _retriever(self) -> LanguageServerSymbolRetriever:
        return LanguageServerSymbolRetriever(self._project)

    def _generate_cursor_id(self) -> str:
        cursor_id = f"c{self._next_cursor_id}"
        self._next_cursor_id += 1
        return cursor_id

    def get_cursor(self, cursor_id: str) -> AnyCursorState:
        if cursor_id not in self._cursors:
            raise ValueError(f"No cursor with id '{cursor_id}'. Active cursors: {list(self._cursors.keys())}")
        return self._cursors[cursor_id]

    def get_lsp_cursor(self, cursor_id: str) -> CursorState:
        """Return the cursor strictly as an LSP :class:`CursorState`.

        Raises :class:`TypeError` when the cursor is a
        :class:`StructuralCursorState` — callers that cannot operate on
        structural cursors use this accessor to fail fast.
        """
        state = self.get_cursor(cursor_id)
        if not isinstance(state, CursorState):
            raise TypeError(
                f"Cursor '{cursor_id}' is a structural cursor at "
                f"{state.relative_path}:{state.name_path!r}; this operation requires an LSP cursor",
            )
        return state

    def list_cursors(self) -> list[str]:
        return list(self._cursors.keys())

    def start_cursor(
        self,
        name_path: str,
        relative_path: str | None = None,
        cursor_id: str | None = None,
        edge_types: frozenset[EdgeType] | None = None,
    ) -> tuple[str, AnyCursorState]:
        """
        Start a new cursor at a symbol identified by name_path.

        The method first consults the language server: if ``find_unique``
        resolves ``name_path`` to an LSP symbol, an LSP-backed
        :class:`CursorState` is returned. When the LSP lookup fails and
        ``relative_path`` points at a file with a registered structural
        backend, resolution falls through to
        :meth:`resolve_structural_name_path`; a successful structural match
        returns a :class:`StructuralCursorState`. This lets cursors address
        container members (dict entries, list/array items, YAML/TOML pairs)
        that the language server does not surface as symbols.

        :param name_path: the name path of the symbol or structural node.
            For Python dict descent the agent writes ``FOO/["members"]``;
            for JSON/TOML/YAML the form is ``foo/bar/[3]``.
        :param relative_path: optional file path to narrow the search.
            Required to enable the structural fallback.
        :param cursor_id: optional explicit cursor ID; auto-generated if None
        :param edge_types: edges the cursor should resolve when its
            neighborhood is rendered. ``None`` (default) leaves the cursor
            with the empty :data:`DEFAULT_EDGE_TYPES` set — the cursor
            renders only its current position and no neighbors are queried.
            Pass an explicit frozenset to opt the cursor into specific
            edges; the same set can be expanded or contracted later via
            :meth:`CursorState.active_edge_types` (or, at the tool layer,
            via ``cursor_configure``). Ignored for structural cursors,
            which carry no LSP edges.
        :return: tuple of (cursor_id, cursor_state); the state is either an
            LSP :class:`CursorState` or a :class:`StructuralCursorState`.
        """
        # validate the cursor id up-front so both resolution paths share the check
        if cursor_id is not None and cursor_id in self._cursors:
            raise ValueError(
                f"Cursor '{cursor_id}' already exists. Close it first or use a different ID. Active cursors: {list(self._cursors.keys())}"
            )

        retriever = self._retriever
        try:
            symbol = retriever.find_unique(name_path, within_relative_path=relative_path)
        except ValueError as lsp_error:
            # LSP missed; fall through to the structural backend when a file is known.
            # Without a relative_path we have no way to pick a backend, so the LSP
            # error is the agent's honest feedback and we re-raise it.
            if relative_path is None:
                raise
            structural = self.resolve_structural_name_path(relative_path, name_path)
            if structural is None:
                raise lsp_error
            assigned_id = cursor_id if cursor_id is not None else self._generate_cursor_id()
            struct_state = StructuralCursorState(
                cursor_id=assigned_id,
                relative_path=relative_path,
                name_path=structural.name_path,
                kind=structural.kind,
            )
            self._cursors[assigned_id] = struct_state
            return assigned_id, struct_state

        location = symbol.location

        assigned_id = cursor_id if cursor_id is not None else self._generate_cursor_id()

        state = CursorState(
            cursor_id=assigned_id,
            current_symbol=symbol,
            current_location=location,
            active_edge_types=edge_types if edge_types is not None else DEFAULT_EDGE_TYPES,
        )
        self._cursors[assigned_id] = state
        return assigned_id, state

    def move_cursor(
        self,
        cursor_id: str,
        target_name: str,
        target_relative_path: str | None = None,
    ) -> CursorState:
        """
        Move a cursor to a neighboring symbol by name.

        The target must be reachable from the current position via one of the active edge types.
        If target_relative_path is provided, it narrows the match.

        :param cursor_id: the cursor to move
        :param target_name: name (or name_path) of the target symbol
        :param target_relative_path: optional file path to disambiguate
        :return: updated cursor state
        """
        # cursor_move operates on LSP graph edges; structural cursors have no such
        # edges, so we raise a targeted error pointing the agent at cursor_start.
        state = self.get_lsp_cursor(cursor_id)

        # First, try to find the target among current neighbors
        neighbors = self.resolve_neighbors(cursor_id)
        candidates = [n for n in neighbors if target_name in n.name or n.name in target_name]
        if target_relative_path:
            candidates = [n for n in candidates if n.relative_path and target_relative_path in n.relative_path]

        if not candidates:
            # Fall back to global symbol search
            retriever = self._retriever
            symbol = retriever.find_unique(target_name, within_relative_path=target_relative_path)
            location = symbol.location
        elif len(candidates) == 1:
            candidate = candidates[0]
            retriever = self._retriever
            if candidate.relative_path and candidate.line is not None and candidate.column is not None:
                symbol_location = LanguageServerSymbolLocation(
                    relative_path=candidate.relative_path,
                    line=candidate.line,
                    column=candidate.column,
                )
                found = retriever.find_by_location(symbol_location)
                if found:
                    symbol = found
                    location = symbol.location
                else:
                    symbol = retriever.find_unique(candidate.name, within_relative_path=candidate.relative_path)
                    location = symbol.location
            else:
                symbol = retriever.find_unique(candidate.name, within_relative_path=candidate.relative_path)
                location = symbol.location
        else:
            # Multiple candidates — try exact name match
            exact = [n for n in candidates if n.name == target_name]
            if len(exact) == 1:
                candidate = exact[0]
            else:
                names = [f"  {n.name} ({n.kind}) [{n.location_str}]" for n in candidates]
                raise ValueError(f"Ambiguous target '{target_name}'. Candidates:\n" + "\n".join(names))
            retriever = self._retriever
            symbol = retriever.find_unique(candidate.name, within_relative_path=candidate.relative_path)
            location = symbol.location

        state.record_move(symbol, location)
        return state

    def close_cursor(self, cursor_id: str) -> None:
        """Close and remove a cursor."""
        if cursor_id in self._cursors:
            del self._cursors[cursor_id]

    def resolve_neighbors(self, cursor_id: str, depth: int = 1) -> list[NeighborSymbol]:
        """
        Resolve all neighbors of the cursor's current symbol via active edge types.

        :param cursor_id: the cursor whose neighborhood to resolve
        :param depth: traversal depth (currently only 1 is supported)
        :return: list of neighbor symbols with their edge types
        """
        state = self.get_cursor(cursor_id)
        # structural cursors use the structural-cache walk to surface container
        # membership as the CONTAINS edge; other edge types remain no-ops.
        if isinstance(state, StructuralCursorState):
            return self._resolve_structural_neighbors(state)
        symbol = state.current_symbol
        location = state.current_location
        neighbors: list[NeighborSymbol] = []

        if location.relative_path is None or location.line is None or location.column is None:
            log.warning(f"Cursor {cursor_id} symbol has incomplete location, cannot resolve neighbors")
            return neighbors

        rel_path = location.relative_path
        line = location.line
        col = location.column

        # Contains: children of the current symbol
        if EdgeType.CONTAINS in state.active_edge_types:
            for child in symbol.iter_children():
                neighbors.append(
                    NeighborSymbol(
                        name=child.name,
                        kind=child.symbol_kind_name,
                        relative_path=child.relative_path or rel_path,
                        line=child.line,
                        column=child.column,
                        edge_type=EdgeType.CONTAINS,
                    )
                )

        retriever = self._retriever
        ls = retriever.get_language_server(rel_path)
        failed_edge_types: list[EdgeType] = []

        # References: symbols that THIS symbol references (definitions it points to)
        if EdgeType.REFERENCES in state.active_edge_types:
            try:
                definitions = ls.request_definition(rel_path, line, col)
                for defn in definitions:
                    defn_rel_path = defn.get("relativePath")
                    defn_range = defn.get("range", {})
                    defn_start = defn_range.get("start", {})
                    if defn_rel_path:
                        # Try to get the symbol name at this location
                        defn_line = defn_start.get("line", 0)
                        defn_col = defn_start.get("character", 0)
                        name = self._symbol_name_at(defn_rel_path, defn_line, defn_col)
                        neighbors.append(
                            NeighborSymbol(
                                name=name,
                                kind="",
                                relative_path=defn_rel_path,
                                line=defn_line,
                                column=defn_col,
                                edge_type=EdgeType.REFERENCES,
                            )
                        )
            except SolidLSPException as e:
                log.debug(f"Failed to resolve definitions for cursor: {e}")
                failed_edge_types.append(EdgeType.REFERENCES)

        # Referenced-by: symbols that reference THIS symbol
        if EdgeType.REFERENCED_BY in state.active_edge_types:
            try:
                ref_symbols = ls.request_referencing_symbols(rel_path, line, col, include_imports=False, include_self=False)
                for ref in ref_symbols:
                    ref_sym = ref.symbol
                    ref_rel_path = ref_sym["location"].get("relativePath", "")
                    sel_range = ref_sym.get("selectionRange", {})
                    sel_start = sel_range.get("start", {})
                    neighbors.append(
                        NeighborSymbol(
                            name=ref_sym["name"],
                            kind=SymbolKind(ref_sym["kind"]).name,
                            relative_path=ref_rel_path,
                            line=sel_start.get("line"),
                            column=sel_start.get("character"),
                            edge_type=EdgeType.REFERENCED_BY,
                        )
                    )
            except SolidLSPException as e:
                log.debug(f"Failed to resolve referencing symbols for cursor: {e}")
                failed_edge_types.append(EdgeType.REFERENCED_BY)

        # Calls: symbols that this symbol calls (outgoing calls)
        if EdgeType.CALLS in state.active_edge_types:
            try:
                outgoing = ls.request_call_hierarchy_outgoing(rel_path, line, col)
                for outgoing_call in outgoing:
                    target = outgoing_call["to"]
                    neighbors.append(self._neighbor_from_hierarchy_item(target, EdgeType.CALLS))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve outgoing calls for cursor: {e}")
                failed_edge_types.append(EdgeType.CALLS)

        # Called-by: symbols that call this symbol (incoming calls)
        if EdgeType.CALLED_BY in state.active_edge_types:
            try:
                incoming = ls.request_call_hierarchy_incoming(rel_path, line, col)
                for incoming_call in incoming:
                    caller = incoming_call["from"]
                    neighbors.append(self._neighbor_from_hierarchy_item(caller, EdgeType.CALLED_BY))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve incoming calls for cursor: {e}")
                failed_edge_types.append(EdgeType.CALLED_BY)

        # Inherits: supertypes of the current symbol
        if EdgeType.INHERITS in state.active_edge_types:
            try:
                supertypes = ls.request_type_hierarchy_supertypes(rel_path, line, col)
                for item in supertypes:
                    neighbors.append(self._neighbor_from_type_hierarchy_item(item, EdgeType.INHERITS))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve supertypes for cursor: {e}")
                failed_edge_types.append(EdgeType.INHERITS)

        # Inherited-by: subtypes of the current symbol
        if EdgeType.INHERITED_BY in state.active_edge_types:
            try:
                subtypes = ls.request_type_hierarchy_subtypes(rel_path, line, col)
                for item in subtypes:
                    neighbors.append(self._neighbor_from_type_hierarchy_item(item, EdgeType.INHERITED_BY))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve subtypes for cursor: {e}")
                failed_edge_types.append(EdgeType.INHERITED_BY)

        if failed_edge_types:
            names = ", ".join(e.value for e in failed_edge_types)
            log.warning(f"Cursor {cursor_id}: {len(failed_edge_types)} edge type(s) failed to resolve: {names}")

        return neighbors

    def _resolve_structural_neighbors(self, state: StructuralCursorState) -> list[NeighborSymbol]:
        """Surface container membership as CONTAINS-edged neighbors.

        Structural cursors do not participate in the LSP graph, so call
        hierarchy, references, and type hierarchy are all empty. The one
        useful neighborhood is *what this node contains*: for a container
        kind, its direct members; for a leaf member, the empty set. The
        method reads the structural cache so the neighbors reflect whatever
        is on disk at the time of the call.

        :param state: the structural cursor state to resolve around.
        :return: a list of neighbor symbols, each tagged with
            :data:`EdgeType.CONTAINS`. Empty when the cursor is a leaf or
            the file is no longer backed by a registered backend.
        """
        backend = self._structural_registry.for_relative_path(state.relative_path)
        if backend is None:
            return []

        # fresh cache entry so neighbors follow on-disk edits since the last call
        cache_entry = self._structural_cache_entry(backend, state.relative_path)
        if cache_entry is None:
            return []

        # filter the indexed paths to direct children of the cursor's node.
        # a path is a direct child when its segment count is exactly one
        # greater than the cursor's and it shares the cursor path as prefix.
        prefix = state.name_path
        cursor_segment_count = len(_split_name_path_segments(prefix))
        children: list[NeighborSymbol] = []
        for candidate_path, (kind, _node) in cache_entry.nodes_by_path.items():
            if not candidate_path.startswith(prefix + "/"):
                continue
            if len(_split_name_path_segments(candidate_path)) != cursor_segment_count + 1:
                continue
            children.append(
                NeighborSymbol(
                    name=candidate_path,
                    kind=kind,
                    relative_path=state.relative_path,
                    line=None,
                    column=None,
                    edge_type=EdgeType.CONTAINS,
                )
            )
        return children

    def _symbol_name_at(self, relative_path: str, line: int, col: int) -> str:
        """Try to find the symbol name at a location, falling back to file:line."""
        try:
            retriever = self._retriever
            location = LanguageServerSymbolLocation(relative_path=relative_path, line=line, column=col)
            found = retriever.find_by_location(location)
            if found:
                return found.name
        except Exception as e:
            log.debug(f"Could not resolve symbol name at {relative_path}:{line + 1}: {e}")
        return f"{os.path.basename(relative_path)}:{line + 1}"

    def _neighbor_from_hierarchy_item(
        self,
        item: CallHierarchyItem,
        edge_type: EdgeType,
    ) -> NeighborSymbol:
        """Create a NeighborSymbol from a CallHierarchyItem."""
        uri = item["uri"]
        rel_path = PathUtils.get_relative_path(PathUtils.uri_to_path(uri), self._project.project_root)
        sel_start = item["selectionRange"]["start"]
        try:
            kind_name = SymbolKind(item["kind"]).name
        except ValueError:
            kind_name = str(item["kind"])
        return NeighborSymbol(
            name=item["name"],
            kind=kind_name,
            relative_path=rel_path if rel_path else None,
            line=sel_start.get("line"),
            column=sel_start.get("character"),
            edge_type=edge_type,
            detail=item.get("detail"),
        )

    def _neighbor_from_type_hierarchy_item(
        self,
        item: TypeHierarchyItem,
        edge_type: EdgeType,
    ) -> NeighborSymbol:
        """Create a NeighborSymbol from a TypeHierarchyItem."""
        # TypeHierarchyItem and CallHierarchyItem share the same relevant fields
        return self._neighbor_from_hierarchy_item(item, edge_type)  # type: ignore[arg-type]

    def format_cursor_view(self, cursor_id: str) -> str:
        """
        Format the current cursor position and its neighborhood as structured text.

        :param cursor_id: the cursor to format
        :return: human-readable text representation
        """
        state = self.get_cursor(cursor_id)
        if isinstance(state, StructuralCursorState):
            return self._format_structural_cursor_view(state)
        symbol = state.current_symbol
        location = state.current_location

        lines: list[str] = []

        # Header: current symbol
        loc_str = ""
        if location.relative_path and location.line is not None:
            loc_str = f" [{location.relative_path}:{location.line + 1}]"
        lines.append(f"@ {symbol.get_name_path()} ({symbol.symbol_kind_name}){loc_str}")
        lines.append(f"  cursor: {state.cursor_id} | trail: {len(state.trail)} steps")

        # Body (if configured): prefer the statement-widened slice so Python variable
        # symbols whose LSP extent is name-only display the full assignment (e.g. a
        # multi-line list literal); fall back to the LSP-reported body otherwise.
        if state.include_body:
            body_text = self._format_widened_body(symbol)
            if body_text is None:
                body_text = symbol.body
            if body_text:
                lines.append("")
                lines.append("--- body ---")
                lines.append(body_text)
                lines.append("--- end body ---")

        # Neighbors grouped by edge type. Skip the LSP query entirely when
        # the cursor has no edges configured — that's the agent's signal
        # they didn't ask for a neighborhood, and resolving even one edge
        # can be a multi-minute LSP call on large indexed projects.
        if state.active_edge_types:
            neighbors = self.resolve_neighbors(cursor_id)
            neighbors_by_edge: dict[EdgeType, list[NeighborSymbol]] = {}
            for n in neighbors:
                neighbors_by_edge.setdefault(n.edge_type, []).append(n)

            if neighbors_by_edge:
                lines.append("")
                for edge_type in EdgeType:
                    edge_neighbors = neighbors_by_edge.get(edge_type)
                    if edge_neighbors:
                        lines.append(f"  {edge_type.value}:")
                        for n in edge_neighbors:
                            lines.append(f"    {n.format_compact()}")
            else:
                active_names = sorted(e.value for e in state.active_edge_types)
                lines.append("")
                lines.append(f"  (no neighbors found via active edges: {', '.join(active_names)})")
        else:
            lines.append("")
            lines.append("  (no edges configured — call cursor_configure with edge_types=[...] to resolve a neighborhood)")

        lines.append("")
        lines.append("Use cursor_move to navigate to a neighbor, cursor_look to re-examine.")
        return "\n".join(lines)

    def _format_widened_body(self, symbol: LanguageServerSymbol) -> str | None:
        """
        Extract the statement-widened body text for the given symbol.

        Delegates to :func:`serena.symbol_extent.compute_widened_body_text`, which
        consults the per-language :class:`SymbolExtentStrategy` to widen the
        LSP-reported range to the enclosing statement.

        :param symbol: the symbol whose body to widen.
        :return: widened body text sliced from the file; ``None`` when widening does
            not apply (caller should fall back to ``symbol.body``).
        """
        # imported lazily to avoid coupling the module graph at import time,
        # mirroring the LanguageServerCodeEditor pattern in code_editor.py
        from serena.symbol_extent import compute_widened_body_text

        return compute_widened_body_text(symbol, self._project)

    def _format_structural_cursor_view(self, state: StructuralCursorState) -> str:
        """Render a structural cursor's position as text.

        Structural cursors have no LSP neighborhood; the view shows the
        canonical name path, the resolved kind, the node's direct members
        (CONTAINS-edged structural neighbors) and — when ``include_body``
        is set — the serialized source text of the addressed node. The
        neighbor listing and the serialized slice are obtained by
        re-reading the file so the view reflects on-disk state at render
        time.
        """
        lines = [
            f"@ {state.name_path} ({state.kind}) [{state.relative_path}]",
            f"  cursor: {state.cursor_id} | trail: {len(state.trail)} steps | structural",
        ]

        # members of the addressed node — present only when the cursor is on
        # a container; leaf members produce an empty list and no block is shown
        children = self._resolve_structural_neighbors(state)
        if children:
            lines.append("")
            lines.append("  contains:")
            for child in children:
                lines.append(f"    {child.name} ({child.kind})")

        if state.include_body:
            body_text = self._format_structural_node_source(state)
            if body_text is not None:
                lines.append("")
                lines.append("--- body ---")
                lines.append(body_text)
                lines.append("--- end body ---")

        lines.append("")
        lines.append(
            "Structural cursor: LSP edges are inactive. Use cursor_start on a member "
            "path to move; cursor_replace_body / cursor_insert_before / "
            "cursor_insert_after / cursor_insert_at_start / cursor_insert_at_end / "
            "cursor_remove_member dispatch to the container-member backend."
        )
        return "\n".join(lines)

    def _format_structural_node_source(self, state: StructuralCursorState) -> str | None:
        """Return the serialized source text for the node at ``state.name_path``.

        Returns ``None`` when the backend cannot render the addressed node as
        a standalone source string — which today covers the ``container``
        wrapper yielded by Python's ``walk_container_members`` (a bare
        ``cst.Dict``/``cst.List`` has no standalone module serializer
        attached). Callers should treat ``None`` as "no body available".
        """
        backend = self._structural_registry.for_relative_path(state.relative_path)
        if backend is None:
            return None
        # _structural_cache_entry re-parses on mtime change, so the view reflects on-disk state
        cache_entry = self._structural_cache_entry(backend, state.relative_path)
        if cache_entry is None:
            return None
        match = cache_entry.nodes_by_path.get(state.name_path)
        if match is None:
            return None
        _kind, node = match
        try:
            return self._serialize_node_for_display(backend, node)
        except Exception as e:
            # rendering is best-effort: when a backend cannot turn a sub-node
            # into standalone text, omit the body rather than fail the view
            log.debug(f"Could not serialize structural node for display: {e}")
            return None

    @staticmethod
    def _serialize_node_for_display(backend: StructuralLanguage, node: Any) -> str:
        """Coerce ``node`` into text using the backend's native rendering.

        LibCST, the JSON CST, tomlkit and ruamel.yaml all expose a
        ``__str__``/``code`` hook that renders the sub-tree with its original
        formatting. We try the most faithful options in order and fall back to
        ``str(node)`` so callers see *something* even for backends without a
        dedicated renderer.
        """
        # libcst nodes expose a `.code` property for module-level rendering
        code_attr = getattr(node, "code", None)
        if isinstance(code_attr, str):
            return code_attr
        # tomlkit items have a `.as_string()` for their literal text
        as_string = getattr(node, "as_string", None)
        if callable(as_string):
            try:
                result = as_string()
            except TypeError:
                result = None
            if isinstance(result, str):
                return result
        return str(node)

    def find_symbols(
        self,
        name_path_pattern: str,
        relative_path: str | None = None,
        include_kinds: Sequence[SymbolKind] | None = None,
        exclude_kinds: Sequence[SymbolKind] | None = None,
        substring_matching: bool = False,
    ) -> list[LanguageServerSymbol]:
        """
        Pattern/substring search for symbols. Delegates to the language server symbol retriever
        (same backend as the old ``find_symbol`` tool).
        """
        return self._retriever.find(
            name_path_pattern,
            include_kinds=include_kinds,
            exclude_kinds=exclude_kinds,
            substring_matching=substring_matching,
            within_relative_path=relative_path,
        )

    def register_cursor_at_symbol(
        self,
        symbol: LanguageServerSymbol,
        cursor_id: str | None = None,
        edge_types: frozenset[EdgeType] | None = None,
    ) -> tuple[str, CursorState]:
        """
        Register a cursor positioned on an already-resolved symbol (e.g. one returned by
        ``find_symbols``). Used by ``cursor_find`` when the search is unique.

        :param edge_types: edges the cursor should resolve when its
            neighborhood is rendered. ``None`` (default) leaves the cursor
            with the empty :data:`DEFAULT_EDGE_TYPES` set; pass an explicit
            frozenset to opt in. See :meth:`start_cursor` for the full
            rationale (per-symbol LSP cost on large indexed projects).
        """
        location = symbol.location
        if cursor_id is None:
            cursor_id = self._generate_cursor_id()
        elif cursor_id in self._cursors:
            raise ValueError(
                f"Cursor '{cursor_id}' already exists. Close it first or use a different ID. Active cursors: {list(self._cursors.keys())}"
            )
        state = CursorState(
            cursor_id=cursor_id,
            current_symbol=symbol,
            current_location=location,
            active_edge_types=edge_types if edge_types is not None else DEFAULT_EDGE_TYPES,
        )
        self._cursors[cursor_id] = state
        return cursor_id, state

    def resolve_structural_name_path(
        self,
        relative_path: str,
        name_path: str,
    ) -> StructuralResolution | None:
        """Resolve ``name_path`` against the structural backend for ``relative_path``.

        Routes through the structural backend registry: if a backend is registered
        for the file's extension, the file is parsed (with per-file caching keyed
        on mtime) and ``walk_nodes`` is indexed by name path. Synthetic paths like
        ``Class/method/if_stmt#0`` resolve to the same dict — the structural
        backend decides what is addressable.

        Returns ``None`` rather than raising when the file is outside a registered
        language, missing from disk, or when ``name_path`` has no match: callers
        are expected to fall back to LSP-based resolution on ``None``.

        :param relative_path: POSIX-style path relative to the project root.
        :param name_path: full name path of the target node as emitted by
            :meth:`~solidlsp.structural.base.StructuralLanguage.walk_nodes`.
        :return: a :class:`StructuralResolution` on match, or ``None``.
        """
        # route by the file's extension; no registered backend -> caller falls back
        backend = self._structural_registry.for_relative_path(relative_path)
        if backend is None:
            return None

        # look up against the per-file cache, re-parsing on cache miss or mtime change
        cache_entry = self._structural_cache_entry(backend, relative_path)
        if cache_entry is None:
            return None

        # O(1) dict lookup — walk_nodes already produced canonical name paths
        match = cache_entry.nodes_by_path.get(name_path)
        if match is None:
            return None
        kind, node = match
        return StructuralResolution(name_path=name_path, kind=kind, node=node)

    def _structural_cache_entry(
        self,
        backend: StructuralLanguage,
        relative_path: str,
    ) -> _StructuralNodeCacheEntry | None:
        """Return a per-file structural walk cache entry, populating or refreshing as needed.

        The cache is keyed on the relative path with the file's mtime as the
        invalidation signal. A missing file yields ``None`` so the public
        resolver can fall through to LSP-based handling.

        :param backend: backend to use for parsing and walking the file.
        :param relative_path: project-relative path of the file.
        :return: a fresh or reused :class:`_StructuralNodeCacheEntry`, or
            ``None`` when the file cannot be read.
        """
        # resolve to an absolute path so we can stat and read independently of cwd
        abs_path = os.path.join(self._project.project_root, relative_path)
        try:
            mtime_ns = os.stat(abs_path).st_mtime_ns
        except (FileNotFoundError, NotADirectoryError):
            # stale cache entries for deleted files are dropped so re-creation re-parses
            self._structural_nodes_cache.pop(relative_path, None)
            return None

        # hit the cache when the file has not changed since the last walk
        cached = self._structural_nodes_cache.get(relative_path)
        if cached is not None and cached.mtime_ns == mtime_ns:
            return cached

        # miss or stale: re-read, re-parse, and re-index by canonical name path
        try:
            source = self._project.read_file(relative_path)
        except (FileNotFoundError, NotADirectoryError):
            self._structural_nodes_cache.pop(relative_path, None)
            return None
        tree = backend.parse(source)
        nodes_by_path: dict[str, tuple[KindName, Any]] = {}
        for node_name_path, kind, node in backend.walk_nodes(tree):
            nodes_by_path[node_name_path] = (kind, node)

        entry = _StructuralNodeCacheEntry(mtime_ns=mtime_ns, nodes_by_path=nodes_by_path)
        self._structural_nodes_cache[relative_path] = entry
        return entry

    def _validate_container_edit_positioning(
        self,
        state: StructuralCursorState,
        operation: str,
    ) -> None:
        """Raise a friendlier :class:`ValueError` for common op/cursor-position mismatches.

        Additive to the backend-level guard: :meth:`apply_container_edit`
        still dispatches when this method returns, and each backend keeps
        its own validation for subtler cases (e.g. a
        :class:`~solidlsp.structural.backends.python.PythonStructuralLanguage`
        ``container_member`` whose value happens to be a scalar). This check
        catches the two mismatches whose backend errors the user will hit
        most often and whose underlying cause is positional:

        * ``insert_start`` / ``insert_end`` on a cursor whose captured
          ``kind`` is a known scalar (``"string"`` / ``"number"`` /
          ``"boolean"`` / ``"null"`` / ``"scalar"``) — the value is not a
          container so no member can be inserted into it.
        * ``insert_before`` / ``insert_after`` / ``replace`` / ``remove`` on
          a cursor whose ``name_path`` has no parent segment — there is no
          enclosing container to host the edit.

        :param state: the structural cursor's state.
        :param operation: the validated operation name (already known to be
            a member of ``valid_ops`` in :meth:`apply_container_edit`).
        :raises ValueError: with a message naming the operation, the
            cursor's name path and kind, and a concrete retry hint.
        """
        # insert_start / insert_end need a container at the cursor's position
        if operation in _CONTAINER_POSITIONED_OPERATIONS:
            if state.kind in _STRUCTURAL_SCALAR_KINDS:
                parent = _parent_name_path(state.name_path)
                retry_hint = f" Use cursor_start on '{parent}' (the enclosing container) then retry." if parent is not None else ""
                raise ValueError(
                    f"{operation} requires a cursor positioned on a container, "
                    f"but cursor '{state.cursor_id}' is on '{state.name_path}' "
                    f"(kind {state.kind!r}, a scalar value).{retry_hint}"
                )
            return

        # before/after/replace/remove need the cursor on a member of some parent container
        if operation in _MEMBER_ANCHORED_OPERATIONS:
            parent = _parent_name_path(state.name_path)
            if parent is None:
                raise ValueError(
                    f"{operation} requires a cursor positioned on a container member, "
                    f"but cursor '{state.cursor_id}' is on top-level path "
                    f"'{state.name_path}' (kind {state.kind!r}); there is no parent "
                    f"container to anchor the edit against. Use cursor_start on a "
                    f"member path nested inside '{state.name_path}' then retry."
                )

    def apply_container_edit(
        self,
        cursor_id: str,
        operation: str,
        source: str,
    ) -> tuple[str, str]:
        """Apply a container-member edit at the position of a structural cursor.

        Used by the cursor edit tools to dispatch replace/insert/remove
        operations to the appropriate structural backend. The method reads
        the file, parses it, calls the matching backend method, serializes,
        and writes the result atomically. The structural cache is invalidated
        after the write so subsequent re-anchors see fresh node handles.

        :param cursor_id: the structural cursor whose position to edit.
        :param operation: one of ``"replace"``, ``"insert_before"``,
            ``"insert_after"``, ``"insert_start"``, ``"insert_end"``,
            ``"remove"``. ``"insert_start"`` / ``"insert_end"`` expect the
            cursor to be positioned on a *container* and insert at the
            beginning or end of that container. The member-anchored
            ``"insert_before"`` / ``"insert_after"`` expect the cursor to be
            on an existing member. ``"remove"`` deletes the member at the
            cursor's position.
        :param source: the source-text fragment passed to the backend. For
            replacements this is the bare value expression; for insertions
            it is the key/value pair (mapping containers) or a bare value
            (sequence containers). Ignored for ``"remove"``.
        :return: a tuple ``(before_contents, after_contents)`` letting the
            caller compute a unified-diff summary without re-reading the file.
        :raises TypeError: if ``cursor_id`` is not a structural cursor.
        :raises ValueError: for unknown operations or when the backend cannot
            service the request (e.g. no backend registered for the file).
        """
        state = self.get_cursor(cursor_id)
        if not isinstance(state, StructuralCursorState):
            raise TypeError(
                f"Cursor '{cursor_id}' is an LSP cursor; apply_container_edit requires a structural cursor",
            )
        valid_ops = {
            "replace",
            "insert_before",
            "insert_after",
            "insert_start",
            "insert_end",
            "remove",
        }
        if operation not in valid_ops:
            raise ValueError(f"unknown container edit operation: {operation!r}")

        # front-load two common op/cursor-position mismatches with a friendlier
        # message than the backend-level guard produces; the backend guards
        # still run for subtler cases this helper cannot detect
        self._validate_container_edit_positioning(state, operation)

        # route to a backend by file extension; no backend -> the cursor is stale
        backend = self._structural_registry.for_relative_path(state.relative_path)
        if backend is None:
            raise ValueError(
                f"No structural backend registered for {state.relative_path!r}; "
                f"structural cursor '{cursor_id}' cannot dispatch a container edit.",
            )

        # read fresh content so concurrent on-disk edits are picked up on each call
        before_contents = self._project.read_file(state.relative_path)
        tree = backend.parse(before_contents)

        # dispatch to the matching ABC method; each returns a new tree handle
        if operation == "replace":
            new_tree = backend.container_replace_member(tree, state.name_path, source)
        elif operation == "remove":
            new_tree = backend.container_remove_member(tree, state.name_path)
        else:
            # insert_before | insert_after | insert_start | insert_end share
            # the container_insert_member signature; the position suffix maps
            # directly to the backend's position parameter.
            position = operation[len("insert_") :]
            new_tree = backend.container_insert_member(
                tree,
                state.name_path,
                source,
                position=position,
            )

        after_contents = backend.serialize(new_tree)
        # short-circuit writes when the backend round-trip is a no-op; this keeps
        # mtime stable for semantically empty operations and avoids a spurious diff.
        if after_contents != before_contents:
            self._write_file_contents(state.relative_path, after_contents)
        # invalidate the per-file structural cache so re-anchors see fresh node handles
        self._structural_nodes_cache.pop(state.relative_path, None)
        return before_contents, after_contents

    def _write_file_contents(self, relative_path: str, contents: str) -> None:
        """Write ``contents`` to ``relative_path`` using the project's encoding/line-ending.

        Structural edits bypass the LSP buffer (JSON/TOML/YAML have no LSP
        integration at all, and Python structural edits are semantic so the
        LSP will pick them up on the next open). We honor the project's
        configured encoding and newline so the file round-trips cleanly with
        other tools.
        """
        abs_path = os.path.join(self._project.project_root, relative_path)
        encoding = self._project.project_config.encoding
        newline = self._project.line_ending.newline_str
        with open(abs_path, "w", encoding=encoding, newline=newline) as f:
            f.write(contents)

    def reanchor_cursor(
        self,
        cursor_id: str,
        name_path: str | None = None,
        relative_path: str | None = None,
    ) -> AnyCursorState:
        """
        Re-resolve the cursor's current symbol from the language server, after an edit
        potentially changed line numbers or the symbol's name. The cursor remains on the
        same logical symbol; its stored position is refreshed.

        For structural cursors, re-resolution routes through the backend's
        ``walk_nodes`` index after invalidating the per-file cache — this
        catches the case where a container-member edit has rewritten the
        file on disk and we need the new node handle.

        :param cursor_id: the cursor to re-anchor
        :param name_path: override name path (e.g. after a rename); defaults to the current symbol's name path
        :param relative_path: override file path; defaults to the current cursor location's file
        :return: the updated cursor state (same kind as the stored one)
        """
        state = self.get_cursor(cursor_id)
        if isinstance(state, StructuralCursorState):
            # structural re-anchor: invalidate the per-file cache so walk_nodes
            # re-indexes against the freshly-written file, then look the path up.
            resolved_name = name_path if name_path is not None else state.name_path
            resolved_path = relative_path if relative_path is not None else state.relative_path
            self._structural_nodes_cache.pop(resolved_path, None)
            resolution = self.resolve_structural_name_path(resolved_path, resolved_name)
            if resolution is None:
                raise ValueError(
                    f"Structural cursor '{cursor_id}' cannot re-anchor: path {resolved_name!r} no longer resolves in {resolved_path!r}",
                )
            state.relative_path = resolved_path
            state.name_path = resolution.name_path
            state.kind = resolution.kind
            return state

        resolved_name = name_path if name_path is not None else state.current_symbol.get_name_path()
        within_path = relative_path if relative_path is not None else state.current_location.relative_path
        retriever = self._retriever
        symbol = retriever.find_unique(resolved_name, within_relative_path=within_path)
        state.current_symbol = symbol
        state.current_location = symbol.location
        return state

    def format_trail(self, cursor_id: str) -> str:
        """Format the cursor's visited trail as text."""
        state = self.get_cursor(cursor_id)
        if isinstance(state, StructuralCursorState):
            if not state.trail:
                return f"Cursor {cursor_id}: no trail (at starting position)"
            lines = [f"Cursor {cursor_id} trail ({len(state.trail)} steps):"]
            for i, prior_path in enumerate(state.trail):
                lines.append(f"  {i + 1}. {state.relative_path}:{prior_path}")
            lines.append(f"  -> {state.relative_path}:{state.name_path} (current)")
            return "\n".join(lines)

        if not state.trail:
            return f"Cursor {cursor_id}: no trail (at starting position)"

        lines = [f"Cursor {cursor_id} trail ({len(state.trail)} steps):"]
        for i, loc in enumerate(state.trail):
            loc_str = ""
            if loc.relative_path and loc.line is not None:
                loc_str = f"{loc.relative_path}:{loc.line + 1}"
            else:
                loc_str = str(loc.relative_path or "?")
            lines.append(f"  {i + 1}. {loc_str}")

        # Current position
        cur = state.current_location
        if cur.relative_path and cur.line is not None:
            lines.append(f"  -> {cur.relative_path}:{cur.line + 1} (current)")

        return "\n".join(lines)
